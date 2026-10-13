from __future__ import annotations

import ast
import json
import logging
import re
import ssl
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import respx
import trustme

from unifi_carto.config import TlsConfig
from unifi_carto.errors import (
    ApiError,
    AuthRefused,
    ConfigError,
    EndpointUnavailable,
    ReadOnlyViolation,
    TlsPinMismatch,
)
from unifi_carto.http import ReadOnlyClient, build_ssl_context, fingerprint, format_fingerprint, normalize_fingerprint

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://192.0.2.10/proxy/network/integration"
KEY = "test-api-key-5f2b9c0e"  # synthetic
USER = "carto-readonly"
PASSWORD = "synthetic-pw-7d1e4a"  # synthetic
COOKIE = "synthetic-token-cookie-9a8b7c"  # synthetic


class Sleeps(list[float]):
    def __call__(self, seconds: float) -> None:
        self.append(seconds)


def client(sleeps: Sleeps | None = None, base: str = BASE, **kw: object) -> ReadOnlyClient:
    return ReadOnlyClient(base, api_key=KEY, sleep=sleeps if sleeps is not None else Sleeps(), **kw)  # type: ignore[arg-type]


# -- read-only enforcement ----------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post"])
def test_non_get_methods_refused(method: str) -> None:
    c = client()
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route().respond(200, json={})
        with pytest.raises(ReadOnlyViolation):
            c._send(method, BASE + "/v1/info")
        with pytest.raises(ReadOnlyViolation):
            c._send(method, "https://192.0.2.10/api/auth/login", capability=object())
        assert not route.called


def test_body_refused_on_get() -> None:
    with pytest.raises(ReadOnlyViolation):
        client()._send("GET", BASE + "/v1/info", json_body={"a": 1})


def test_underlying_client_not_exposed() -> None:
    c = client()
    public = {n: getattr(c, n) for n in dir(c) if not n.startswith("_")}
    assert not any(isinstance(v, httpx.Client) for v in public.values())
    for name in ("client", "_client", "post", "put", "patch", "delete", "request"):
        with pytest.raises(AttributeError):
            getattr(c, name)
    with pytest.raises(AttributeError):
        c.extra = 1  # type: ignore[attr-defined]


_SENDING_ATTRS = frozenset({"post", "put", "patch", "delete", "request", "stream", "send"})
_WRITE_VERBS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Second HTTP stacks that would bypass ReadOnlyClient (httpx included: only http.py may build a client).
_NET_MODULES = frozenset({"urllib.request", "requests", "http.client", "aiohttp", "socket", "httpx"})


def _write_risks(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _SENDING_ATTRS:
                found.append((node.lineno, f"call .{func.attr}()"))
            if isinstance(func, ast.Name) and func.id == "getattr" and len(node.args) >= 2:
                name = node.args[1]
                if not isinstance(name, ast.Constant) or not isinstance(name.value, str):
                    found.append((node.lineno, "getattr with a computed attribute name"))
                elif name.value in _SENDING_ATTRS:
                    found.append((node.lineno, f"getattr(..., {name.value!r})"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.upper() in _WRITE_VERBS:
            found.append((node.lineno, f"write verb literal {node.value!r}"))
        elif isinstance(node, ast.Import):
            found.extend((node.lineno, f"import {a.name}") for a in node.names if _is_net_module(a.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
            if any(_is_net_module(m) for m in mods):
                found.append((node.lineno, f"from {node.module} import ..."))
    return found


def _is_net_module(name: str) -> bool:
    return any(name == m or name.startswith(m + ".") for m in _NET_MODULES)


def test_no_write_calls_outside_http() -> None:
    offenders = []
    for path in sorted((ROOT / "src" / "unifi_carto").rglob("*.py")):
        if path.name == "http.py" and path.parent.name == "unifi_carto":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders.extend(f"{path.relative_to(ROOT)}:{line}: {what}" for line, what in _write_risks(tree))
    assert offenders == []


@pytest.mark.parametrize(
    "code",
    [
        "c.post(url)",
        "c.post (url, json={})",
        "c.request(VERB, url)",
        "c.stream('GET', url)",
        "getattr(c, 'po' + 'st')(url)",
        "getattr(c, 'delete')(url)",
        "m = 'patch'",
        "import urllib.request",
        "from urllib import request",
        "from urllib.request import urlopen",
        "import http.client",
        "from http import client",
        "import socket",
        "import requests",
        "import aiohttp",
        "import httpx",
    ],
)
def test_write_scanner_flags(code: str) -> None:
    assert _write_risks(ast.parse(code))


@pytest.mark.parametrize(
    "code",
    [
        "c.get(url)",
        "getattr(t, 'close', None)",
        "from urllib.parse import quote",
        "import http",
        "x = 'GET'",
        "x = 'posting'",
    ],
)
def test_write_scanner_allows(code: str) -> None:
    assert _write_risks(ast.parse(code)) == []


def test_repr_hides_key() -> None:
    assert KEY not in repr(client())


# -- GET and error mapping ------------------------------------------------------------------


@respx.mock
def test_get_json_sends_key_and_agent() -> None:
    route = respx.get(BASE + "/v1/info").respond(200, json={"applicationVersion": "10.6.106"})
    assert client().get_json("/v1/info") == {"applicationVersion": "10.6.106"}
    req = route.calls.last.request
    assert req.headers["X-API-KEY"] == KEY
    assert req.headers["User-Agent"].startswith("unifi-cartographer/")


@respx.mock
def test_custom_key_header() -> None:
    route = respx.get("https://api.ui.com/v1/hosts").respond(200, json={"data": []})
    ReadOnlyClient("https://api.ui.com", api_key=KEY, api_key_header="X-API-Key").get_json("/v1/hosts")
    assert route.calls.last.request.headers["X-API-Key"] == KEY


@pytest.mark.parametrize(
    ("status", "exc"),
    [(401, AuthRefused), (403, AuthRefused), (404, EndpointUnavailable), (400, ApiError), (500, ApiError)],
)
@respx.mock
def test_status_mapping(status: int, exc: type[ApiError]) -> None:
    respx.get(BASE + "/v1/x").respond(status, json={"message": "nope"})
    with pytest.raises(exc) as info:
        client().get_json("/v1/x")
    assert type(info.value) is exc
    assert info.value.status == status
    assert info.value.url == "/proxy/network/integration/v1/x"


@respx.mock
def test_redirect_not_followed() -> None:
    respx.get(BASE + "/v1/x").respond(302, headers={"Location": "https://192.0.2.10/login"})
    with pytest.raises(ApiError, match="redirect not followed") as info:
        client().get_json("/v1/x")
    assert info.value.status == 302


@respx.mock
def test_429_retry_after_seconds() -> None:
    respx.get(BASE + "/v1/x").side_effect = [
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.Response(200, json={"ok": True}),
    ]
    sleeps = Sleeps()
    assert client(sleeps).get_json("/v1/x") == {"ok": True}
    assert sleeps == [2.0]


@respx.mock
def test_429_retry_after_http_date_capped() -> None:
    when = format_datetime(datetime.now(UTC) + timedelta(hours=1), usegmt=True)
    respx.get(BASE + "/v1/x").side_effect = [
        httpx.Response(429, headers={"Retry-After": when}),
        httpx.Response(200, json=[]),
    ]
    sleeps = Sleeps()
    assert client(sleeps).get_json("/v1/x") == []
    assert sleeps == [60.0]


@respx.mock
def test_429_exhausted() -> None:
    respx.get(BASE + "/v1/x").respond(429, headers={"Retry-After": "1"})
    sleeps = Sleeps()
    with pytest.raises(ApiError) as info:
        client(sleeps).get_json("/v1/x")
    assert info.value.status == 429
    assert sleeps == [1.0, 1.0, 1.0]


@respx.mock
def test_503_backoff_then_error() -> None:
    route = respx.get(BASE + "/v1/x").respond(503)
    sleeps = Sleeps()
    with pytest.raises(ApiError) as info:
        client(sleeps).get_json("/v1/x")
    assert info.value.status == 503
    assert sleeps == [0.5, 1.0, 2.0]
    assert route.call_count == 4


@respx.mock
def test_502_recovers() -> None:
    respx.get(BASE + "/v1/x").side_effect = [httpx.Response(502), httpx.Response(200, json={"a": 1})]
    sleeps = Sleeps()
    assert client(sleeps).get_json("/v1/x") == {"a": 1}
    assert sleeps == [0.5]


@respx.mock
def test_transport_error_status_zero() -> None:
    respx.get(BASE + "/v1/x").side_effect = httpx.ConnectError("boom")
    sleeps = Sleeps()
    with pytest.raises(ApiError) as info:
        client(sleeps).get_json("/v1/x")
    assert info.value.status == 0
    assert sleeps == [0.5, 1.0, 2.0]


@respx.mock
def test_non_json_body() -> None:
    respx.get(BASE + "/v1/x").respond(200, text="<html>")
    with pytest.raises(ApiError, match="not JSON"):
        client().get_json("/v1/x")


@pytest.mark.parametrize(
    ("msg", "exc"),
    [("api.err.NoPermission", AuthRefused), ("api.err.LoginRequired", AuthRefused), ("api.err.X", ApiError)],
)
@respx.mock
def test_legacy_envelope_error(msg: str, exc: type[ApiError]) -> None:
    respx.get(BASE + "/api/s/default/stat/device").respond(200, json={"meta": {"rc": "error", "msg": msg}, "data": []})
    with pytest.raises(exc, match=re.escape(msg)) as info:
        client().get_json("/api/s/default/stat/device")
    assert type(info.value) is exc


@respx.mock
def test_legacy_envelope_ok() -> None:
    body = {"meta": {"rc": "ok"}, "data": [{"mac": "02:00:00:00:00:01"}]}
    respx.get(BASE + "/api/s/default/stat/device").respond(200, json=body)
    assert client().get_json("/api/s/default/stat/device") == body


@respx.mock
def test_legacy_envelope_on_http_error() -> None:
    respx.get(BASE + "/api/x").respond(401, json={"meta": {"rc": "error", "msg": "api.err.LoginRequired"}})
    with pytest.raises(AuthRefused, match=r"api\.err\.LoginRequired"):
        client().get_json("/api/x")


# -- pagination -----------------------------------------------------------------------------


def _page(offset: int, limit: int, items: list[dict[str, object]], total: int) -> httpx.Response:
    return httpx.Response(
        200, json={"offset": offset, "limit": limit, "count": len(items), "totalCount": total, "data": items}
    )


@respx.mock
def test_paginated_three_pages() -> None:
    items = [{"id": f"id-{i}"} for i in range(5)]

    def handler(request: httpx.Request) -> httpx.Response:
        off, lim = int(request.url.params["offset"]), int(request.url.params["limit"])
        assert request.url.params["filter"] == "x"
        return _page(off, lim, items[off : off + lim], len(items))

    route = respx.get(BASE + "/v1/sites").mock(side_effect=handler)
    assert client().get_paginated("/v1/sites", {"filter": "x"}, limit=2) == items
    assert route.call_count == 3


@respx.mock
def test_paginated_server_caps_limit() -> None:
    items = [{"id": f"id-{i}"} for i in range(5)]

    def handler(request: httpx.Request) -> httpx.Response:
        off = int(request.url.params["offset"])
        return _page(off, 3, items[off : off + 3], len(items))  # server ignores limit=200, serves 3

    respx.get(BASE + "/v1/sites").mock(side_effect=handler)
    assert client().get_paginated("/v1/sites") == items


@respx.mock
def test_paginated_stops_on_empty_page() -> None:
    respx.get(BASE + "/v1/sites").side_effect = [_page(0, 2, [{"id": "a"}], 10), _page(1, 2, [], 10)]
    assert client().get_paginated("/v1/sites", limit=2) == [{"id": "a"}]


@respx.mock
def test_paginated_offset_ignored() -> None:
    respx.get(BASE + "/v1/sites").mock(return_value=_page(0, 2, [{"id": "a"}, {"id": "b"}], 10))
    with pytest.raises(ApiError, match="ignored offset"):
        client().get_paginated("/v1/sites", limit=2)


@respx.mock
def test_paginated_bad_shape() -> None:
    respx.get(BASE + "/v1/sites").respond(200, json=[1, 2])
    with pytest.raises(ApiError):
        client().get_paginated("/v1/sites")


# -- legacy login ---------------------------------------------------------------------------

LEGACY = "https://192.0.2.10/proxy/network"
LOGIN = "https://192.0.2.10/api/auth/login"


@respx.mock
def test_login_legacy_switches_to_cookie() -> None:
    login = respx.post(LOGIN).respond(
        200,
        json={"username": USER},
        headers={"Set-Cookie": f"TOKEN={COOKIE}; Path=/; Secure; HttpOnly", "X-Updated-CSRF-Token": "csrf-123"},
    )
    data = respx.get(LEGACY + "/api/s/default/stat/device").respond(200, json={"meta": {"rc": "ok"}, "data": []})
    c = client(base=LEGACY)
    c.login_legacy(USER, PASSWORD)
    assert json.loads(login.calls.last.request.content) == {"username": USER, "password": PASSWORD, "rememberMe": False}
    c.get_json("/api/s/default/stat/device")
    req = data.calls.last.request
    assert f"TOKEN={COOKIE}" in req.headers["Cookie"]
    assert "X-API-KEY" not in req.headers
    assert "csrf-123" not in repr(c) and KEY not in repr(c)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, {"errors": ["invalid"]}),
        (403, {}),
        (499, {"code": "MFA_AUTH_REQUIRED", "errors": ["Ubic2faTokenRequired"]}),
        (200, {"errors": ["Ubic2faTokenRequired"]}),
    ],
)
@respx.mock
def test_login_legacy_refused(status: int, body: dict[str, object]) -> None:
    respx.post(LOGIN).respond(status, json=body)
    with pytest.raises(AuthRefused, match="View-Only account without 2FA") as info:
        client(base=LEGACY).login_legacy(USER, PASSWORD)
    assert PASSWORD not in str(info.value) and USER not in str(info.value)


@respx.mock
def test_login_legacy_not_unifi_os() -> None:
    respx.post(LOGIN).respond(302, headers={"Location": "/manage"})
    with pytest.raises(ApiError, match="not a UniFi OS console"):
        client(base=LEGACY).login_legacy(USER, PASSWORD)
    respx.post(LOGIN).respond(404)
    with pytest.raises(EndpointUnavailable):
        client(base=LEGACY).login_legacy(USER, PASSWORD)


@pytest.mark.parametrize("failure", [429, 502, 503, 504, "connect"])
@respx.mock
def test_login_legacy_is_never_retried(failure: int | str) -> None:
    route = respx.post(LOGIN)
    if failure == "connect":
        route.side_effect = httpx.ConnectError("refused")
    else:
        route.respond(failure, headers={"Retry-After": "0"}, json={})
    sleeps = Sleeps()
    with pytest.raises(ApiError) as info:
        client(sleeps, base=LEGACY).login_legacy(USER, PASSWORD)
    assert route.call_count == 1 and sleeps == []
    assert PASSWORD not in str(info.value)


def test_login_capability_checks_the_origin() -> None:
    from unifi_carto.http import _LOGIN_CAP

    c = client(base=LEGACY)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route().respond(200, json={})
        for url in ("https://198.51.100.7/api/auth/login", "http://192.0.2.10/api/auth/login"):
            with pytest.raises(ReadOnlyViolation):
                c._send("POST", url, json_body={"username": USER}, capability=_LOGIN_CAP)
        assert not route.called


@respx.mock
def test_legacy_invalid_envelope_is_auth_refused() -> None:
    respx.get(LEGACY + "/api/s/default/stat/device").respond(
        200, json={"meta": {"rc": "error", "msg": "api.err.Invalid"}, "data": []}
    )
    with pytest.raises(AuthRefused):
        client(base=LEGACY).get_json("/api/s/default/stat/device")


def test_legacy_auth_messages_are_shared() -> None:
    from unifi_carto import http, legacy
    from unifi_carto.errors import LEGACY_AUTH_MESSAGES

    assert {"api.err.LoginRequired", "api.err.NoPermission", "api.err.Invalid", "api.err.Unauthorized"} == set(
        LEGACY_AUTH_MESSAGES
    )
    assert http.LEGACY_AUTH_MESSAGES is legacy.LEGACY_AUTH_MESSAGES is LEGACY_AUTH_MESSAGES


# -- pagination hardening (Amendment 2.7) ------------------------------------------------------


@respx.mock
def test_pagination_page_cap() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"totalCount": 10**9, "data": [{"id": f"d{calls}"}]})

    respx.get(BASE + "/v1/sites").mock(side_effect=handler)
    with pytest.raises(ApiError, match="more than 1000 pages"):
        client().get_paginated("/v1/sites", limit=1)
    assert calls == 1000


@respx.mock
def test_pagination_drops_non_objects_with_one_debug_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="unifi_carto")
    pages = [[{"id": "a"}, None, "x"], [7, {"id": "b"}, []]]

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json={"totalCount": 6, "data": pages[offset // 3]})

    respx.get(BASE + "/v1/sites").mock(side_effect=handler)
    assert client().get_paginated("/v1/sites", limit=3) == [{"id": "a"}, {"id": "b"}]
    dropped = [r for r in caplog.records if "non-object" in r.getMessage()]
    assert len(dropped) == 1 and "4" in dropped[0].getMessage()


@respx.mock
def test_pagination_identical_page_stops_even_with_ids() -> None:
    respx.get(BASE + "/v1/sites").respond(200, json={"totalCount": 10, "data": [{"id": "a"}, {"id": "b"}]})
    with pytest.raises(ApiError, match="repeats the previous one"):
        client().get_paginated("/v1/sites", limit=2)


# -- ca_cert (Amendment 2.8) -------------------------------------------------------------------


def test_ca_cert_problems_are_config_errors(tmp_path: Path) -> None:
    not_pem = tmp_path / "not.pem"
    not_pem.write_text("not a certificate\n")
    unreadable = tmp_path / "unreadable.pem"
    unreadable.write_text("x")
    unreadable.chmod(0)
    for bad in (tmp_path, tmp_path / "missing.pem", not_pem, unreadable):
        with pytest.raises(ConfigError):
            build_ssl_context(TlsConfig(ca_cert=bad), "192.0.2.10", 443)


# -- logging (Amendment 2.1) -------------------------------------------------------------------


@pytest.mark.parametrize("debug", [True, False])
def test_cli_debug_raises_only_own_loggers(debug: bool, capsys: pytest.CaptureFixture[str]) -> None:
    from unifi_carto.cli import main
    from unifi_carto.http import THIRD_PARTY_LOGGERS

    for name in THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)
    try:
        assert main([*(["--debug"] if debug else []), "schema"]) == 0
        capsys.readouterr()
        assert logging.getLogger("unifi_carto").level == (logging.DEBUG if debug else logging.NOTSET)
        for name in (*THIRD_PARTY_LOGGERS, "httpcore.http11", "httpx._client"):
            assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING
    finally:
        logging.getLogger("unifi_carto").setLevel(logging.NOTSET)


@respx.mock
def test_secrets_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    respx.post(LOGIN).respond(
        200, json={}, headers={"Set-Cookie": f"TOKEN={COOKIE}; Path=/", "X-CSRF-Token": "csrf-secret-55"}
    )
    respx.get(LEGACY + "/ok").respond(200, json={"a": 1})
    respx.get(LEGACY + "/denied").respond(403, json={"meta": {"rc": "error", "msg": "api.err.NoPermission"}})
    c = client(base=LEGACY)
    errors: list[str] = []
    try:
        c.get_json("/denied")
    except ApiError as exc:
        errors.append(str(exc) + repr(exc))
    c.login_legacy(USER, PASSWORD)
    c.get_json("/ok")
    try:
        c.get_json("/denied")
    except ApiError as exc:
        errors.append(str(exc) + repr(exc))
    respx.post(LOGIN).respond(401, json={})
    try:
        client(base=LEGACY).login_legacy(USER, PASSWORD)
    except ApiError as exc:
        errors.append(str(exc) + repr(exc))
    haystack = caplog.text + "\n".join(errors) + repr(c)
    assert "GET /proxy/network/ok -> 200" in caplog.text
    assert len(errors) == 3
    for secret in (KEY, PASSWORD, COOKIE, "csrf-secret-55"):
        assert secret not in haystack


# -- TLS ------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tls_server() -> Iterator[tuple[trustme.CA, bytes, int]]:
    ca = trustme.CA()
    leaf = ca.issue_cert("127.0.0.1")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    leaf.configure_cert(ctx)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b'{"applicationVersion": "10.6.106"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.handle_error = lambda *a: None  # fingerprint probes hang up after the handshake
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    der = ssl.PEM_cert_to_DER_cert(leaf.cert_chain_pems[0].bytes().decode())
    try:
        yield ca, der, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_fingerprint_matches_leaf(tls_server: tuple[trustme.CA, bytes, int]) -> None:
    _, der, port = tls_server
    fp = fingerprint(f"https://127.0.0.1:{port}/")
    assert fp == format_fingerprint(der)
    assert re.fullmatch(r"sha256:([0-9A-F]{2}:){31}[0-9A-F]{2}", fp)


def test_pin_ok(tls_server: tuple[trustme.CA, bytes, int]) -> None:
    _, der, port = tls_server
    pin = format_fingerprint(der).lower().replace("sha256:", "")  # case/prefix tolerant
    with ReadOnlyClient(f"https://127.0.0.1:{port}", api_key=None, tls=TlsConfig(fingerprint=pin)) as c:
        assert c.get_json("/v1/info") == {"applicationVersion": "10.6.106"}


def test_pin_context_flags(tls_server: tuple[trustme.CA, bytes, int]) -> None:
    _, der, port = tls_server
    ctx = build_ssl_context(TlsConfig(fingerprint=format_fingerprint(der)), "127.0.0.1", port)
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and not ctx.check_hostname
    assert ctx.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN
    assert not ctx.verify_flags & ssl.VERIFY_X509_STRICT


def test_pin_mismatch(tls_server: tuple[trustme.CA, bytes, int]) -> None:
    _, _, port = tls_server
    with pytest.raises(TlsPinMismatch):
        ReadOnlyClient(
            f"https://127.0.0.1:{port}", api_key=None, tls=TlsConfig(fingerprint="sha256:" + "AB:" * 31 + "AB")
        )


def test_ca_file(tls_server: tuple[trustme.CA, bytes, int], tmp_path: Path) -> None:
    ca, _, port = tls_server
    ca_file = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(str(ca_file))
    with ReadOnlyClient(f"https://127.0.0.1:{port}", api_key=None, tls=TlsConfig(ca_cert=ca_file)) as c:
        assert c.get_json("/v1/info")["applicationVersion"] == "10.6.106"


def test_system_trust_rejects_self_signed(tls_server: tuple[trustme.CA, bytes, int]) -> None:
    _, _, port = tls_server
    sleeps = Sleeps()
    with pytest.raises(ApiError, match="TLS") as info:
        ReadOnlyClient(f"https://127.0.0.1:{port}", api_key=None, sleep=sleeps).get_json("/v1/info")
    assert info.value.status == 0
    assert sleeps == []  # TLS failures are not retried


def test_system_trust_default() -> None:
    assert build_ssl_context(None, "192.0.2.10", 443) is True
    assert build_ssl_context(TlsConfig(), "192.0.2.10", 443) is True


@pytest.mark.parametrize("value", ["sha256:AB:CD", "zz" * 32, ""])
def test_normalize_fingerprint_rejects(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_fingerprint(value)


def test_http_base_url_refused() -> None:
    with pytest.raises(ValueError):
        ReadOnlyClient("http://192.0.2.10", api_key=None)
