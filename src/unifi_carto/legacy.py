"""Legacy (pre-Integration) Network API collector -> LegacySnapshot.

Read-only: every request is a GET through the given Transport, except the sanctioned
``Transport.login_legacy`` cookie fallback. Returns untouched JSON records; interpretation
happens in build.py.

Refusal details are exposed via ``Legacy.last_warnings`` (reset on every ``collect_site`` call):
when ``collect_site`` returns ``None`` the reason is there; when it returns a snapshot the same
warnings are also in ``snapshot.warnings``.
"""

from __future__ import annotations

import logging
from typing import Any, Literal
from urllib.parse import quote

from unifi_carto.errors import ApiError, AuthRefused, EndpointUnavailable
from unifi_carto.model import CollectionWarning
from unifi_carto.raw import JSON, LegacySnapshot
from unifi_carto.transport import Transport

log = logging.getLogger(__name__)

# Legacy envelope messages that mean "not authorized" even when the HTTP status was 200.
_AUTH_MESSAGES = frozenset({"api.err.LoginRequired", "api.err.NoPermission", "api.err.Invalid"})


class LegacyEnvelopeError(ApiError):
    """A legacy response whose envelope says ``meta.rc != "ok"``."""


class Legacy:
    """Collect one site's legacy data from ``https://<console>/proxy/network``."""

    def __init__(self, transport: Transport, *, console: str | None = None) -> None:
        self.transport = transport
        self.console = console
        self.last_warnings: list[CollectionWarning] = []

    def collect_site(
        self,
        site_name: str,
        *,
        include_clients: bool,
        credentials: tuple[str, str] | None,
    ) -> LegacySnapshot | None:
        """Fetch stat/device (required) plus rest/portconf, rest/networkconf, rest/wlanconf and,
        when ``include_clients``, stat/sta. Never raises for API problems: returns ``None`` when
        stat/device cannot be read, with the reason in ``last_warnings``.
        """
        self.last_warnings = []
        base = f"/api/s/{quote(site_name, safe='')}"
        auth: Literal["api_key", "login"] = "api_key"

        try:
            devices = self._get_list(f"{base}/stat/device")
        except AuthRefused:
            if credentials is None:
                self._warn(
                    "legacy_auth_refused",
                    "legacy API refused the API key and no legacy credentials are configured; port-level data skipped",
                    site_name,
                )
                return None
            try:
                self.transport.login_legacy(*credentials)
                devices = self._get_list(f"{base}/stat/device")
            except AuthRefused:
                self._warn(
                    "legacy_auth_refused",
                    "legacy API refused both the API key and the legacy login; port-level data skipped",
                    site_name,
                )
                return None
            except ApiError as exc:
                self._warn("legacy_error", f"legacy stat/device failed after login (status {exc.status})", site_name)
                return None
            auth = "login"
        except ApiError as exc:
            self._warn("legacy_error", f"legacy stat/device failed (status {exc.status})", site_name)
            return None

        snapshot = LegacySnapshot(site_name=site_name, auth=auth, devices=devices)
        snapshot.portconfs = self._optional(f"{base}/rest/portconf", site_name)
        snapshot.networkconfs = self._optional(f"{base}/rest/networkconf", site_name)
        snapshot.wlanconfs = self._optional(f"{base}/rest/wlanconf", site_name)
        if include_clients:
            snapshot.clients = self._optional(f"{base}/stat/sta", site_name)
        snapshot.warnings = list(self.last_warnings)
        return snapshot

    def _optional(self, path: str, site_name: str) -> list[JSON]:
        try:
            return self._get_list(path)
        except EndpointUnavailable:
            self._warn("endpoint_unavailable", f"legacy endpoint not available: {_tail(path)}", site_name)
        except ApiError as exc:
            self._warn("legacy_error", f"legacy {_tail(path)} failed (status {exc.status})", site_name)
        return []

    def _get_list(self, path: str) -> list[JSON]:
        data = unwrap(self.transport.get_json(path), url=path)
        log.debug("legacy GET %s -> %d records", path, len(data))
        return data

    def _warn(self, code: str, message: str, site_name: str) -> None:
        self.last_warnings.append(CollectionWarning(code=code, message=message, console=self.console, site=site_name))


def unwrap(body: Any, *, url: str = "") -> list[JSON]:
    """Return the records of a legacy response.

    Accepts the envelope ``{"meta": {"rc": "ok"}, "data": [...]}`` or a bare list. An envelope
    with ``rc != "ok"`` raises AuthRefused for login/permission messages, else LegacyEnvelopeError.
    Non-dict records are dropped.
    """
    if isinstance(body, list):
        records: Any = body
    elif isinstance(body, dict):
        meta = body.get("meta")
        if isinstance(meta, dict) and meta.get("rc", "ok") != "ok":
            msg = str(meta.get("msg", "unknown"))
            if msg in _AUTH_MESSAGES:
                raise AuthRefused(f"legacy API refused: {msg}", status=401, url=url)
            raise LegacyEnvelopeError(f"legacy API error: {msg}", status=200, url=url)
        records = body.get("data", [])
    else:
        raise LegacyEnvelopeError("legacy API returned an unexpected body", status=200, url=url)
    if not isinstance(records, list):
        raise LegacyEnvelopeError("legacy API data is not a list", status=200, url=url)
    return [r for r in records if isinstance(r, dict)]


def _tail(path: str) -> str:
    return "/".join(path.rsplit("/", 2)[-2:])
