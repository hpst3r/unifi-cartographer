"""End-to-end tests of collect.collect: real ReadOnlyClient, respx-mocked consoles (tests/fixtures/e2e/)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import respx

from conftest import GENERATED_AT, legacy_key, load_legacy, load_official
from fixtures.e2e.fakenet import (
    BRANCH_DIR,
    BRANCH_HOST,
    BRANCH_LOCAL_TOML,
    COOKIE,
    ENV,
    HOST_IDS,
    HQ_DIR,
    HQ_HOST,
    HQ_KEY,
    HQ_TOML,
    LEGACY_PASSWORD,
    LEGACY_USER,
    LOGIN_PATH,
    SM_HOST,
    SM_KEY,
    SM_TOML,
    FakeNet,
    connector_prefix,
    fake_net,
    legacy_creds_toml,
    password_file,
    requests,
    standard_net,
    write_config,
)
from unifi_carto.build import build_inventory
from unifi_carto.collect import Result, collect
from unifi_carto.config import load_config
from unifi_carto.errors import AuthRefused, ConfigError

Net = tuple[FakeNet, respx.MockRouter]


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def net() -> Iterator[Net]:
    with fake_net() as pair:
        yield pair


def run(tmp_path: Path, toml: str, **kw: object) -> Result:
    return collect(load_config(write_config(tmp_path / "config.toml", toml)), **kw)  # type: ignore[arg-type]


def codes(result: Result) -> dict[str, list[str | None]]:
    out: dict[str, list[str | None]] = {}
    for w in result.inventory.warnings:
        out.setdefault(w.code, []).append(w.console)
    return out


def ported_links(result: Result, console_id: str) -> list[str]:
    sites = {s.id for s in result.inventory.sites if s.console_id == console_id}
    return [lk.id for lk in result.inventory.links if lk.site_id in sites and lk.a.port_idx and lk.b.port_idx]


# -- scenario 1: Site Manager discovery ------------------------------------------------------------------


def test_discovery_local_connector_and_nvr(tmp_path: Path, net: Net) -> None:
    fake, mock = net
    standard_net(fake)
    result = run(tmp_path, SM_TOML + HQ_TOML)

    assert result.exit_code == 0
    consoles = {c.name: c for c in result.inventory.consoles}
    assert sorted(consoles) == ["branch", "hq"]
    assert consoles["hq"].access == "local" and consoles["hq"].id == HOST_IDS["hq"]
    assert consoles["hq"].legacy == "api_key" and consoles["hq"].model == "UDMPRO"
    assert consoles["branch"].access == "connector" and consoles["branch"].legacy == "unavailable"
    assert {s.name for s in result.inventory.sites} == {"Site A", "Site B"}

    found = codes(result)
    assert found["legacy_skipped_connector"] == ["branch"]
    assert found["console_not_network"] == ["nvr"]
    assert "console_failed" not in found and "console_auth_refused" not in found

    assert ported_links(result, HOST_IDS["hq"])
    assert ported_links(result, HOST_IDS["branch"]) == []
    branch_links = [
        lk
        for lk in result.inventory.links
        if lk.site_id in {s.id for s in result.inventory.sites if s.name == "Site B"}
    ]
    assert branch_links and all(lk.source.value == "official" for lk in branch_links)

    # hq goes local only; branch only via the connector with the Site Manager key; no legacy via connector
    assert not requests(mock, host=SM_HOST, contains=connector_prefix("hq"))
    branch_reqs = requests(mock, host=SM_HOST, contains=connector_prefix("branch"))
    assert branch_reqs and all(r.headers["x-api-key"] == SM_KEY for r in branch_reqs)
    assert all(r.headers["x-api-key"] == HQ_KEY for r in requests(mock, host=HQ_HOST))
    assert not requests(mock, host=SM_HOST, contains="/proxy/network/api")
    assert requests(mock, host=SM_HOST, contains=connector_prefix("nvr"))
    assert fake.unmatched == [f"GET {SM_HOST}{connector_prefix('nvr')}/v1/info"]


def test_device_and_link_counts_match_offline_build(tmp_path: Path, net: Net) -> None:
    """The HTTP path yields the same inventory shape as building straight from the fixture files."""

    fake, _ = net
    fake.official(HQ_HOST, "/proxy/network/integration", HQ_DIR, HQ_KEY)
    fake.legacy(HQ_HOST, HQ_DIR, HQ_KEY)
    result = run(tmp_path, HQ_TOML)
    off = load_official(HQ_DIR)
    offline = build_inventory(
        [off],
        {legacy_key(off): load_legacy(HQ_DIR)},
        collector_version="t",
        generated_at=GENERATED_AT,
        include_clients=False,
    )
    assert result.exit_code == 0
    assert len(result.inventory.devices) == len(offline.devices)
    assert sorted(lk.id for lk in result.inventory.links) == sorted(lk.id for lk in offline.links)


# -- scenario 2/3: legacy refuses the API key ------------------------------------------------------------


def test_legacy_login_fallback(tmp_path: Path, net: Net) -> None:
    fake, mock = net
    fake.official(HQ_HOST, "/proxy/network/integration", HQ_DIR, HQ_KEY)
    fake.legacy(HQ_HOST, HQ_DIR, HQ_KEY, mode="login_only")
    toml = HQ_TOML + legacy_creds_toml(password_file(tmp_path))
    result = run(tmp_path, toml)

    assert result.exit_code == 0
    (console,) = result.inventory.consoles
    assert console.legacy == "login"
    assert ported_links(result, console.id)

    posts = [c.request for c in mock.calls if c.request.method == "POST"]
    assert len(posts) == 1
    assert str(posts[0].url) == f"https://{HQ_HOST}{LOGIN_PATH}"  # the origin, not under /proxy/network
    assert b'"username"' in posts[0].content and LEGACY_USER.encode() in posts[0].content

    legacy_gets = requests(mock, host=HQ_HOST, contains="/proxy/network/api/")
    login_at = next(i for i, c in enumerate(mock.calls) if c.request.method == "POST")
    before = [c.request for c in mock.calls[:login_at] if "/proxy/network/api/" in c.request.url.path]
    after = [c.request for c in mock.calls[login_at + 1 :] if "/proxy/network/api/" in c.request.url.path]
    assert [r.url.path for r in before] == ["/proxy/network/api/s/default/stat/device"]
    assert before[0].headers["x-api-key"] == HQ_KEY
    assert len(after) == len(legacy_gets) - 1 >= 4
    for req in after:
        assert "x-api-key" not in req.headers
        assert f"TOKEN={COOKIE}" in req.headers["cookie"]
    # the official client never learns the cookie
    for req in requests(mock, host=HQ_HOST, contains="/integration/"):
        assert "cookie" not in req.headers and req.headers["x-api-key"] == HQ_KEY


def test_legacy_login_refused_by_console(tmp_path: Path, net: Net, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, mock = net
    fake.official(HQ_HOST, "/proxy/network/integration", HQ_DIR, HQ_KEY)
    fake.legacy(HQ_HOST, HQ_DIR, HQ_KEY, mode="login_only")
    monkeypatch.setenv("E2E_LEGACY_USER", "someone-else")
    result = run(tmp_path, HQ_TOML + legacy_creds_toml(password_file(tmp_path)))

    assert result.exit_code == 0
    assert codes(result)["legacy_auth_refused"] == ["hq"]
    assert result.inventory.consoles[0].legacy == "unavailable"
    assert len([c for c in mock.calls if c.request.method == "POST"]) == 1


def test_legacy_refused_without_credentials(tmp_path: Path, net: Net) -> None:
    fake, mock = net
    fake.official(HQ_HOST, "/proxy/network/integration", HQ_DIR, HQ_KEY)
    fake.legacy(HQ_HOST, HQ_DIR, HQ_KEY, mode="login_only")
    result = run(tmp_path, HQ_TOML)

    # SPEC: exit 1 is for a failed console/site; legacy is a supplement that is "skipped with a warning".
    assert result.exit_code == 0
    assert codes(result)["legacy_auth_refused"] == ["hq"]
    (console,) = result.inventory.consoles
    assert console.legacy == "unavailable"
    assert result.inventory.links and ported_links(result, console.id) == []
    assert all(lk.source.value == "official" for lk in result.inventory.links)
    assert not [c for c in mock.calls if c.request.method == "POST"]
    assert [r.url.path for r in requests(mock, contains="/proxy/network/api/")] == [
        "/proxy/network/api/s/default/stat/device"
    ]


def test_legacy_half_configured_credentials_is_config_error(tmp_path: Path, net: Net) -> None:
    fake, _ = net
    fake.official(HQ_HOST, "/proxy/network/integration", HQ_DIR, HQ_KEY)
    fake.legacy(HQ_HOST, HQ_DIR, HQ_KEY, mode="login_only")
    with pytest.raises(ConfigError, match="both"):
        run(tmp_path, HQ_TOML + 'legacy_username_env = "E2E_LEGACY_USER"\n')


# -- scenario 4: --no-legacy -----------------------------------------------------------------------------


def test_no_legacy(tmp_path: Path, net: Net) -> None:
    fake, mock = net
    standard_net(fake)
    result = run(tmp_path, SM_TOML + HQ_TOML, legacy=False)

    assert result.exit_code == 0
    assert not requests(mock, contains="/proxy/network/api/")
    assert not requests(mock, contains=LOGIN_PATH)
    assert {c.legacy for c in result.inventory.consoles} == {"disabled"}
    assert result.legacy == {}
    assert "legacy_skipped_connector" not in codes(result)
    assert all(lk.source.value == "official" for lk in result.inventory.links)


# -- scenario 5: official 401 ----------------------------------------------------------------------------


def test_official_refused_on_one_console_is_partial(tmp_path: Path, net: Net) -> None:
    fake, mock = net
    fake.site_manager()
    fake.official(HQ_HOST, "/proxy/network/integration", HQ_DIR, HQ_KEY, refuse=True)
    fake.legacy(HQ_HOST, HQ_DIR, HQ_KEY)
    fake.official(SM_HOST, connector_prefix("branch"), BRANCH_DIR, SM_KEY)
    result = run(tmp_path, SM_TOML + HQ_TOML)

    assert result.exit_code == 1
    assert [c.name for c in result.inventory.consoles] == ["branch"]
    assert codes(result)["console_auth_refused"] == ["hq"]
    assert result.inventory.devices
    assert not requests(mock, host=HQ_HOST, contains="/proxy/network/api/")  # no legacy after official refusal


def test_official_refused_everywhere_collects_nothing(tmp_path: Path, net: Net) -> None:
    fake, _ = net
    fake.official(HQ_HOST, "/proxy/network/integration", HQ_DIR, HQ_KEY, refuse=True)
    fake.official(BRANCH_HOST, "/proxy/network/integration", BRANCH_DIR, "unused", refuse=True)

    result = run(tmp_path, HQ_TOML + BRANCH_LOCAL_TOML)
    assert result.exit_code == 3
    assert result.inventory.consoles == [] and result.inventory.devices == []
    assert codes(result)["console_auth_refused"] == ["hq", "branch"]


def test_site_manager_key_refused(tmp_path: Path, net: Net, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = net
    fake.site_manager()
    monkeypatch.setenv("E2E_SM_KEY", "wrong-key")
    with pytest.raises(AuthRefused) as info:
        run(tmp_path, SM_TOML)
    assert "wrong-key" not in str(info.value)


# -- scenario 6: filters ---------------------------------------------------------------------------------


def test_console_filter(tmp_path: Path, net: Net) -> None:
    fake, mock = net
    standard_net(fake)
    result = run(tmp_path, SM_TOML + HQ_TOML, console_names=["hq"])

    assert result.exit_code == 0
    assert [c.name for c in result.inventory.consoles] == ["hq"]
    assert not requests(mock, host=SM_HOST, contains="/v1/connector/")
    assert "console_not_network" not in codes(result)


def test_console_filter_by_host_id(tmp_path: Path, net: Net) -> None:
    fake, mock = net
    standard_net(fake)
    result = run(tmp_path, SM_TOML + HQ_TOML, console_names=[HOST_IDS["branch"]])
    assert [c.name for c in result.inventory.consoles] == ["branch"]
    assert not requests(mock, host=HQ_HOST)


def test_site_filter(tmp_path: Path, net: Net) -> None:
    fake, mock = net
    standard_net(fake)
    result = run(tmp_path, SM_TOML + HQ_TOML, site_names=["Site B"])

    assert [s.name for s in result.inventory.sites] == ["Site B"]
    assert [c.name for c in result.inventory.consoles] == ["branch"]
    assert not requests(mock, host=HQ_HOST, contains="/v1/sites/")  # hq lists its sites, collects none
    assert not requests(mock, host=HQ_HOST, contains="/proxy/network/api/")


def test_unknown_console_is_config_error(tmp_path: Path, net: Net) -> None:
    fake, _ = net
    standard_net(fake)
    with pytest.raises(ConfigError, match="nope"):
        run(tmp_path, SM_TOML + HQ_TOML, console_names=["hq", "nope"])


# -- secrets in the model ---------------------------------------------------------------------------------


def test_inventory_never_contains_secrets(tmp_path: Path, net: Net) -> None:
    fake, _ = net
    standard_net(fake, hq_legacy="login_only")
    result = run(tmp_path, SM_TOML + HQ_TOML + legacy_creds_toml(password_file(tmp_path)))
    dumped = result.inventory.model_dump_json()
    for secret in (SM_KEY, HQ_KEY, LEGACY_PASSWORD, COOKIE):
        assert secret not in dumped
    assert result.inventory.consoles[0].legacy == "login" or result.inventory.consoles[1].legacy == "login"
