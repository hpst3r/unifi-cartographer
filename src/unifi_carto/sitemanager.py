"""Site Manager API (api.ui.com): console and site discovery.

Pagination is ``pageSize`` + ``nextToken`` (top-level in the response next to ``data``),
not the Integration API's offset/limit, so it is implemented here on top of ``get_json``.
"""

from __future__ import annotations

from typing import Any, Final

from unifi_carto.errors import ApiError
from unifi_carto.raw import ConsoleRef
from unifi_carto.transport import Transport

BASE_URL: Final = "https://api.ui.com"
API_KEY_HEADER: Final = "X-API-Key"
PAGE_SIZE: Final = 100
_MAX_PAGES: Final = 1000


def connector_base_url(host_id: str) -> str:
    """Integration API base for a console reached through the Site Manager connector."""
    return f"{BASE_URL}/v1/connector/consoles/{host_id}/network/integration"


class SiteManager:
    """Read-only Site Manager client over a ``Transport`` bound to ``https://api.ui.com``."""

    def __init__(self, transport: Transport, *, page_size: int = PAGE_SIZE) -> None:
        self.transport = transport
        self.page_size = page_size

    def hosts(self) -> list[dict[str, Any]]:
        return self._paged("/v1/hosts")

    def sites(self) -> list[dict[str, Any]]:
        return self._paged("/v1/sites")

    def devices(self) -> list[dict[str, Any]]:
        """Devices grouped per host: ``[{hostId, hostName, updatedAt, devices: [...]}, ...]``."""
        return self._paged("/v1/devices")

    def _paged(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        token: str | None = None
        seen: set[str] = set()
        for _ in range(_MAX_PAGES):
            params: dict[str, str | int] = {"pageSize": str(self.page_size)}
            if token:
                params["nextToken"] = token
            page = self.transport.get_json(path, params)
            if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                raise ApiError(f"GET {path}: expected an object with 'data'", status=200, url=path)
            items.extend(item for item in page["data"] if isinstance(item, dict))
            token = page.get("nextToken") or None
            if not token or not page["data"]:
                return items
            if token in seen:
                raise ApiError(f"GET {path}: server repeated a nextToken", status=200, url=path)
            seen.add(token)
        raise ApiError(f"GET {path}: more than {_MAX_PAGES} pages", status=200, url=path)


def host_summary(host: dict[str, Any]) -> ConsoleRef:
    """Map a ``GET /v1/hosts`` item to a connector ``ConsoleRef``.

    ``reportedState`` is free-form in the spec; the documented example carries ``name``,
    ``hostname``, ``version`` and ``firmware_version``. UniFi OS consoles additionally report
    ``hardware.{shortname,name}`` (not in the spec example, so treated as optional).
    """
    state = host.get("reportedState")
    state = state if isinstance(state, dict) else {}
    hardware = state.get("hardware")
    hardware = hardware if isinstance(hardware, dict) else {}
    host_id = str(host.get("id") or "")
    if not host_id:
        raise ApiError("Site Manager host without an id", status=200, url="/v1/hosts")
    return ConsoleRef(
        id=host_id,
        name=_first_str(state.get("name"), state.get("hostname"), host_id) or host_id,
        access="connector",
        model=_first_str(hardware.get("shortname"), hardware.get("name"), state.get("model")),
        firmware=_first_str(
            state.get("version"),
            state.get("firmware_version"),
            state.get("firmwareVersion"),
            hardware.get("firmwareVersion"),
        ),
    )


def _first_str(*values: object) -> str | None:
    for v in values:
        if isinstance(v, str) and v:
            return v
    return None
