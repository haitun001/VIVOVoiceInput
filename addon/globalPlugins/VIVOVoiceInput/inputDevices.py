# Copyright (C) 2026 haitun
# This file is covered by the GNU General Public License.

import comtypes
from pycaw.constants import DEVICE_STATE, EDataFlow, ERole
from pycaw.utils import AudioUtilities

DEFAULT_DEVICE_ID = "default"
_E_NOTFOUND = -2147023728  # HRESULT_FROM_WIN32(ERROR_NOT_FOUND)


def getInputDevices() -> list[tuple[str, str]]:
	"""Return capture endpoint IDs and names, following NVDA's MMDevice enumeration."""
	devices = AudioUtilities.GetAllDevices(
		data_flow=EDataFlow.eCapture.value,
		device_state=DEVICE_STATE.ACTIVE.value,
	)
	result = []
	for device in devices:
		if device is None:
			continue
		name = device.FriendlyName
		if not isinstance(name, str) or not name:
			raise ValueError("Input device has no friendly name")
		result.append((device.id, name))
	return result


def isInputDeviceAvailable(deviceId: str) -> bool:
	"""Check endpoint state without opening a recording stream or reading device properties."""
	enumerator = AudioUtilities.GetDeviceEnumerator()
	if deviceId == DEFAULT_DEVICE_ID:
		try:
			device = enumerator.GetDefaultAudioEndpoint(EDataFlow.eCapture.value, ERole.eMultimedia.value)
		except comtypes.COMError as error:
			if error.hresult == _E_NOTFOUND:
				return False
			raise
		return device.GetState() == DEVICE_STATE.ACTIVE.value
	endpoints = enumerator.EnumAudioEndpoints(EDataFlow.eCapture.value, DEVICE_STATE.ACTIVE.value)
	return any(endpoints.Item(index).GetId() == deviceId for index in range(endpoints.GetCount()))
