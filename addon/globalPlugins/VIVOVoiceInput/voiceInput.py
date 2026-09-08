# Copyright (C) 2026 haitun
# This file is covered by the GNU General Public License.

from dataclasses import dataclass, field
import threading

import addonHandler
import api
import brailleInput
import eventHandler
import inputCore
import keyboardHandler
from logHandler import log
import tones
import ui
import winUser
import wx

from . import _, credentials, recognition


@dataclass
class _Press:
	mainKey: tuple
	keys: set
	stop: threading.Event = field(default_factory=threading.Event)


class VoiceInput:
	def __init__(self, script):
		self._script = script
		self._lock = threading.Lock()
		self._down = set()
		self._press = None
		self._session = None
		self._timer = None
		self._terminated = False
		inputCore.decide_handleRawKey.register(self._handleKey)
		inputCore.decide_executeGesture.register(self._handleGesture)

	def _handleGesture(self, gesture):
		if isinstance(gesture, keyboardHandler.KeyboardInputGesture) and gesture.script == self._script:
			with self._lock:
				key = (gesture.vkCode, gesture.isExtended)
				press = _Press(key, set(gesture.modifiers) | {key})
				if not press.keys.issubset(self._down):
					press.stop.set()
				self._press = press
				# Reserve this physical press before NVDA queues the script on the main thread.
				gesture._vivoPress = press
		return True

	def _handleKey(self, vkCode, scanCode, extended, pressed):
		key = (vkCode, extended)
		with self._lock:
			if pressed:
				self._down.add(key)
				if self._press and key == self._press.mainKey:
					return False
			else:
				self._down.discard(key)
				if self._press:
					if key in self._press.keys:
						self._press.stop.set()
					if key == self._press.mainKey:
						self._press = None
		# NVDA must process key-ups to clear its modifiers and trapped keys.
		return True

	def start(self, gesture):
		from . import (
			_getConfigSection,
			_getEffectiveInputDevice,
			LOGGED_IN_KEY,
			NVDACN_USERNAME_KEY,
			NVDACN_PASSWORD_KEY,
		)

		press = getattr(gesture, "_vivoPress", None)
		if self._terminated or press is None or press.stop.is_set():
			return
		if self._session is not None:
			# Translators: A previous recording is still being recognized or waiting to insert its result.
			ui.delayedMessage(_("The previous voice input is still being processed."))
			return
		section = _getConfigSection()
		if not section[LOGGED_IN_KEY]:
			# Translators: Voice input requires a successful login in the add-on settings.
			ui.delayedMessage(_("Please log in in the VIVO Voice Input settings first."))
			return
		try:
			username = section[NVDACN_USERNAME_KEY]
			password = credentials.decryptPassword(section[NVDACN_PASSWORD_KEY])
			if not username or not password:
				raise ValueError("Missing saved credentials")
			version = addonHandler.getCodeAddon().manifest["version"]
			self._focus = api.getFocusObject()
			self._keys = press.keys
			self._startBeep = self._stopBeep = False
			self._cancelNotified = False
			self._session = recognition.Session(_getEffectiveInputDevice(), press.stop)
			self._session.start(username, password, version)
		except Exception as error:
			log.error("Unable to start VIVO voice input (%s).", type(error).__name__)
			if self._session:
				self._session.cancel()
			self._session = None
			ui.delayedMessage(recognition.RECOGNITION_ERROR)
			return
		self._poll()

	def focusChanged(self, obj):
		if self._session and obj != self._focus and not self._cancelNotified:
			self._cancelNotified = True
			self._session.cancel()
			# Translators: The result will not be inserted because focus moved during this session.
			ui.delayedMessage(_("The focus has changed. This voice input has been cancelled."))

	def _poll(self):
		self._timer = None
		session = self._session
		if self._terminated or session is None:
			return
		self.focusChanged(api.getFocusObject())
		if session.started and not self._startBeep:
			self._startBeep = True
			tones.beep(300, 60)
			# Keep both beeps audible when a very short recording finishes between polls.
			self._timer = wx.CallLater(60, self._poll)
			return
		if session.started and session.recordingDone.is_set() and not self._stopBeep:
			self._stopBeep = True
			tones.beep(800, 60)
		if session.done.is_set():
			if session.error or session.cancelled.is_set():
				self._session = None
				if session.error and not self._cancelNotified:
					ui.delayedMessage(session.error)
				return
			if not session.text:
				self._session = None
				# Translators: Recognition completed without any text.
				ui.delayedMessage(_("No text was recognized."))
				return
			with self._lock:
				waiting = bool(self._down.intersection(self._keys)) or any(
					key[0] in keyboardHandler.KeyboardInputGesture.NORMAL_MODIFIER_KEYS
					or keyboardHandler.isNVDAModifierKey(*key)
					for key in self._down
				)
			waiting = waiting or any(
				winUser.getAsyncKeyState(vk) & 0x8000
				for vk in keyboardHandler.KeyboardInputGesture.NORMAL_MODIFIER_KEYS
				if isinstance(vk, int)
			)
			if not waiting and not eventHandler.isPendingEvents("gainFocus"):
				self._session = None
				try:
					brailleInput.handler.sendChars(session.text)
				except Exception as error:
					log.error("VIVO Unicode input failed (%s).", type(error).__name__)
					# Translators: NVDA could not submit the recognized text as Unicode input.
					ui.delayedMessage(_("Voice input failed."))
				return
		self._timer = wx.CallLater(20, self._poll)

	def terminate(self):
		self._terminated = True
		inputCore.decide_handleRawKey.unregister(self._handleKey)
		inputCore.decide_executeGesture.unregister(self._handleGesture)
		if self._timer:
			self._timer.Stop()
		if self._session:
			self._session.cancel()
			self._session = None
