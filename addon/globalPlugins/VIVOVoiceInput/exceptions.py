# Copyright (C) 2026 haitun
# This file is covered by the GNU General Public License.


class AuthenticationError(Exception):
	"""Raised when NVDACN authentication fails."""


class NetworkError(Exception):
	"""Raised when a network request fails."""


class ApiError(Exception):
	"""Raised when a service response is invalid."""
