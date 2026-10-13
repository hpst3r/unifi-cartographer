"""Exception types."""

from __future__ import annotations


class CartoError(Exception):
    """Base class. Messages must never contain secrets."""


class ConfigError(CartoError):
    """Invalid or unsafe configuration (exit code 2)."""


class ReadOnlyViolation(CartoError):
    """A non-GET request was attempted outside the sanctioned legacy login."""


class TlsPinMismatch(CartoError):
    """The console's certificate does not match the configured fingerprint."""


class ApiError(CartoError):
    """An HTTP error from a UniFi API. ``status`` is the HTTP status (0 for transport errors)."""

    def __init__(self, message: str, *, status: int, url: str) -> None:
        super().__init__(message)
        self.status = status
        self.url = url  # must not contain credentials (they are headers, never query params)


class AuthRefused(ApiError):
    """401/403: the credential is valid but not allowed here, or invalid."""


class EndpointUnavailable(ApiError):
    """404 on a documented endpoint: older application version."""
