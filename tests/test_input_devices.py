"""Run on Windows with Python 3.13 and NVDA's wxPython, requests, configobj, pycaw and comtypes."""

import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import comtypes
from configobj import ConfigObj
from pycaw.constants import DEVICE_STATE, EDataFlow, ERole
from pycaw.utils import AudioUtilities
import wx

from test_login import BoxSizerHelper, ConfigManager, SettingsPanel, initTranslation


class GlobalPlugin:
	def terminate(self):
		pass


def choose(panel, deviceId):
	selection = panel._inputDeviceIds.index(deviceId)
	panel.inputDeviceChoice.SetSelection(selection)
	event = wx.CommandEvent(wx.EVT_CHOICE.typeId, panel.inputDeviceChoice.GetId())
	event.SetEventObject(panel.inputDeviceChoice)
	event.SetInt(selection)
	panel.inputDeviceChoice.ProcessWindowEvent(event)


def finishCheck(instance):
	instance._inputDeviceCheckThread.join(timeout=5)
	assert not instance._inputDeviceCheckThread.is_alive()
	wx.GetApp().ProcessPendingEvents()


def runChecks(directory, frame):
	filename = directory / "nvda.ini"
	conf = ConfigManager(filename)
	mainThread = threading.get_ident()
	speech = Mock(side_effect=lambda message: assertMainThread(mainThread))
	hostModules = {
		"addonHandler": SimpleNamespace(initTranslation=initTranslation),
		"config": SimpleNamespace(conf=conf),
		"globalPluginHandler": SimpleNamespace(GlobalPlugin=GlobalPlugin),
		"NVDAState": SimpleNamespace(shouldWriteToDisk=lambda: True),
		"gui": SimpleNamespace(guiHelper=SimpleNamespace(BoxSizerHelper=BoxSizerHelper)),
		"gui.message": SimpleNamespace(MessageDialog=SimpleNamespace(alert=Mock())),
		"gui.settingsDialogs": SimpleNamespace(
			SettingsPanel=SettingsPanel,
			NVDASettingsDialog=SimpleNamespace(categoryClasses=[]),
		),
		"logHandler": SimpleNamespace(log=Mock()),
		"systemUtils": SimpleNamespace(ExecAndPump=Mock()),
		"scriptHandler": SimpleNamespace(script=lambda **kwargs: lambda function: function),
		"VIVOVoiceInput.voiceInput": SimpleNamespace(VoiceInput=Mock()),
		"ui": SimpleNamespace(delayedMessage=speech),
	}
	endpoints = [
		SimpleNamespace(id="input-a", FriendlyName="Same microphone", state=1, flow=1),
		SimpleNamespace(id="input-b", FriendlyName="Same microphone", state=1, flow=1),
		SimpleNamespace(id="virtual", FriendlyName="Virtual \u9ea6\u514b\u98ce", state=1, flow=1),
		SimpleNamespace(id="disabled", FriendlyName="Disabled microphone", state=2, flow=1),
		SimpleNamespace(id="removed", FriendlyName="Removed microphone", state=4, flow=1),
		SimpleNamespace(id="unplugged", FriendlyName="Unplugged microphone", state=8, flow=1),
		SimpleNamespace(id="speakers", FriendlyName="Speakers", state=1, flow=0),
	]
	defaultEndpoint = [endpoints[0]]

	def activeEndpoints(flow, state):
		assert flow == EDataFlow.eCapture.value
		assert state == DEVICE_STATE.ACTIVE.value
		return [endpoint for endpoint in endpoints if endpoint.flow == flow and endpoint.state & state]

	def allDevices(data_flow, device_state):
		return activeEndpoints(data_flow, device_state) + [None]

	def enumEndpoints(flow, state):
		active = activeEndpoints(flow, state)
		return SimpleNamespace(
			GetCount=lambda: len(active),
			Item=lambda index: SimpleNamespace(GetId=lambda: active[index].id),
		)

	def getDefault(flow, role):
		assert flow == EDataFlow.eCapture.value
		assert role == ERole.eMultimedia.value
		if defaultEndpoint[0] is None:
			raise comtypes.COMError(-2147023728, "No default device", None)
		return SimpleNamespace(GetState=lambda: defaultEndpoint[0].state)

	enumerator = SimpleNamespace(EnumAudioEndpoints=enumEndpoints, GetDefaultAudioEndpoint=getDefault)
	with (
		patch.dict(sys.modules, hostModules),
		patch.object(AudioUtilities, "GetAllDevices", side_effect=allDevices) as enumerateDevices,
		patch.object(AudioUtilities, "GetDeviceEnumerator", return_value=enumerator),
	):
		pluginPath = Path(__file__).resolve().parents[1] / "addon/globalPlugins/VIVOVoiceInput/__init__.py"
		spec = importlib.util.spec_from_file_location("VIVOVoiceInput", pluginPath)
		plugin = importlib.util.module_from_spec(spec)
		sys.modules[spec.name] = plugin
		spec.loader.exec_module(plugin)
		devices = plugin.inputDevices
		section = plugin._getConfigSection()
		conf.save()
		panel = plugin.VIVOVoiceInputSettingsPanel(frame)
		assert panel.inputDeviceChoice.GetItems() == [
			"System default input device",
			"Same microphone",
			"Same microphone",
			"Virtual \u9ea6\u514b\u98ce",
		]
		assert panel.inputDeviceChoice.GetSelection() == 0
		assert devices.isInputDeviceAvailable("default")
		assert devices.isInputDeviceAvailable("input-a")
		for unavailable in ("missing", "speakers", "disabled", "removed", "unplugged"):
			assert not devices.isInputDeviceAvailable(unavailable)

		# Applying changes only updates memory. Cancel restores the most recently applied selection.
		before = filename.read_bytes()
		choose(panel, "input-b")
		assert section["inputDevice"] == "default"
		panel.onSave()
		assert section["inputDevice"] == "input-b"
		assert filename.read_bytes() == before
		choose(panel, "virtual")
		panel.onDiscard()
		assert panel._inputDeviceIds[panel.inputDeviceChoice.GetSelection()] == "input-b"
		panel.onSave()
		assert section["inputDevice"] == "input-b" and filename.read_bytes() == before
		conf.save()
		assert ConfigObj(str(filename))[plugin.CONFIG_SECTION]["inputDevice"] == "input-b"

		# Reordering endpoints never changes which of two identically named devices is selected.
		endpoints.reverse()
		panel = plugin.VIVOVoiceInputSettingsPanel(frame)
		assert panel._inputDeviceIds[panel.inputDeviceChoice.GetSelection()] == "input-b"
		assert panel.inputDeviceChoice.GetStringSelection() == "Same microphone"
		endpoints.reverse()

		# A valid preference, including the system default, starts silently without touching disk.
		beforeStartup = filename.read_bytes()
		for deviceId in ("input-b", "default"):
			section["inputDevice"] = deviceId
			speech.reset_mock()
			instance = plugin.GlobalPlugin()
			finishCheck(instance)
			assert plugin._getEffectiveInputDevice() == deviceId
			speech.assert_not_called()
			instance.terminate()
		assert filename.read_bytes() == beforeStartup

		# A missing preference falls back only for this session, including when credentials are saved.
		section["inputDevice"] = "input-a"
		conf.save()
		before = filename.read_bytes()
		panel = plugin.VIVOVoiceInputSettingsPanel(frame)
		endpoints[0].state = DEVICE_STATE.UNPLUGGED.value
		defaultEndpoint[0] = endpoints[1]
		speech.reset_mock()
		instance = plugin.GlobalPlugin()
		finishCheck(instance)
		assert plugin._getEffectiveInputDevice() == "default"
		assert panel.inputDeviceChoice.GetSelection() == 0
		assert not panel._inputDeviceChanged
		assert section["inputDevice"] == "input-a" and filename.read_bytes() == before
		speech.assert_called_once()
		assert "Switched to the system default recording device." in speech.call_args.args[0]
		panel.onSave()
		panel.onDiscard()
		conf.save()
		for loggedIn in (True, False):
			plugin._saveCredentials(
				"test-user" if loggedIn else "",
				plugin.credentials.encryptPassword("test-password") if loggedIn else "",
				loggedIn,
			)
			assert ConfigObj(str(filename))[plugin.CONFIG_SECTION]["inputDevice"] == "input-a"
		instance.terminate()

		# Restarting checks the original device again, and restores it if it has returned.
		endpoints[0].state = DEVICE_STATE.ACTIVE.value
		speech.reset_mock()
		instance = plugin.GlobalPlugin()
		finishCheck(instance)
		assert plugin._getEffectiveInputDevice() == "input-a"
		assert panel._inputDeviceIds[panel.inputDeviceChoice.GetSelection()] == "input-a"
		speech.assert_not_called()
		instance.terminate()

		# No default device must not produce a false successful-fallback message.
		defaultEndpoint[0] = None
		for deviceId in ("missing", "default"):
			section["inputDevice"] = deviceId
			speech.reset_mock()
			instance = plugin.GlobalPlugin()
			finishCheck(instance)
			speech.assert_called_once()
			assert "Please check the system input device settings." in speech.call_args.args[0]
			assert section["inputDevice"] == deviceId
			instance.terminate()
		defaultEndpoint[0] = endpoints[1]
		defaultEndpoint[0].state = DEVICE_STATE.DISABLED.value
		assert not devices.isInputDeviceAvailable("default")
		defaultEndpoint[0].state = DEVICE_STATE.ACTIVE.value

		# A COM failure is not evidence that a saved device no longer exists.
		section["inputDevice"] = "input-a"
		speech.reset_mock()
		uninitializeCOM = comtypes.CoUninitialize

		def releaseCOM():
			assert sys.exception() is None
			uninitializeCOM()

		with (
			patch.object(devices, "isInputDeviceAvailable", side_effect=OSError("Audio service failure")),
			patch.object(comtypes, "CoInitializeEx", wraps=comtypes.CoInitializeEx) as initialize,
			patch.object(comtypes, "CoUninitialize", side_effect=releaseCOM) as uninitialize,
		):
			instance = plugin.GlobalPlugin()
			finishCheck(instance)
			initialize.assert_called_once_with(comtypes.COINIT_MULTITHREADED)
			uninitialize.assert_called_once_with()
		speech.assert_called_once_with(plugin._INPUT_DEVICE_CHECK_FAILED)
		assert plugin._getEffectiveInputDevice() == "default" and section["inputDevice"] == "input-a"
		instance.terminate()
		with (
			patch.object(comtypes, "CoInitializeEx", side_effect=OSError("COM unavailable")),
			patch.object(comtypes, "CoUninitialize") as uninitialize,
		):
			instance = plugin.GlobalPlugin()
			finishCheck(instance)
			uninitialize.assert_not_called()
		instance.terminate()
		with patch.object(
			enumerator,
			"GetDefaultAudioEndpoint",
			side_effect=comtypes.COMError(-1, "Failure", None),
		):
			try:
				devices.isInputDeviceAvailable("default")
			except comtypes.COMError:
				pass
			else:
				raise AssertionError("Unexpected COM errors must propagate")

		# Merely viewing a fallback never saves it; explicitly choosing that same default item does.
		panel = plugin.VIVOVoiceInputSettingsPanel(frame)
		assert panel.inputDeviceChoice.GetSelection() == 0
		panel.onSave()
		assert section["inputDevice"] == "input-a"
		choose(panel, "default")
		panel.onSave()
		assert section["inputDevice"] == "default" and plugin._inputDeviceFallback is None

		# Enumeration errors keep login usable and cannot be saved as a device change.
		section["inputDevice"] = "input-a"
		enumerateDevices.side_effect = OSError("Enumeration failure")
		speech.reset_mock()
		failedPanel = plugin.VIVOVoiceInputSettingsPanel(frame)
		assert not failedPanel.inputDeviceChoice.IsEnabled()
		assert failedPanel.usernameEdit.IsEnabled() and failedPanel.loginButton.IsEnabled()
		failedPanel.onPanelActivated()
		failedPanel.onPanelActivated()
		speech.assert_called_once_with(plugin._INPUT_DEVICE_CHECK_FAILED)
		failedPanel._setLoginState(True)
		failedPanel._setLoginState(False)
		assert not failedPanel.inputDeviceChoice.IsEnabled()
		choose(failedPanel, "default")
		failedPanel.onSave()
		assert section["inputDevice"] == "input-a"
		enumerateDevices.side_effect = allDevices
		panel = plugin.VIVOVoiceInputSettingsPanel(frame)
		assert panel.inputDeviceChoice.IsEnabled()

		# A pending check must not overwrite a newer applied choice, even an A -> B -> A change.
		entered = threading.Event()
		release = threading.Event()

		def slowCheck(deviceId):
			assert threading.get_ident() != mainThread
			entered.set()
			assert release.wait(timeout=5)
			return deviceId == "default"

		with patch.object(devices, "isInputDeviceAvailable", side_effect=slowCheck):
			speech.reset_mock()
			instance = plugin.GlobalPlugin()
			try:
				assert entered.wait(timeout=5)
				assert instance._inputDeviceCheckThread.is_alive()
				for deviceId in ("input-b", "input-a"):
					choose(panel, deviceId)
					panel.onSave()
			finally:
				release.set()
				finishCheck(instance)
			assert plugin._getEffectiveInputDevice() == "input-a"
			speech.assert_not_called()
			instance.terminate()

		# Unapplied edits remain visible when the old, still-active preference needs a fallback.
		with patch.object(
			devices,
			"isInputDeviceAvailable",
			side_effect=lambda deviceId: deviceId == "default",
		):
			instance = plugin.GlobalPlugin()
			choose(panel, "input-b")
			finishCheck(instance)
			assert panel._inputDeviceIds[panel.inputDeviceChoice.GetSelection()] == "input-b"
			panel.onDiscard()
			assert panel.inputDeviceChoice.GetSelection() == 0
			assert section["inputDevice"] == "input-a"
			instance.terminate()

		# Configuration reloads and plugin termination invalidate already queued results.
		for action in ("reload", "terminate"):
			with patch.object(
				devices,
				"isInputDeviceAvailable",
				side_effect=lambda deviceId: deviceId == "default",
			):
				speech.reset_mock()
				instance = plugin.GlobalPlugin()
				instance._inputDeviceCheckThread.join(timeout=5)
				assert not instance._inputDeviceCheckThread.is_alive()
				if action == "reload":
					conf.save()
					conf.profiles[0] = ConfigObj(str(filename), encoding="UTF-8")
					section = plugin._getConfigSection()
				else:
					instance.terminate()
				wx.GetApp().ProcessPendingEvents()
				speech.assert_not_called()
				assert plugin._getEffectiveInputDevice() == section["inputDevice"]
				if action == "reload":
					instance.terminate()


def assertMainThread(mainThread):
	assert threading.get_ident() == mainThread


if __name__ == "__main__":
	app = wx.App(False)
	frame = wx.Frame(None)
	try:
		with tempfile.TemporaryDirectory() as directory:
			runChecks(Path(directory), frame)
	finally:
		frame.Destroy()
		app.ProcessPendingEvents()
		app.Destroy()
	print(
		"Input device enumeration, selection, persistence, fallback and asynchronous lifecycle checks passed.",
	)
