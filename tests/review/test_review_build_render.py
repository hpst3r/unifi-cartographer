"""Builder and renderer proofs over small synthetic snapshots."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any

from unifi_carto.build import build_inventory
from unifi_carto.model import Device, Inventory, Site
from unifi_carto.raw import ConsoleRef, LegacySnapshot, OfficialSnapshot
from unifi_carto.render import render_drawio

AT = datetime(2026, 1, 1, tzinfo=UTC)
CONSOLE = ConsoleRef(id="c1", name="hq", access="local", local_url="https://192.0.2.1")
SITE = {"id": "s1", "name": "Main", "internalReference": "default"}
GW = "6f1a0000-0000-4000-8000-000000000001"
SW = "6f1a0000-0000-4000-8000-000000000002"
GW_MAC, SW_MAC = "02:00:00:00:00:01", "02:00:00:00:00:02"


def official_device(dev_id: str, mac: str, name: str, feature: str, uplink: str | None) -> dict[str, Any]:
    d: dict[str, Any] = {"id": dev_id, "macAddress": mac, "name": name, "features": [feature]}
    if uplink:
        d["uplink"] = {"deviceId": uplink}
    return d


def test_bld01_one_cable_seen_from_both_ends_is_one_link() -> None:
    snap = OfficialSnapshot(
        console=CONSOLE,
        site=SITE,
        app_version="10.6.106",
        devices=[
            official_device(GW, GW_MAC, "ugw-main", "gateway", None),
            official_device(SW, SW_MAC, "usw-agg-a", "switching", GW),
        ],
    )
    leg = LegacySnapshot(
        site_name="default",
        auth="api_key",
        devices=[
            # switch: legacy uplink knows its own port, not the gateway's
            {"mac": SW_MAC, "type": "usw", "uplink": {"uplink_mac": GW_MAC, "port_idx": 1, "type": "wire"}},
            # gateway: LLDP sees the switch on port 4; no usable neighbor port id
            {
                "mac": GW_MAC,
                "type": "udm",
                "lldp_table": [{"local_port_idx": 4, "chassis_id": SW_MAC, "is_wired": True}],
            },
        ],
    )
    inv = build_inventory([snap], {("c1", "s1"): leg}, collector_version="0", generated_at=AT, include_clients=False)
    between = [lk for lk in inv.links if {lk.a.device_id, lk.b.device_id} == {GW, SW}]
    assert len(between) == 1, [lk.id for lk in between]
    assert (between[0].a.device_id, between[0].a.port_idx) == (SW, 1)
    assert (between[0].b.device_id, between[0].b.port_idx) == (GW, 4)


def test_rnd01_control_characters_in_names_keep_the_document_well_formed() -> None:
    inv = Inventory(
        generated_at=AT,
        collector_version="0",
        sites=[Site(id="s1", console_id="c1", name="Main\x1b[0m")],
        devices=[Device(id="d1", site_id="s1", mac=SW_MAC, name="usw-agg-a\x00\x07", role="switch")],
    )
    xml = render_drawio(inv)
    ET.fromstring(xml)  # raises ParseError on invalid characters
    assert "usw-agg-a" in xml
