from __future__ import annotations

import httpx
import pytest
import respx

from unifi_carto.errors import ApiError
from unifi_carto.http import ReadOnlyClient
from unifi_carto.sitemanager import API_KEY_HEADER, BASE_URL, SiteManager, connector_base_url, host_summary

KEY = "sm-test-key-0001"  # synthetic
HOST_ID = "900A6F00301100000000074A6BA90000000007A3387E0000000063EC9853:123456789"


def sm() -> SiteManager:
    return SiteManager(ReadOnlyClient(BASE_URL, api_key=KEY, api_key_header=API_KEY_HEADER), page_size=2)


def envelope(data: list[dict[str, object]], token: str | None = None) -> httpx.Response:
    body: dict[str, object] = {"data": data, "httpStatusCode": 200, "traceId": "trace-1"}
    if token is not None:
        body["nextToken"] = token
    return httpx.Response(200, json=body)


@respx.mock
def test_hosts_follow_next_token() -> None:
    route = respx.get(f"{BASE_URL}/v1/hosts").mock(
        side_effect=[envelope([{"id": "a"}, {"id": "b"}], "t1"), envelope([{"id": "c"}], "")]
    )
    assert [h["id"] for h in sm().hosts()] == ["a", "b", "c"]
    first, second = (c.request for c in route.calls)
    assert first.headers["X-API-Key"] == KEY
    assert first.url.params["pageSize"] == "2" and "nextToken" not in first.url.params
    assert second.url.params["nextToken"] == "t1"


@respx.mock
def test_sites_single_page_and_devices() -> None:
    respx.get(f"{BASE_URL}/v1/sites").mock(return_value=envelope([{"siteId": "s1", "hostId": HOST_ID}]))
    grouped = {"hostId": HOST_ID, "hostName": "console-a", "devices": [{"mac": "020000000001", "model": "USW"}]}
    respx.get(f"{BASE_URL}/v1/devices").mock(return_value=envelope([grouped]))
    client = sm()
    assert client.sites() == [{"siteId": "s1", "hostId": HOST_ID}]
    assert client.devices() == [grouped]


@respx.mock
def test_repeated_token_is_an_error() -> None:
    respx.get(f"{BASE_URL}/v1/hosts").mock(return_value=envelope([{"id": "a"}], "same"))
    with pytest.raises(ApiError, match="nextToken"):
        sm().hosts()


@respx.mock
def test_bad_envelope() -> None:
    respx.get(f"{BASE_URL}/v1/hosts").respond(200, json=[{"id": "a"}])
    with pytest.raises(ApiError):
        sm().hosts()


def test_connector_base_url() -> None:
    # Amendment 2.11: the server-supplied host id is one percent-encoded path segment (":" -> "%3A").
    encoded = HOST_ID.replace(":", "%3A")
    assert connector_base_url(HOST_ID) == f"https://api.ui.com/v1/connector/consoles/{encoded}/network/integration"
    assert connector_base_url("../x?y#z").startswith("https://api.ui.com/v1/connector/consoles/..%2Fx%3Fy%23z/")
    assert "/consoles/%2E%2E/" in connector_base_url("..")


def test_host_summary_unifi_os() -> None:
    host = {
        "id": HOST_ID,
        "type": "console",
        "ipAddress": "203.0.113.7",
        "reportedState": {
            "name": "console-a",
            "hostname": "console-a.example.invalid",
            "version": "4.3.6",
            "hardware": {"shortname": "UDMPROSE", "name": "UniFi Dream Machine Pro SE"},
        },
    }
    ref = host_summary(host)
    assert (ref.id, ref.name, ref.access, ref.model, ref.firmware) == (
        HOST_ID,
        "console-a",
        "connector",
        "UDMPROSE",
        "4.3.6",
    )
    assert ref.local_url is None


def test_host_summary_spec_example_shape() -> None:
    # The spec's self-hosted example: hostname + firmware_version, no hardware block.
    ref = host_summary(
        {"id": "h1", "reportedState": {"hostname": "ctrl.example.invalid", "firmware_version": "8.3.11"}}
    )
    assert (ref.name, ref.model, ref.firmware) == ("ctrl.example.invalid", None, "8.3.11")


def test_host_summary_minimal() -> None:
    ref = host_summary({"id": "h2", "reportedState": None})
    assert (ref.id, ref.name, ref.model, ref.firmware) == ("h2", "h2", None, None)
    with pytest.raises(ApiError):
        host_summary({"reportedState": {}})
