"""Run on Windows with Python 3.13 and NVDA's wxPython, requests, configobj, pycaw and comtypes."""

import importlib
import importlib.util
import json
from functools import partial
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from configobj import ConfigObj
from configobj.validate import Validator
import requests
import wx


class ConfigManager:
	def __init__(self, filename):
		self.spec = ConfigObj()
		self.BASE_ONLY_SECTIONS = set()
		self.validator = Validator()
		self.profiles = [ConfigObj(str(filename), encoding="UTF-8")]

	def __getitem__(self, key):
		assert key in self.BASE_ONLY_SECTIONS
		return self.profiles[0][key]

	def save(self):
		self.profiles[0].write()


class SettingsPanel(wx.Panel):
	def __init__(self, parent):
		super().__init__(parent)
		sizer = wx.BoxSizer(wx.VERTICAL)
		self.makeSettings(sizer)
		self.SetSizerAndFit(sizer)
		self.focusCalls = []
		for control in (self.usernameEdit, self.passwordEdit, self.loginButton):
			control.SetFocus = partial(self._recordFocus, control, control.SetFocus)

	def _recordFocus(self, control, setFocus):
		self.focusCalls.append(
			(
				control,
				self._busy,
				bool(self) and self.loginButton.IsEnabled(),
				bool(control) and control.IsEnabled(),
			),
		)
		setFocus()

	def onPanelActivated(self):
		self.Show()

	def onDiscard(self):
		pass


class BoxSizerHelper:
	def __init__(self, parent, sizer):
		self.parent = parent
		self.sizer = sizer

	def addLabeledControl(self, label, controlType, **kwargs):
		self.addItem(wx.StaticText(self.parent, label=label))
		return self.addItem(controlType(self.parent, **kwargs))

	def addItem(self, control):
		self.sizer.Add(control)
		return control


def initTranslation():
	sys._getframe(1).f_globals["_"] = lambda message: message


def response(data):
	result = requests.Response()
	result.status_code = 200
	result._content = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
	return result


def click(panel):
	wasBusy = panel._busy
	if not wasBusy:
		panel.focusCalls.clear()
	event = wx.CommandEvent(wx.EVT_BUTTON.typeId, panel.loginButton.GetId())
	event.SetEventObject(panel.loginButton)
	panel.loginButton.ProcessWindowEvent(event)
	if panel and not wasBusy:
		target = panel.loginButton if panel.loginButton.GetLabel() == "Log out" else panel.usernameEdit
		assert panel.focusCalls == [(target, False, True, True)]


def runChecks(directory, frame):
	filename = directory / "nvda.ini"
	conf = ConfigManager(filename)
	notifications = []
	delayedMessage = Mock(side_effect=lambda message: notifications.append(("speech", message)))
	alert = Mock(
		side_effect=lambda message, caption, parent: notifications.append(
			("dialog", message, bool(parent.focusCalls)),
		),
	)
	execute = Mock(side_effect=lambda function, *args: SimpleNamespace(funcRes=function(*args)))
	state = SimpleNamespace(shouldWriteToDisk=Mock(return_value=True))
	hostModules = {
		"addonHandler": SimpleNamespace(initTranslation=initTranslation),
		"config": SimpleNamespace(conf=conf),
		"globalPluginHandler": SimpleNamespace(GlobalPlugin=object),
		"NVDAState": state,
		"gui": SimpleNamespace(guiHelper=SimpleNamespace(BoxSizerHelper=BoxSizerHelper)),
		"gui.message": SimpleNamespace(MessageDialog=SimpleNamespace(alert=alert)),
		"gui.settingsDialogs": SimpleNamespace(
			SettingsPanel=SettingsPanel,
			NVDASettingsDialog=SimpleNamespace(categoryClasses=[]),
		),
		"logHandler": SimpleNamespace(log=Mock()),
		"systemUtils": SimpleNamespace(ExecAndPump=execute),
		"scriptHandler": SimpleNamespace(script=lambda **kwargs: lambda function: function),
		"ui": SimpleNamespace(delayedMessage=delayedMessage),
	}
	with (
		patch.dict(sys.modules, hostModules),
		patch("pycaw.utils.AudioUtilities.GetAllDevices", return_value=[]),
	):
		pluginPath = Path(__file__).resolve().parents[1] / "addon/globalPlugins/VIVOVoiceInput/__init__.py"
		spec = importlib.util.spec_from_file_location("VIVOVoiceInput", pluginPath)
		plugin = importlib.util.module_from_spec(spec)
		sys.modules[spec.name] = plugin
		spec.loader.exec_module(plugin)
		auth = importlib.import_module("VIVOVoiceInput.auth._vivo_auth")
		sectionName = plugin.CONFIG_SECTION
		username = "test-user&+?"
		password = "test-p\u00e4ssword&+?"
		encrypted = plugin.credentials.encryptPassword(password)
		assert encrypted.startswith("dpapi:") and password not in encrypted
		assert plugin.credentials.decryptPassword(encrypted) == password
		assert plugin.credentials.decryptPassword(plugin.credentials.encryptPassword("")) == ""
		conf.profiles[0][sectionName] = {"nvdacnUsername": username, "nvdacnPassword": encrypted}
		conf.profiles[0]["unrelated"] = {"keep": "unchanged"}
		conf.save()

		with patch.object(
			auth.network,
			"sendRequest",
			return_value=response({"code": 200, "data": "signature"}),
		) as request:
			panel = plugin.VIVOVoiceInputSettingsPanel(frame)
			section = conf[sectionName]
			assert section["loggedIn"] is False
			assert panel.usernameEdit.GetValue() == username
			assert panel.passwordEdit.GetValue() == password
			assert panel.passwordEdit.GetWindowStyle() & wx.TE_PASSWORD
			assert panel.loginButton.GetLabel() == "Log in"
			assert section["nvdacnPassword"] == encrypted
			request.assert_not_called()

			# OK and Cancel must not persist unverified edits or erase the original credentials.
			panel.usernameEdit.SetValue("unverified")
			panel.passwordEdit.SetValue("unverified")
			panel.onSave()
			panel.onDiscard()
			assert section["nvdacnUsername"] == username and section["nvdacnPassword"] == encrypted
			before = filename.read_bytes()
			panel.passwordEdit.Clear()
			click(panel)
			request.assert_not_called()
			assert "Please enter your NVDACN username and password" in alert.call_args.args[0]
			panel.usernameEdit.SetValue(username)
			panel.passwordEdit.SetValue(password)

			for data, message in (
				({"code": 403, "data": "denied"}, "Login was unsuccessful."),
				(None, "The NVDACN authentication server returned an invalid response"),
				([], "The NVDACN authentication server returned an invalid response"),
				(b"{invalid json", "The NVDACN authentication server returned an invalid response"),
				({"code": 200, "data": ""}, "The NVDACN authentication server returned an invalid response"),
				({"code": 200, "data": " "}, "The NVDACN authentication server returned an invalid response"),
				(
					{"code": 200, "data": None},
					"The NVDACN authentication server returned an invalid response",
				),
				({"code": 200, "data": 123}, "The NVDACN authentication server returned an invalid response"),
			):
				request.return_value = response(data)
				click(panel)
				assert alert.call_args.args[0].startswith(message), data
				assert not section["loggedIn"] and not panel._loggedIn
				assert section["nvdacnPassword"] == encrypted
				assert panel.usernameEdit.IsEnabled() and panel.loginButton.IsEnabled()
				assert filename.read_bytes() == before

			for error in (auth.NetworkError("offline"), auth.ApiError("HTTP 503")):
				request.side_effect = error
				click(panel)
				assert alert.call_args.args[0].startswith(
					"Could not connect to the NVDACN authentication server.",
				)
				assert not section["loggedIn"] and filename.read_bytes() == before
			request.side_effect = None
			request.return_value = response({"code": 200, "data": "signature"})

			with patch.object(
				plugin.credentials,
				"encryptPassword",
				side_effect=OSError("encryption failed"),
			):
				click(panel)
				assert alert.call_args.args[0].startswith("Could not encrypt the password.")
			with patch.object(conf, "save", side_effect=PermissionError("read only")):
				click(panel)
				assert alert.call_args.args[0].startswith("Authentication succeeded, but an error occurred")
			state.shouldWriteToDisk.return_value = False
			click(panel)
			state.shouldWriteToDisk.return_value = True
			assert not panel._loggedIn and section["loggedIn"] is False
			assert section["nvdacnPassword"] == encrypted and filename.read_bytes() == before

			# A reentrant button event during ExecAndPump must not start a second request.
			def executeWithReentry(function, *args):
				click(panel)
				return SimpleNamespace(funcRes=function(*args))

			execute.side_effect = executeWithReentry
			request.reset_mock()
			click(panel)
			request.assert_called_once()
			assert request.call_args.kwargs["method"] == "POST"
			assert request.call_args.kwargs["timeout"] == 6
			assert request.call_args.kwargs["data"].startswith(b"GET\n/asr/v2\n\n")
			from urllib.parse import parse_qs, urlsplit

			assert parse_qs(urlsplit(request.call_args.kwargs["url"]).query) == {
				"user": [username],
				"pass": [password],
				"name": ["vivo"],
				"action": ["signature"],
			}
			assert section["loggedIn"] is True and panel._loggedIn
			assert panel.loginButton.GetLabel() == "Log out"
			assert all(not child.IsEnabled() for child in panel._credentialControls)
			assert panel.inputDeviceChoice.IsEnabled()
			assert panel.usernameEdit.IsShown() and panel.passwordEdit.IsShown()
			assert ConfigObj(str(filename))[sectionName]["loggedIn"] == "True"
			assert plugin.credentials.decryptPassword(section["nvdacnPassword"]) == password
			assert password.encode("utf-8") not in filename.read_bytes()
			panel.onDiscard()

			# Reopen using a fresh configuration object, as after an NVDA restart.
			conf.profiles[0] = ConfigObj(str(filename), encoding="UTF-8")
			request.reset_mock()
			panel = plugin.VIVOVoiceInputSettingsPanel(frame)
			section = conf[sectionName]
			assert panel._loggedIn and section["loggedIn"] is True
			request.assert_not_called()
			beforeLogout = filename.read_bytes()
			with patch.object(conf, "save", side_effect=PermissionError("read only")):
				click(panel)
			assert alert.call_args.args[0].startswith("Logout is incomplete.")
			assert panel._loggedIn and section["loggedIn"] is True
			assert panel.loginButton.IsEnabled() and panel.loginButton.GetLabel() == "Log out"
			assert filename.read_bytes() == beforeLogout
			click(panel)
			request.assert_not_called()
			assert not panel._loggedIn and section["loggedIn"] is False
			assert panel.usernameEdit.GetValue() == panel.passwordEdit.GetValue() == ""
			assert panel.usernameEdit.IsEnabled() and panel.passwordEdit.IsEnabled()
			assert panel.loginButton.GetLabel() == "Log in"
			panel.onSave()
			panel.onDiscard()
			conf.save()
			disk = ConfigObj(str(filename), encoding="UTF-8")
			assert disk[sectionName] == {
				"nvdacnUsername": "",
				"nvdacnPassword": "",
				"loggedIn": "False",
				"inputDevice": "default",
			}
			assert disk["unrelated"]["keep"] == "unchanged"
			assert username not in filename.read_text(encoding="UTF-8")
			assert "dpapi:" not in filename.read_text(encoding="UTF-8")

			section.update({"nvdacnUsername": username, "nvdacnPassword": "dpapi:invalid", "loggedIn": True})
			panel = plugin.VIVOVoiceInputSettingsPanel(frame)
			alert.reset_mock()
			delayedMessage.reset_mock()
			panel.onPanelActivated()
			assert alert.call_args.args[0].startswith("Could not read the saved password.")
			panel.onPanelActivated()
			alert.assert_called_once()
			delayedMessage.assert_called_once_with(alert.call_args.args[0])
			assert panel.focusCalls == [(panel.passwordEdit, False, True, True)]
			assert not panel._loggedIn and panel.passwordEdit.IsEnabled()
			assert section["nvdacnPassword"] == "dpapi:invalid"
			panel.passwordEdit.SetValue(password)
			click(panel)
			assert panel._loggedIn
			click(panel)

			# A destroyed panel must not save credentials when its request finishes.
			panel.usernameEdit.SetValue(username)
			panel.passwordEdit.SetValue(password)
			before = filename.read_bytes()

			def executeAfterClose(function, *args):
				panel.Destroy()
				wx.Yield()
				return SimpleNamespace(funcRes=function(*args))

			execute.side_effect = executeAfterClose
			click(panel)
			assert section["loggedIn"] is False and filename.read_bytes() == before
			assert not panel.focusCalls
			beforeClose = len(notifications)
			panel._showError("Ignored after the panel has closed")
			assert len(notifications) == beforeClose

			# Every popup schedules the same text once, and no focus is set before the popup returns.
			assert len(notifications) % 2 == 0
			for index in range(0, len(notifications), 2):
				spoken, dialog = notifications[index : index + 2]
				assert spoken == ("speech", dialog[1])
				assert dialog[0] == "dialog" and dialog[2] is False
			logged = repr(hostModules["logHandler"].log.mock_calls)
			assert username not in logged and password not in logged and "{invalid json" not in logged


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
	print("Login, logout, persistence, focus, speech notifications and Windows DPAPI checks passed.")
