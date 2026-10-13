from __future__ import annotations

from typing import Any

import pytest

from conftest import SITE_A, load_json
from unifi_carto.errors import ApiError, AuthRefused, EndpointUnavailable
from unifi_carto.legacy import Legacy, LegacyEnvelopeError, unwrap
from unifi_carto.transport import Params

LEGACY = SITE_A / "legacy"
FILES = {
    "/api/s/default/stat/device": "stat_device.json",
    "/api/s/default/rest/portconf": "rest_portconf.json",
    "/api/s/default/rest/networkconf": "rest_networkconf.json",
    "/api/s/default/rest/wlanconf": "rest_wlanconf.json",
    "/api/s/default/stat/sta": "stat_sta.json",
}


class FakeTransport:
    """Serves the synthetic legacy fixtures; ``refuse_key`` makes the API key fail until login."""

    base_url = "https://10.0.0.1/proxy/network"

    def __init__(
        self,
        *,
        refuse_key: bool = False,
        login_ok: bool = True,
        overrides: dict[str, Any] | None = None,
    ) -> None:
        self.refuse_key = refuse_key
        self.login_ok = login_ok
        self.logged_in = False
        self.overrides = overrides or {}
        self.calls: list[str] = []
        self.logins: list[tuple[str, str]] = []

    def get_json(self, path: str, params: Params | None = None) -> Any:
        self.calls.append(path)
        if self.refuse_key and not self.logged_in:
            raise AuthRefused("401", status=401, url=path)
        if path in self.overrides:
            body = self.overrides[path]
            if isinstance(body, Exception):
                raise body
            return body
        if path not in FILES:
            raise EndpointUnavailable("404", status=404, url=path)
        return load_json(LEGACY / FILES[path])

    def get_paginated(self, path: str, params: Params | None = None, *, limit: int = 200) -> list[dict[str, Any]]:
        raise AssertionError("legacy collector must not use the paginated Integration helper")

    def login_legacy(self, username: str, password: str) -> None:
        self.logins.append((username, password))
        if not self.login_ok:
            raise AuthRefused("401", status=401, url="/api/auth/login")
        self.logged_in = True


def test_api_key_success() -> None:
    t = FakeTransport()
    leg = Legacy(t, console="hq")
    snap = leg.collect_site("default", include_clients=False, credentials=("ro", "pw"))
    assert snap is not None
    assert snap.auth == "api_key" and snap.site_name == "default"
    assert len(snap.devices) == 7 and snap.portconfs[0]["name"] == "AP Trunk"
    assert len(snap.networkconfs) == 5 and len(snap.wlanconfs) == 2
    assert snap.clients is None and snap.warnings == [] and leg.last_warnings == []
    assert t.logins == []
    assert "/api/s/default/stat/sta" not in t.calls


def test_401_then_login_success() -> None:
    t = FakeTransport(refuse_key=True)
    leg = Legacy(t, console="hq")
    snap = leg.collect_site("default", include_clients=False, credentials=("ro", "pw"))
    assert snap is not None and snap.auth == "login"
    assert t.logins == [("ro", "pw")]
    assert t.calls.count("/api/s/default/stat/device") == 2
    assert len(snap.devices) == 7


def test_401_without_credentials() -> None:
    t = FakeTransport(refuse_key=True)
    leg = Legacy(t, console="hq")
    assert leg.collect_site("default", include_clients=True, credentials=None) is None
    assert [w.code for w in leg.last_warnings] == ["legacy_auth_refused"]
    assert (leg.last_warnings[0].console, leg.last_warnings[0].site) == ("hq", "default")
    assert t.logins == []


def test_login_refused() -> None:
    t = FakeTransport(refuse_key=True, login_ok=False)
    leg = Legacy(t)
    assert leg.collect_site("default", include_clients=False, credentials=("ro", "bad")) is None
    assert [w.code for w in leg.last_warnings] == ["legacy_auth_refused"]
    assert "bad" not in leg.last_warnings[0].message


def test_envelope_login_required_is_auth_refusal() -> None:
    refused = load_json(LEGACY / "refused.json")
    t = FakeTransport(overrides={"/api/s/default/stat/device": refused})
    leg = Legacy(t)
    assert leg.collect_site("default", include_clients=False, credentials=None) is None
    assert [w.code for w in leg.last_warnings] == ["legacy_auth_refused"]


def test_envelope_rc_error_on_optional_endpoint() -> None:
    err = {"meta": {"rc": "error", "msg": "api.err.ServerBusy"}, "data": []}
    t = FakeTransport(overrides={"/api/s/default/rest/portconf": err})
    snap = Legacy(t).collect_site("default", include_clients=False, credentials=None)
    assert snap is not None and snap.portconfs == []
    assert [w.code for w in snap.warnings] == ["legacy_error"]
    assert len(snap.networkconfs) == 5


def test_envelope_rc_error_on_stat_device() -> None:
    err = {"meta": {"rc": "error", "msg": "api.err.ServerBusy"}, "data": []}
    leg = Legacy(FakeTransport(overrides={"/api/s/default/stat/device": err}))
    assert leg.collect_site("default", include_clients=False, credentials=None) is None
    assert [w.code for w in leg.last_warnings] == ["legacy_error"]


def test_optional_endpoints_unavailable_or_failing() -> None:
    t = FakeTransport(
        overrides={
            "/api/s/default/rest/wlanconf": EndpointUnavailable("404", status=404, url="x"),
            "/api/s/default/rest/networkconf": ApiError("500", status=500, url="x"),
        }
    )
    snap = Legacy(t).collect_site("default", include_clients=False, credentials=None)
    assert snap is not None
    assert [w.code for w in snap.warnings] == ["legacy_error", "endpoint_unavailable"]
    assert snap.wlanconfs == [] and snap.networkconfs == [] and len(snap.portconfs) == 1


def test_clients_only_when_requested() -> None:
    t = FakeTransport()
    snap = Legacy(t).collect_site("default", include_clients=True, credentials=None)
    assert snap is not None and snap.clients is not None and len(snap.clients) == 3
    assert "/api/s/default/stat/sta" in t.calls


def test_bare_list_and_site_name_quoting() -> None:
    t = FakeTransport(overrides={"/api/s/a%2Fb/stat/device": [{"mac": "02:00:00:00:00:01"}, "junk"]})
    snap = Legacy(t).collect_site("a/b", include_clients=False, credentials=None)
    assert snap is not None and snap.devices == [{"mac": "02:00:00:00:00:01"}]


def test_unwrap() -> None:
    assert unwrap({"meta": {"rc": "ok"}, "data": [{"a": 1}]}) == [{"a": 1}]
    assert unwrap([{"a": 1}]) == [{"a": 1}]
    with pytest.raises(AuthRefused):
        unwrap({"meta": {"rc": "error", "msg": "api.err.NoPermission"}})
    with pytest.raises(LegacyEnvelopeError):
        unwrap({"meta": {"rc": "error", "msg": "api.err.Other"}})
    with pytest.raises(LegacyEnvelopeError):
        unwrap("nope")


def test_no_body_logging_at_info(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG", logger="unifi_carto.legacy")
    Legacy(FakeTransport()).collect_site("default", include_clients=True, credentials=None)
    assert all(r.levelname == "DEBUG" for r in caplog.records)
    assert not any("AP Trunk" in r.getMessage() or "10.0.0." in r.getMessage() for r in caplog.records)
