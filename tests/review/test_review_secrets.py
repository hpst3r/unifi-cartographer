"""Secret leakage proofs: debug logs, the inventory model, in-memory reprs."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler

import pytest

from review.conftest import LocalTls, send
from unifi_carto.build import build_inventory
from unifi_carto.collect import Target
from unifi_carto.config import TlsConfig
from unifi_carto.http import ReadOnlyClient
from unifi_carto.raw import ConsoleRef, OfficialSnapshot

KEY = "review-api-key-0c1d2e"  # synthetic
COOKIE = "review-token-cookie-4f5a6b"  # synthetic
CSRF = "review-csrf-7c8d9e"  # synthetic
PPPOE_PASSWORD = "review-pppoe-pw-1a2b"  # synthetic
RADIUS_SECRET = "review-radius-secret-3c4d"  # synthetic


def test_sec01_debug_log_has_no_session_cookie_or_csrf(local_tls: LocalTls, caplog: pytest.LogCaptureFixture) -> None:

    def login(h: BaseHTTPRequestHandler) -> None:
        send(h, 200, b"{}", {"Set-Cookie": f"TOKEN={COOKIE}; Path=/; Secure; HttpOnly", "X-CSRF-Token": CSRF})

    local_tls.on_post = login
    local_tls.on_get = lambda h: send(h, 200, b'{"meta": {"rc": "ok"}, "data": []}')
    caplog.set_level(logging.DEBUG)  # what `unifi-carto --debug` configures on the root logger
    with ReadOnlyClient(
        f"{local_tls.base}/proxy/network", api_key=KEY, tls=TlsConfig(ca_cert=local_tls.ca_file)
    ) as client:
        client.get_json("/api/s/default/stat/device")
        client.login_legacy("carto-ro", "review-password-5e6f")
        client.get_json("/api/s/default/stat/device")
    assert "/api/s/default/stat/device" in caplog.text  # debug logging was active
    for secret in (COOKIE, CSRF, KEY):
        assert secret not in caplog.text


def test_sec02_wan_raw_does_not_carry_credentials() -> None:
    snap = OfficialSnapshot(
        console=ConsoleRef(id="c1", name="hq", access="local"),
        site={"id": "s1", "name": "Main", "internalReference": "default"},
        app_version="10.6.106",
        wans=[
            {
                "id": "wan-1",
                "name": "Internet 1",
                "type": "PPPOE",
                "pppoe": {"username": "carto@isp.example", "password": PPPOE_PASSWORD},
                "radius": {"sharedSecret": RADIUS_SECRET},
            }
        ],
    )
    inv = build_inventory(
        [snap], {}, collector_version="0", generated_at=datetime(2026, 1, 1, tzinfo=UTC), include_clients=False
    )
    assert [w.id for w in inv.wans] == ["wan-1"]
    dumped = inv.model_dump_json()
    assert PPPOE_PASSWORD not in dumped
    assert RADIUS_SECRET not in dumped


def test_sec03_target_repr_hides_api_key() -> None:
    ref = ConsoleRef(id="hq", name="hq", access="local", local_url="https://192.0.2.10")
    target = Target(ref, None, "https://192.0.2.10/proxy/network/integration", KEY, "X-API-KEY", None)
    assert KEY not in repr(target)
    assert KEY not in str(target)
