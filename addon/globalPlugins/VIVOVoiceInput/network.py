# Copyright (C) 2026 haitun
# This file is covered by the GNU General Public License.

import requests

from .exceptions import ApiError, NetworkError


def sendRequest(method: str, url: str, data: bytes, timeout: int):
	try:
		response = requests.request(method=method, url=url, data=data, timeout=timeout)
	except requests.exceptions.RequestException:
		raise NetworkError("Network request failed") from None
	if response.status_code >= 400:
		raise ApiError(f"HTTP {response.status_code}")
	return response
