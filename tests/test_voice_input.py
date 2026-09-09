"""Run on Windows with Python 3.13 and NVDA's wxPython, configobj, pycaw and comtypes.

Uses fake capture buffers and a local WebSocket server; never records or submits real text.
"""

import asyncio
from ctypes import POINTER, cast, create_string_buffer, c_ubyte
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlsplit

import wx

from test_login import ConfigManager, initTranslation


class KeyboardGesture:
	NORMAL_MODIFIER_KEYS = {16: None, 17: None, 18: None, 91: "windows", 92: "windows", "windows": None}

	def __init__(self, script, keys):
		self.script = script
		self.modifiers = set(keys[:-1])
		self.vkCode, self.isExtended = keys[-1]
		self.scanCode = 0


def script(**metadata):
	def decorate(function):
		function.__dict__.update(metadata)
		return function

	return decorate


class GlobalPlugin:
	def terminate(self):
		pass


def keyboardChecks(plugin, host):
	from VIVOVoiceInput import recognition

	keys = [(45, True), (16, False), (86, False)]
	section = plugin._getConfigSection()
	section.update(
		{
			"nvdacnUsername": "test-user",
			"nvdacnPassword": plugin.credentials.encryptPassword("test-pass"),
		},
	)
	section["loggedIn"] = True
	assert plugin.GlobalPlugin.script_voiceInput.gesture == "kb:NVDA+shift+v"
	assert plugin.GlobalPlugin.script_voiceInput.description == (
		"Hold for speech recognition; release to stop recognition"
	)
	with (
		patch.object(plugin.inputDevices, "isInputDeviceAvailable", return_value=True),
		patch.object(recognition.Session, "start") as start,
		patch("wx.CallLater", side_effect=lambda delay, callback: SimpleNamespace(Stop=Mock())),
	):
		instance = plugin.GlobalPlugin()
		instance._inputDeviceCheckThread.join(timeout=2)
		assert not instance._inputDeviceCheckThread.is_alive()
		wx.GetApp().ProcessPendingEvents()
		controller = instance._voiceInput
		send = host["brailleInput"].handler.sendChars
		speech = host["ui"].delayedMessage
		beep = host["tones"].beep
		focus = host["api"].getFocusObject.return_value

		def key(key, pressed):
			return controller._handleKey(key[0], 0, key[1], pressed)

		def press(combo=keys, execute=True, modifiers=()):
			for item in combo:
				assert key(item, True)
			gesture = KeyboardGesture(instance.script_voiceInput, combo)
			gesture.modifiers.update(modifiers)
			assert controller._handleGesture(gesture)
			if execute:
				instance.script_voiceInput(gesture)
			return gesture

		def release(combo=keys):
			for item in reversed(combo):
				assert key(item, False)

		# Release before the queued script runs, including release and a new press of the same key.
		old = press(execute=False)
		assert not key(keys[-1], True)
		release()
		current = press(execute=False)
		instance.script_voiceInput(old)
		start.assert_not_called()
		release()
		instance.script_voiceInput(current)
		start.assert_not_called()

		section["loggedIn"] = False
		press()
		assert "settings first" in speech.call_args.args[0]
		start.assert_not_called()
		release()
		section["loggedIn"] = True
		for name, value in (
			("nvdacnUsername", ""),
			("nvdacnPassword", ""),
			("nvdacnPassword", "dpapi:invalid"),
		):
			previous = section[name]
			section[name] = value
			press()
			assert speech.call_args.args == ("Please log in in the VIVO Voice Input settings first.",)
			start.assert_not_called()
			assert controller._session is None
			release()
			section[name] = previous

		# Releasing any member stops recording. Repeats remain trapped through the 60-second stop.
		for releasedKey in keys:
			beep.reset_mock()
			press()
			session = controller._session
			assert session.deviceId == "default"
			assert start.call_args.args == ("test-user", "test-pass", "0.1")
			session.started = True
			controller._poll()
			assert beep.call_args.args == (300, 60)
			assert key(releasedKey, False)
			assert session.stop.is_set()
			if releasedKey != keys[-1]:
				assert not key(keys[-1], True)
			session.recordingDone.set()
			session.done.set()
			session.text = "recognized \u4e2d\U0001f600" * 1000
			controller._poll()
			assert beep.call_args_list == [((300, 60),), ((800, 60),)]
			send.assert_not_called()
			# Extra physical modifiers and already-held modifiers both delay the Unicode commit.
			assert key((17, False), True)
			release()
			controller._poll()
			send.assert_not_called()
			assert key((17, False), False)
			host["winUser"].getAsyncKeyState.return_value = 0x8000
			controller._poll()
			send.assert_not_called()
			host["winUser"].getAsyncKeyState.return_value = 0
			host["eventHandler"].isPendingEvents.return_value = True
			controller._poll()
			send.assert_not_called()
			host["eventHandler"].isPendingEvents.return_value = False
			controller._poll()
			send.assert_called_once_with(session.text)
			controller._poll()
			send.assert_called_once()
			send.reset_mock()

		press()
		session = controller._session
		session.stop.set()
		session.recordingDone.set()
		assert not key(keys[-1], True)
		release()
		before = start.call_count
		press()
		assert start.call_count == before
		assert "previous voice input" in speech.call_args.args[0]
		release()

		# Focus changes cancel permanently, even when focus returns before result delivery.
		speech.reset_mock()
		nextHandler = Mock()
		equivalentFocus = SimpleNamespace(control="editor")
		assert equivalentFocus == focus and equivalentFocus is not focus
		host["api"].getFocusObject.return_value = equivalentFocus
		instance.event_gainFocus(equivalentFocus, nextHandler)
		controller._poll()
		assert not session.cancelled.is_set()
		nextHandler.reset_mock()
		instance.event_gainFocus(object(), nextHandler)
		instance.event_gainFocus(focus, nextHandler)
		assert nextHandler.call_count == 2
		assert session.cancelled.is_set()
		session.text = "must not be inserted"
		session.done.set()
		controller._poll()
		assert speech.call_count == 1
		assert "focus has changed" in speech.call_args.args[0]
		send.assert_not_called()

		# Remapping uses the actual gesture keys, not a hard-coded V or Insert.
		remapped = [(20, False), (16, False), (90, False)]
		press(remapped)
		session = controller._session
		release(remapped)
		session.text = ""
		session.done.set()
		controller._poll()
		assert speech.call_args.args == ("No text was recognized.",)

		# NVDA adds a NumLock modifier for numpad operators without a physical NumLock press.
		numpad = [(20, False), (16, False), (107, False)]
		press(numpad, modifiers={(144, False)})
		session = controller._session
		assert session is not None and not session.stop.is_set()
		release(numpad)
		assert session.stop.is_set()
		session.text = "numpad"
		session.done.set()
		controller._poll()
		send.assert_called_once_with("numpad")
		send.reset_mock()

		press()
		session = controller._session
		release()
		session.text = "text"
		session.done.set()
		send.side_effect = OSError("injection unavailable")
		controller._poll()
		assert speech.call_args.args == ("Voice input failed.",)
		send.side_effect = None

		press()
		session = controller._session
		timer = controller._timer
		instance.terminate()
		assert session.cancelled.is_set() and session.stop.is_set()
		timer.Stop.assert_called_once()
		host["inputCore"].decide_handleRawKey.unregister.assert_called_once_with(controller._handleKey)
		host["inputCore"].decide_executeGesture.unregister.assert_called_once_with(controller._handleGesture)
		assert (
			plugin.VIVOVoiceInputSettingsPanel
			not in host["gui.settingsDialogs"].NVDASettingsDialog.categoryClasses
		)
		assert all(isinstance(call.args[0], int) for call in host["winUser"].getAsyncKeyState.call_args_list)


def recordingChecks(recording):
	def capturePackets(packets, deviceId="default", clock=None):
		stop = threading.Event()
		output = []
		held = [False]
		released = []
		storage = []
		packets = iter(packets)
		current = [next(packets, None)]

		def nextSize():
			if current[0] is None:
				stop.set()
				return 0
			return current[0][1]

		def getBuffer():
			data, frames, flags = current[0]
			held[0] = True
			if data is None:
				pointer = POINTER(c_ubyte)()
			else:
				storage.append(create_string_buffer(data))
				pointer = cast(storage[-1], POINTER(c_ubyte))
			return pointer, frames, flags, 0, 0

		def release(frames):
			assert held[0]
			held[0] = False
			released.append(frames)
			current[0] = next(packets, None)

		def onAudio(data):
			assert not held[0], "Release WASAPI buffer before passing data to another thread"
			output.append(data)

		def initialize(mode, flags, duration, period, format, guid):
			format = cast(format, POINTER(recording.WAVEFORMATEX)).contents
			assert (mode, flags, duration, period, guid) == (0, 0x80000000, 1000000, 0, None)
			assert (format.nSamplesPerSec, format.wBitsPerSample, format.nChannels, format.nBlockAlign) == (
				16000,
				16,
				1,
				2,
			)

		capture = SimpleNamespace(GetNextPacketSize=nextSize, GetBuffer=getBuffer, ReleaseBuffer=release)
		client = Mock(Initialize=Mock(side_effect=initialize))
		client.GetService.return_value.QueryInterface.return_value = capture
		device = Mock()
		device.Activate.return_value.QueryInterface.return_value = client
		enumerator = Mock()
		enumerator.GetDefaultAudioEndpoint.return_value = device
		enumerator.GetDevice.return_value = device
		with (
			patch.object(recording.AudioUtilities, "GetDeviceEnumerator", return_value=enumerator),
			patch.object(recording.time, "monotonic", side_effect=clock or (lambda: 0)),
		):
			recording.record(deviceId, stop, lambda: None, onAudio)
		assert client.Start.call_count == client.Stop.call_count == 1
		if deviceId == "default":
			enumerator.GetDefaultAudioEndpoint.assert_called_once_with(1, 1)
		else:
			enumerator.GetDevice.assert_called_once_with(deviceId)
		return output, released

	output, released = capturePackets([(b"\x01\x02" * 400, 400, 0), (None, 300, 2)], "selected-endpoint")
	assert [len(frame) for frame in output] == [1280, 120]
	assert b"".join(output) == b"\x01\x02" * 400 + bytes(600)
	assert released == [400, 300]
	output, released = capturePackets([(None, 960010, 2)])
	assert sum(map(len, output)) == 1920000
	assert released == [960010]
	output, _ = capturePackets([(None, 10, 2)], clock=iter([0, 60]).__next__)
	assert output == [bytes(20)]


async def waitDone(session):
	async with asyncio.timeout(4):
		while not session.done.is_set():
			await asyncio.sleep(0.01)
	session.worker.join(timeout=1)
	assert not session.worker.is_alive()
	assert not session.recorder.is_alive()


def result(text, resultId=1, correction=1, last=True):
	return json.dumps(
		{
			"action": "result",
			"code": 0,
			"type": "asr",
			"is_finish": False,
			"data": {"text": text, "result_id": resultId, "reformation": correction, "is_last": last},
		},
	)


async def protocolChecks(recognition):
	from VIVOVoiceInput.auth._vivo_auth import _genCanonicalQueryString
	from VIVOVoiceInput._vendor.websockets.asyncio.server import serve

	# Exercise the real client/server and recording/network workers, without real audio or credentials.
	for early in (False, True):
		frames = []
		requests = []
		started = threading.Event()
		params = []
		stop = threading.Event()
		session = recognition.Session("selected-endpoint", stop)

		def record(deviceId, stop, onStarted, onAudio):
			assert deviceId == "selected-endpoint"
			onStarted()
			onAudio(b"a" * 1280)
			onAudio(b"b" * 40)
			started.set()
			assert stop.wait(3)

		def sign(username, password, method, uri, query):
			assert started.wait(2), "Recording must not wait for authentication"
			assert (username, password, method, uri) == ("test-user &+", "test-pass", "GET", "/asr/v2")
			params.append(query)
			return {"X-AI-GATEWAY-SIGNATURE": "test-signature"}

		async def server(socket):
			requests.append(socket.request)
			first = await socket.recv()
			assert isinstance(first, str)
			start = json.loads(first)
			assert start["type"] == "started" and len(start["request_id"]) == 32
			int(start["request_id"], 16)
			assert start["asr_info"] == {
				"end_vad_time": 2000,
				"audio_type": "pcm",
				"chinese2digital": 1,
				"punctuation": 1,
			}
			await socket.send(json.dumps({"action": "started", "code": 0, "data": ""}))
			sentFinal = False
			async for frame in socket:
				frames.append(frame)
				if (early or frame == b"--end--") and not sentFinal:
					await socket.send(result("part", 1, 0, False))
					await socket.send(result("more", 2, 0, False))
					await socket.send(result("more", 2, 0, False))
					final = result("final \u4e2d\U0001f600", 3)
					await socket.send([final[:15], final[15:]])
					sentFinal = True
				if frame == b"--close--":
					return

		async with serve(server, "127.0.0.1", 0, compression=None) as local:
			port = local.sockets[0].getsockname()[1]
			with (
				patch.object(recognition, "ENDPOINT", f"ws://127.0.0.1:{port}/asr/v2"),
				patch.object(recognition.recording, "record", side_effect=record),
				patch.object(recognition, "genSignHeaders", side_effect=sign),
			):
				session.start("test-user &+", "test-pass", "0.1")
				if not early:
					asyncio.get_running_loop().call_later(0.05, stop.set)
				await waitDone(session)
		assert session.error is None, session.error
		assert session.text == "final \u4e2d\U0001f600"
		assert session.recordingDone.is_set() and session.stop.is_set()
		assert frames.count(b"--end--") == frames.count(b"--close--") == 1
		assert frames[-2:] == [b"--end--", b"--close--"]
		if not early:
			assert frames[:-2] == [b"a" * 1280, b"b" * 40]
		assert len(requests) == 1 and len(params) == 1
		query = urlsplit(requests[0].path).query
		assert query == _genCanonicalQueryString(params[0])
		assert parse_qs(query)["net_type"] == ["1"]
		assert len(parse_qs(query)["user_id"][0]) == 32
		assert requests[0].headers["X-AI-GATEWAY-SIGNATURE"] == "test-signature"

	# Appends, replacements and duplicate result IDs, followed by one terminal result.
	session = recognition.Session("default", threading.Event())
	socket = SimpleNamespace(
		recv=AsyncMock(
			side_effect=[
				result("one", 1, 0, False),
				result("two", 2, 0, False),
				result("two", 2, 0, False),
				result("three", 3, 0),
			],
		),
	)
	assert await session._receive(socket) == "onetwothree"
	for invalid in (
		"{bad json",
		"[]",
		'{"action":"error","code":10008}',
		'{"action":"started","code":false}',
		result("backwards", -1),
		result("bad flag", last=1),
		result("bad correction", correction=2),
		result(None),
	):
		socket = SimpleNamespace(recv=AsyncMock(return_value=invalid))
		try:
			await session._receive(socket)
		except ValueError:
			pass
		else:
			raise AssertionError("Malformed response accepted")

	# The final-result deadline is measured from --end--, not from the start of recording.
	async def silentServer(socket):
		async for frame in socket:
			if frame == b"--end--":
				await socket.wait_closed()

	async with serve(silentServer, "127.0.0.1", 0) as local:
		url = f"ws://127.0.0.1:{local.sockets[0].getsockname()[1]}/asr/v2"
		session = recognition.Session("default", threading.Event())
		session.audio.put(None)
		with patch.object(recognition, "RESULT_TIMEOUT", 0.05):
			try:
				await session._recognize(url, {})
			except TimeoutError:
				pass
			else:
				raise AssertionError("Missing final result did not time out")

	# Cancellation must interrupt an outstanding handshake, and blocked sends have a deadline.
	async def blocked(*args, **kwargs):
		await asyncio.Event().wait()

	connection = SimpleNamespace(__aenter__=AsyncMock(side_effect=blocked))
	with patch.object(recognition, "connect") as connect:
		connect.return_value.__aenter__.side_effect = connection.__aenter__
		session = recognition.Session("default", threading.Event())
		asyncio.get_running_loop().call_later(0.03, session.cancel)
		assert await asyncio.wait_for(session._recognize("ws://127.0.0.1:1", {}), 1) is None
	with patch.object(recognition, "SEND_TIMEOUT", 0.03):
		try:
			await session._send(SimpleNamespace(send=blocked), b"audio")
		except TimeoutError:
			pass
		else:
			raise AssertionError("Blocked send did not time out")

	# Recording failure cleans up both workers and takes precedence over a network result.
	with (
		patch.object(recognition, "ENDPOINT", "ws://127.0.0.1:1/asr/v2"),
		patch.object(recognition.recording, "record", side_effect=OSError("device removed")),
		patch.object(recognition, "genSignHeaders", return_value={"X-AI-GATEWAY-SIGNATURE": "test"}),
	):
		session = recognition.Session("missing", threading.Event())
		session.start("test-user", "test-pass", "0.1")
		await waitDone(session)
		assert session.error == recognition.RECORDING_ERROR
		assert session.cancelled.is_set() and session.text is None

	# A press released before capture produces no network request.
	stop = threading.Event()
	stop.set()
	with (
		patch.object(recognition.recording, "record", return_value=None),
		patch.object(recognition, "genSignHeaders") as sign,
	):
		session = recognition.Session("default", stop)
		session.start("test-user", "test-pass", "0.1")
		await waitDone(session)
	assert session.error == recognition.NO_TEXT_ERROR
	sign.assert_not_called()

	# Cancellation and a recording failure arriving during the final drain must take precedence.
	for recordingError in (None, recognition.RECORDING_ERROR):
		session = recognition.Session("default", threading.Event())
		session.stop.set()

		def finishRecording():
			session.error = recordingError
			session.cancel()

		with patch.object(session.recordingDone, "wait", side_effect=finishRecording):
			assert session._skipShortRecording()
		assert session.error == recordingError

	# Finished audio below one frame is skipped; a full frame remains eligible for recognition.
	for size in (0, 2, recognition.recording.FRAME_BYTES):
		session = recognition.Session("default", threading.Event())
		session._onAudio(bytes(size))
		session.stop.set()
		session.recordingDone.set()
		assert session._skipShortRecording() == (size < recognition.recording.FRAME_BYTES)

	def activeRecording(deviceId, stop, onStarted, onAudio):
		onStarted()
		onAudio(bytes(recognition.recording.FRAME_BYTES))
		assert stop.wait(3)

	networkError = recognition.AuthenticationError("authentication unavailable")
	networkError.__cause__ = recognition.NetworkError("offline")
	for failure, expected in (
		(recognition.AuthenticationError("denied"), recognition.AUTHENTICATION_ERROR),
		(networkError, recognition.AUTHENTICATION_NETWORK_ERROR),
		(recognition.ApiError("invalid response"), recognition.AUTHENTICATION_RESPONSE_ERROR),
	):
		with (
			patch.object(recognition.recording, "record", side_effect=activeRecording),
			patch.object(recognition, "genSignHeaders", side_effect=failure),
		):
			session = recognition.Session("default", threading.Event())
			session.start("test-user", "test-pass", "0.1")
			await waitDone(session)
		assert session.error == expected

	for code in (10003, 10004):

		async def serviceError(socket):
			await socket.recv()
			await socket.send(json.dumps({"action": "error", "code": code}))

		async with serve(serviceError, "127.0.0.1", 0, compression=None) as local:
			port = local.sockets[0].getsockname()[1]
			with (
				patch.object(recognition, "ENDPOINT", f"ws://127.0.0.1:{port}/asr/v2"),
				patch.object(recognition.recording, "record", side_effect=activeRecording),
				patch.object(recognition, "genSignHeaders", return_value={"X-AI-GATEWAY-SIGNATURE": "test"}),
			):
				session = recognition.Session("default", threading.Event())
				session.start("test-user", "test-pass", "0.1")
				await waitDone(session)
		assert session.error == recognition.RECOGNITION_ERROR

	# Cancellation after capture starts must still prevent an authentication request.
	session = recognition.Session("default", threading.Event())

	def cancelAfterStart():
		assert session.recordingStarted.wait(2)
		session.cancel()
		return True

	with (
		patch.object(recognition.recording, "record", side_effect=activeRecording),
		patch.object(session, "_waitForRecordingStart", side_effect=cancelAfterStart),
		patch.object(recognition, "genSignHeaders") as sign,
	):
		session.start("test-user", "test-pass", "0.1")
		await waitDone(session)
	assert session.error is None
	sign.assert_not_called()


def runChecks(directory):
	host = {
		"addonHandler": SimpleNamespace(
			initTranslation=initTranslation,
			getCodeAddon=lambda: SimpleNamespace(manifest={"version": "0.1"}),
		),
		"config": SimpleNamespace(conf=ConfigManager(directory / "nvda.ini")),
		"globalPluginHandler": SimpleNamespace(GlobalPlugin=GlobalPlugin),
		"NVDAState": SimpleNamespace(shouldWriteToDisk=lambda: True),
		"gui": SimpleNamespace(guiHelper=Mock()),
		"gui.message": SimpleNamespace(MessageDialog=Mock()),
		"gui.settingsDialogs": SimpleNamespace(
			SettingsPanel=object,
			NVDASettingsDialog=SimpleNamespace(categoryClasses=[]),
		),
		"logHandler": SimpleNamespace(log=Mock()),
		"scriptHandler": SimpleNamespace(script=script),
		"systemUtils": SimpleNamespace(ExecAndPump=Mock()),
		"ui": SimpleNamespace(delayedMessage=Mock()),
		"tones": SimpleNamespace(beep=Mock()),
		"api": SimpleNamespace(getFocusObject=Mock(return_value=SimpleNamespace(control="editor"))),
		"brailleInput": SimpleNamespace(handler=SimpleNamespace(sendChars=Mock())),
		"eventHandler": SimpleNamespace(isPendingEvents=Mock(return_value=False)),
		"inputCore": SimpleNamespace(decide_handleRawKey=Mock(), decide_executeGesture=Mock()),
		"keyboardHandler": SimpleNamespace(
			KeyboardInputGesture=KeyboardGesture,
			isNVDAModifierKey=lambda vk, extended: vk in (20, 45),
		),
		"winUser": SimpleNamespace(VK_NUMLOCK=144, getAsyncKeyState=Mock(return_value=0)),
	}
	with patch.dict(sys.modules, host):
		pluginPath = Path(__file__).resolve().parents[1] / "addon/globalPlugins/VIVOVoiceInput/__init__.py"
		spec = importlib.util.spec_from_file_location("VIVOVoiceInput", pluginPath)
		plugin = importlib.util.module_from_spec(spec)
		sys.modules[spec.name] = plugin
		spec.loader.exec_module(plugin)
		keyboardChecks(plugin, host)
		from VIVOVoiceInput import recording, recognition

		recordingChecks(recording)
		asyncio.run(protocolChecks(recognition))


if __name__ == "__main__":
	app = wx.App(False)
	try:
		with tempfile.TemporaryDirectory() as directory:
			runChecks(Path(directory))
	finally:
		app.ProcessPendingEvents()
		app.Destroy()
	print("Voice input keyboard, focus, Unicode, PCM, protocol, timeout and cleanup checks passed.")
