from __future__ import annotations

import threading
from typing import Any

import httpx
import pytest
import respx

from unifi_carto.errors import ApiError, AuthRefused, EndpointUnavailable
from unifi_carto.http import ReadOnlyClient
from unifi_carto.integration import Integration
from unifi_carto.raw import ConsoleRef
from unifi_carto.transport import Params

BASE = "https://192.0.2.1/proxy/network/integration"
SITE_ID = "88f7af54-98f8-306a-a1c7-c9349722b1f6"
SITE = {"id": SITE_ID, "internalReference": "default", "name": "Default"}
S = f"{BASE}/v1/sites/{SITE_ID}"
CONSOLE = ConsoleRef(id="console-a", name="console-a", access="local", local_url="https://192.0.2.1")

GW, AGG, ACC1, ACC2, AP1, AP2 = (f"00000000-0000-4000-8000-00000000000{i}" for i in range(1, 7))
LAG_ID = "00000000-0000-4000-8000-0000000000aa"
NET_ID = "00000000-0000-4000-8000-0000000000b1"
WLAN_ID = "00000000-0000-4000-8000-0000000000c1"


def device(dev_id: str, n: int, name: str, model: str, features: list[str], interfaces: list[str]) -> dict[str, Any]:
    return {
        "id": dev_id,
        "macAddress": f"02:00:00:00:00:0{n}",
        "ipAddress": f"192.0.2.{n}",
        "name": name,
        "model": model,
        "state": "ONLINE",
        "supported": True,
        "firmwareUpdatable": False,
        "firmwareVersion": "7.1.66",
        "features": features,
        "interfaces": interfaces,
    }


DEVICES = [
    device(GW, 1, "ucg-a", "UCG Ultra", ["gateway", "switching"], ["ports"]),
    device(AGG, 2, "usw-agg-a", "USW Aggregation", ["switching"], ["ports"]),
    device(ACC1, 3, "usw-acc-a", "USW Pro 24 PoE", ["switching"], ["ports"]),
    device(ACC2, 4, "usw-acc-b", "USW Pro 24 PoE", ["switching"], ["ports"]),
    device(AP1, 5, "uap-a", "U7 Pro", ["accessPoint"], ["radios"]),
    device(AP2, 6, "uap-b", "U7 Pro", ["accessPoint"], ["radios"]),
]
PARENT = {AGG: GW, ACC1: AGG, ACC2: AGG, AP1: ACC1, AP2: ACC2}


def detail(item: dict[str, Any]) -> dict[str, Any]:
    out = dict(item)
    out["configurationId"] = "cfg-1"
    feats: dict[str, Any] = {f: {} for f in item["features"]}
    if "switching" in feats:
        feats["switching"] = {"lags": []}
    if item["id"] == AGG:
        feats["switching"] = {"lags": [{"id": LAG_ID, "metadata": {"origin": "USER_DEFINED"}, "portIdxs": [7, 8]}]}
    out["features"] = feats
    ifaces: dict[str, Any] = {}
    if "ports" in item["interfaces"]:
        ifaces["ports"] = [
            {"idx": 1, "connector": "SFPPLUS", "state": "UP", "maxSpeedMbps": 10000, "speedMbps": 10000},
            {"idx": 2, "connector": "RJ45", "state": "DOWN", "maxSpeedMbps": 1000, "speedMbps": 0},
        ]
    if "radios" in item["interfaces"]:
        ifaces["radios"] = [{"frequencyGHz": 5, "channel": 36, "channelWidthMHz": 80, "wlanStandard": "802.11be"}]
    out["interfaces"] = ifaces
    if item["id"] in PARENT:
        out["uplink"] = {"deviceId": PARENT[item["id"]]}
    return out


NETWORK = {
    "id": NET_ID,
    "name": "lan-mgmt",
    "vlanId": 10,
    "enabled": True,
    "default": False,
    "management": "GATEWAY",
    "metadata": {"origin": "USER_DEFINED"},
}
WLAN = {
    "id": WLAN_ID,
    "name": "corp-wifi",
    "type": "STANDARD",
    "enabled": True,
    "metadata": {"origin": "USER_DEFINED"},
    "network": {"type": "SPECIFIC", "networkId": NET_ID},
    "securityConfiguration": {"type": "WPA2_WPA3_PERSONAL"},
}
LAG = {"id": LAG_ID, "type": "LOCAL", "members": [{"deviceId": AGG, "portIdxs": [7, 8]}]}


def paged(items: list[dict[str, Any]], page_cap: int = 200) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        off = int(request.url.params["offset"])
        lim = min(int(request.url.params["limit"]), page_cap)  # servers may cap the page size
        chunk = items[off : off + lim]
        return httpx.Response(
            200, json={"offset": off, "limit": lim, "count": len(chunk), "totalCount": len(items), "data": chunk}
        )

    return handler


def mock_site(mock: respx.MockRouter, *, devices: list[dict[str, Any]] = DEVICES) -> None:
    mock.get(f"{BASE}/v1/info").respond(200, json={"applicationVersion": "10.6.106"})
    mock.get(f"{S}/devices").mock(side_effect=paged(devices))
    for d in devices:
        mock.get(f"{S}/devices/{d['id']}").respond(200, json=detail(d))
    mock.get(f"{S}/networks").mock(side_effect=paged([NETWORK]))
    mock.get(f"{S}/networks/{NET_ID}").respond(200, json={**NETWORK, "dhcpGuarding": None})
    mock.get(f"{S}/wifi/broadcasts").mock(side_effect=paged([WLAN]))
    mock.get(f"{S}/wans").mock(side_effect=paged([{"id": "wan-1", "name": "Internet 1"}]))
    mock.get(f"{S}/switching/lags").mock(side_effect=paged([LAG]))
    mock.get(f"{S}/switching/mc-lag-domains").mock(side_effect=paged([]))
    mock.get(f"{S}/switching/switch-stacks").mock(side_effect=paged([]))


def integration() -> Integration:
    return Integration(ReadOnlyClient(BASE, api_key="int-test-key", sleep=lambda _s: None))


def test_full_site() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_site(mock)
        clients = mock.get(f"{S}/clients").mock(side_effect=paged([]))
        wlan_detail = mock.get(f"{S}/wifi/broadcasts/{WLAN_ID}").respond(200, json=WLAN)
        snap = integration().collect_site(CONSOLE, SITE, include_clients=False)
    assert snap.console == CONSOLE and snap.site == SITE
    assert snap.app_version == "10.6.106"
    assert snap.devices == DEVICES
    assert list(snap.device_details) == [d["id"] for d in DEVICES]  # deterministic order
    assert snap.device_details[AP1]["uplink"] == {"deviceId": ACC1}
    assert snap.device_details[AGG]["features"]["switching"]["lags"][0]["portIdxs"] == [7, 8]
    assert snap.networks == [{**NETWORK, "dhcpGuarding": None}]  # detail replaces list item
    assert snap.wlans == [WLAN]  # list items already carry network + securityConfiguration
    assert not wlan_detail.called
    assert snap.wans == [{"id": "wan-1", "name": "Internet 1"}]
    assert snap.lags == [LAG]
    assert snap.mc_lag_domains == [] and snap.switch_stacks == []
    assert snap.clients is None and not clients.called
    assert snap.warnings == []


def test_pagination_across_three_pages() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_site(mock)
        route = mock.get(f"{S}/devices", name="devices").mock(side_effect=paged(DEVICES, page_cap=2))
        snap = integration().collect_site(CONSOLE, SITE, include_clients=False)
    assert snap.devices == DEVICES
    assert route.call_count == 3
    assert [c.request.url.params["offset"] for c in route.calls] == ["0", "2", "4"]
    assert route.calls[0].request.url.params["limit"] == "200"


def test_mc_lag_404_is_warning() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_site(mock)
        mock.get(f"{S}/switching/mc-lag-domains").respond(404, json={"message": "Not Found"})
        snap = integration().collect_site(CONSOLE, SITE, include_clients=False)
    assert snap.mc_lag_domains == []
    assert [(w.code, w.console, w.site) for w in snap.warnings] == [("endpoint_unavailable", "console-a", "Default")]
    assert "mc-lag" in snap.warnings[0].message
    assert snap.lags == [LAG]


def test_device_detail_errors_reported() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_site(mock)
        mock.get(f"{S}/devices/{ACC2}").respond(500)
        mock.get(f"{S}/devices/{AP2}").respond(404)
        snap = integration().collect_site(CONSOLE, SITE, include_clients=False)
    assert ACC2 not in snap.device_details and AP2 not in snap.device_details
    assert len(snap.device_details) == 4
    codes = sorted((w.code, ACC2 in w.message or AP2 in w.message) for w in snap.warnings)
    assert codes == [("endpoint_error", True), ("endpoint_unavailable", True)]


def test_optional_endpoint_error_is_warning() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_site(mock)
        mock.get(f"{S}/wans").respond(400, json={"message": "bad"})
        mock.get(f"{S}/networks/{NET_ID}").respond(500)
        snap = integration().collect_site(CONSOLE, SITE, include_clients=False)
    assert snap.wans == []
    assert snap.networks == [NETWORK]  # list item kept when detail fails
    assert sorted(w.code for w in snap.warnings) == ["endpoint_error", "endpoint_error"]


def test_clients_when_requested() -> None:
    client = {"id": "c1", "type": "WIRED", "name": "printer-a", "ipAddress": "192.0.2.50"}
    with respx.mock(assert_all_called=False) as mock:
        mock_site(mock)
        mock.get(f"{S}/clients").mock(side_effect=paged([client]))
        snap = integration().collect_site(CONSOLE, SITE, include_clients=True)
    assert snap.clients == [client]


def test_wlan_detail_when_list_lacks_security() -> None:
    bare = {"id": WLAN_ID, "name": "corp-wifi", "enabled": True}
    with respx.mock(assert_all_called=False) as mock:
        mock_site(mock)
        mock.get(f"{S}/wifi/broadcasts").mock(side_effect=paged([bare]))
        mock.get(f"{S}/wifi/broadcasts/{WLAN_ID}").respond(200, json=WLAN)
        snap = integration().collect_site(CONSOLE, SITE, include_clients=False)
    assert snap.wlans == [WLAN]


@pytest.mark.parametrize("path", ["/v1/info", f"/v1/sites/{SITE_ID}/devices"])
def test_auth_refused_aborts(path: str) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_site(mock)
        mock.get(BASE + path).respond(401)
        with pytest.raises(AuthRefused):
            integration().collect_site(CONSOLE, SITE, include_clients=False)


def test_info_and_sites() -> None:
    with respx.mock(assert_all_called=False) as mock:
        info = mock.get(f"{BASE}/v1/info").respond(200, json={"applicationVersion": "10.6.106"})
        mock.get(f"{BASE}/v1/sites").mock(side_effect=paged([SITE]))
        integ = integration()
        assert integ.info() == {"applicationVersion": "10.6.106"}
        assert integ.info() == {"applicationVersion": "10.6.106"}
        assert info.call_count == 1  # cached per console
        assert integ.sites() == [SITE]


class FakeTransport:
    """Transport fake: proves Integration needs nothing beyond the protocol and is thread-safe enough."""

    base_url = "https://api.ui.com/v1/connector/consoles/h1/network/integration"

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.paths: list[str] = []

    def get_json(self, path: str, params: Params | None = None) -> Any:
        with self.lock:
            self.paths.append(path)
        if path == "/v1/info":
            return {"applicationVersion": "10.6.102"}
        if "/devices/" in path:
            dev = next(d for d in DEVICES if path.endswith(d["id"]))
            return detail(dev)
        if path.endswith(f"/networks/{NET_ID}"):
            return NETWORK
        raise EndpointUnavailable("nope", status=404, url=path)

    def get_paginated(self, path: str, params: Params | None = None, *, limit: int = 200) -> list[dict[str, Any]]:
        if path.endswith("/devices"):
            return DEVICES
        if path.endswith("/networks"):
            return [NETWORK]
        if path.endswith("/switching/lags"):
            raise ApiError("boom", status=500, url=path)
        raise EndpointUnavailable("nope", status=404, url=path)

    def login_legacy(self, username: str, password: str) -> None:
        raise AssertionError("not used")


def test_with_fake_transport_connector() -> None:
    transport = FakeTransport()
    snap = Integration(transport).collect_site(CONSOLE, SITE, include_clients=True, max_workers=3)
    assert snap.app_version == "10.6.102"
    assert list(snap.device_details) == [d["id"] for d in DEVICES]
    assert snap.clients == []
    codes = sorted(w.code for w in snap.warnings)
    # wifi, wans, mc-lag, stacks, clients unavailable; lags errored
    assert codes == ["endpoint_error"] + ["endpoint_unavailable"] * 5
