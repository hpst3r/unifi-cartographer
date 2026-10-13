"""Transport protocol: the seam between API collectors and the HTTP implementation.

``unifi_carto.http.ReadOnlyClient`` implements it; tests use fakes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

Params = Mapping[str, str | int]


class Transport(Protocol):
    """A read-only JSON transport bound to one base URL and one credential."""

    base_url: str

    def get_json(self, path: str, params: Params | None = None) -> Any:
        """GET ``base_url + path`` and return decoded JSON.

        Raises AuthRefused (401/403), EndpointUnavailable (404) or ApiError (anything else).
        """
        ...

    def get_paginated(self, path: str, params: Params | None = None, *, limit: int = 200) -> list[dict[str, Any]]:
        """GET an Integration API list endpoint, following offset/limit/totalCount; return all ``data`` items."""
        ...

    def login_legacy(self, username: str, password: str) -> None:
        """Legacy cookie login (POST /api/auth/login) on a UniFi OS console; the only allowed non-GET call.

        After success, subsequent get_json calls carry the session cookie instead of the API key.
        Raises AuthRefused on bad credentials or when 2FA is required.
        """
        ...
