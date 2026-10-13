"""End-to-end tests of the CLI (cli.main): collect, render, capture, pin, exit codes and secret hygiene."""

from __future__ import annotations

import json
import logging
import re
import socket
import ssl
import threading
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import respx
import trustme

from conftest import GENERATED_AT, legacy_key, load_legacy, load_official
from fixtures.e2e.fakenet import (
    BRANCH_DIR,
    ENV,
    HOST_IDS,
    HQ_DIR,
    HQ_HOST,
    HQ_KEY,
    HQ_TOML,
    LEGACY_PASSWORD,
    SECRETS,
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
from unifi_carto.cli import main
from unifi_carto.http import format_fingerprint
from unifi_carto.model import Inventory

Net = tuple[FakeNet, respx.MockRouter]


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def net() -> Iterator[Net]:
    with fake_net() as pair:
        yield pair


def config(tmp_path: Path, toml: str) -> str:
    return str(write_config(tmp_path / "config.toml", toml))


def collect_cli(tmp_path: Path, toml: str, *extra: str) -> tuple[int, Path]:
    out = tmp_path / "inventory.json"
    code = main(["collect", "-c", config(tmp_path, toml), "-o", str(out), *extra])
    return code, out


# -- exit codes (scenarios 5, 6, 7) ------------------------------------------------------------------------


def test_collect_ok_writes_inventory(tmp_path: Path, net: Net, capsys: pytest.CaptureFixture[str]) -> None:
    standard_net(net[0])
    code, out = collect_cli(tmp_path, SM_TOML + HQ_TOML)
    assert code == 0
    inv = Inventory.model_validate_json(out.read_text())
    assert sorted(c.name for c in inv.consoles) == ["branch", "hq"]
    captured = capsys.readouterr()
    assert captured.out.startswith(f"wrote {out}: 2 console(s), 2 site(s)")
    assert "[console_not_network] nvr" in captured.err and "[legacy_skipped_connector] branch" in captured.err


def test_collect_partial_exit_1(tmp_path: Path, net: Net) -> None:
    fake, _ = net
    fake.site_manager()
    fake.official(HQ_HOST, "/proxy/network/integration", HQ_DIR, HQ_KEY, refuse=True)
    fake.official(SM_HOST, connector_prefix("branch"), BRANCH_DIR, SM_KEY)
    code, out = collect_cli(tmp_path, SM_TOML + HQ_TOML)
    assert code == 1
    inv = Inventory.model_validate_json(out.read_text())
    assert [c.name for c in inv.consoles] == ["branch"] and inv.devices
    assert any(w.code == "console_auth_refused" and w.console == "hq" for w in inv.warnings)


def test_collect_nothing_exit_3(tmp_path: Path, net: Net) -> None:
    fake, _ = net
    fake.site_manager()
    fake.official(HQ_HOST, "/proxy/network/integration", HQ_DIR, HQ_KEY, refuse=True)
    fake.official(SM_HOST, connector_prefix("branch"), BRANCH_DIR, SM_KEY, refuse=True)
    code, out = collect_cli(tmp_path, SM_TOML + HQ_TOML)
    assert code == 3
    inv = Inventory.model_validate_json(out.read_text())
    assert inv.consoles == [] and {w.console for w in inv.warnings if w.code == "console_auth_refused"} == {
        "hq",
        "branch",
    }


def test_unknown_console_exit_2(tmp_path: Path, net: Net, capsys: pytest.CaptureFixture[str]) -> None:
    standard_net(net[0])
    code, out = collect_cli(tmp_path, SM_TOML + HQ_TOML, "--console", "hq", "--console", "atlantis")
    assert code == 2
    err = capsys.readouterr().err
    assert "config error" in err and "atlantis" in err
    assert not out.exists()
    assert not requests(net[1], host=HQ_HOST)


def test_console_and_site_flags(tmp_path: Path, net: Net) -> None:
    standard_net(net[0])
    code, out = collect_cli(tmp_path, SM_TOML + HQ_TOML, "--console", "hq", "--console", "branch", "--site", "default")
    assert code == 0
    inv = Inventory.model_validate_json(out.read_text())
    assert sorted(s.name for s in inv.sites) == ["Site A", "Site B"]  # both sites are "default" internally
    assert not requests(net[1], host=SM_HOST, contains=connector_prefix("nvr"))


def test_missing_env_var_exit_2(
    tmp_path: Path, net: Net, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    standard_net(net[0])
    monkeypatch.delenv("E2E_HQ_KEY")
    code, out = collect_cli(tmp_path, SM_TOML + HQ_TOML)
    assert code == 2
    captured = capsys.readouterr()
    assert "E2E_HQ_KEY" in captured.err and "config error" in captured.err
    for secret in SECRETS:
        assert secret not in captured.err and secret not in captured.out
    assert not out.exists()


def test_insecure_password_file_exit_2(tmp_path: Path, net: Net, capsys: pytest.CaptureFixture[str]) -> None:
    fake, _ = net
    fake.official(HQ_HOST, "/proxy/network/integration", HQ_DIR, HQ_KEY)
    fake.legacy(HQ_HOST, HQ_DIR, HQ_KEY, mode="login_only")
    pw = password_file(tmp_path)
    pw.chmod(0o644)
    code, _ = collect_cli(tmp_path, HQ_TOML + legacy_creds_toml(pw))
    assert code == 2
    err = capsys.readouterr().err
    assert "chmod 600" in err and LEGACY_PASSWORD not in err


def test_inline_secret_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _ = collect_cli(
        tmp_path, f'[[console]]\nname = "hq"\nlocal_url = "https://{HQ_HOST}"\napi_key = "{HQ_KEY}"\n'
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "inline secret" in err and HQ_KEY not in err


# -- scenario 9: secrets end to end ----------------------------------------------------------------------


def test_secrets_never_leak_with_debug(
    tmp_path: Path, net: Net, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    standard_net(net[0], hq_legacy="login_only")
    toml = SM_TOML + HQ_TOML + legacy_creds_toml(password_file(tmp_path))
    out = tmp_path / "inventory.json"
    code = main(["--debug", "collect", "-c", config(tmp_path, toml), "-o", str(out)])
    assert code == 0

    inv = Inventory.model_validate_json(out.read_text())
    assert {c.name: c.legacy for c in inv.consoles}["hq"] == "login"  # the cookie path really ran
    assert any(r.levelno == logging.DEBUG for r in caplog.records)  # debug logging was really on
    captured = capsys.readouterr()
    logs = caplog.text + "\n".join(str(r.args) for r in caplog.records)
    for secret in SECRETS:
        for where, text in (("stdout", captured.out), ("stderr", captured.err), ("logs", logs)):
            assert secret not in text, f"secret leaked to {where}"
        assert secret not in out.read_text()
    assert "csrf-e2e" not in logs


# -- scenario 10: capture --------------------------------------------------------------------------------


def _fixture_strings() -> dict[str, set[str]]:
    """Identifying values from the e2e fixtures that must not survive scrubbing."""
    found: dict[str, set[str]] = {"mac": set(), "name": set(), "ip": set(), "ssid": set(), "site": set()}
    for base in (HQ_DIR, BRANCH_DIR):
        texts = [p.read_text() for p in sorted(base.rglob("*.json")) if p.name not in ("clients.json", "stat_sta.json")]
        for text in texts:
            found["mac"].update(re.findall(r"02:00:00:0[01]:[0-9a-f]{2}:[0-9a-f]{2}", text))
            found["ip"].update(re.findall(r"(?<![\d.])10\.[01]\.\d{1,3}\.\d{1,3}(?![\d.])", text))
        for dev in json.loads((base / "official" / "devices.json").read_text()):
            found["name"].add(dev["name"])
        for wlan in json.loads((base / "official" / "wlans.json").read_text()):
            found["ssid"].add(wlan["name"])
        found["site"].add(json.loads((base / "official" / "site.json").read_text())["name"])
    found["name"].update({"usw-lab", "br-usw-lab"})  # legacy-only device
    return found


def test_capture_scrubs_and_round_trips(tmp_path: Path, net: Net, capsys: pytest.CaptureFixture[str]) -> None:
    standard_net(net[0])
    toml = SM_TOML + HQ_TOML
    code, inv_path = collect_cli(tmp_path, toml)
    assert code == 0
    unscrubbed = Inventory.model_validate_json(inv_path.read_text())

    out = tmp_path / "captured"
    code = main(["capture", "-c", config(tmp_path, toml), "-o", str(out), "--salt", "e2e-salt"])
    assert code == 0
    assert "review them before committing" in capsys.readouterr().out

    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.json"))
    assert {f.split("/")[0] for f in files} == {"console-1", "console-2"}
    site_dirs = sorted({"/".join(f.split("/")[:2]) for f in files})
    assert len(site_dirs) == 2
    legacy_dirs = sorted({"/".join(f.split("/")[:2]) for f in files if "/legacy/" in f})
    assert len(legacy_dirs) == 1  # only hq has a local URL
    for d in site_dirs:
        assert (out / d / "official" / "device_details.json").is_file()
    assert (out / legacy_dirs[0] / "legacy" / "stat_device.json").is_file()
    assert not list(out.rglob("clients.json")) and not list(out.rglob("stat_sta.json"))

    tree = "\n".join(p.read_text() for p in sorted(out.rglob("*")) if p.is_file())
    for kind, values in _fixture_strings().items():
        assert values, kind
        for value in values:
            if kind == "ip":
                assert not re.search(rf"(?<![\d.]){re.escape(value)}(?![\d.])", tree), f"{kind} {value} survived"
            else:
                assert value not in tree, f"{kind} {value} survived"
    for secret in SECRETS:
        assert secret not in tree
    for host_id in HOST_IDS.values():  # hardware-derived Site Manager ids
        assert host_id not in tree
    assert '"hq"' not in tree and '"branch"' not in tree  # console names
    for path in out.rglob("*"):
        assert "hq" not in path.name and "branch" not in path.name

    # the scrubbed fixtures load through the conftest loaders and keep every join
    officials = [load_official(out / d) for d in site_dirs]
    legacies = {
        legacy_key(o): load_legacy(out / d) for o, d in zip(officials, site_dirs, strict=True) if d in legacy_dirs
    }
    rebuilt = build_inventory(
        officials, legacies, collector_version="t", generated_at=GENERATED_AT, include_clients=False
    )
    assert len(rebuilt.devices) == len(unscrubbed.devices)
    assert len(rebuilt.links) == len(unscrubbed.links)
    ported = sum(1 for lk in rebuilt.links if lk.a.port_idx and lk.b.port_idx)
    assert ported == sum(1 for lk in unscrubbed.links if lk.a.port_idx and lk.b.port_idx) > 0
    assert len(rebuilt.lags) == len(unscrubbed.lags)
    assert sorted(lk.source for lk in rebuilt.links) == sorted(lk.source for lk in unscrubbed.links)


def test_capture_is_deterministic_for_a_salt(tmp_path: Path, net: Net) -> None:
    standard_net(net[0])
    cfg = config(tmp_path, SM_TOML + HQ_TOML)
    trees: list[dict[str, Any]] = []
    for n in (1, 2):
        out = tmp_path / f"cap{n}"
        assert main(["capture", "-c", cfg, "-o", str(out), "--salt", "fixed"]) == 0
        trees.append({p.relative_to(out).as_posix(): p.read_text() for p in out.rglob("*.json")})
    assert trees[0] == trees[1]


# -- scenario 11: collect -> render ----------------------------------------------------------------------


def test_collect_then_render(tmp_path: Path, net: Net) -> None:
    standard_net(net[0])
    code, inv_path = collect_cli(tmp_path, SM_TOML + HQ_TOML)
    assert code == 0
    drawio = tmp_path / "out.drawio"
    assert main(["render", str(inv_path), "-o", str(drawio)]) == 0

    root = ET.fromstring(drawio.read_text())
    assert root.tag == "mxfile"
    names = [d.get("name") or "" for d in root.iter("diagram")]
    for site in ("Site A", "Site B"):
        for label in ("Logical", "Cable schedule", "Inventory"):
            assert names.count(f"{site} - {label}") == 1, (site, label, names)
        cables = [n for n in names if re.fullmatch(rf"{site} - Cables( \(\d+/\d+\))?", n)]
        assert len(cables) >= 1
    assert len(names) == len(set(names))
    for d in root.iter("diagram"):
        assert d.find("mxGraphModel") is not None


def test_render_page_selection_and_bad_page(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    inv = Path(__file__).parent / "fixtures" / "render" / "site-a.json"
    drawio = tmp_path / "out.drawio"
    assert main(["render", str(inv), "-o", str(drawio), "--pages", "logical,rack-inventory"]) == 0
    names = [d.get("name") or "" for d in ET.fromstring(drawio.read_text()).iter("diagram")]
    assert len(names) == 2 and names[0].endswith(" - Logical") and names[1].endswith(" - Inventory")
    assert main(["render", str(inv), "-o", str(drawio), "--pages", "logical,bogus"]) == 2
    assert "bogus" in capsys.readouterr().err


def test_render_bad_input_is_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    drawio = tmp_path / "out.drawio"
    assert main(["render", str(tmp_path / "missing.json"), "-o", str(drawio)]) == 2
    assert "cannot read inventory" in capsys.readouterr().err
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema_version": "1"}')
    assert main(["render", str(bad), "-o", str(drawio)]) == 2
    assert "not a valid inventory" in capsys.readouterr().err
    assert not drawio.exists()


# -- scenario 12: pin ------------------------------------------------------------------------------------


@pytest.fixture
def tls_server() -> Iterator[tuple[bytes, int]]:
    ca = trustme.CA()
    leaf = ca.issue_cert("127.0.0.1")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    leaf.configure_cert(ctx)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.handle_error = lambda *a: None  # type: ignore[method-assign]  # pin hangs up after the handshake
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    der = ssl.PEM_cert_to_DER_cert(leaf.cert_chain_pems[0].bytes().decode())
    try:
        yield der, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_pin_prints_leaf_fingerprint(tls_server: tuple[bytes, int], capsys: pytest.CaptureFixture[str]) -> None:
    der, port = tls_server
    assert main(["pin", f"https://127.0.0.1:{port}/"]) == 0
    printed = capsys.readouterr().out.strip()
    assert re.fullmatch(r"sha256:[0-9A-F]{2}(:[0-9A-F]{2}){31}", printed)
    assert printed == format_fingerprint(der)


def test_pin_unreachable_is_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert main(["pin", f"https://127.0.0.1:{port}/"]) == 3  # nothing listens: a message, not a traceback
    assert "cannot fetch TLS certificate" in capsys.readouterr().err


def test_pin_without_host_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["pin", "https:///"]) == 2
    assert "no host" in capsys.readouterr().err


def test_schema_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert "properties" in schema and "devices" in schema["properties"]
