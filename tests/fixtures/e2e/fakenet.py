"""A fake UniFi network for end-to-end tests: Site Manager, connector and local consoles behind respx.

Every request the real ``ReadOnlyClient`` sends is routed by (host, path) to a handler that serves the
JSON under ``tests/fixtures/e2e/`` and checks credentials like a console would. Unknown paths answer 404.
All secrets here are synthetic.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

import httpx
import respx

E2E = Path(__file__).resolve().parent
HQ_DIR = E2E / "hq"
BRANCH_DIR = E2E / "branch"
HOSTS = json.loads((E2E / "sitemanager" / "hosts.json").read_text())
HOST_IDS = {h["reportedState"]["name"]: h["id"] for h in HOSTS["data"]}

SM_HOST = "api.ui.com"
HQ_HOST = "10.0.0.1"
HQ_URL = f"https://{HQ_HOST}"
BRANCH_HOST = "10.1.0.1"
BRANCH_URL = f"https://{BRANCH_HOST}"

# Synthetic secrets, distinctive enough to grep for.
SM_KEY = "sm-e2e-key-6c1d0a9f"
HQ_KEY = "hq-e2e-key-3b7f52e1"
BRANCH_KEY = "br-e2e-key-8a04d6c3"
LEGACY_USER = "carto-ro-e2e"
LEGACY_PASSWORD = "legacy-e2e-pw-91ac44b2"
COOKIE = "e2e-session-cookie-77d2f0aa"
SECRETS = (SM_KEY, HQ_KEY, BRANCH_KEY, LEGACY_PASSWORD, COOKIE)

LOGIN_PATH = "/api/auth/login"
LEGACY_REFUSED = {"meta": {"rc": "error", "msg": "api.err.LoginRequired"}, "data": []}

Handler = Callable[[httpx.Request], httpx.Response]
LegacyMode = Literal["api_key", "login_only"]


def load(path: Path) -> Any:
    return json.loads(path.read_text())


def connector_prefix(name: str) -> str:
    return f"/v1/connector/consoles/{HOST_IDS[name]}/network/integration"


class FakeNet:
    """respx side effect: dispatch on (host, path)."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Handler] = {}
        self.unmatched: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        handler = self.routes.get((request.url.host, request.url.path))
        if handler is None:
            self.unmatched.append(f"{request.method} {request.url.host}{request.url.path}")
            return httpx.Response(404, json={"statusCode": 404, "message": "not found"})
        return handler(request)

    # -- Site Manager ------------------------------------------------------------------------

    def site_manager(self, names: tuple[str, ...] = ("hq", "branch", "nvr")) -> None:
        body = {**HOSTS, "data": [h for h in HOSTS["data"] if h["reportedState"]["name"] in names]}

        def hosts(request: httpx.Request) -> httpx.Response:
            if request.headers.get("x-api-key") != SM_KEY:
                return httpx.Response(401, json={"code": "unauthorized", "message": "invalid api key"})
            return httpx.Response(200, json=body)

        self.routes[(SM_HOST, "/v1/hosts")] = hosts

    # -- official Integration API ------------------------------------------------------------

    def official(self, host: str, prefix: str, data_dir: Path, key: str, *, refuse: bool = False) -> None:
        """Serve ``data_dir/official`` under ``https://host{prefix}/v1/...``; ``refuse`` answers 401 everywhere."""
        d = data_dir / "official"
        site = load(d / "site.json")
        base = f"{prefix}/v1/sites/{site['id']}"
        details: dict[str, Any] = load(d / "device_details.json")
        lists = {
            "/devices": load(d / "devices.json"),
            "/networks": load(d / "networks.json"),
            "/wifi/broadcasts": load(d / "wlans.json"),
            "/wans": load(d / "wans.json"),
            "/switching/lags": load(d / "lags.json"),
            "/switching/mc-lag-domains": load(d / "mc_lag_domains.json"),
            "/switching/switch-stacks": load(d / "switch_stacks.json"),
            "/clients": load(d / "clients.json"),
        }

        def authed(make: Callable[[httpx.Request], httpx.Response]) -> Handler:
            def handler(request: httpx.Request) -> httpx.Response:
                if refuse or request.headers.get("x-api-key") != key:
                    return httpx.Response(401, json={"statusCode": 401, "message": "Unauthorized"})
                return make(request)

            return handler

        def obj(body: Any) -> Handler:
            return authed(lambda _r: httpx.Response(200, json=body))

        def paged(items: list[Any]) -> Handler:
            def make(request: httpx.Request) -> httpx.Response:
                offset = int(request.url.params.get("offset", 0))
                limit = int(request.url.params.get("limit", 25))
                data = items[offset : offset + limit]
                body = {"offset": offset, "limit": limit, "count": len(data), "totalCount": len(items), "data": data}
                return httpx.Response(200, json=body)

            return authed(make)

        self.routes[(host, f"{prefix}/v1/info")] = obj(load(d / "info.json"))
        self.routes[(host, f"{prefix}/v1/sites")] = paged([site])
        for suffix, items in lists.items():
            self.routes[(host, base + suffix)] = paged(items)
        for dev_id, detail in details.items():
            self.routes[(host, f"{base}/devices/{dev_id}")] = obj(detail)
        for suffix in ("/networks", "/wifi/broadcasts"):
            for item in lists[suffix]:
                self.routes[(host, f"{base}{suffix}/{item['id']}")] = obj(item)

    # -- legacy ------------------------------------------------------------------------------

    def legacy(self, host: str, data_dir: Path, key: str, *, mode: LegacyMode = "api_key") -> None:
        """Serve ``data_dir/legacy`` for site ``default``.

        ``api_key``: the network API key is accepted. ``login_only``: the key is refused
        (``api.err.LoginRequired``) and only the session cookie from ``POST /api/auth/login`` works.
        """
        d = data_dir / "legacy"
        files = {
            "stat/device": "stat_device.json",
            "stat/sta": "stat_sta.json",
            "rest/portconf": "rest_portconf.json",
            "rest/networkconf": "rest_networkconf.json",
            "rest/wlanconf": "rest_wlanconf.json",
        }

        def serve(body: Any) -> Handler:
            def handler(request: httpx.Request) -> httpx.Response:
                key_ok = mode == "api_key" and request.headers.get("x-api-key") == key
                cookie_ok = f"TOKEN={COOKIE}" in request.headers.get("cookie", "")
                if not (key_ok or cookie_ok):
                    return httpx.Response(401, json=LEGACY_REFUSED)
                return httpx.Response(200, json=body)

            return handler

        for tail, name in files.items():
            self.routes[(host, f"/proxy/network/api/s/default/{tail}")] = serve(load(d / name))

        def login(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content or b"{}")
            if body.get("username") != LEGACY_USER or body.get("password") != LEGACY_PASSWORD:
                return httpx.Response(401, json={"code": "AUTHENTICATION_FAILED_INVALID_CREDENTIALS"})
            return httpx.Response(
                200,
                json={"username": LEGACY_USER},
                headers={"Set-Cookie": f"TOKEN={COOKIE}; Path=/; Secure; HttpOnly", "X-Updated-CSRF-Token": "csrf-e2e"},
            )

        self.routes[(host, LOGIN_PATH)] = login


@contextmanager
def fake_net() -> Iterator[tuple[FakeNet, respx.MockRouter]]:
    """Activate respx with a catch-all route into a fresh ``FakeNet``; check read-only on exit."""
    net = FakeNet()
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        mock.route().mock(side_effect=net)
        yield net, mock
        assert_read_only(mock)


def assert_read_only(mock: respx.MockRouter) -> None:
    """Every request is a GET, except at most one legacy login POST to the console origin."""
    posts = []
    for call in mock.calls:
        req = call.request
        if req.method == "POST" and req.url.path == LOGIN_PATH:
            posts.append(req)
            continue
        assert req.method == "GET", f"non-GET request {req.method} {req.url}"
    assert len(posts) <= 1, f"{len(posts)} login POSTs"


def requests(mock: respx.MockRouter, *, host: str | None = None, contains: str = "") -> list[httpx.Request]:
    return [
        c.request for c in mock.calls if (host is None or c.request.url.host == host) and contains in c.request.url.path
    ]


def write_config(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


SM_TOML = """
[site_manager]
api_key_env = "E2E_SM_KEY"
"""

HQ_TOML = f"""
[[console]]
name = "hq"
local_url = "{HQ_URL}"
api_key_env = "E2E_HQ_KEY"
"""

BRANCH_LOCAL_TOML = f"""
[[console]]
name = "branch"
local_url = "{BRANCH_URL}"
api_key_env = "E2E_BRANCH_KEY"
"""

ENV = {"E2E_SM_KEY": SM_KEY, "E2E_HQ_KEY": HQ_KEY, "E2E_BRANCH_KEY": BRANCH_KEY, "E2E_LEGACY_USER": LEGACY_USER}


def legacy_creds_toml(password_file: Path) -> str:
    return f'legacy_username_env = "E2E_LEGACY_USER"\nlegacy_password_file = "{password_file}"\n'


def password_file(directory: Path) -> Path:
    path = directory / "hq.pass"
    path.write_text(LEGACY_PASSWORD + "\n")
    path.chmod(0o600)
    return path


def standard_net(net: FakeNet, *, hq_legacy: LegacyMode | None = "api_key") -> None:
    """Scenario 1 world: Site Manager with hq (local), branch (connector) and nvr (Protect only)."""
    net.site_manager()
    net.official(HQ_HOST, "/proxy/network/integration", HQ_DIR, HQ_KEY)
    if hq_legacy is not None:
        net.legacy(HQ_HOST, HQ_DIR, HQ_KEY, mode=hq_legacy)
    net.official(SM_HOST, connector_prefix("branch"), BRANCH_DIR, SM_KEY)
    # nvr: nothing registered, so its connector integration path answers 404
