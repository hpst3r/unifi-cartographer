"""Orchestration robustness proofs (collect.py, integration.py) with fake transports."""

from __future__ import annotations

from typing import Any

import pytest

from unifi_carto.collect import collect, plan_targets
from unifi_carto.config import Config, ConsoleConfig, SecretRef, TlsConfig
from unifi_carto.errors import ApiError, EndpointUnavailable, TlsPinMismatch
from unifi_carto.model import CollectionWarning
from unifi_carto.sitemanager import BASE_URL as SM_URL

SITE = {"id": "6f1a0000-0000-4000-8000-000000000001", "name": "Main", "internalReference": "default"}


def device(n: int) -> dict[str, Any]:
    return {
        "id": f"6f1a0000-0000-4000-8000-0000000001{n:02d}",
        "macAddress": f"02:00:00:00:01:{n:02x}",
        "name": f"usw-acc-{n}",
        "model": "USW-24",
        "ipAddress": f"192.0.2.{n}",
        "features": ["switching"],
    }


class Fake:
    """Transport fake: a dict of path -> JSON; paginated paths return lists."""

    def __init__(self, base_url: str, routes: dict[str, Any]) -> None:
        self.base_url = base_url
        self.routes = routes
        self.logins = 0

    def get_json(self, path: str, params: Any = None) -> Any:
        if path not in self.routes:
            raise EndpointUnavailable(f"GET {path}: HTTP 404", status=404, url=path)
        return self.routes[path]

    def get_paginated(self, path: str, params: Any = None, *, limit: int = 200) -> list[dict[str, Any]]:
        if path not in self.routes:
            return []
        data: list[dict[str, Any]] = self.routes[path]
        return data

    def login_legacy(self, username: str, password: str) -> None:
        self.logins += 1


def official_routes(devs: list[dict[str, Any]]) -> dict[str, Any]:
    base = f"/v1/sites/{SITE['id']}"
    routes: dict[str, Any] = {"/v1/info": {"applicationVersion": "10.6.106"}, "/v1/sites": [SITE]}
    routes[f"{base}/devices"] = devs
    for d in devs:
        routes[f"{base}/devices/{d['id']}"] = d
    return routes


LEGACY_ROUTES = {"/api/s/default/stat/device": {"meta": {"rc": "ok"}, "data": []}}


def local(name: str, ip: str) -> ConsoleConfig:
    return ConsoleConfig(name=name, local_url=f"https://{ip}", api_key=SecretRef(env=f"REVIEW_KEY_{name.upper()}"))


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("A", "B", "HQ", "SM"):
        monkeypatch.setenv(f"REVIEW_KEY_{name}", f"synthetic-key-{name.lower()}")


def test_col01_pin_mismatch_on_one_console_does_not_abort_run() -> None:
    cfg = Config(consoles=(local("a", "192.0.2.10"), local("b", "192.0.2.20")))

    def factory(
        base_url: str, *, api_key: str, api_key_header: str = "X-API-KEY", tls: TlsConfig | None = None
    ) -> Fake:
        if "192.0.2.20" in base_url:
            raise TlsPinMismatch(
                "192.0.2.20:443 certificate fingerprint sha256:AA:.. does not match the configured pin"
            )
        return Fake(base_url, official_routes([device(1)]) if base_url.endswith("integration") else LEGACY_ROUTES)

    result = collect(cfg, client_factory=factory)
    assert [d.name for d in result.inventory.devices] == ["usw-acc-1"]
    assert result.exit_code == 1
    assert any(w.console == "b" for w in result.inventory.warnings)


def test_col02_legacy_connection_failure_keeps_official_data() -> None:
    cfg = Config(consoles=(local("a", "192.0.2.10"),))

    def factory(
        base_url: str, *, api_key: str, api_key_header: str = "X-API-KEY", tls: TlsConfig | None = None
    ) -> Fake:
        if base_url.endswith("/proxy/network"):  # legacy client: e.g. the TLS pin re-fetch times out
            raise ApiError("cannot fetch TLS certificate from 192.0.2.10:443: timed out", status=0, url="/")
        return Fake(base_url, official_routes([device(1)]))

    result = collect(cfg, client_factory=factory)
    assert [d.name for d in result.inventory.devices] == ["usw-acc-1"]
    assert result.exit_code in (0, 1)


def test_col03_ambiguous_site_manager_host_name_is_not_guessed() -> None:
    host_a = {"id": "HOSTA0000:1", "reportedState": {"name": "hq"}}
    host_b = {"id": "HOSTB0000:2", "reportedState": {"name": "hq"}}
    cfg = Config(site_manager_key=SecretRef(env="REVIEW_KEY_SM"), consoles=(local("hq", "192.0.2.10"),))

    def factory(
        base_url: str, *, api_key: str, api_key_header: str = "X-API-KEY", tls: TlsConfig | None = None
    ) -> Fake:
        assert base_url == SM_URL
        return Fake(base_url, {"/v1/hosts": {"data": [host_a, host_b]}})

    warnings: list[CollectionWarning] = []
    targets = plan_targets(cfg, client_factory=factory, warnings=warnings)
    local_target = next(t for t in targets if t.ref.access == "local")
    # Either the local console is not bound to an unverified host id, or the ambiguity is reported.
    unbound = local_target.ref.id not in (host_a["id"], host_b["id"])
    reported = any("hq" in f"{w.console} {w.message}" for w in warnings)
    assert unbound or reported
