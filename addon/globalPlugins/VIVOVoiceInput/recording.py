# Copyright (C) 2026 haitun
# This file is covered by the GNU General Public License.

from ctypes import HRESULT, POINTER, byref, c_ubyte, c_uint32, c_uint64, string_at
import time

import comtypes
from comtypes import COMMETHOD, GUID, IUnknown
from pycaw.api.audioclient import IAudioClient
from pycaw.api.audioclient.depend import WAVEFORMATEX
from pycaw.constants import EDataFlow, ERole
from pycaw.utils import AudioUtilities

from .inputDevices import DEFAULT_DEVICE_ID

SAMPLE_RATE = 16000
FRAME_BYTES = 1280
MAX_SECONDS = 60
MAX_BYTES = SAMPLE_RATE * 2 * MAX_SECONDS


class IAudioCaptureClient(IUnknown):
	_iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
	_methods_ = (
		COMMETHOD(
			[],
			HRESULT,
			"GetBuffer",
			(["out"], POINTER(POINTER(c_ubyte)), "data"),
			(["out"], POINTER(c_uint32), "frames"),
			(["out"], POINTER(c_uint32), "flags"),
			(["out"], POINTER(c_uint64), "devicePosition"),
			(["out"], POINTER(c_uint64), "qpcPosition"),
		),
		COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "frames")),
		COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "frames")),
	)


def record(deviceId, stop, onStarted, onAudio):
	"""Capture on the caller's COM-initialized thread; release interfaces before returning."""
	enumerator = device = client = capture = None
	running = False
	try:
		enumerator = AudioUtilities.GetDeviceEnumerator()
		device = (
			enumerator.GetDefaultAudioEndpoint(EDataFlow.eCapture.value, ERole.eMultimedia.value)
			if deviceId == DEFAULT_DEVICE_ID
			else enumerator.GetDevice(deviceId)
		)
		client = device.Activate(IAudioClient._iid_, comtypes.CLSCTX_ALL, None).QueryInterface(IAudioClient)
		format = WAVEFORMATEX(
			wFormatTag=1,
			nChannels=1,
			nSamplesPerSec=SAMPLE_RATE,
			nAvgBytesPerSec=SAMPLE_RATE * 2,
			nBlockAlign=2,
			wBitsPerSample=16,
			cbSize=0,
		)
		# Shared-mode WASAPI converts the endpoint mix format to 16 kHz mono PCM.
		client.Initialize(0, 0x80000000, 1000000, 0, byref(format), None)
		capture = client.GetService(IAudioCaptureClient._iid_).QueryInterface(IAudioCaptureClient)
		if stop.is_set():
			return
		client.Start()
		running = True
		deadline = time.monotonic() + MAX_SECONDS
		onStarted()
		remaining = MAX_BYTES
		pending = bytearray()
		while remaining:
			if running and (stop.is_set() or time.monotonic() >= deadline):
				client.Stop()
				running = False
			if not capture.GetNextPacketSize():
				if not running:
					break
				stop.wait(0.005)
				continue
			data, frames, flags, _, _ = capture.GetBuffer()
			try:
				size = min(frames * 2, remaining)
				packet = bytes(size) if flags & 2 else string_at(data, size)
			finally:
				capture.ReleaseBuffer(frames)
			remaining -= size
			pending.extend(packet)
			while len(pending) >= FRAME_BYTES:
				onAudio(bytes(pending[:FRAME_BYTES]))
				del pending[:FRAME_BYTES]
		if pending:
			onAudio(bytes(pending))
	finally:
		try:
			if running:
				client.Stop()
		finally:
			capture = client = device = enumerator = None
