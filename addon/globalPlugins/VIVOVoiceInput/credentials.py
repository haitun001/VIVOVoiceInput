# Copyright (C) 2026 haitun
# This file is covered by the GNU General Public License.

import base64
import ctypes
from ctypes import wintypes

_PREFIX = "dpapi:"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DATA_BLOB(ctypes.Structure):
	_fields_ = (
		("cbData", wintypes.DWORD),
		("pbData", ctypes.POINTER(ctypes.c_ubyte)),
	)


_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_crypt32.CryptProtectData.argtypes = (
	ctypes.POINTER(_DATA_BLOB),
	wintypes.LPCWSTR,
	ctypes.POINTER(_DATA_BLOB),
	wintypes.LPVOID,
	wintypes.LPVOID,
	wintypes.DWORD,
	ctypes.POINTER(_DATA_BLOB),
)
_crypt32.CryptProtectData.restype = wintypes.BOOL
_crypt32.CryptUnprotectData.argtypes = (
	ctypes.POINTER(_DATA_BLOB),
	ctypes.POINTER(wintypes.LPWSTR),
	ctypes.POINTER(_DATA_BLOB),
	wintypes.LPVOID,
	wintypes.LPVOID,
	wintypes.DWORD,
	ctypes.POINTER(_DATA_BLOB),
)
_crypt32.CryptUnprotectData.restype = wintypes.BOOL
_kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
_kernel32.LocalFree.restype = wintypes.HLOCAL


def _blobFromBytes(data: bytes) -> tuple[_DATA_BLOB, ctypes.Array]:
	buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
	return _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _protect(data: bytes) -> bytes:
	inBlob, inBuffer = _blobFromBytes(data)
	outBlob = _DATA_BLOB()
	try:
		if not _crypt32.CryptProtectData(
			ctypes.byref(inBlob),
			"VIVOVoiceInput NVDACN password",
			None,
			None,
			None,
			_CRYPTPROTECT_UI_FORBIDDEN,
			ctypes.byref(outBlob),
		):
			raise ctypes.WinError(ctypes.get_last_error())
		return ctypes.string_at(outBlob.pbData, outBlob.cbData)
	finally:
		if outBlob.pbData:
			_kernel32.LocalFree(ctypes.cast(outBlob.pbData, wintypes.HLOCAL))


def _unprotect(data: bytes) -> bytes:
	inBlob, inBuffer = _blobFromBytes(data)
	outBlob = _DATA_BLOB()
	try:
		if not _crypt32.CryptUnprotectData(
			ctypes.byref(inBlob),
			None,
			None,
			None,
			None,
			_CRYPTPROTECT_UI_FORBIDDEN,
			ctypes.byref(outBlob),
		):
			raise ctypes.WinError(ctypes.get_last_error())
		return ctypes.string_at(outBlob.pbData, outBlob.cbData)
	finally:
		if outBlob.pbData:
			_kernel32.LocalFree(ctypes.cast(outBlob.pbData, wintypes.HLOCAL))


def encryptPassword(password: str) -> str:
	if not password:
		return ""
	protected = _protect(password.encode("utf-8"))
	return _PREFIX + base64.b64encode(protected).decode("ascii")


def decryptPassword(value: str) -> str:
	if not value:
		return ""
	if not value.startswith(_PREFIX):
		raise ValueError("Stored password is not DPAPI encrypted")
	encrypted = base64.b64decode(value[len(_PREFIX) :])
	return _unprotect(encrypted).decode("utf-8")
