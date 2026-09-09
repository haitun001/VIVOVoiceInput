# Copyright (C) 2026 haitun
# This file is covered by the GNU General Public License.

import asyncio
import hashlib
import json
import logging
import queue
import threading
import time
import uuid

import comtypes
from logHandler import log

from . import _, recording
from .auth._vivo_auth import _genCanonicalQueryString, genSignHeaders
from .exceptions import ApiError, AuthenticationError, NetworkError
from ._vendor.websockets.asyncio.client import connect
from ._vendor.websockets.exceptions import ConnectionClosedOK

ENDPOINT = "ws://api-ai.vivo.com.cn/asr/v2"
SEND_TIMEOUT = 5
RESULT_TIMEOUT = 10
_protocolLog = logging.Logger("VIVOVoiceInput.websocket", level=logging.CRITICAL + 1)
_protocolLog.propagate = False

# Translators: Recording could not start or failed while reading the selected input device.
RECORDING_ERROR = _("Recording failed. Please check the input device and try again.")
# Translators: Authentication, connection, or speech recognition failed.
RECOGNITION_ERROR = _("Speech recognition failed. Please try again.")
# Translators: The speech service did not return recognized text.
NO_TEXT_ERROR = _("No text was recognized.")
# Translators: The saved VIVO credentials were rejected by NVDACN.
AUTHENTICATION_ERROR = _("Please log in in the VIVO Voice Input settings first.")
# Translators: The authentication service could not be reached.
AUTHENTICATION_NETWORK_ERROR = _("Could not connect to the authentication server.")
# Translators: The authentication service returned an invalid response.
AUTHENTICATION_RESPONSE_ERROR = _("Invalid response from the authentication server.")


class RecognitionServiceError(ValueError):
	def __init__(self, code):
		super().__init__(f"VIVO recognition service error ({code})")
		self.code = code


class Session:
	def __init__(self, deviceId, stop):
		self.deviceId = deviceId
		self.stop = stop
		self.cancelled = threading.Event()
		self.recordingDone = threading.Event()
		self.done = threading.Event()
		self.audio = queue.SimpleQueue()
		self.started = False
		self.error = None
		self.text = None
		self._finalReceived = False
		self._audioBytes = 0
		self.recordingStarted = threading.Event()

	def start(self, username, password, version):
		self.recorder = threading.Thread(target=self._record, name="VIVOVoiceInput recording", daemon=True)
		self.worker = threading.Thread(
			target=self._run,
			args=(username, password, version),
			name="VIVOVoiceInput recognition",
			daemon=True,
		)
		self.worker.start()

	def cancel(self):
		self.cancelled.set()
		self.stop.set()

	def _record(self):
		initialized = False
		try:
			comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
			initialized = True
			recording.record(self.deviceId, self.stop, self._started, self._onAudio)
		except Exception as error:
			log.error("VIVO recording failed (%s).", type(error).__name__)
			self.error = RECORDING_ERROR
			self.cancel()
		finally:
			if initialized:
				comtypes.CoUninitialize()
			self.stop.set()
			self.audio.put(None)
			self.recordingDone.set()

	def _started(self):
		self.started = True
		self.recordingStarted.set()

	def _onAudio(self, data):
		self._audioBytes += len(data)
		self.audio.put(data)

	def _run(self, username, password, version):
		try:
			if self.cancelled.is_set():
				return
			self.recorder.start()
			if not self._waitForRecordingStart():
				if not self.cancelled.is_set() and not self.error:
					self.error = NO_TEXT_ERROR
				return
			if self._skipShortRecording():
				return
			params = {
				"client_version": version,
				"package": "VIVOVoiceInput",
				"sdk_version": "unknown",
				"user_id": hashlib.sha256(username.encode("utf-8")).hexdigest()[:32],
				"android_version": "unknown",
				"system_time": str(int(time.time() * 1000)),
				"net_type": 1,
				"engineid": "shortasrinput",
			}
			headers = genSignHeaders(username, password, "GET", "/asr/v2", params)
			signature = headers.get("X-AI-GATEWAY-SIGNATURE")
			if not isinstance(signature, str) or not signature.strip():
				raise ApiError("Invalid authentication signature")
			password = username = None
			if not self._skipShortRecording():
				url = ENDPOINT + "?" + _genCanonicalQueryString(params)
				self.text = asyncio.run(self._recognize(url, headers))
		except AuthenticationError as error:
			if not self.cancelled.is_set():
				if isinstance(error.__cause__, NetworkError):
					self.error = AUTHENTICATION_NETWORK_ERROR
				elif isinstance(error.__cause__, ApiError):
					self.error = AUTHENTICATION_RESPONSE_ERROR
				else:
					self.error = AUTHENTICATION_ERROR
			log.error("VIVO recognition authentication failed (%s).", type(error).__name__)
		except NetworkError as error:
			if not self.cancelled.is_set():
				self.error = AUTHENTICATION_NETWORK_ERROR
			log.error("VIVO recognition authentication service failed (%s).", type(error).__name__)
		except ApiError as error:
			if not self.cancelled.is_set():
				self.error = AUTHENTICATION_RESPONSE_ERROR
			log.error("VIVO recognition authentication response failed (%s).", type(error).__name__)
		except RecognitionServiceError as error:
			if not self.cancelled.is_set():
				self.error = RECOGNITION_ERROR
			log.error("VIVO recognition service failed (code=%s).", error.code)
		except Exception as error:
			log.error("VIVO recognition failed (%s).", type(error).__name__)
			if not self.cancelled.is_set():
				self.error = RECOGNITION_ERROR
		finally:
			self.stop.set()
			if self.recorder.ident is not None:
				self.recorder.join()
			while not self.audio.empty():
				self.audio.get_nowait()
			self.done.set()

	def _waitForRecordingStart(self):
		while not self.recordingStarted.wait(0.01):
			if self.cancelled.is_set() or self.recordingDone.is_set():
				return False
		return True

	def _skipShortRecording(self):
		if self.cancelled.is_set() or self.error:
			return True
		if not self.stop.is_set():
			return False
		self.recordingDone.wait()
		if self.cancelled.is_set() or self.error:
			return True
		if self._audioBytes < recording.FRAME_BYTES:
			self.error = NO_TEXT_ERROR
			return True
		return False

	async def _recognize(self, url, headers):
		# Cancellation also covers an in-progress handshake, without another worker thread.
		operation = asyncio.create_task(self._exchange(url, headers))
		try:
			while not operation.done():
				if self.cancelled.is_set():
					return None
				await asyncio.sleep(0.01)
			return await operation
		finally:
			operation.cancel()
			await asyncio.gather(operation, return_exceptions=True)

	async def _exchange(self, url, headers):
		async with connect(
			url,
			additional_headers=headers,
			open_timeout=10,
			close_timeout=1,
			compression=None,
			ping_interval=None,
			logger=_protocolLog,
		) as socket:
			await self._send(
				socket,
				json.dumps(
					{
						"type": "started",
						"request_id": uuid.uuid4().hex,
						"asr_info": {
							"end_vad_time": 2000,
							"audio_type": "pcm",
							"chinese2digital": 1,
							"punctuation": 1,
						},
					},
				),
			)
			sender = asyncio.create_task(self._sendAudio(socket))
			receiver = asyncio.create_task(self._receive(socket))
			try:
				finished, _ = await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
				if receiver not in finished:
					await sender
					text = await asyncio.wait_for(receiver, RESULT_TIMEOUT)
				else:
					text = await receiver
					self.stop.set()
					# Finish any current binary send, discard unsent audio, then send --end--.
					self._finalReceived = True
					try:
						await sender
					except ConnectionClosedOK:
						pass
				try:
					await self._send(socket, b"--close--")
				except ConnectionClosedOK:
					pass
				return text
			finally:
				self.stop.set()
				# Close before cancelling a send: an interrupted send must never be reused.
				try:
					await socket.close()
				finally:
					sender.cancel()
					receiver.cancel()
					await asyncio.gather(sender, receiver, return_exceptions=True)

	async def _send(self, socket, data):
		await asyncio.wait_for(socket.send(data), SEND_TIMEOUT)

	async def _sendAudio(self, socket):
		while not self._finalReceived:
			try:
				packet = self.audio.get_nowait()
			except queue.Empty:
				await asyncio.sleep(0.01)
				continue
			if packet is None:
				break
			await self._send(socket, packet)
		await self._send(socket, b"--end--")

	async def _receive(self, socket):
		text = ""
		lastResultId = -1
		while True:
			message = json.loads(await socket.recv())
			if not isinstance(message, dict):
				raise ValueError("Invalid or unsuccessful recognition response")
			action = message.get("action")
			code = message.get("code")
			if type(code) is not int:
				raise ValueError("Invalid or unsuccessful recognition response")
			if code != 0:
				raise RecognitionServiceError(code)
			if action in ("started", "vad"):
				continue
			if action != "result":
				raise ValueError("Unexpected recognition action")
			if message.get("type") in ("nlu", "common"):
				continue
			data = message.get("data")
			if message.get("type") != "asr" or not isinstance(data, dict):
				raise ValueError("Invalid recognition data")
			resultId = data.get("result_id")
			if (
				not isinstance(data.get("text"), str)
				or type(data.get("is_last")) is not bool
				or type(resultId) is not int
				or resultId < 0
				or resultId < lastResultId
				or type(data.get("reformation")) is not int
				or data["reformation"] not in (0, 1)
			):
				raise ValueError("Invalid recognition result")
			if data["reformation"]:
				text = data["text"]
			elif resultId != lastResultId:
				text += data["text"]
			lastResultId = resultId
			if data["is_last"]:
				return text
