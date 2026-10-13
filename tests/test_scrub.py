from __future__ import annotations

import ipaddress
import json
import re
import uuid
from typing import Any

import pytest

from unifi_carto.scrub import drop_key, scrub

SALT = "fixture-salt"
MAC = "02:00:00:aa:bb:01"
OTHER_MAC = "02:00:00:aa:bb:02"
DEV_ID = "6f1c2b9e-4b7d-4a63-9d2e-0c1f2a3b4c5d"

CAPTURE: dict[str, Any] = {
    "official": {
        "id": DEV_ID,
        "macAddress": MAC.upper(),
        "ipAddress": "10.20.30.5",
        "name": "usw-agg-a",
        "model": "USW Aggregation",
        "firmwareVersion": "10.6.106.1",
        "state": "ONLINE",
        "interfaces": {"ports": [{"idx": 1, "connector": "SFPPLUS", "media": "SFP+"}]},
        "uplink": {"deviceId": DEV_ID},
    },
    "legacy": {
        "mac": MAC,
        "ip": "10.20.30.5",
        "type": "usw",
        "serial": "020000AABB01",
        "x_authkey": "deadbeef",
        "uplink": {"uplink_mac": OTHER_MAC, "uplink_remote_port": 3},
        "port_table": [{"port_idx": 1, "sfp_serial": "S123", "name": "uplink to core"}],
        "lldp_table": [{"chassis_id": OTHER_MAC.replace(":", "-"), "port_id": "Port 3 of 02:00:00:aa:bb:02"}],
        "config_network": {"ip": "192.168.7.10", "netmask": "255.255.255.0", "gateway": "192.168.7.1"},
    },
    "wlanconf": [
        {
            "name": "corp-wifi",
            "x_passphrase": "hunter2-synthetic",
            "security": "wpapsk",
            "wpa_psk": "x",
            "nested": [{"radius": {"secret": "s", "apiKey": "k", "privateKey": "p", "keyword": "kept"}}],
        }
    ],
    "site": {"desc": "Head Office", "name": "default", "ip_subnet": "10.20.30.1/24", "license_key": "L"},
    "wan": {"wan_ip": "8.8.4.4", "ipv6": "2001:db8:1:2::10", "ll": "fe80::1", "prefix": "2001:db8:1:2::/64"},
    "token_value": "abc",
    "user": {"username": "jdoe", "password": "pw", "lastLoginToken": "t"},
}


def flatten(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True)


def test_deterministic() -> None:
    assert scrub(CAPTURE, salt=SALT) == scrub(json.loads(json.dumps(CAPTURE)), salt=SALT)
    assert scrub(CAPTURE, salt=SALT) != scrub(CAPTURE, salt="other-salt")


def test_input_not_mutated() -> None:
    before = flatten(CAPTURE)
    scrub(CAPTURE, salt=SALT)
    assert flatten(CAPTURE) == before


def test_secrets_removed_at_any_depth() -> None:
    out = flatten(scrub(CAPTURE, salt=SALT))
    for secret in ("hunter2-synthetic", "deadbeef", "020000AABB01", "S123", '"pw"'):
        assert secret not in out
    for key in ("x_passphrase", "x_authkey", "wpa_psk", "secret", "apiKey", "privateKey", "serial", "sfp_serial",
                "license_key", "token_value", "password", "lastLoginToken"):  # fmt: skip
        assert f'"{key}"' not in out
    assert '"keyword": "kept"' in out


@pytest.mark.parametrize(
    ("key", "dropped"),
    [
        ("apiKey", True),
        ("x_iapp_key", True),
        ("wpakey", True),
        ("privateKeys", True),
        ("x_ssh_username", True),
        ("passphrase", True),
        ("keyword", False),
        ("keepalive", False),
        ("model", False),
        ("macAddress", False),
    ],
)
def test_drop_key(key: str, dropped: bool) -> None:
    assert drop_key(key) is dropped


def test_mac_join_preserved() -> None:
    out = scrub(CAPTURE, salt=SALT)
    official_mac = out["official"]["macAddress"]
    assert re.fullmatch(r"02(:[0-9a-f]{2}){5}", official_mac)
    assert official_mac != MAC
    assert out["legacy"]["mac"] == official_mac  # case-insensitive input, same output
    upstream = out["legacy"]["uplink"]["uplink_mac"]
    assert out["legacy"]["lldp_table"][0]["chassis_id"] == upstream.replace(":", "-")  # dash form joins too
    assert out["legacy"]["lldp_table"][0]["port_id"] == f"Port 3 of {upstream}"  # embedded
    assert upstream != official_mac


def test_uuid_remap_preserves_joins() -> None:
    out = scrub(CAPTURE, salt=SALT)
    new_id = out["official"]["id"]
    assert new_id != DEV_ID
    assert uuid.UUID(new_id).version == 5
    assert out["official"]["uplink"]["deviceId"] == new_id
    assert scrub({DEV_ID: [f"/v1/devices/{DEV_ID}"]}, salt=SALT) == {new_id: [f"/v1/devices/{new_id}"]}


def test_private_ipv4() -> None:
    out = scrub(CAPTURE, salt=SALT)
    a = ipaddress.IPv4Address(out["official"]["ipAddress"])
    assert a in ipaddress.IPv4Network("10.0.0.0/8") and str(a).endswith(".5")
    assert out["legacy"]["ip"] == str(a)
    cfg = out["legacy"]["config_network"]
    assert cfg["netmask"] == "255.255.255.0"
    ip, gw = ipaddress.IPv4Address(cfg["ip"]), ipaddress.IPv4Address(cfg["gateway"])
    assert ip in ipaddress.IPv4Network("192.168.0.0/16") and str(ip).endswith(".10")
    assert ipaddress.IPv4Network(f"{ip}/24", strict=False) == ipaddress.IPv4Network(f"{gw}/24", strict=False)
    subnet = out["site"]["ip_subnet"]
    assert subnet.endswith(".1/24") and ipaddress.ip_interface(subnet).ip == ipaddress.IPv4Address(
        str(a).rsplit(".", 1)[0] + ".1"
    )


def test_distinct_private_slash24s_stay_distinct() -> None:
    nets = [f"10.{b}.{c}.1" for b in range(0, 256, 15) for c in range(0, 256, 3)]
    out = scrub(nets, salt=SALT)
    assert len({o.rsplit(".", 1)[0] for o in out}) == len(nets)
    assert all(o.startswith("10.") and o.endswith(".1") for o in out)


def test_public_ipv4_and_ipv6() -> None:
    wan = scrub(CAPTURE, salt=SALT)["wan"]
    pub = ipaddress.IPv4Address(wan["wan_ip"])
    assert any(pub in ipaddress.IPv4Network(n) for n in ("203.0.113.0/24", "198.51.100.0/24"))
    v6 = ipaddress.IPv6Address(wan["ipv6"])
    assert v6 in ipaddress.IPv6Network("fd00::/8")
    prefix = ipaddress.ip_network(wan["prefix"])
    assert prefix.prefixlen == 64 and v6 in prefix  # the /64 maps as a unit
    assert ipaddress.IPv6Address(wan["ll"]) in ipaddress.IPv6Network("fd00::/8")


def test_identity_fields_and_kept_values() -> None:
    out = scrub(CAPTURE, salt=SALT)
    assert re.fullmatch(r"name-[0-9a-f]{6}", out["official"]["name"])
    assert re.fullmatch(r"site-[0-9a-f]{6}", out["site"]["desc"])
    assert re.fullmatch(r"ssid-[0-9a-f]{6}", scrub({"essid": "corp-wifi"}, salt=SALT)["essid"])
    assert re.fullmatch(r"user-[0-9a-f]{6}", out["user"]["username"])
    assert out["legacy"]["port_table"][0]["name"] != "uplink to core"
    assert out["official"]["model"] == "USW Aggregation"
    assert out["official"]["firmwareVersion"] == "10.6.106.1"
    assert out["official"]["state"] == "ONLINE"
    assert out["official"]["interfaces"]["ports"][0] == {"idx": 1, "connector": "SFPPLUS", "media": "SFP+"}
    assert out["legacy"]["type"] == "usw"
    assert out["wlanconf"][0]["security"] == "wpapsk"
    # same name in two places maps identically
    assert (
        scrub([{"name": "usw-agg-a"}, {"hostname": "usw-agg-a"}], salt=SALT)[0]["name"][-6:]
        == scrub({"hostname": "usw-agg-a"}, salt=SALT)["hostname"][-6:]
    )


def test_scalars_untouched() -> None:
    assert scrub({"n": 1, "b": True, "x": None, "f": 1.5, "s": "plain"}, salt=SALT) == {
        "n": 1,
        "b": True,
        "x": None,
        "f": 1.5,
        "s": "plain",
    }


def test_mac_notations_map_alike_and_keep_their_form() -> None:
    colon = scrub("02:00:00:aa:bb:03", salt=SALT)
    digits = colon.replace(":", "")
    out = scrub(
        {
            "a": "02-00-00-AA-BB-03",
            "b": "0200.00aa.bb03",
            "c": "020000aabb03",
            "d": "mac:02:00:00:aa:bb:03",
            "e": "uplink-02:00:00:AA:BB:03",
        },
        salt=SALT,
    )
    assert out == {
        "a": colon.replace(":", "-"),
        "b": ".".join(digits[i : i + 4] for i in range(0, 12, 4)),
        "c": digits,
        "d": f"mac:{colon}",
        "e": f"uplink-{colon}",
    }


def test_hex_runs_that_are_not_macs_are_kept() -> None:
    object_id = "5f1e0a2b3c4d5e6f7a8b9c0d"  # legacy _id: 24 hex, not two MACs
    v6 = "fe80::aa:bb:cc:dd:ee:ff"
    bad = "2001:db8::02:00:00:aa:bb:04"  # has "::" but too many groups for an address
    out = scrub({"_id": object_id, "seven": "aa:bb:cc:dd:ee:ff:01", "v6": f"addr {v6}", "bad": bad}, salt=SALT)
    assert out["_id"] == object_id
    assert out["seven"] == "aa:bb:cc:dd:ee:ff:01"
    mapped = ipaddress.IPv6Address(out["v6"].removeprefix("addr "))  # one IPv6 address, not a MAC inside it
    assert mapped in ipaddress.IPv6Network("fd00::/8")
    assert out["bad"] == "2001:db8::" + scrub("02:00:00:aa:bb:04", salt=SALT)
    assert scrub(DEV_ID, salt=SALT) == scrub({"id": DEV_ID}, salt=SALT)["id"]  # the UUID's last group is not a MAC


@pytest.mark.parametrize(
    "key",
    ["uplink_device_name", "chassis_name", "system_name", "displayName", "alias", "description", "port_descr",
     "domain_name", "domain", "wan_username", "label", "ssid", "email", "notes", "comment", "x509_hostname"],
)  # fmt: skip
def test_identity_keys_by_pattern(key: str) -> None:
    out = scrub({key: "acme-thing"}, salt=SALT)
    assert "acme" not in json.dumps(out)


@pytest.mark.parametrize(
    ("key", "value"),
    [("model_name", "USW Pro 24"), ("shortname", "USW24"), ("purpose", "corporate"), ("ifname", "eth8"),
     ("radio_name", "wifi0"), ("device_type", "switch"), ("state", "connected")],
)  # fmt: skip
def test_enum_name_keys_kept(key: str, value: str) -> None:
    assert scrub({key: value}, salt=SALT) == {key: value}


@pytest.mark.parametrize(
    ("key", "dropped"),
    [("geo_info", True), ("geoCode", True), ("latitude", True), ("lat", True), ("longitude", True), ("lng", True),
     ("city", True), ("country_code", True), ("countryCode", True), ("address", True), ("isp_name", True),
     ("ispOrganization", True), ("timezone", True), ("time_zone", True),
     ("latency", False), ("long_retry", False), ("ip_address", False), ("macAddress", False), ("ipv6", False)],
)  # fmt: skip
def test_location_keys_dropped(key: str, dropped: bool) -> None:
    assert drop_key(key) is dropped
    assert (key in scrub({key: 1}, salt=SALT)) is not dropped


def test_wifi_passphrases_dropped() -> None:
    official_wlan = {
        "id": DEV_ID,
        "name": "corp-wifi",
        "securityConfiguration": {"type": "WPA2_PERSONAL", "passphrase": "hunter2-synthetic"},
    }
    legacy_wlan = {"name": "corp-wifi", "security": "wpapsk", "x_passphrase": "hunter2-synthetic"}
    out = scrub({"official": official_wlan, "legacy": legacy_wlan}, salt=SALT)
    assert "hunter2" not in json.dumps(out)
    assert out["official"]["securityConfiguration"] == {"type": "WPA2_PERSONAL"}
    assert "x_passphrase" not in out["legacy"]


def test_ip_addresses_inside_longer_strings() -> None:
    out = scrub(
        {
            "inform": "http://[2001:db8:4242::1]:8080/inform",
            "dns": "2001:db8:4242::53,2001:db8:4242::54",
            "route": "via 2001:db8:4242::/48 dev eth0",
            "mapped": "peer ::ffff:8.8.8.8",
            "v4": "http://8.8.4.4:8443/x, 10.20.30.0/24",
            "clock": "uptime 12:00:00",
        },
        salt=SALT,
    )
    assert "4242" not in json.dumps(out)
    assert "8.8." not in json.dumps(out)
    assert out["clock"] == "uptime 12:00:00"
    url_v6 = out["inform"].removeprefix("http://[").removesuffix("]:8080/inform")
    assert url_v6 == scrub("2001:db8:4242::1", salt=SALT)  # same mapping as a whole-string address
    net = ipaddress.ip_network(out["route"].split()[1])
    assert net.prefixlen == 48
    assert ipaddress.ip_network(out["v4"].split(", ")[1]).prefixlen == 24


def test_public_ipv4_injective_and_stable_across_calls() -> None:
    ips = [str(ipaddress.IPv4Address("100.64.0.0") + i * 7) for i in range(600)]  # beyond the 508 TEST-NET slots
    out = scrub(ips, salt="injective-salt")
    assert len(set(out)) == len(ips)
    assert all(not ipaddress.IPv4Address(o).is_global for o in out)
    assert [scrub(ip, salt="injective-salt") for ip in ips[:5]] == out[:5]  # joinable across files in one run
