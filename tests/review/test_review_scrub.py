"""Scrubber completeness proofs: identifying data a real capture would carry into public fixtures."""

from __future__ import annotations

import ipaddress
import json
from typing import Any

from review.conftest import review_bug
from unifi_carto.scrub import scrub

SALT = "review-salt"


def dumped(obj: Any) -> str:
    return json.dumps(scrub(obj, salt=SALT))


def test_scr01_mac_notations_are_scrubbed() -> None:
    review_bug("SCR-01: MACs without separators, in dotted form or after ':'/'-' (e.g. 'mac:aa:..') survive scrubbing")
    samples = {
        "device_mac": "a4b1c2d3e4f5",
        "macAddress": "A4B1C2D3E4F6",
        "port_id": "a4b1.c2d3.e4f7",
        "port_descr": "mac:a4:b1:c2:d3:e4:f8",
        "local_port_name": "uplink-A4:B1:C2:D3:E4:F9",
    }
    out = scrub(samples, salt=SALT)
    leaked = {k: v for k, v in out.items() if "a4b1" in v.lower().replace(":", "").replace(".", "").replace("-", "")}
    assert leaked == {}


def test_scr02_names_under_unlisted_keys_are_scrubbed() -> None:
    review_bug("SCR-02: names/usernames/domains under keys outside _IDENTITY_KEYS are kept verbatim")
    legacy_device = {
        "mac": "02:00:00:00:02:01",
        "uplink": {"uplink_mac": "02:00:00:00:02:02", "uplink_device_name": "acme-hq-core-sw"},
        "lldp_table": [{"chassis_id": "02:00:00:00:02:03", "chassis_name": "acme-hq-fw", "system_name": "acme-fw-01"}],
        "alias": "acme-closet-3",
        "description": "Acme Corp HQ, 3rd floor",
    }
    networkconf = {
        "name": "WAN",
        "purpose": "wan",
        "wan_username": "acme0042@isp.example",
        "domain_name": "acme.example",
    }
    out = dumped({"device": legacy_device, "networkconf": networkconf})
    for value in (
        "acme-hq-core-sw",
        "acme-hq-fw",
        "acme-fw-01",
        "acme-closet-3",
        "Acme Corp",
        "acme0042",
        "acme.example",
    ):
        assert value not in out


def test_scr03_location_data_is_scrubbed() -> None:
    review_bug("SCR-03: gateway geo_info (coordinates, city, ISP) passes the scrubber untouched")
    gateway = {
        "mac": "02:00:00:00:03:01",
        "type": "udm",
        "geo_info": {
            "WAN": {
                "latitude": 47.6062,
                "longitude": -122.3321,
                "city": "Springfield",
                "isp_name": "Example Fiber",
                "address": "742 Evergreen Terrace",
            }
        },
    }
    out = dumped(gateway)
    for value in ("47.6062", "-122.3321", "Springfield", "Example Fiber", "Evergreen"):
        assert value not in out


def test_scr04_ipv6_inside_longer_strings_is_scrubbed() -> None:
    review_bug("SCR-04: IPv6 addresses are only scrubbed when they are the whole string (URLs, lists leak)")
    out = scrub(
        {"inform_url": "http://[2001:db8:4242::1]:8080/inform", "dns": "2001:db8:4242::53, 2001:db8:4242::54"},
        salt=SALT,
    )
    assert "4242" not in json.dumps(out)


def test_scr05_public_ipv4_mapping_is_injective() -> None:
    review_bug("SCR-05: public IPv4s map into 508 addresses, so distinct addresses collide and joins break")
    ips = [str(ipaddress.IPv4Address("192.0.2.1") + i) for i in range(100)]  # TEST-NET-1, mapped like public
    out = scrub(ips, salt=SALT)
    assert len(set(out)) == len(ips)


def test_scr06_console_identity_in_capture_is_scrubbed() -> None:
    review_bug("SCR-06: capture's console.json keeps the Site Manager host id and the local_url hostname")
    console = {
        "id": "9C05D6000000000007A3387E0000000063EC9853:1234567890",
        "name": "hq",
        "access": "local",
        "local_url": "https://unifi.acme-hq.example",
        "model": "UDM-Pro",
        "firmware": "4.1.13",
    }
    out = dumped(console)
    assert "7A3387E" not in out
    assert "acme-hq" not in out
