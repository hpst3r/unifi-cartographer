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
    assert out["legacy"]["lldp_table"][0]["chassis_id"] == upstream  # dash form joins too
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
