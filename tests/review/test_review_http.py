"""Transport proofs: pagination termination, TLS config errors, the write-call scanner, integration robustness."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest
import respx

from unifi_carto.config import TlsConfig
from unifi_carto.errors import ApiError, ConfigError
from unifi_carto.http import ReadOnlyClient
from unifi_carto.integration import Integration
from unifi_carto.raw import ConsoleRef

ROOT = Path(__file__).resolve().parents[2]
BASE = "https://192.0.2.10/proxy/network/integration"
KEY = "review-api-key-9f8e7d"  # synthetic
SITE = {"id": "6f1a0000-0000-4000-8000-000000000001", "name": "Main", "internalReference": "default"}


def client() -> ReadOnlyClient:
    return ReadOnlyClient(BASE, api_key=KEY, sleep=lambda _s: None)


class Runaway(Exception):
    pass


@respx.mock
def test_rob01_pagination_terminates_when_offset_ignored_and_items_have_no_id() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls > 100:
            raise Runaway(f"{calls} requests for one list")
        return httpx.Response(200, json={"totalCount": 10**6, "data": [{"name": "a"}, {"name": "b"}]})

    respx.get(BASE + "/v1/sites/s1/wans").mock(side_effect=handler)
    with pytest.raises(ApiError):
        client().get_paginated("/v1/sites/s1/wans", limit=2)


@respx.mock
def test_col04_non_object_list_items_do_not_crash_collect_site() -> None:
    dev = {"id": "6f1a0000-0000-4000-8000-000000000101", "macAddress": "02:00:00:00:01:01", "name": "usw-acc-1"}
    base = f"{BASE}/v1/sites/{SITE['id']}"
    respx.get(f"{BASE}/v1/info").respond(200, json={"applicationVersion": "10.6.106"})
    respx.get(f"{base}/devices").respond(200, json={"totalCount": 3, "data": [dev, None, "junk"]})
    respx.get(f"{base}/devices/{dev['id']}").respond(200, json=dev)
    respx.route().respond(200, json={"totalCount": 0, "data": []})
    snap = Integration(client()).collect_site(ConsoleRef(id="a", name="a", access="local"), SITE, include_clients=False)
    assert [d.get("name") for d in snap.devices if isinstance(d, dict)] == ["usw-acc-1"]


def test_tls01_ca_cert_directory_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        ReadOnlyClient(BASE, api_key=KEY, tls=TlsConfig(ca_cert=tmp_path))
    with pytest.raises(ConfigError):
        ReadOnlyClient(BASE, api_key=KEY, tls=TlsConfig(ca_cert=tmp_path / "missing.pem"))


def test_ro01_write_call_scanner_catches_obvious_spellings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = importlib.util.spec_from_file_location("review_scanner_under_test", ROOT / "tests" / "test_http.py")
    assert spec is not None and spec.loader is not None
    scanner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scanner)
    evil_root = tmp_path / "repo"
    pkg = evil_root / "src" / "unifi_carto"
    pkg.mkdir(parents=True)
    samples = {
        "spaced.py": "def f(c, url):\n    return c.post (url, json={})\n",
        "getattr_call.py": "def f(c, url):\n    return getattr(c, 'po' + 'st')(url)\n",
        "verb_variable.py": "VERB = 'DEL' + 'ETE'\n\ndef f(c, url):\n    return c.request(VERB, url)\n",
        "stdlib.py": "import urllib.request\n\ndef f(url):\n    return urllib.request.urlopen(url, data=b'x')\n",
    }
    monkeypatch.setattr(scanner, "ROOT", evil_root)
    missed = []
    for name, code in samples.items():
        for old in pkg.glob("*.py"):
            old.unlink()
        (pkg / name).write_text(code, encoding="utf-8")
        try:
            scanner.test_no_write_calls_outside_http()
        except AssertionError:
            continue
        missed.append(name)
    assert missed == []
