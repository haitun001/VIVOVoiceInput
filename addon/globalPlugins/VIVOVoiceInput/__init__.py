# Copyright (C) 2026 haitun
# This file is covered by the GNU General Public License.

import threading
from weakref import WeakSet

import addonHandler
import comtypes
import config
import globalPluginHandler
import NVDAState
import ui
import wx
from gui import guiHelper
from gui.message import MessageDialog
from gui.settingsDialogs import NVDASettingsDialog, SettingsPanel
from logHandler import log
from scriptHandler import script
from systemUtils import ExecAndPump

from . import credentials, inputDevices
from .exceptions import ApiError, AuthenticationError, NetworkError

addonHandler.initTranslation()

CONFIG_SECTION = "VIVOVoiceInput"
NVDACN_USERNAME_KEY = "nvdacnUsername"
NVDACN_PASSWORD_KEY = "nvdacnPassword"
LOGGED_IN_KEY = "loggedIn"
INPUT_DEVICE_KEY = "inputDevice"

_CONFIG_SPEC = {
	NVDACN_USERNAME_KEY: 'string(default="")',
	NVDACN_PASSWORD_KEY: 'string(default="")',
	LOGGED_IN_KEY: "boolean(default=False)",
	INPUT_DEVICE_KEY: f'string(default="{inputDevices.DEFAULT_DEVICE_ID}")',
}

# Translators: Device detection failed; the saved selection is preserved while using the default device.
_INPUT_DEVICE_CHECK_FAILED = _(
	"VIVO Voice Input could not correctly detect the system input configuration. "
	"Switched to the default input device.",
)

_inputDeviceFallback = None
_inputDeviceRevision = 0
_settingsPanels = WeakSet()


def _ensureConfig() -> None:
	if config.conf is None:
		return
	if CONFIG_SECTION not in config.conf.spec:
		config.conf.spec[CONFIG_SECTION] = {}
	config.conf.spec[CONFIG_SECTION].update(_CONFIG_SPEC)
	config.conf.BASE_ONLY_SECTIONS.add(CONFIG_SECTION)

	baseConfig = config.conf.profiles[0]
	if CONFIG_SECTION not in baseConfig:
		baseConfig[CONFIG_SECTION] = {}
	section = baseConfig[CONFIG_SECTION]
	section.configspec = config.conf.spec[CONFIG_SECTION]
	baseConfig.validate(config.conf.validator, section=section)


def _getConfigSection():
	_ensureConfig()
	return config.conf[CONFIG_SECTION]


def _getEffectiveInputDevice() -> str:
	section = _getConfigSection()
	deviceId = section[INPUT_DEVICE_KEY]
	# Reloading configuration or changing the preference invalidates the session-only fallback.
	if (
		_inputDeviceFallback is not None
		and _inputDeviceFallback[0] is section
		and _inputDeviceFallback[1] == deviceId
	):
		return inputDevices.DEFAULT_DEVICE_ID
	return deviceId


def _saveCredentials(username: str, encryptedPassword: str, loggedIn: bool) -> None:
	if not NVDAState.shouldWriteToDisk():
		raise PermissionError("NVDA configuration cannot be saved in this mode")
	section = _getConfigSection()
	previous = {key: section[key] for key in _CONFIG_SPEC}
	try:
		section[NVDACN_USERNAME_KEY] = username
		section[NVDACN_PASSWORD_KEY] = encryptedPassword
		section[LOGGED_IN_KEY] = loggedIn
		config.conf.save()
	except Exception:
		section.update(previous)
		raise


class VIVOVoiceInputSettingsPanel(SettingsPanel):
	# Translators: This is the label for the VIVO Voice Input settings category.
	title = _("VIVO Voice Input")

	def makeSettings(self, settingsSizer: wx.BoxSizer) -> None:
		section = _getConfigSection()
		sHelper = guiHelper.BoxSizerHelper(self, sizer=settingsSizer)
		self._busy = False

		# Translators: This is the label for the NVDACN username edit field.
		usernameLabel = _("NVDACN &username:")
		self.usernameEdit = sHelper.addLabeledControl(usernameLabel, wx.TextCtrl)
		self.usernameEdit.SetValue(section[NVDACN_USERNAME_KEY])

		# Translators: This is the label for the NVDACN password edit field.
		passwordLabel = _("NVDACN &password:")
		self.passwordEdit = sHelper.addLabeledControl(passwordLabel, wx.TextCtrl, style=wx.TE_PASSWORD)
		self._passwordLoadFailed = False
		try:
			self.passwordEdit.SetValue(credentials.decryptPassword(section[NVDACN_PASSWORD_KEY]))
		except Exception:
			self._passwordLoadFailed = True
			log.error("Unable to decrypt stored NVDACN password.", exc_info=True)

		# Translators: Button to verify and save the NVDACN credentials.
		self.loginButton = sHelper.addItem(wx.Button(self, label=_("Log in")))
		self.loginButton.Bind(wx.EVT_BUTTON, self.onLogin)
		self._credentialControls = tuple(
			child for child in self.GetChildren() if child is not self.loginButton
		)
		self._setLoginState(
			bool(section[LOGGED_IN_KEY])
			and bool(self.usernameEdit.GetValue())
			and bool(self.passwordEdit.GetValue()),
		)

		self._inputDeviceChanged = False
		self._inputDeviceLoadFailed = False
		# Translators: The first input device choice follows the Windows default recording device.
		devices = [(inputDevices.DEFAULT_DEVICE_ID, _("System default input device"))]
		try:
			devices.extend(inputDevices.getInputDevices())
		except Exception:
			global _inputDeviceFallback
			_inputDeviceFallback = (section, section[INPUT_DEVICE_KEY])
			self._inputDeviceLoadFailed = True
			log.error("Unable to enumerate input devices.", exc_info=True)
		self._inputDeviceIds, names = zip(*devices)
		self.inputDeviceChoice = sHelper.addLabeledControl(
			# Translators: The label for the recording device selection in the add-on settings.
			_("Input device"),
			wx.Choice,
			choices=names,
		)
		self.inputDeviceChoice.Enable(not self._inputDeviceLoadFailed)
		self._refreshInputDeviceSelection()
		self.inputDeviceChoice.Bind(wx.EVT_CHOICE, self.onInputDeviceChanged)
		_settingsPanels.add(self)

	def onPanelActivated(self) -> None:
		super().onPanelActivated()
		if not self._inputDeviceChanged:
			self._refreshInputDeviceSelection()
		if self._inputDeviceLoadFailed:
			self._inputDeviceLoadFailed = False
			ui.delayedMessage(_INPUT_DEVICE_CHECK_FAILED)
		if self._passwordLoadFailed:
			self._passwordLoadFailed = False
			self._showError(
				# Translators: The saved password could not be decrypted; the stored credentials are preserved.
				_("Could not read the saved password. Please enter your password again and click Log in."),
			)
			if self:
				self.passwordEdit.SetFocus()

	def _setLoginState(self, loggedIn: bool) -> None:
		self._loggedIn = loggedIn
		# Translators: The same button logs out after a successful login, and logs in otherwise.
		self.loginButton.SetLabel(_("Log out") if loggedIn else _("Log in"))
		for child in self._credentialControls:
			child.Enable(not loggedIn)

	def _refreshInputDeviceSelection(self) -> None:
		deviceId = _getEffectiveInputDevice()
		try:
			selection = self._inputDeviceIds.index(deviceId)
		except ValueError:
			selection = 0
		self.inputDeviceChoice.SetSelection(selection)

	def onInputDeviceChanged(self, event: wx.CommandEvent) -> None:
		self._inputDeviceChanged = True

	def _showError(self, message: str) -> None:
		if self:
			ui.delayedMessage(message)
			MessageDialog.alert(message, self.title, parent=self)

	def onLogin(self, event: wx.CommandEvent) -> None:
		if self._busy:
			return
		self._busy = True
		self.loginButton.Disable()
		try:
			if self._loggedIn:
				self._logout()
			else:
				self._login()
		finally:
			self._busy = False
			if self:
				self.loginButton.Enable()
				(self.loginButton if self._loggedIn else self.usernameEdit).SetFocus()

	def _login(self) -> None:
		from .auth._vivo_auth import genSignHeaders

		username = self.usernameEdit.GetValue()
		password = self.passwordEdit.GetValue()
		if not username or not password:
			self._showError(
				# Translators: Both NVDACN credential fields are required before logging in.
				_("Please enter your NVDACN username and password before logging in."),
			)
			return

		try:
			# ExecAndPump processes window messages, so prevent reentrant settings actions while it runs.
			with wx.WindowDisabler():
				headers = ExecAndPump(genSignHeaders, username, password, "GET", "/asr/v2", {}).funcRes
			signature = headers.get("X-AI-GATEWAY-SIGNATURE") if isinstance(headers, dict) else None
			if not isinstance(signature, str) or not signature.strip():
				raise ApiError("NVDACN returned an invalid signature")
		except AuthenticationError as error:
			if isinstance(error.__cause__, (NetworkError, ApiError)):
				# Translators: The authentication request could not reach the service successfully.
				message = _(
					"Could not connect to the NVDACN authentication server. "
					"Please check your network connection or try again later.",
				)
			else:
				# Translators: NVDACN rejected the login credentials.
				message = _(
					"Login was unsuccessful. Please check your NVDACN username and password and try again.",
				)
			self._showError(message)
			return
		except Exception as error:
			log.error("Invalid NVDACN authentication response (%s).", type(error).__name__)
			self._showError(
				# Translators: An invalid response prevents confirming the login.
				_(
					"The NVDACN authentication server returned an invalid response, so login cannot be completed. "
					"Please try again later.",
				),
			)
			return

		if not self:
			return
		try:
			encryptedPassword = credentials.encryptPassword(password)
		except Exception:
			log.error("Unable to encrypt NVDACN password.", exc_info=True)
			self._showError(
				# Translators: The new login credentials have not been saved because encryption failed.
				_(
					"Could not encrypt the password. These login details have not been saved. "
					"Please try logging in again.",
				),
			)
			return
		try:
			_saveCredentials(username, encryptedPassword, True)
		except Exception:
			log.error("Unable to save NVDACN login credentials.", exc_info=True)
			self._showError(
				# Translators: Authentication succeeded, but saving the login failed.
				_(
					"Authentication succeeded, but an error occurred while saving your login details. "
					"Please try logging in again.",
				),
			)
			return
		if self:
			self._setLoginState(True)

	def _logout(self) -> None:
		try:
			_saveCredentials("", "", False)
		except Exception:
			log.error("Unable to clear saved NVDACN credentials.", exc_info=True)
			self._showError(
				# Translators: Logout can be retried because saving the cleared credentials failed.
				_(
					"Logout is incomplete. An error occurred while saving the NVDA configuration. "
					"Please click Log out again to retry.",
				),
			)
			return
		if self:
			self.usernameEdit.Clear()
			self.passwordEdit.Clear()
			self._setLoginState(False)

	def onSave(self) -> None:
		# Login and logout persist immediately; unverified edits must not overwrite saved credentials.
		# Displaying a fallback alone must not overwrite the preferred input device.
		if self._inputDeviceChanged and self.inputDeviceChoice.IsEnabled():
			selection = self.inputDeviceChoice.GetSelection()
			if selection == wx.NOT_FOUND:
				return
			global _inputDeviceFallback, _inputDeviceRevision
			_getConfigSection()[INPUT_DEVICE_KEY] = self._inputDeviceIds[selection]
			_inputDeviceFallback = None
			_inputDeviceRevision += 1
			self._inputDeviceChanged = False

	def onDiscard(self) -> None:
		self._inputDeviceChanged = False
		self._refreshInputDeviceSelection()
		super().onDiscard()


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		from .voiceInput import VoiceInput

		super().__init__()
		_ensureConfig()
		self._terminated = False
		self._voiceInput = VoiceInput(self.script_voiceInput)
		if VIVOVoiceInputSettingsPanel not in NVDASettingsDialog.categoryClasses:
			NVDASettingsDialog.categoryClasses.append(VIVOVoiceInputSettingsPanel)
		section = _getConfigSection()
		self._inputDeviceCheckThread = threading.Thread(
			target=self._checkInputDevice,
			args=(section, section[INPUT_DEVICE_KEY], _inputDeviceRevision),
			name="VIVOVoiceInput input device check",
			daemon=True,
		)
		self._inputDeviceCheckThread.start()

	@script(
		# Translators: Gesture description for hold-to-record speech recognition.
		description=_("Hold for speech recognition; release to stop recognition"),
		category=_("VIVO Voice Input"),
		gesture="kb:NVDA+shift+v",
		speakOnDemand=True,
	)
	def script_voiceInput(self, gesture):
		self._voiceInput.start(gesture)

	def event_gainFocus(self, obj, nextHandler):
		self._voiceInput.focusChanged(obj)
		nextHandler()

	def _checkInputDevice(self, section, deviceId: str, revision: int) -> None:
		message = None
		initialized = False
		try:
			comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
			initialized = True
			if not inputDevices.isInputDeviceAvailable(deviceId):
				if deviceId != inputDevices.DEFAULT_DEVICE_ID and inputDevices.isInputDeviceAvailable(
					inputDevices.DEFAULT_DEVICE_ID,
				):
					# Translators: The unavailable selection is preserved for the next NVDA startup.
					message = _(
						"VIVO Voice Input: The selected input device is unavailable. "
						"Switched to the system default recording device.",
					)
				else:
					# Translators: No usable system default recording device was found.
					message = _(
						"VIVO Voice Input detected that the system default input device is unavailable. "
						"Please check the system input device settings.",
					)
		except Exception:
			log.error("Unable to check the selected input device.", exc_info=True)
			message = _INPUT_DEVICE_CHECK_FAILED
		finally:
			# Handle exceptions first so their tracebacks no longer retain COM interfaces.
			if initialized:
				comtypes.CoUninitialize()
		if not self._terminated:
			wx.CallAfter(self._inputDeviceCheckComplete, section, deviceId, revision, message)

	def _inputDeviceCheckComplete(self, section, deviceId: str, revision: int, message: str | None) -> None:
		if (
			self._terminated
			or config.conf is None
			or _getConfigSection() is not section
			or section[INPUT_DEVICE_KEY] != deviceId
			or _inputDeviceRevision != revision
		):
			return
		global _inputDeviceFallback
		_inputDeviceFallback = (section, deviceId) if message else None
		for panel in tuple(_settingsPanels):
			if panel and not panel._inputDeviceChanged:
				panel._refreshInputDeviceSelection()
		if message:
			ui.delayedMessage(message)

	def terminate(self):
		self._terminated = True
		self._voiceInput.terminate()
		try:
			NVDASettingsDialog.categoryClasses.remove(VIVOVoiceInputSettingsPanel)
		except ValueError:
			pass
		super().terminate()
