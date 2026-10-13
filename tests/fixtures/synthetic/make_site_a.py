"""Generate the synthetic site-a fixtures (official + legacy JSON).

Run from the repo root: ``uv run python tests/fixtures/synthetic/make_site_a.py``. The JSON files are
checked in; this script documents how they were made. Synthetic data only: 10.x addresses,
locally administered MACs (02:00:00:...), invented names.

Topology:
  gw (UDM-Pro) port 11 SFP+ LAN  <-  agg-a (USW-Aggregation) port 8
  agg-a ports 1+2 (LAG)          <-  acc-1 (USW-Pro-48-PoE) ports 49+50 (LAG)
  agg-a port 3                   <-  acc-2 (USW-Pro-48-PoE) port 49
  acc-1 port 5 ("AP Trunk")      <-  ap-1 port 1
  ap-1                           <~  ap-2 (wireless mesh)
  acc-2 port 10                  <-  LLDP-only server (not UniFi)
  acc-2 port 12                  <-  usw-lab (legacy-only device, not in the official API)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent / "site-a"

SITE_ID = "5a1e0000-0000-4000-8000-00000000a001"
CONSOLE = {
    "id": "console-hq",
    "name": "hq",
    "access": "local",
    "local_url": "https://10.0.0.1",
    "model": "UDM-Pro",
    "firmware": "4.1.13",
}


def uuid(n: int) -> str:
    return f"de000000-0000-4000-8000-{n:012d}"


def mac(n: int) -> str:
    return f"02:00:00:00:00:{n:02x}"


GW, AGG, ACC1, ACC2, AP1, AP2, LAB = 1, 2, 3, 4, 5, 6, 7
SERVER_MAC = "02:00:00:00:99:01"
LAG_ACC1 = "1a900000-0000-4000-8000-000000000001"
LAG_AGG = "1a900000-0000-4000-8000-000000000002"
NET = {
    "default": "0e700000-0000-4000-8000-000000000001",
    "staff": "0e700000-0000-4000-8000-000000000010",
    "voice": "0e700000-0000-4000-8000-000000000020",
    "guest": "0e700000-0000-4000-8000-000000000030",
}
NC = {  # legacy networkconf _id (24 hex, like the controller's object ids)
    "default": "64f000000000000000000001",
    "staff": "64f000000000000000000010",
    "voice": "64f000000000000000000020",
    "guest": "64f000000000000000000030",
    "wan": "64f0000000000000000000f1",
}
PC_AP_TRUNK = "65a000000000000000000001"


def envelope(data: list[dict[str, Any]]) -> dict[str, Any]:
    return {"meta": {"rc": "ok"}, "data": data}


# ---- official ----------------------------------------------------------------------------------


def off_port(idx: int, connector: str, max_speed: int, *, up: bool, speed: int | None = None, poe: bool = False):
    port: dict[str, Any] = {
        "idx": idx,
        "state": "UP" if up else "DOWN",
        "connector": connector,
        "maxSpeedMbps": max_speed,
        "speedMbps": (speed or max_speed) if up else 0,
    }
    if poe:
        port["poe"] = {"standard": "802.3at", "type": 2, "enabled": True, "state": "UP" if up else "DOWN"}
    return port


def usw48_ports(up: set[int], speeds: dict[int, int]) -> list[dict[str, Any]]:
    ports = [off_port(i, "RJ45", 1000, up=i in up, speed=speeds.get(i), poe=True) for i in range(1, 49)]
    ports += [off_port(i, "SFPPLUS", 10000, up=i in up, speed=speeds.get(i)) for i in range(49, 53)]
    return ports


DEVICES = [
    (GW, "udm-pro", "UDM Pro", "10.0.0.1", ["gateway", "switching"], ["ports"]),
    (AGG, "usw-agg-a", "USW Aggregation", "10.0.0.2", ["switching"], ["ports"]),
    (ACC1, "usw-acc-1", "USW Pro 48 PoE", "10.0.0.3", ["switching"], ["ports"]),
    (ACC2, "usw-acc-2", "USW Pro 48 PoE", "10.0.0.4", ["switching"], ["ports"]),
    (AP1, "uap-1", "U6 Pro", "10.0.0.5", ["accessPoint"], ["ports", "radios"]),
    (AP2, "uap-2", "U6 Pro", "10.0.0.6", ["accessPoint"], ["ports", "radios"]),
]
PARENT = {AGG: GW, ACC1: AGG, ACC2: AGG, AP1: ACC1, AP2: AP1}


def official() -> dict[str, Any]:
    devices = [
        {
            "id": uuid(n),
            "macAddress": mac(n),
            "name": name,
            "model": model,
            "ipAddress": ip,
            "state": "ONLINE",
            "supported": True,
            "firmwareVersion": "7.1.66",
            "firmwareUpdatable": False,
            "features": features,
            "interfaces": interfaces,
        }
        for n, name, model, ip, features, interfaces in DEVICES
    ]
    gw_ports = [off_port(i, "RJ45", 1000, up=i == 9) for i in range(1, 10)]
    gw_ports += [off_port(10, "SFPPLUS", 10000, up=False), off_port(11, "SFPPLUS", 10000, up=True)]
    ports = {
        GW: gw_ports,
        AGG: [off_port(i, "SFPPLUS", 10000, up=i in (1, 2, 3, 8)) for i in range(1, 9)],
        ACC1: usw48_ports({5, 7, 8, 49, 50}, {5: 2500}),
        ACC2: usw48_ports({10, 12, 14, 49}, {}),
        AP1: [off_port(1, "RJ45", 2500, up=True)],
        AP2: [off_port(1, "RJ45", 2500, up=False)],
    }
    radios = [
        {"wlanStandard": "802.11ax", "frequencyGHz": 2.4, "channelWidthMHz": 20, "channel": 6},
        {"wlanStandard": "802.11ax", "frequencyGHz": 5, "channelWidthMHz": 80, "channel": 36},
    ]
    details = {}
    for dev in devices:
        n = int(dev["id"][-12:])
        detail = {k: v for k, v in dev.items() if k not in ("features", "interfaces")}
        detail["configurationId"] = f"cfg{n}"
        detail["features"] = {f: {} for f in dev["features"]}
        detail["interfaces"] = {"ports": ports[n]}
        if n in (AP1, AP2):
            detail["interfaces"]["radios"] = radios
        if n in PARENT:
            detail["uplink"] = {"deviceId": uuid(PARENT[n])}
        details[dev["id"]] = detail
    details[uuid(ACC1)]["features"]["switching"] = {"lags": [{"id": LAG_ACC1, "portIdxs": [49, 50]}]}
    return {
        "console.json": CONSOLE,
        "info.json": {"applicationVersion": "10.6.106"},
        "site.json": {"id": SITE_ID, "internalReference": "default", "name": "Site A"},
        "devices.json": devices,
        "device_details.json": details,
        "networks.json": [
            {
                "id": NET["default"],
                "name": "Default",
                "vlanId": 1,
                "default": True,
                "enabled": True,
                "management": "GATEWAY",
            },
            {
                "id": NET["staff"],
                "name": "Staff",
                "vlanId": 10,
                "default": False,
                "enabled": True,
                "management": "GATEWAY",
            },
            {
                "id": NET["voice"],
                "name": "Voice",
                "vlanId": 20,
                "default": False,
                "enabled": True,
                "management": "GATEWAY",
            },
            {
                "id": NET["guest"],
                "name": "Guest",
                "vlanId": 30,
                "default": False,
                "enabled": True,
                "management": "GATEWAY",
            },
        ],
        "wlans.json": [
            {
                "id": "3f100000-0000-4000-8000-000000000001",
                "name": "corp",
                "enabled": True,
                "network": {"type": "SPECIFIC", "networkId": NET["staff"]},
                "securityConfiguration": {"type": "WPA2_PERSONAL"},
                "broadcastingFrequenciesGHz": [2.4, 5],
            },
            {
                "id": "3f100000-0000-4000-8000-000000000002",
                "name": "guest",
                "enabled": True,
                "network": {"type": "SPECIFIC", "networkId": NET["guest"]},
                "securityConfiguration": {"type": "OPEN"},
                "broadcastingFrequenciesGHz": [5],
            },
        ],
        "wans.json": [{"id": "3a400000-0000-4000-8000-000000000001", "name": "Internet 1"}],
        "lags.json": [
            {
                "id": LAG_ACC1,
                "type": "LOCAL",
                "members": [{"deviceId": uuid(ACC1), "portIdxs": [49, 50]}],
                "metadata": {"origin": "USER_DEFINED"},
            },
            {
                "id": LAG_AGG,
                "type": "LOCAL",
                "members": [{"deviceId": uuid(AGG), "portIdxs": [1, 2]}],
                "metadata": {"origin": "USER_DEFINED"},
            },
        ],
        "mc_lag_domains.json": [],
        "switch_stacks.json": [],
        "clients.json": [
            {
                "id": "c1000000-0000-4000-8000-000000000001",
                "type": "WIRED",
                "name": "phone-101",
                "macAddress": "02:00:00:00:a0:01",
                "ipAddress": "10.0.20.101",
                "uplinkDeviceId": uuid(ACC2),
            },
            {
                "id": "c1000000-0000-4000-8000-000000000002",
                "type": "WIRELESS",
                "name": "laptop-7",
                "macAddress": "02:00:00:00:a0:02",
                "ipAddress": "10.0.10.57",
                "uplinkDeviceId": uuid(AP1),
            },
        ],
    }


# ---- legacy ------------------------------------------------------------------------------------


def leg_port(idx: int, media: str, *, up: bool, speed: int = 1000, **extra: Any) -> dict[str, Any]:
    port: dict[str, Any] = {
        "port_idx": idx,
        "name": f"Port {idx}",
        "media": media,
        "enable": True,
        "up": up,
        "speed": speed if up else 0,
        "is_uplink": False,
    }
    port.update(extra)
    return port


def sfp(idx: int) -> dict[str, Any]:
    return {"sfp_found": True, "sfp_vendor": "Ubiquiti Inc.", "sfp_part": "UACC-DAC-SFP10-1M", "sfp_serial": f"X{idx}"}


def lldp(local: int, chassis: str, port_id: str) -> dict[str, Any]:
    return {"local_port_idx": local, "chassis_id": chassis, "port_id": port_id, "is_wired": True}


def usw48(up: set[int], extra: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    ports = [leg_port(i, "GE", up=i in up, port_poe=True, poe_enable=True, **extra.get(i, {})) for i in range(1, 49)]
    ports += [leg_port(i, "SFP+", up=i in up, speed=10000, **extra.get(i, {})) for i in range(49, 53)]
    return ports


def legacy() -> dict[str, Any]:
    gw_ports = [leg_port(i, "GE", up=i == 9) for i in range(1, 10)]
    gw_ports += [leg_port(10, "SFP+", up=False), leg_port(11, "SFP+", up=True, speed=10000, **sfp(11))]
    agg_ports = [leg_port(i, "SFP+", up=i in (1, 2, 3, 8), speed=10000) for i in range(1, 9)]
    for i in (1, 2, 3, 8):
        agg_ports[i - 1].update(sfp(100 + i))
    agg_ports[0].update(aggregated_by=False, lag_member=True)
    agg_ports[1].update(aggregated_by=1, lag_member=True)
    agg_ports[7]["is_uplink"] = True
    acc1_ports = usw48(
        {5, 7, 8, 49, 50},
        {
            5: {"name": "ap-1", "speed": 2500, "portconf_id": PC_AP_TRUNK},
            7: {"name": "staff-only", "native_networkconf_id": NC["staff"], "tagged_vlan_mgmt": "block_all"},
            8: {
                "name": "no-guest",
                "native_networkconf_id": NC["default"],
                "tagged_vlan_mgmt": "custom",
                "excluded_networkconf_ids": [NC["guest"]],
            },
            49: {"is_uplink": True, "aggregated_by": False, "lag_member": True, **sfp(49)},
            50: {"aggregated_by": 49, "lag_member": True, **sfp(50)},
        },
    )
    acc2_ports = usw48(
        {10, 12, 14, 49},
        {10: {"name": "server"}, 12: {"name": "lab"}, 49: {"is_uplink": True, **sfp(49)}},
    )

    def dev(n: int, dtype: str, model: str, ports: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        name = next((d[1] for d in DEVICES if d[0] == n), "usw-lab")
        record = {
            "_id": f"66d0000000000000000000{n:02d}",
            "mac": mac(n),
            "name": name,
            "type": dtype,
            "model": model,
            "ip": f"10.0.0.{n}",
            "version": "7.1.66.15000",
            "state": 1,
            "adopted": True,
            "port_table": ports,
        }
        record.update(extra)
        return record

    devices = [
        dev(GW, "udm", "UDMPRO", gw_ports, uplink={"type": "wire", "port_idx": 9, "speed": 1000, "up": True}),
        dev(
            AGG,
            "usw",
            "USAGGPRO",
            agg_ports,
            uplink={"type": "wire", "port_idx": 8, "uplink_mac": mac(GW), "uplink_remote_port": 11, "speed": 10000},
            lldp_table=[
                lldp(1, mac(ACC1), "Port 49"),
                lldp(2, mac(ACC1), "Port 50"),
                lldp(3, mac(ACC2), "Port 49"),
                lldp(8, mac(GW), "Port 11"),
            ],
        ),
        dev(
            ACC1,
            "usw",
            "US48PRO",
            acc1_ports,
            uplink={"type": "wire", "port_idx": 49, "uplink_mac": mac(AGG), "uplink_remote_port": 1, "speed": 20000},
            lldp_table=[lldp(49, mac(AGG), "Port 1"), lldp(50, mac(AGG), "Port 2"), lldp(5, mac(AP1), "eth0")],
        ),
        dev(
            ACC2,
            "usw",
            "US48PRO",
            acc2_ports,
            uplink={"type": "wire", "port_idx": 49, "uplink_mac": mac(AGG), "uplink_remote_port": 3, "speed": 10000},
            lldp_table=[lldp(49, mac(AGG), "Port 3"), lldp(10, SERVER_MAC, "eno1")],
        ),
        dev(
            AP1,
            "uap",
            "UAP6MP",
            [leg_port(1, "GE", up=True, speed=2500, is_uplink=True)],
            uplink={"type": "wire", "port_idx": 1, "uplink_mac": mac(ACC1), "uplink_remote_port": 5, "speed": 2500},
            lldp_table=[lldp(1, mac(ACC1), "Port 5")],
        ),
        dev(
            AP2,
            "uap",
            "UAP6MP",
            [leg_port(1, "GE", up=False)],
            uplink={"type": "wireless", "uplink_mac": mac(AP1), "uplink_remote_port": 0, "speed": 866},
        ),
        dev(
            LAB,
            "usw",
            "USMINI",
            [leg_port(i, "GE", up=i == 1, is_uplink=i == 1) for i in range(1, 6)],
            uplink={"type": "wire", "port_idx": 1, "uplink_mac": mac(ACC2), "uplink_remote_port": 12, "speed": 1000},
        ),
    ]
    networkconfs = [
        {
            "_id": NC["default"],
            "name": "Default",
            "purpose": "corporate",
            "attr_hidden_id": "LAN",
            "ip_subnet": "10.0.0.1/24",
            "dhcpd_enabled": True,
            "enabled": True,
        },
        {
            "_id": NC["staff"],
            "name": "Staff",
            "purpose": "corporate",
            "vlan_enabled": True,
            "vlan": 10,
            "ip_subnet": "10.0.10.1/24",
            "dhcpd_enabled": True,
            "enabled": True,
        },
        {
            "_id": NC["voice"],
            "name": "Voice",
            "purpose": "corporate",
            "vlan_enabled": True,
            "vlan": "20",
            "ip_subnet": "10.0.20.1/24",
            "dhcpd_enabled": True,
            "enabled": True,
        },
        {
            "_id": NC["guest"],
            "name": "Guest",
            "purpose": "guest",
            "vlan_enabled": True,
            "vlan": 30,
            "ip_subnet": "10.0.30.1/24",
            "dhcpd_enabled": True,
            "enabled": True,
        },
        {"_id": NC["wan"], "name": "Internet 1", "purpose": "wan", "wan_type": "dhcp", "enabled": True},
    ]
    portconfs = [
        {
            "_id": PC_AP_TRUNK,
            "name": "AP Trunk",
            "native_networkconf_id": NC["default"],
            "tagged_vlan_mgmt": "auto",
            "poe_mode": "auto",
        }
    ]
    wlanconfs = [
        {
            "_id": "67e000000000000000000001",
            "name": "corp",
            "enabled": True,
            "networkconf_id": NC["staff"],
            "security": "wpapsk",
            "wlan_bands": ["2g", "5g"],
        },
        {
            "_id": "67e000000000000000000002",
            "name": "guest",
            "enabled": True,
            "networkconf_id": NC["guest"],
            "security": "open",
            "wlan_bands": ["5g"],
        },
    ]
    clients = [
        {
            "mac": "02:00:00:00:a0:01",
            "hostname": "phone-101",
            "ip": "10.0.20.101",
            "is_wired": True,
            "sw_mac": mac(ACC2),
            "sw_port": 14,
            "vlan": 20,
        },
        {
            "mac": "02:00:00:00:a0:02",
            "hostname": "laptop-7",
            "ip": "10.0.10.57",
            "is_wired": False,
            "ap_mac": mac(AP1),
            "vlan": 10,
        },
        {
            "mac": "02:00:00:00:a0:03",
            "hostname": "printer",
            "ip": "10.0.10.80",
            "is_wired": True,
            "sw_mac": mac(ACC1),
            "sw_port": 8,
            "vlan": 10,
        },
    ]
    return {
        "stat_device.json": envelope(devices),
        "rest_portconf.json": envelope(portconfs),
        "rest_networkconf.json": envelope(networkconfs),
        "rest_wlanconf.json": envelope(wlanconfs),
        "stat_sta.json": envelope(clients),
        "refused.json": {"meta": {"rc": "error", "msg": "api.err.LoginRequired"}, "data": []},
    }


def main() -> None:
    for sub, files in (("official", official()), ("legacy", legacy())):
        out = HERE / sub
        out.mkdir(parents=True, exist_ok=True)
        for name, body in files.items():
            (out / name).write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
