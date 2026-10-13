"""Fix wave 1 (SPEC Amendment 2): per-console isolation, console identity, config validation, id encoding."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unifi_carto import collect as collect_mod
from unifi_carto.cli import main
from unifi_carto.collect import collect, plan_targets
from unifi_carto.config import Config, ConsoleConfig, SecretRef, TlsConfig, load_config
from unifi_carto.errors import ApiError, ConfigError, EndpointUnavailable
from unifi_carto.integration import Integration
from unifi_carto.model import CollectionWarning
from unifi_carto.raw import ConsoleRef
from unifi_carto.sitemanager import BASE_URL as SM_URL

SITE = {"id": "6f1a0000-0000-4000-8000-000000000001", "name": "Main", "internalReference": "default"}
DEVICE = {"id": "6f1a0000-0000-4000-8000-000000000101", "macAddress": "02:00:00:00:01:01", "name": "usw-acc-1"}


class Fake:
    """Transport fake: path -> JSON; records every path requested."""

    def __init__(self, base_url: str, routes: dict[str, Any]) -> None:
        self.base_url = base_url
        self.routes = routes
        self.paths: list[str] = []

    def get_json(self, path: str, params: Any = None) -> Any:
        self.paths.append(path)
        if path not in self.routes:
            raise EndpointUnavailable(f"GET {path}: HTTP 404", status=404, url=path)
        return self.routes[path]

    def get_paginated(self, path: str, params: Any = None, *, limit: int = 200) -> list[dict[str, Any]]:
        self.paths.append(path)
        data: list[dict[str, Any]] = self.routes.get(path, [])
        return data

    def login_legacy(self, username: str, password: str) -> None:
        pass


def official_routes() -> dict[str, Any]:
    base = f"/v1/sites/{SITE['id']}"
    return {
        "/v1/info": {"applicationVersion": "10.6.106"},
        "/v1/sites": [SITE],
        f"{base}/devices": [DEVICE],
        f"{base}/devices/{DEVICE['id']}": DEVICE,
    }


def hosts(*pairs: tuple[str, str]) -> dict[str, Any]:
    return {"/v1/hosts": {"data": [{"id": hid, "reportedState": {"name": name}} for hid, name in pairs]}}


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("A", "B", "SM"):
        monkeypatch.setenv(f"HARDEN_KEY_{name}", f"synthetic-key-{name.lower()}")


def local(name: str, ip: str, **kw: Any) -> ConsoleConfig:
    return ConsoleConfig(
        name=name, local_url=f"https://{ip}", api_key=SecretRef(env=f"HARDEN_KEY_{name.upper()}"), **kw
    )


def no_network(*a: object, **kw: object) -> Fake:
    raise AssertionError("no network expected")


def sm_factory(routes: dict[str, Any]) -> Any:
    def factory(base_url: str, **kw: Any) -> Fake:
        return Fake(base_url, routes if base_url == SM_URL else official_routes())

    return factory


# -- console identity (Amendment 2.4) -------------------------------------------------------------


def test_duplicate_host_id_is_a_config_error() -> None:
    cfg = Config(consoles=(local("a", "192.0.2.10", host_id="H1"), local("b", "192.0.2.20", host_id="H1")))
    with pytest.raises(ConfigError, match="host_id"):
        plan_targets(cfg, client_factory=no_network, warnings=[])


def test_bad_fingerprint_is_a_config_error_before_network() -> None:
    cfg = Config(consoles=(local("a", "192.0.2.10", tls=TlsConfig(fingerprint="sha256:AB:CD")),))
    with pytest.raises(ConfigError, match="console a"):
        collect(cfg, client_factory=no_network)


def test_host_id_binding_never_falls_back_to_name() -> None:
    cfg = Config(
        site_manager_key=SecretRef(env="HARDEN_KEY_SM"), consoles=(local("a", "192.0.2.10", host_id="HOST-GONE"),)
    )
    targets = plan_targets(cfg, client_factory=sm_factory(hosts(("HOST-A", "a"))), warnings=[])
    assert sorted((t.ref.id, t.ref.access) for t in targets) == [("HOST-A", "connector"), ("a", "local")]


def test_ambiguous_name_binds_nothing_and_collects_every_host() -> None:
    cfg = Config(site_manager_key=SecretRef(env="HARDEN_KEY_SM"), consoles=(local("a", "192.0.2.10"),))
    warnings: list[CollectionWarning] = []
    targets = plan_targets(cfg, client_factory=sm_factory(hosts(("H1", "a"), ("H2", "a"))), warnings=warnings)
    assert sorted((t.ref.id, t.ref.access) for t in targets) == [
        ("H1", "connector"),
        ("H2", "connector"),
        ("a", "local"),
    ]
    assert [(w.code, w.console) for w in warnings] == [("console_ambiguous", "a")]


def test_name_match_on_a_host_claimed_by_host_id_is_ambiguous() -> None:
    cfg = Config(
        site_manager_key=SecretRef(env="HARDEN_KEY_SM"),
        consoles=(local("a", "192.0.2.10", host_id="H1"), ConsoleConfig(name="b")),
    )
    warnings: list[CollectionWarning] = []
    targets = plan_targets(cfg, client_factory=sm_factory(hosts(("H1", "b"))), warnings=warnings)
    assert [(t.ref.id, t.ref.name, t.ref.access) for t in targets] == [("H1", "a", "local")]
    assert [(w.code, w.console) for w in warnings] == [("console_ambiguous", "b")]


def test_unique_name_still_binds() -> None:
    cfg = Config(site_manager_key=SecretRef(env="HARDEN_KEY_SM"), consoles=(local("a", "192.0.2.10"),))
    targets = plan_targets(cfg, client_factory=sm_factory(hosts(("H1", "a"))), warnings=[])
    assert [(t.ref.id, t.ref.access) for t in targets] == [("H1", "local")]


# -- per-console isolation (Amendment 2.2) ----------------------------------------------------------


def test_legacy_failure_is_a_warning_and_partial() -> None:
    cfg = Config(consoles=(local("a", "192.0.2.10"), local("b", "192.0.2.20")))

    def factory(base_url: str, **kw: Any) -> Fake:
        if base_url == "https://192.0.2.10/proxy/network":
            raise ApiError("cannot fetch TLS certificate from 192.0.2.10:443: timed out", status=0, url="/")
        if base_url.endswith("/proxy/network"):
            return Fake(base_url, {"/api/s/default/stat/device": {"meta": {"rc": "ok"}, "data": []}})
        return Fake(base_url, official_routes())

    result = collect(cfg, client_factory=factory)
    assert result.exit_code == 1
    assert sorted(c.name for c in result.inventory.consoles) == ["a", "b"]
    assert [w.console for w in result.inventory.warnings if w.code == "legacy_error"] == ["a"]
    assert list(result.legacy) == [("b", str(SITE["id"]))]


def test_legacy_secret_error_is_fatal_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config(consoles=(local("a", "192.0.2.10", legacy_username=SecretRef(env="HARDEN_MISSING_USER")),))
    monkeypatch.delenv("HARDEN_MISSING_USER", raising=False)
    with pytest.raises(ConfigError, match="HARDEN_MISSING_USER"):
        collect(cfg, client_factory=no_network)


def test_no_legacy_does_not_resolve_legacy_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config(consoles=(local("a", "192.0.2.10", legacy_username=SecretRef(env="HARDEN_MISSING_USER")),))
    monkeypatch.delenv("HARDEN_MISSING_USER", raising=False)
    result = collect(cfg, legacy=False, client_factory=lambda base_url, **kw: Fake(base_url, official_routes()))
    assert result.exit_code == 0


# -- config (Amendment 2.8) -------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["missing", "directory", "not_pem"])
def test_ca_cert_checked_at_load(tmp_path: Path, kind: str) -> None:
    ca = {"missing": tmp_path / "nope.pem", "directory": tmp_path, "not_pem": tmp_path / "bad.pem"}[kind]
    if kind == "not_pem":
        ca.write_text("garbage\n")
    toml = tmp_path / "config.toml"
    toml.write_text(f'[[console]]\nname = "a"\nlocal_url = "https://192.0.2.10"\nca_cert = "{ca}"\n')
    with pytest.raises(ConfigError, match="console a: ca_cert"):
        load_config(toml)


# -- ids in URL paths (Amendment 2.11) -------------------------------------------------------------


def test_server_supplied_ids_are_percent_encoded() -> None:
    site = {"id": "s/../x", "name": "Main"}
    dev = {"id": "d?1#x", "macAddress": "02:00:00:00:01:01"}
    net = {"id": "..", "name": "LAN"}
    routes: dict[str, Any] = {
        "/v1/info": {},
        "/v1/sites/s%2F..%2Fx/devices": [dev],
        "/v1/sites/s%2F..%2Fx/networks": [net],
    }
    fake = Fake("https://192.0.2.10/proxy/network/integration", routes)
    Integration(fake).collect_site(ConsoleRef(id="a", name="a", access="local"), site, include_clients=False)
    assert "/v1/sites/s%2F..%2Fx/devices/d%3F1%23x" in fake.paths
    assert "/v1/sites/s%2F..%2Fx/networks/%2E%2E" in fake.paths
    assert all(p.startswith("/v1/sites/s%2F..%2Fx") or p == "/v1/info" for p in fake.paths)


# -- collect -o (Amendment 2.17) ---------------------------------------------------------------------


def test_collect_into_missing_directory_fails_before_collecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*a: object, **kw: object) -> None:
        raise AssertionError("collect must not run")

    monkeypatch.setattr(collect_mod, "collect", boom)
    out = tmp_path / "missing" / "inventory.json"
    assert main(["collect", "-o", str(out)]) == 2
    assert "does not exist" in capsys.readouterr().err
    assert not out.parent.exists()
