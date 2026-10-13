"""Official UniFi Network Integration API (v1) -> ``OfficialSnapshot``.

The transport's base URL ends in ``/proxy/network/integration`` (local) or the Site Manager
connector base; every path here starts with ``/v1/``. JSON is returned untouched.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from unifi_carto.errors import ApiError, AuthRefused, EndpointUnavailable
from unifi_carto.model import CollectionWarning
from unifi_carto.raw import JSON, ConsoleRef, OfficialSnapshot
from unifi_carto.transport import Transport


class Integration:
    """Read-only collector for one console's Integration API."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self._info: JSON | None = None

    def info(self) -> JSON:
        """``GET /v1/info`` (``applicationVersion``); cached per instance."""
        if self._info is None:
            data = self.transport.get_json("/v1/info")
            if not isinstance(data, dict):
                raise ApiError("GET /v1/info: expected an object", status=200, url="/v1/info")
            self._info = data
        return self._info

    def sites(self) -> list[JSON]:
        return self.transport.get_paginated("/v1/sites")

    def collect_site(
        self, console: ConsoleRef, site: JSON, *, include_clients: bool, max_workers: int = 4
    ) -> OfficialSnapshot:
        """Collect everything for one site.

        AuthRefused on the first calls (info, device list) propagates; collect.py decides what to do.
        A 404 on an optional endpoint becomes ``endpoint_unavailable``, other API errors ``endpoint_error``.
        """
        site_id = str(site["id"])
        base = f"/v1/sites/{_seg(site_id)}"
        warnings: list[CollectionWarning] = []

        def warn(code: str, message: str) -> None:
            warnings.append(CollectionWarning(code=code, message=message, console=console.name, site=_site_label(site)))

        def optional(what: str, path: str) -> list[JSON]:
            try:
                return self.transport.get_paginated(f"{base}{path}")
            except EndpointUnavailable as exc:
                warn("endpoint_unavailable", f"{what}: not available on this Network version ({exc})")
            except ApiError as exc:
                warn("endpoint_error", f"{what}: {exc}")
            return []

        try:
            app_version = self.info().get("applicationVersion")
        except AuthRefused:
            raise
        except ApiError as exc:
            warn("endpoint_error", f"/v1/info: {exc}")
            app_version = None
        devices = self.transport.get_paginated(f"{base}/devices")

        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
            device_ids = [str(d["id"]) for d in devices if d.get("id")]
            fetched = list(pool.map(lambda i: self._detail(f"{base}/devices/{_seg(i)}"), device_ids))
            device_details: dict[str, JSON] = {}
            for dev_id, (detail, err) in zip(device_ids, fetched, strict=True):
                if detail is not None:
                    device_details[dev_id] = detail
                elif err is not None:
                    code = "endpoint_unavailable" if isinstance(err, EndpointUnavailable) else "endpoint_error"
                    warn(code, f"device {dev_id} detail: {err}")

            network_items = optional("networks", "/networks")
            networks = self._with_details(pool, network_items, f"{base}/networks", lambda _item: True, warn, "network")
            wlan_items = optional("wifi broadcasts", "/wifi/broadcasts")
            wlans = self._with_details(
                pool,
                wlan_items,
                f"{base}/wifi/broadcasts",
                lambda item: "securityConfiguration" not in item,
                warn,
                "wifi broadcast",
            )

        return OfficialSnapshot(
            console=console,
            site=site,
            app_version=app_version if isinstance(app_version, str) else None,
            devices=devices,
            device_details=device_details,
            networks=networks,
            wlans=wlans,
            wans=optional("wans", "/wans"),
            lags=optional("switching lags", "/switching/lags"),
            mc_lag_domains=optional("mc-lag domains", "/switching/mc-lag-domains"),
            switch_stacks=optional("switch stacks", "/switching/switch-stacks"),
            clients=optional("clients", "/clients") if include_clients else None,
            warnings=warnings,
        )

    def _detail(self, path: str) -> tuple[JSON | None, ApiError | None]:
        try:
            data = self.transport.get_json(path)
        except ApiError as exc:
            return None, exc
        if not isinstance(data, dict):
            return None, ApiError(f"GET {path}: expected an object", status=200, url=path)
        return data, None

    def _with_details(
        self,
        pool: ThreadPoolExecutor,
        items: list[JSON],
        path: str,
        wanted: Callable[[JSON], bool],
        warn: Callable[[str, str], None],
        what: str,
    ) -> list[JSON]:
        """Replace list items by their detail objects where ``wanted``; keep the list item if detail fails."""
        targets = [i for i, item in enumerate(items) if item.get("id") and wanted(item)]
        fetched = list(pool.map(lambda i: self._detail(f"{path}/{_seg(str(items[i]['id']))}"), targets))
        out = list(items)
        for i, (detail, err) in zip(targets, fetched, strict=True):
            if detail is not None:
                out[i] = detail
            elif err is not None:
                warn("endpoint_error", f"{what} {items[i]['id']} detail: {err}")
        return out


def _seg(value: str) -> str:
    """A server-supplied id as one URL path segment (``/``, ``?``, ``#``, ``..`` cannot redirect the request)."""
    seg = quote(value, safe="")
    return seg.replace(".", "%2E") if seg in (".", "..") else seg


def _site_label(site: JSON) -> str | None:
    for key in ("name", "internalReference", "id"):
        value = site.get(key)
        if isinstance(value, str) and value:
            return value
    return None
