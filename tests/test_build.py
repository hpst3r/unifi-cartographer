from __future__ import annotations

import copy
import random
from typing import Any

import pytest

from conftest import GENERATED_AT, legacy_key, load_legacy, load_official
from unifi_carto.build import build_inventory
from unifi_carto.model import CollectionWarning, Device, Inventory, Link, Source
from unifi_carto.raw import LegacySnapshot, OfficialSnapshot


def uuid(n: int) -> str:
    return f"de000000-0000-4000-8000-{n:012d}"


GW, AGG, ACC1, ACC2, AP1, AP2 = (uuid(n) for n in range(1, 7))
LAB = "mac:02:00:00:00:00:07"
LAG_ACC1 = "1a900000-0000-4000-8000-000000000001"
LAG_AGG = "1a900000-0000-4000-8000-000000000002"


def build(
    official: OfficialSnapshot,
    legacy: LegacySnapshot | None = None,
    *,
    include_clients: bool = False,
    extra_warnings: list[CollectionWarning] | None = None,
) -> Inventory:
    return build_inventory(
        [official],
        {legacy_key(official): legacy} if legacy is not None else {},
        collector_version="test",
        generated_at=GENERATED_AT,
        include_clients=include_clients,
        extra_warnings=extra_warnings or [],
    )


def link_from(inv: Inventory, device_id: str, port_idx: int | None = None) -> Link:
    found = [x for x in inv.links if x.a.device_id == device_id and (port_idx is None or x.a.port_idx == port_idx)]
    assert len(found) == 1, found
    return found[0]


def device(inv: Inventory, device_id: str) -> Device:
    return next(d for d in inv.devices if d.id == device_id)


def port(inv: Inventory, device_id: str, idx: int) -> Any:
    return next(p for p in device(inv, device_id).ports if p.idx == idx)


def test_official_only_parent_links(official_a: OfficialSnapshot) -> None:
    inv = build(official_a)
    assert {(x.a.device_id, x.b.device_id) for x in inv.links} == {
        (AGG, GW),
        (ACC1, AGG),
        (ACC2, AGG),
        (AP1, ACC1),
        (AP2, AP1),
    }
    for link in inv.links:
        assert link.a.port_idx is None and link.b.port_idx is None
        assert link.source == Source.OFFICIAL
        assert link.kind == "unknown"
    assert inv.consoles[0].legacy == "unavailable"
    assert inv.consoles[0].network_version == "10.6.106"
    assert all(d.sources == [Source.OFFICIAL] for d in inv.devices)
    assert port(inv, ACC1, 5).native_vlan is None and port(inv, ACC1, 5).tagged_vlans is None
    assert inv.clients is None


def test_legacy_refused_variant(official_a: OfficialSnapshot, legacy_refused_warning: CollectionWarning) -> None:
    inv = build(official_a, extra_warnings=[legacy_refused_warning])
    assert [w.code for w in inv.warnings] == ["legacy_auth_refused"]
    assert all(x.source == Source.OFFICIAL for x in inv.links)


def test_site_and_devices(official_a: OfficialSnapshot, legacy_a: LegacySnapshot) -> None:
    inv = build(official_a, legacy_a)
    assert inv.sites[0].internal_reference == "default"
    assert inv.consoles[0].legacy == "api_key"
    roles = {d.name: d.role for d in inv.devices}
    assert roles == {
        "udm-pro": "gateway",
        "usw-agg-a": "switch",
        "usw-acc-1": "switch",
        "usw-acc-2": "switch",
        "uap-1": "access_point",
        "uap-2": "access_point",
        "usw-lab": "switch",
    }
    assert device(inv, AP1).radios[0].frequency_ghz == 2.4
    gw11 = port(inv, GW, 11)
    assert gw11.connector == "SFPPLUS" and gw11.media == "SFP+"
    assert gw11.sfp is not None and gw11.sfp.part == "UACC-DAC-SFP10-1M"
    assert gw11.sources == [Source.LEGACY, Source.OFFICIAL]
    acc1_5 = port(inv, ACC1, 5)
    assert acc1_5.name == "ap-1" and acc1_5.enabled is True
    assert acc1_5.poe is not None and acc1_5.poe.standard == "802.3at"
    assert port(inv, ACC1, 49).is_uplink is True


def test_legacy_only_device(official_a: OfficialSnapshot, legacy_a: LegacySnapshot) -> None:
    inv = build(official_a, legacy_a)
    lab = device(inv, LAB)
    assert lab.sources == [Source.LEGACY]
    assert lab.state == "ONLINE" and lab.model == "USMINI"
    assert lab.uplink_device_id == ACC2
    link = link_from(inv, LAB)
    assert (link.a.port_idx, link.b.device_id, link.b.port_idx) == (1, ACC2, 12)


def test_port_accurate_links(official_a: OfficialSnapshot, legacy_a: LegacySnapshot) -> None:
    inv = build(official_a, legacy_a)
    agg = link_from(inv, AGG)
    assert (agg.a.port_idx, agg.b.device_id, agg.b.port_idx) == (8, GW, 11)
    assert agg.source == Source.LEGACY and agg.kind == "wired" and agg.speed_mbps == 10000
    ap1 = link_from(inv, AP1)
    assert (ap1.a.port_idx, ap1.b.device_id, ap1.b.port_idx) == (1, ACC1, 5)
    assert ap1.id == f"{AP1}:1-{ACC1}:5"
    assert [x.id for x in inv.links] == sorted(x.id for x in inv.links)
    assert len({x.id for x in inv.links}) == len(inv.links)
    assert not any(x.a.device_id == GW for x in inv.links)


def test_lag_member_links(official_a: OfficialSnapshot, legacy_a: LegacySnapshot) -> None:
    inv = build(official_a, legacy_a)
    first, second = link_from(inv, ACC1, 49), link_from(inv, ACC1, 50)
    assert (first.b.device_id, first.b.port_idx, first.source) == (AGG, 1, Source.LEGACY)
    assert (second.b.device_id, second.b.port_idx, second.source) == (AGG, 2, Source.LLDP)
    assert first.lag_id == second.lag_id == LAG_ACC1
    assert {lag.id: lag.kind for lag in inv.lags} == {LAG_ACC1: "local", LAG_AGG: "local"}
    assert port(inv, AGG, 1).lag_id == port(inv, AGG, 2).lag_id == LAG_AGG
    assert port(inv, ACC1, 50).lag_id == LAG_ACC1


def test_cable_seen_from_both_ends_is_one_link(official_a: OfficialSnapshot, legacy_a: LegacySnapshot) -> None:
    inv = build(official_a, legacy_a)
    ends: list[tuple[str | None, int | None]] = []
    for link in inv.links:
        ends += [(link.a.device_id, link.a.port_idx), (link.b.device_id, link.b.port_idx)]
    ported = [e for e in ends if e[1] is not None]
    assert len(ported) == len(set(ported)), "a (device, port) appears on two links"
    between = [x for x in inv.links if {x.a.device_id, x.b.device_id} == {ACC1, AGG}]
    assert len(between) == 2  # the two LAG members, not four


def test_lldp_only_cable_seen_from_both_ends(official_a: OfficialSnapshot, legacy_a: LegacySnapshot) -> None:
    """LAG member 50 seen only by LLDP on both switches, with the remote port unparseable on one side."""
    leg = copy.deepcopy(legacy_a)
    acc1 = next(d for d in leg.devices if d["mac"] == "02:00:00:00:00:03")
    for entry in acc1["lldp_table"]:
        if entry["local_port_idx"] == 50:
            entry["port_id"] = "xe-0/0/1"
    inv = build(official_a, leg)
    second = [x for x in inv.links if (ACC1, 50) in {(x.a.device_id, x.a.port_idx), (x.b.device_id, x.b.port_idx)}]
    assert len(second) == 1
    assert (second[0].a.device_id, second[0].b.device_id, second[0].b.port_idx) == (ACC1, AGG, 2)


def test_mesh_link(official_a: OfficialSnapshot, legacy_a: LegacySnapshot) -> None:
    inv = build(official_a, legacy_a)
    mesh = link_from(inv, AP2)
    assert mesh.kind == "mesh" and mesh.b.device_id == AP1
    assert mesh.a.port_idx is None and mesh.b.port_idx is None
    assert mesh.source == Source.LEGACY


def test_lldp_external_peer(official_a: OfficialSnapshot, legacy_a: LegacySnapshot) -> None:
    inv = build(official_a, legacy_a)
    ext = link_from(inv, ACC2, 10)
    assert ext.source == Source.LLDP and ext.b.device_id is None
    assert ext.b.external is not None
    assert (ext.b.external.chassis_id, ext.b.external.port_id) == ("02:00:00:00:99:01", "eno1")
    assert ext.id == f"{ACC2}:10-ext:02:00:00:00:99:01:?"


def test_vlan_resolution(official_a: OfficialSnapshot, legacy_a: LegacySnapshot) -> None:
    inv = build(official_a, legacy_a)
    trunk = port(inv, ACC1, 5)  # profile "AP Trunk": native Default, tagged auto
    assert (trunk.profile, trunk.native_vlan, trunk.tagged_vlans) == ("AP Trunk", 1, "all")
    blocked = port(inv, ACC1, 7)  # block_all, native Staff
    assert (blocked.profile, blocked.native_vlan, blocked.tagged_vlans) == (None, 10, [])
    custom = port(inv, ACC1, 8)  # custom excluding Guest, native Default
    assert (custom.native_vlan, custom.tagged_vlans) == (1, [10, 20])
    assert port(inv, ACC1, 9).native_vlan is None  # nothing configured -> unknown


def test_port_fields_override_profile(official_a: OfficialSnapshot, legacy_a: LegacySnapshot) -> None:
    leg = copy.deepcopy(legacy_a)
    acc1 = next(d for d in leg.devices if d["mac"] == "02:00:00:00:00:03")
    p5 = next(p for p in acc1["port_table"] if p["port_idx"] == 5)
    p5["native_networkconf_id"] = "64f000000000000000000020"  # Voice
    acc1["port_overrides"] = [{"port_idx": 5, "tagged_vlan_mgmt": "block_all"}]
    inv = build(official_a, leg)
    p = port(inv, ACC1, 5)
    assert (p.profile, p.native_vlan, p.tagged_vlans) == ("AP Trunk", 20, [])


def test_uplink_conflict(official_a: OfficialSnapshot, legacy_a: LegacySnapshot) -> None:
    off = copy.deepcopy(official_a)
    off.device_details[ACC2]["uplink"] = {"deviceId": ACC1}
    inv = build(off, legacy_a)
    link = link_from(inv, ACC2, 49)
    assert link.b.device_id == AGG
    assert device(inv, ACC2).uplink_device_id == AGG
    conflicts = [w for w in inv.warnings if w.code == "uplink_conflict"]
    assert len(conflicts) == 1 and ACC2 in conflicts[0].message
    assert conflicts[0].console == "console-hq"


def test_uplink_by_name_is_inferred(official_a: OfficialSnapshot, legacy_a: LegacySnapshot) -> None:
    leg = copy.deepcopy(legacy_a)
    lab = next(d for d in leg.devices if d["name"] == "usw-lab")
    del lab["uplink"]["uplink_mac"]
    lab["uplink"]["uplink_device_name"] = "usw-acc-2"
    inv = build(official_a, leg)
    link = link_from(inv, LAB)
    assert link.source == Source.INFERRED and link.b.device_id == ACC2


def test_networks_and_wlans(official_a: OfficialSnapshot, legacy_a: LegacySnapshot) -> None:
    inv = build(official_a, legacy_a)
    nets = {n.name: n for n in inv.networks}
    assert {n: (x.vlan_id, x.purpose, x.subnet) for n, x in nets.items()} == {
        "Default": (1, "corporate", "10.0.0.1/24"),
        "Staff": (10, "corporate", "10.0.10.1/24"),
        "Voice": (20, "corporate", "10.0.20.1/24"),
        "Guest": (30, "guest", "10.0.30.1/24"),
        "Internet 1": (None, "wan", None),
    }
    assert nets["Staff"].sources == [Source.LEGACY, Source.OFFICIAL]
    assert nets["Internet 1"].id.startswith("legacy:")
    wlans = {w.name: w for w in inv.wlans}
    assert wlans["corp"].network_id == nets["Staff"].id
    assert wlans["corp"].security == "WPA2_PERSONAL" and wlans["corp"].bands == ["2.4", "5"]
    assert wlans["guest"].sources == [Source.LEGACY, Source.OFFICIAL]
    assert [w.name for w in inv.wans] == ["Internet 1"]
    assert inv.wans[0].raw["id"] == inv.wans[0].id


def test_official_networks_without_legacy(official_a: OfficialSnapshot) -> None:
    inv = build(official_a)
    staff = next(n for n in inv.networks if n.name == "Staff")
    assert (staff.vlan_id, staff.purpose, staff.subnet, staff.sources) == (10, "gateway", None, [Source.OFFICIAL])


def test_clients_only_when_requested() -> None:
    off = load_official(include_clients=True)
    leg = load_legacy(include_clients=True)
    inv = build(off, leg, include_clients=True)
    assert inv.clients is not None
    by_name = {c.name: c for c in inv.clients}
    phone = by_name["phone-101"]
    assert (phone.uplink_device_id, phone.switch_port, phone.vlan) == (ACC2, 14, 20)
    assert phone.sources == [Source.LEGACY, Source.OFFICIAL]
    printer = by_name["printer"]
    assert printer.id == "mac:02:00:00:00:a0:03" and printer.uplink_device_id == ACC1
    assert build(off, leg, include_clients=False).clients is None


def test_deterministic(official_a: OfficialSnapshot, legacy_a: LegacySnapshot) -> None:
    first = build(official_a, legacy_a).model_dump_json()
    shuffled_off, shuffled_leg = copy.deepcopy(official_a), copy.deepcopy(legacy_a)
    rng = random.Random(7)
    for seq in (shuffled_off.devices, shuffled_off.networks, shuffled_leg.devices, shuffled_leg.networkconfs):
        rng.shuffle(seq)
    for rec in shuffled_leg.devices:
        rng.shuffle(rec.get("lldp_table", []))
        rng.shuffle(rec["port_table"])
    assert build(official_a, legacy_a).model_dump_json() == first
    assert build(shuffled_off, shuffled_leg).model_dump_json() == first


def test_validates_and_roundtrips(official_a: OfficialSnapshot, legacy_a: LegacySnapshot) -> None:
    inv = build(official_a, legacy_a)
    again = Inventory.model_validate_json(inv.model_dump_json())
    assert again == inv
    assert again.model_dump_json() == inv.model_dump_json()


def _drop_random_fields(obj: Any, rng: random.Random) -> None:
    if isinstance(obj, dict):
        for key in list(obj):
            if rng.random() < 0.3:
                del obj[key]
            else:
                _drop_random_fields(obj[key], rng)
    elif isinstance(obj, list):
        for item in obj:
            _drop_random_fields(item, rng)


@pytest.mark.parametrize("seed", range(20))
def test_tolerates_missing_legacy_fields(official_a: OfficialSnapshot, legacy_a: LegacySnapshot, seed: int) -> None:
    leg = copy.deepcopy(legacy_a)
    rng = random.Random(seed)
    for records in (leg.devices, leg.portconfs, leg.networkconfs, leg.wlanconfs):
        _drop_random_fields(records, rng)
    inv = build(official_a, leg)
    Inventory.model_validate_json(inv.model_dump_json())
    assert {uuid(n) for n in range(1, 7)} <= {d.id for d in inv.devices}


def test_odd_legacy_values_become_parse_errors(official_a: OfficialSnapshot, legacy_a: LegacySnapshot) -> None:
    leg = copy.deepcopy(legacy_a)
    leg.devices.append({"mac": "not-a-mac", "name": "broken"})
    acc2 = next(d for d in leg.devices if d.get("mac") == "02:00:00:00:00:04")
    acc2["port_table"].append({"port_idx": "x", "name": "bad"})
    acc2["port_table"][0]["speed"] = {"weird": True}
    acc2["lldp_table"].append({"local_port_idx": None, "chassis_id": 7})
    acc2["uplink"]["uplink_remote_port"] = "upstream"
    inv = build(official_a, leg)
    codes = [w.code for w in inv.warnings]
    assert codes.count("parse_error") == 2
    assert any("02:00:00:00:00:04" in w.message for w in inv.warnings)
    link = link_from(inv, ACC2, 49)
    assert link.b.device_id == AGG and link.b.port_idx == 3  # remote port recovered from LLDP
