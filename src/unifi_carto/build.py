"""Raw snapshots -> Inventory. Pure: no I/O, no clock, deterministic output.

Field sources (field names are facts; no code was copied):

Official (Integration API v1), from the OpenAPI document for Network 10.6.106
(opastorello/unifi-api-docs network/v10.6.106/openapi.json + reference.md):
  device: id, macAddress, name, model, ipAddress, firmwareVersion, state, features (list of names in
  list items, object keyed by name in details), interfaces.ports[] {idx, connector, speedMbps,
  maxSpeedMbps, state, poe {enabled, standard, state}}, interfaces.radios[] {frequencyGHz, channel,
  channelWidthMHz, wlanStandard}, uplink.deviceId, features.switching.lags[] {id, portIdxs};
  site: id, name, internalReference; network: id, name, vlanId, enabled, management, default;
  wifi broadcast: id, name, enabled, network {type NATIVE|SPECIFIC, networkId}, securityConfiguration.type,
  broadcastingFrequenciesGHz; client: id, macAddress, name, ipAddress, type, uplinkDeviceId;
  info: applicationVersion.
  switching/lags {id, type LOCAL|MULTI_CHASSIS|SWITCH_STACK, members[] {deviceId, portIdxs}};
  mc-lag-domains {id, name, lags[], peers[] {deviceId, linkPorts, role}};
  switch-stacks {id, name, lags[], members[]}; wans {id, name}:
  from unpoller/unifi types.go (LAG, LAGMember, MCLAGDomain, MCLAGPeer, SwitchStack, IntegrationWAN).
  unpoller names the LAG member port list ``portIndexes``; the OpenAPI/MODEL.md name is ``portIdxs``.
  Both are accepted.

Legacy (stat/device, rest/*, stat/sta), undocumented; every field is treated as optional:
  stat/device: mac, name, model, type, ip, version, state, port_table, uplink: unpoller/unifi
  (usw.go, udm.go, uap.go). port_table[]: port_idx, name, media, speed, up, enable, is_uplink,
  poe_enable, poe_mode, port_poe, sfp_found, sfp_vendor, sfp_part, sfp_serial, portconf_id,
  aggregated_by: unpoller/unifi types.go (Port). uplink: uplink_mac, uplink_remote_port, type, speed:
  unpoller/unifi uap.go (Uplink); uplink.port_idx: enuno/unifi-mcp-server src/tools/topology.py.
  uplink.uplink_device_name, lldp_table[] {local_port_idx, chassis_id, port_id, is_wired},
  port_table[].native_networkconf_id / tagged_vlan_mgmt / excluded_networkconf_ids / lag_member,
  and port_overrides[] (same keys, keyed by port_idx): docs/MODEL.md and the W2 brief; not present in
  the cited Go structs, so unverified until real-console fixtures land.
  rest/portconf {_id, name, native_networkconf_id, tagged_vlan_mgmt, excluded_networkconf_ids},
  rest/networkconf {_id, name, purpose, vlan, ip_subnet, dhcpd_enabled, enabled},
  rest/wlanconf {_id, name, enabled, networkconf_id, security, wlan_bands},
  stat/sta {mac, name, hostname, ip, is_wired, sw_mac, sw_port, vlan}: docs/MODEL.md and
  enuno/unifi-mcp-server topology.py (sw_port).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from unifi_carto.model import (
    Client,
    CollectionWarning,
    Console,
    Device,
    ExternalPeer,
    Inventory,
    Lag,
    LagMember,
    Link,
    LinkEnd,
    Network,
    PoE,
    Port,
    Radio,
    Sfp,
    Site,
    Source,
    Wan,
    Wlan,
    device_key,
    normalize_mac,
)
from unifi_carto.raw import JSON, LegacySnapshot, OfficialSnapshot
from unifi_carto.scrub import drop_key

_PARSE_ERRORS = (ValueError, TypeError, KeyError, AttributeError)
_LINK_RANK = {Source.LEGACY: 0, Source.LLDP: 1, Source.OFFICIAL: 2, Source.INFERRED: 3}
_NON_LAN_PURPOSES = frozenset({"wan", "remote-user-vpn", "site-vpn", "vpn-client"})
_LAG_KINDS: dict[str, Literal["local", "multi_chassis", "switch_stack"]] = {
    "LOCAL": "local",
    "MULTI_CHASSIS": "multi_chassis",
    "SWITCH_STACK": "switch_stack",
}
_LEGACY_ROLES: dict[str, Literal["gateway", "switch", "access_point"]] = {
    "ugw": "gateway",
    "udm": "gateway",
    "uxg": "gateway",
    "usw": "switch",
    "uap": "access_point",
}
_LEGACY_BANDS = {"2g": "2.4", "5g": "5", "6g": "6"}
_LLDP_PORT = re.compile(r"(?:port\s*)?(\d+)", re.IGNORECASE)
# Wan.raw keeps only these non-secret official WAN keys (SPEC A2.6); anything else may carry credentials.
_WAN_RAW_KEYS = frozenset(
    {
        "id",
        "name",
        "type",
        "wanType",
        "interface",
        "interfaceName",
        "networkGroup",
        "role",
        "priority",
        "failoverPriority",
        "loadBalanceWeight",
        "weight",
        "enabled",
        "state",
        "status",
        "mode",
        "vlanId",
        "vlan",
        "mtu",
        "ipAddress",
        "subnet",
        "gateway",
        "dns",
        "dnsServers",
        "ipConfiguration",
        "ipv4",
        "ipv4Configuration",
        "ipv6",
        "ipv6Configuration",
    }
)
# Nested keys dropped from allowed WAN values on top of ``scrub.drop_key`` (PPPoE/RADIUS logins).
_WAN_CREDENTIAL_WORDS = ("user", "login", "auth", "credential", "radius")


def build_inventory(
    official: Sequence[OfficialSnapshot],
    legacy: Mapping[tuple[str, str], LegacySnapshot],
    *,
    collector_version: str,
    generated_at: datetime,
    include_clients: bool,
    extra_warnings: Sequence[CollectionWarning] = (),
) -> Inventory:
    """Merge official snapshots and (optional) legacy snapshots keyed by ``(console_id, official_site_id)``."""
    consoles: dict[str, Console] = {}
    sites: dict[str, Site] = {}
    out = _Out()
    warnings: list[CollectionWarning] = []

    for snap in official:
        ref = snap.console
        leg = None
        site_id = _str(snap.site.get("id"))
        if site_id is not None:
            leg = legacy.get((ref.id, site_id))
        console = consoles.get(ref.id)
        if console is None:
            console = consoles[ref.id] = Console(
                id=ref.id,
                name=ref.name,
                access=ref.access,
                local_url=ref.local_url,
                model=ref.model,
                firmware=ref.firmware,
                network_version=snap.app_version,
            )
        if console.network_version is None:
            console.network_version = snap.app_version
        if leg is not None and console.legacy != "api_key":
            console.legacy = leg.auth

        warnings.extend(snap.warnings)
        if leg is not None:
            warnings.extend(leg.warnings)
        if site_id is None:
            warnings.append(_warning("parse_error", "official site without id skipped", ref.id, None))
            continue
        sites[site_id] = Site(
            id=site_id,
            console_id=ref.id,
            name=_str(snap.site.get("name")) or site_id,
            internal_reference=_str(snap.site.get("internalReference")),
        )
        builder = _SiteBuilder(snap, leg, site_id, include_clients)
        builder.build()
        out.extend(builder.out)
        warnings.extend(builder.warnings)

    warnings.extend(extra_warnings)
    return Inventory(
        generated_at=generated_at,
        collector_version=collector_version,
        consoles=sorted(consoles.values(), key=lambda c: c.id),
        sites=sorted(sites.values(), key=lambda s: s.id),
        devices=sorted(out.devices, key=lambda d: d.id),
        links=sorted(out.links, key=lambda x: x.id),
        lags=sorted(out.lags, key=lambda x: x.id),
        networks=sorted(out.networks, key=lambda n: n.id),
        wlans=sorted(out.wlans, key=lambda w: w.id),
        wans=sorted(out.wans, key=lambda w: w.id),
        clients=sorted(out.clients, key=lambda c: c.id) if include_clients else None,
        warnings=warnings,
    )


@dataclass
class _Out:
    devices: list[Device] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    lags: list[Lag] = field(default_factory=list)
    networks: list[Network] = field(default_factory=list)
    wlans: list[Wlan] = field(default_factory=list)
    wans: list[Wan] = field(default_factory=list)
    clients: list[Client] = field(default_factory=list)

    def extend(self, other: _Out) -> None:
        self.devices += other.devices
        self.links += other.links
        self.lags += other.lags
        self.networks += other.networks
        self.wlans += other.wlans
        self.wans += other.wans
        self.clients += other.clients


@dataclass
class _Uplink:
    """A device's resolved uplink before it becomes a Link."""

    parent_id: str | None
    external: ExternalPeer | None
    port_idx: int | None
    remote_port: int | None
    kind: Literal["wired", "wireless", "mesh", "unknown"]
    speed: int | None
    source: Source


class _SiteBuilder:
    def __init__(self, snap: OfficialSnapshot, leg: LegacySnapshot | None, site_id: str, include_clients: bool) -> None:
        self.snap = snap
        self.leg = leg
        self.site_id = site_id
        self.include_clients = include_clients
        self.out = _Out()
        self.warnings: list[CollectionWarning] = []
        self.devices: dict[str, Device] = {}  # id -> device
        self.by_mac: dict[str, str] = {}  # mac -> device id
        self.ports: dict[str, dict[int, Port]] = {}  # device id -> idx -> port
        self.legacy_dev: dict[str, JSON] = {}  # device id -> legacy stat/device record
        # legacy networkconf _id -> (vlan or None, model network id once known)
        self.nc_vlan: dict[str, int | None] = {}
        self.nc_model_id: dict[str, str] = {}
        self.portconfs: dict[str, JSON] = {}

    # ---- orchestration -------------------------------------------------------------------------

    def build(self) -> None:
        self._index_legacy_config()
        self._official_devices()
        if self.leg is not None:
            self._legacy_devices()
        self._lags()
        self._links()
        self._networks()
        self._wlans()
        self._wans()
        if self.include_clients:
            self._clients()
        for dev in self.devices.values():
            dev.ports = sorted(self.ports.get(dev.id, {}).values(), key=lambda p: p.idx)
        self.out.devices = list(self.devices.values())

    def _warn(self, code: str, message: str) -> None:
        self.warnings.append(_warning(code, message, self.snap.console.id, self.site_id))

    # ---- devices -------------------------------------------------------------------------------

    def _official_devices(self) -> None:
        merged: dict[str, JSON] = {}
        for item in self.snap.devices:
            dev_id = _str(item.get("id"))
            if dev_id is not None:
                merged[dev_id] = dict(item)
        for dev_id, detail in self.snap.device_details.items():
            merged[dev_id] = {**merged.get(dev_id, {}), **detail}
        for dev_id in sorted(merged):
            obj = merged[dev_id]
            mac = _mac(obj.get("macAddress"))
            if mac is None:
                self._warn("parse_error", f"official device {dev_id} has no usable macAddress; skipped")
                continue
            uplink = _dict(obj.get("uplink"))
            dev = Device(
                id=device_key(dev_id, mac),
                site_id=self.site_id,
                mac=mac,
                name=_str(obj.get("name")),
                model=_str(obj.get("model")),
                role=_official_role(obj.get("features")),
                ip=_str(obj.get("ipAddress")),
                firmware=_str(obj.get("firmwareVersion")),
                state=_str(obj.get("state")),
                uplink_device_id=_str(uplink.get("deviceId")),
                sources=[Source.OFFICIAL],
            )
            interfaces = _dict(obj.get("interfaces"))
            ports: dict[int, Port] = {}
            for raw in _dicts(interfaces.get("ports")):
                port = _official_port(raw)
                if port is not None:
                    ports[port.idx] = port
            for lag in _dicts(_dict(_dict(obj.get("features")).get("switching")).get("lags")):
                lag_id = _str(lag.get("id"))
                for idx in _port_idxs(lag):
                    if lag_id is not None and idx in ports:
                        ports[idx].lag_id = lag_id
            dev.radios = [_official_radio(r) for r in _dicts(interfaces.get("radios"))]
            self._add_device(dev, ports)

    def _add_device(self, dev: Device, ports: dict[int, Port]) -> None:
        self.devices[dev.id] = dev
        self.by_mac[dev.mac] = dev.id
        self.ports[dev.id] = ports

    def _legacy_devices(self) -> None:
        assert self.leg is not None
        records: list[tuple[str, JSON]] = []
        for rec in self.leg.devices:
            mac = _mac(rec.get("mac"))
            if mac is None:
                self._warn("parse_error", "legacy device without a usable mac skipped")
                continue
            records.append((mac, rec))
        for mac, rec in sorted(records, key=lambda r: r[0]):
            try:
                self._legacy_device(mac, rec)
            except _PARSE_ERRORS as exc:
                self._warn("parse_error", f"legacy device {mac}: {type(exc).__name__}; partially enriched")

    def _legacy_device(self, mac: str, rec: JSON) -> None:
        dev_id = self.by_mac.get(mac)
        if dev_id is None:
            dev = Device(
                id=device_key(None, mac),
                site_id=self.site_id,
                mac=mac,
                role=_LEGACY_ROLES.get(_str(rec.get("type")) or "", "other"),
                state=_legacy_state(rec.get("state")),
            )
            self._add_device(dev, {})
        else:
            dev = self.devices[dev_id]
        dev.sources = _with(dev.sources, Source.LEGACY)
        self.legacy_dev[dev.id] = rec
        if dev.name is None:
            dev.name = _str(rec.get("name"))
        if dev.model is None:
            dev.model = _str(rec.get("model")) or _str(rec.get("model_name"))
        if dev.ip is None:
            dev.ip = _str(rec.get("ip"))
        if dev.firmware is None:
            dev.firmware = _str(rec.get("version"))
        if dev.role == "other":
            dev.role = _LEGACY_ROLES.get(_str(rec.get("type")) or "", "other")

        overrides = {idx: o for o in _dicts(rec.get("port_overrides")) if (idx := _int(o.get("port_idx"))) is not None}
        ports = self.ports[dev.id]
        for raw in _dicts(rec.get("port_table")):
            idx = _int(raw.get("port_idx"))
            if idx is None or idx < 1:
                self._warn("parse_error", f"legacy device {mac}: port without a valid port_idx skipped")
                continue
            port = ports.get(idx)
            if port is None:
                port = ports[idx] = Port(idx=idx)
            try:
                self._legacy_port(port, raw, overrides.get(idx, {}))
            except _PARSE_ERRORS as exc:
                self._warn("parse_error", f"legacy device {mac} port {idx}: {type(exc).__name__}")

    def _legacy_port(self, port: Port, raw: JSON, override: JSON) -> None:
        port.sources = _with(port.sources, Source.LEGACY)
        port.name = _str(raw.get("name")) or port.name
        port.media = _str(raw.get("media")) or port.media
        if (enable := _bool(raw.get("enable"))) is not None:
            port.enabled = enable
        if (is_uplink := _bool(raw.get("is_uplink"))) is not None:
            port.is_uplink = is_uplink
        if port.state == "unknown" and (up := _bool(raw.get("up"))) is not None:
            port.state = "up" if up else "down"
        if port.speed_mbps is None and port.state == "up":
            port.speed_mbps = _int(raw.get("speed"))
        if port.poe is None and (_bool(raw.get("port_poe")) or "poe_enable" in raw):
            port.poe = PoE(enabled=_bool(raw.get("poe_enable")))
        if any(raw.get(k) for k in ("sfp_vendor", "sfp_part", "sfp_serial")) or _bool(raw.get("sfp_found")):
            port.sfp = Sfp(
                vendor=_str(raw.get("sfp_vendor")),
                part=_str(raw.get("sfp_part")),
                serial=_str(raw.get("sfp_serial")),
            )
        self._resolve_vlans(port, {**raw, **{k: v for k, v in override.items() if v is not None}})

    def _resolve_vlans(self, port: Port, raw: JSON) -> None:
        """Port-level native/tagged settings override the assigned port profile's."""
        profile = self.portconfs.get(_str(raw.get("portconf_id")) or "")
        if profile is not None:
            port.profile = _str(profile.get("name"))

        def pick(key: str) -> Any:
            if raw.get(key) is not None:
                return raw.get(key)
            return profile.get(key) if profile is not None else None

        native_id = _str(pick("native_networkconf_id"))
        if native_id is not None and native_id in self.nc_vlan:
            port.native_vlan = self.nc_vlan[native_id]
        mode = _str(pick("tagged_vlan_mgmt"))
        if mode == "auto":
            port.tagged_vlans = "all"
        elif mode == "block_all":
            port.tagged_vlans = []
        elif mode == "custom":
            excluded = {str(x) for x in _list(pick("excluded_networkconf_ids"))}
            vlans = {v for nc_id, v in self.nc_vlan.items() if v is not None and nc_id not in excluded}
            if port.native_vlan is not None:
                vlans.discard(port.native_vlan)  # the untagged VLAN is not also tagged
            port.tagged_vlans = sorted(vlans)

    # ---- LAGs ----------------------------------------------------------------------------------

    def _lags(self) -> None:
        seen: set[str] = set()
        for raw in self.snap.lags:
            lag_id = _str(raw.get("id"))
            kind = _LAG_KINDS.get(_str(raw.get("type")) or "")
            if lag_id is None or kind is None:
                self._warn("parse_error", f"official LAG {lag_id or '?'} without id or known type skipped")
                continue
            members = [
                LagMember(device_id=dev_id, port_idxs=sorted(_port_idxs(m)))
                for m in _dicts(raw.get("members"))
                if (dev_id := _str(m.get("deviceId"))) is not None
            ]
            self._add_lag(Lag(id=lag_id, site_id=self.site_id, kind=kind, members=members))
            seen.add(lag_id)
        for raw in self.snap.mc_lag_domains:
            dom_id = _str(raw.get("id"))
            if dom_id is None or dom_id in seen:
                continue
            members = [
                LagMember(device_id=dev_id, port_idxs=sorted(_ints(p.get("linkPorts"))))
                for p in _dicts(raw.get("peers"))
                if (dev_id := _str(p.get("deviceId"))) is not None
            ]
            self.out.lags.append(Lag(id=dom_id, site_id=self.site_id, kind="multi_chassis", members=members))
            seen.add(dom_id)
        for raw in self.snap.switch_stacks:
            stack_id = _str(raw.get("id"))
            if stack_id is None or stack_id in seen:
                continue
            members = [
                LagMember(device_id=dev_id, port_idxs=[])
                for m in _list(raw.get("members"))
                if (dev_id := _str(m) or _str(_dict(m).get("deviceId"))) is not None
            ]
            self.out.lags.append(Lag(id=stack_id, site_id=self.site_id, kind="switch_stack", members=members))
            seen.add(stack_id)
        self._legacy_lags()

    def _add_lag(self, lag: Lag) -> None:
        self.out.lags.append(lag)
        for member in lag.members:
            ports = self.ports.get(member.device_id, {})
            for idx in member.port_idxs:
                if idx in ports:
                    ports[idx].lag_id = lag.id

    def _legacy_lags(self) -> None:
        """LAGs the legacy port table shows but no official LAG covers (older Network versions)."""
        for dev_id in sorted(self.legacy_dev):
            groups: dict[int, list[int]] = {}
            for raw in _dicts(self.legacy_dev[dev_id].get("port_table")):
                idx = _int(raw.get("port_idx"))
                if idx is None:
                    continue
                master = _int(raw.get("aggregated_by"))
                if master is None and _bool(raw.get("lag_member")):
                    master = idx
                if master is not None and master >= 1:
                    groups.setdefault(master, []).append(idx)
            ports = self.ports.get(dev_id, {})
            for master, idxs in sorted(groups.items()):
                if any(ports[i].lag_id for i in idxs if i in ports):
                    continue
                lag = Lag(
                    id=f"{dev_id}:lag{master}",
                    site_id=self.site_id,
                    kind="local",
                    members=[LagMember(device_id=dev_id, port_idxs=sorted(set(idxs) | {master}))],
                )
                self._add_lag(lag)

    # ---- links ---------------------------------------------------------------------------------

    def _links(self) -> None:
        links: list[Link] = []
        for dev_id in sorted(self.devices):
            up = self._uplink(self.devices[dev_id])
            if up is None:
                continue
            b = LinkEnd(device_id=up.parent_id, port_idx=up.remote_port, external=up.external)
            links.append(
                Link(
                    id="",
                    site_id=self.site_id,
                    a=LinkEnd(device_id=dev_id, port_idx=up.port_idx),
                    b=b,
                    kind=up.kind,
                    speed_mbps=up.speed,
                    source=up.source,
                )
            )
        links = self._lldp_links(links)
        by_id: dict[str, Link] = {}
        for link in links:
            link.lag_id = self._port_lag(link.a) or self._port_lag(link.b)
            link.id = _link_id(link)
            prev = by_id.get(link.id)
            if prev is None or _LINK_RANK[link.source] < _LINK_RANK[prev.source]:
                by_id[link.id] = link
        self.out.links = list(by_id.values())

    def _uplink(self, dev: Device) -> _Uplink | None:
        official_parent = dev.uplink_device_id if dev.uplink_device_id in self.devices else None
        rec = self.legacy_dev.get(dev.id)
        up = _dict(rec.get("uplink")) if rec is not None else {}
        parent: str | None = None
        external: ExternalPeer | None = None
        source = Source.LEGACY
        uplink_mac = _mac(up.get("uplink_mac"))
        if uplink_mac is not None:
            parent = self.by_mac.get(uplink_mac)
            if parent is None and _str(up.get("uplink_device_name")) is None:
                external = ExternalPeer(chassis_id=uplink_mac)
        if parent is None and external is None and (name := _str(up.get("uplink_device_name"))) is not None:
            matches = sorted(d.id for d in self.devices.values() if d.name == name and d.id != dev.id)
            if len(matches) == 1:
                parent, source = matches[0], Source.INFERRED
            elif uplink_mac is not None:
                external = ExternalPeer(chassis_id=uplink_mac, system_name=name)
        if parent == dev.id:
            parent = None

        if parent is None and external is None:
            if official_parent is None:
                return None
            return _Uplink(official_parent, None, None, None, "unknown", None, Source.OFFICIAL)

        if parent is not None:
            if official_parent is not None and official_parent != parent:
                self._warn(
                    "uplink_conflict",
                    f"device {dev.id}: official uplink {official_parent} disagrees with legacy uplink {parent}; "
                    "using legacy",
                )
            dev.uplink_device_id = parent
        utype = _str(up.get("type"))
        kind: Literal["wired", "wireless", "mesh", "unknown"] = "unknown"
        if utype == "wire":
            kind = "wired"
        elif utype == "wireless":
            parent_dev = self.devices.get(parent) if parent is not None else None
            is_mesh = dev.role == "access_point" and parent_dev is not None and parent_dev.role == "access_point"
            kind = "mesh" if is_mesh else "wireless"
        wired = kind in ("wired", "unknown")
        return _Uplink(
            parent_id=parent,
            external=external,
            port_idx=_pos(up.get("port_idx")) if wired else None,
            remote_port=_pos(up.get("uplink_remote_port")) if wired else None,
            kind=kind,
            speed=_int(up.get("speed")),
            source=source,
        )

    def _lldp_links(self, links: list[Link]) -> list[Link]:
        def covering(dev_id: str, idx: int) -> Link | None:
            for link in links:
                for end in (link.a, link.b):
                    if end.device_id == dev_id and end.port_idx == idx:
                        return link
            return None

        for dev_id in sorted(self.legacy_dev):
            entries: list[tuple[int, JSON]] = []
            for raw in _dicts(self.legacy_dev[dev_id].get("lldp_table")):
                idx = _pos(raw.get("local_port_idx"))
                if idx is not None and _bool(raw.get("is_wired")) is not False:
                    entries.append((idx, raw))
            for idx, raw in sorted(entries, key=lambda e: e[0]):
                chassis = _str(raw.get("chassis_id"))
                if chassis is None:
                    continue
                remote = self.by_mac.get(_mac(chassis) or "")
                if remote == dev_id:
                    continue
                remote_port = _lldp_port(raw.get("port_id"))
                existing = covering(dev_id, idx)
                if remote is None:
                    if existing is None:
                        peer = ExternalPeer(chassis_id=_mac(chassis) or chassis, port_id=_str(raw.get("port_id")))
                        links.append(
                            Link(
                                id="",
                                site_id=self.site_id,
                                a=LinkEnd(device_id=dev_id, port_idx=idx),
                                b=LinkEnd(external=peer),
                                kind="wired",
                                source=Source.LLDP,
                            )
                        )
                    continue
                if existing is not None:
                    _fill_end(existing, remote, remote_port)
                    continue
                if remote_port is not None and (existing := covering(remote, remote_port)) is not None:
                    _fill_end(existing, dev_id, idx)
                    continue
                portless = self._portless_link(links, dev_id, remote, remote_port)
                if portless is not None:
                    _fill_end(portless, dev_id, idx)
                    _fill_end(portless, remote, remote_port)
                    if _LINK_RANK[Source.LLDP] < _LINK_RANK[portless.source]:
                        portless.source = Source.LLDP
                    continue
                local_end = LinkEnd(device_id=dev_id, port_idx=idx)
                remote_end = LinkEnd(device_id=remote, port_idx=remote_port)
                upstream_is_local = self.devices[remote].uplink_device_id == dev_id
                links.append(
                    Link(
                        id="",
                        site_id=self.site_id,
                        a=remote_end if upstream_is_local else local_end,
                        b=local_end if upstream_is_local else remote_end,
                        kind="wired",
                        source=Source.LLDP,
                    )
                )
        return links

    @staticmethod
    def _portless_link(links: list[Link], dev_id: str, remote: str, remote_port: int | None) -> Link | None:
        """A link between the two devices that does not yet know the local port (e.g. official-only).

        With ``remote_port`` unknown (unparseable LLDP port id) any remote port matches: the cable
        seen from the other end with only its own port known is the same cable (BLD-01).
        """
        for link in links:
            ends = {link.a.device_id: link.a, link.b.device_id: link.b}
            if (
                dev_id in ends
                and remote in ends
                and ends[dev_id].port_idx is None
                and (remote_port is None or ends[remote].port_idx in (None, remote_port))
            ):
                return link
        return None

    def _port_lag(self, end: LinkEnd) -> str | None:
        if end.device_id is None or end.port_idx is None:
            return None
        port = self.ports.get(end.device_id, {}).get(end.port_idx)
        return port.lag_id if port is not None else None

    # ---- networks, WLANs, WANs -----------------------------------------------------------------

    def _index_legacy_config(self) -> None:
        if self.leg is None:
            return
        for pc in self.leg.portconfs:
            if (pc_id := _str(pc.get("_id"))) is not None:
                self.portconfs[pc_id] = pc
        for nc in self.leg.networkconfs:
            if (nc_id := _str(nc.get("_id"))) is not None:
                self.nc_vlan[nc_id] = _legacy_vlan(nc)

    def _networks(self) -> None:
        legacy = sorted(
            (nc for nc in (self.leg.networkconfs if self.leg else []) if _str(nc.get("_id")) is not None),
            key=lambda nc: str(nc["_id"]),
        )
        used: set[str] = set()
        for raw in sorted(self.snap.networks, key=lambda n: str(n.get("id", ""))):
            net_id = _str(raw.get("id"))
            name = _str(raw.get("name"))
            if net_id is None or name is None:
                self._warn("parse_error", f"official network {net_id or '?'} without id or name skipped")
                continue
            vlan = _int(raw.get("vlanId"))
            if vlan is None and _bool(raw.get("default")):
                vlan = 1
            management = _str(raw.get("management"))
            net = Network(
                id=net_id,
                site_id=self.site_id,
                name=name,
                vlan_id=vlan,
                purpose=management.lower() if management else None,
                enabled=_bool(raw.get("enabled")),
                sources=[Source.OFFICIAL],
            )
            for nc in legacy:
                nc_id = str(nc["_id"])
                if nc_id in used or _str(nc.get("name")) != name:
                    continue
                if self.nc_vlan.get(nc_id) != (vlan if vlan is not None else 1):
                    continue
                used.add(nc_id)
                self.nc_model_id[nc_id] = net_id
                _enrich_network(net, nc)
                break
            self.out.networks.append(net)
        for nc in legacy:
            nc_id = str(nc["_id"])
            name = _str(nc.get("name"))
            if nc_id in used or name is None:
                continue
            net = Network(
                id=f"legacy:{nc_id}",
                site_id=self.site_id,
                name=name,
                vlan_id=self.nc_vlan.get(nc_id),
                enabled=_bool(nc.get("enabled")),
            )
            _enrich_network(net, nc)
            self.nc_model_id[nc_id] = net.id
            self.out.networks.append(net)

    def _wlans(self) -> None:
        default_net = next(
            (_str(n.get("id")) for n in self.snap.networks if _bool(n.get("default"))),
            None,
        )
        legacy = sorted(
            (w for w in (self.leg.wlanconfs if self.leg else []) if _str(w.get("_id")) is not None),
            key=lambda w: str(w["_id"]),
        )
        used: set[str] = set()
        for raw in sorted(self.snap.wlans, key=lambda w: str(w.get("id", ""))):
            wlan_id = _str(raw.get("id"))
            name = _str(raw.get("name"))
            if wlan_id is None or name is None:
                self._warn("parse_error", f"official WiFi broadcast {wlan_id or '?'} without id or name skipped")
                continue
            network = raw.get("network")
            if isinstance(network, dict):
                network_id = _str(network.get("networkId")) or _str(network.get("id"))
                if network_id is None and network.get("type") == "NATIVE":
                    network_id = default_net
            else:
                network_id = _str(network)
            freqs = [_band(f) for f in _list(raw.get("broadcastingFrequenciesGHz"))]
            wlan = Wlan(
                id=wlan_id,
                site_id=self.site_id,
                name=name,
                enabled=_bool(raw.get("enabled")),
                network_id=network_id,
                security=_str(_dict(raw.get("securityConfiguration")).get("type")),
                bands=sorted(f for f in freqs if f is not None) or None,
                sources=[Source.OFFICIAL],
            )
            for wc in legacy:
                wc_id = str(wc["_id"])
                if wc_id not in used and _str(wc.get("name")) == name:
                    used.add(wc_id)
                    self._enrich_wlan(wlan, wc)
                    break
            self.out.wlans.append(wlan)
        for wc in legacy:
            wc_id = str(wc["_id"])
            name = _str(wc.get("name"))
            if wc_id in used or name is None:
                continue
            wlan = Wlan(id=f"legacy:{wc_id}", site_id=self.site_id, name=name)
            self._enrich_wlan(wlan, wc)
            self.out.wlans.append(wlan)

    def _enrich_wlan(self, wlan: Wlan, wc: JSON) -> None:
        wlan.sources = _with(wlan.sources, Source.LEGACY)
        if wlan.enabled is None:
            wlan.enabled = _bool(wc.get("enabled"))
        if wlan.network_id is None:
            wlan.network_id = self.nc_model_id.get(_str(wc.get("networkconf_id")) or "")
        if wlan.security is None:
            wlan.security = _str(wc.get("security"))
        if wlan.bands is None:
            bands = [_LEGACY_BANDS.get(str(b), str(b)) for b in _list(wc.get("wlan_bands"))]
            wlan.bands = sorted(bands) or None

    def _wans(self) -> None:
        for raw in self.snap.wans:
            wan_id = _str(raw.get("id"))
            if wan_id is None:
                self._warn("parse_error", "official WAN without id skipped")
                continue
            kept = {k: _wan_value(v) for k, v in raw.items() if k in _WAN_RAW_KEYS and not _wan_secret(k)}
            self.out.wans.append(Wan(id=wan_id, site_id=self.site_id, name=_str(raw.get("name")), raw=kept))

    # ---- clients -------------------------------------------------------------------------------

    def _clients(self) -> None:
        clients: dict[str, Client] = {}  # mac -> client
        for raw in self.snap.clients or []:
            mac = _mac(raw.get("macAddress"))
            client_id = _str(raw.get("id"))
            if mac is None or client_id is None:
                continue  # VPN/teleport clients may have no MAC
            uplink = _str(raw.get("uplinkDeviceId"))
            clients[mac] = Client(
                id=client_id,
                site_id=self.site_id,
                mac=mac,
                name=_str(raw.get("name")),
                ip=_str(raw.get("ipAddress")),
                type=_str(raw.get("type")),
                uplink_device_id=uplink,
                sources=[Source.OFFICIAL],
            )
        for raw in (self.leg.clients if self.leg else None) or []:
            mac = _mac(raw.get("mac"))
            if mac is None:
                self._warn("parse_error", "legacy client without a usable mac skipped")
                continue
            client = clients.get(mac)
            if client is None:
                client = clients[mac] = Client(id=f"mac:{mac}", site_id=self.site_id, mac=mac)
            client.sources = _with(client.sources, Source.LEGACY)
            client.name = client.name or _str(raw.get("name")) or _str(raw.get("hostname"))
            client.ip = client.ip or _str(raw.get("ip"))
            if client.type is None and (wired := _bool(raw.get("is_wired"))) is not None:
                client.type = "WIRED" if wired else "WIRELESS"
            sw = self.by_mac.get(_mac(raw.get("sw_mac")) or "")
            if client.uplink_device_id is None:
                client.uplink_device_id = sw or self.by_mac.get(_mac(raw.get("ap_mac")) or "")
            client.switch_port = _pos(raw.get("sw_port"))
            client.vlan = _int(raw.get("vlan"))
        self.out.clients = list(clients.values())


# ---- helpers ------------------------------------------------------------------------------------


def _with(sources: list[Source], extra: Source) -> list[Source]:
    """Sorted, unique sources (the model validator does not run on attribute assignment)."""
    return sorted({*sources, extra}, key=lambda s: s.value)


def _warning(code: str, message: str, console: str | None, site: str | None) -> CollectionWarning:
    return CollectionWarning(code=code, message=message, console=console, site=site)


def _link_id(link: Link) -> str:
    def end(e: LinkEnd) -> str:
        ident = e.device_id if e.device_id is not None else f"ext:{e.external.chassis_id if e.external else '?'}"
        return f"{ident}:{e.port_idx if e.port_idx is not None else '?'}"

    return f"{end(link.a)}-{end(link.b)}"


def _wan_secret(key: str) -> bool:
    return drop_key(key) or any(w in key.lower() for w in _WAN_CREDENTIAL_WORDS)


def _wan_value(value: Any) -> Any:
    """Copy an allowed WAN value, dropping secret-looking keys at every depth."""
    if isinstance(value, dict):
        return {k: _wan_value(v) for k, v in value.items() if not (isinstance(k, str) and _wan_secret(k))}
    if isinstance(value, list):
        return [_wan_value(v) for v in value]
    return value


def _fill_end(link: Link, dev_id: str, port_idx: int | None) -> None:
    for end in (link.a, link.b):
        if end.device_id == dev_id and end.port_idx is None and port_idx is not None:
            end.port_idx = port_idx


def _official_role(features: Any) -> Literal["gateway", "switch", "access_point", "other"]:
    names = set(features) if isinstance(features, dict | list) else set()
    if "gateway" in names:
        return "gateway"
    if "switching" in names:
        return "switch"
    if "accessPoint" in names:
        return "access_point"
    return "other"


def _official_port(raw: JSON) -> Port | None:
    idx = _pos(raw.get("idx"))
    if idx is None:
        return None
    state = (_str(raw.get("state")) or "").lower()
    poe_raw = raw.get("poe")
    poe = None
    if isinstance(poe_raw, dict):
        poe = PoE(
            enabled=_bool(poe_raw.get("enabled")),
            standard=_str(poe_raw.get("standard")),
            state=_str(poe_raw.get("state")),
        )
    return Port(
        idx=idx,
        connector=_str(raw.get("connector")),
        speed_mbps=_int(raw.get("speedMbps")),
        max_speed_mbps=_int(raw.get("maxSpeedMbps")),
        state=state if state in ("up", "down") else "unknown",
        poe=poe,
        sources=[Source.OFFICIAL],
    )


def _official_radio(raw: JSON) -> Radio:
    freq = raw.get("frequencyGHz")
    return Radio(
        frequency_ghz=float(freq) if isinstance(freq, int | float) and not isinstance(freq, bool) else None,
        channel=_int(raw.get("channel")),
        channel_width_mhz=_int(raw.get("channelWidthMHz")),
        wlan_standard=_str(raw.get("wlanStandard")),
    )


def _legacy_state(value: Any) -> str | None:
    n = _int(value)
    if n == 1:
        return "ONLINE"
    if n == 0:
        return "OFFLINE"
    return str(value) if value is not None else None


def _legacy_vlan(nc: JSON) -> int | None:
    """VLAN of a legacy networkconf: ``vlan`` when set; untagged LAN (the default network) is VLAN 1."""
    if (_str(nc.get("purpose")) or "") in _NON_LAN_PURPOSES:
        return None
    vlan = _int(nc.get("vlan"))
    if vlan is not None and _bool(nc.get("vlan_enabled")) is not False:
        return vlan
    return 1


def _enrich_network(net: Network, nc: JSON) -> None:
    net.sources = _with(net.sources, Source.LEGACY)
    legacy_purpose = _str(nc.get("purpose"))
    if legacy_purpose is not None:
        net.purpose = legacy_purpose
    net.subnet = _str(nc.get("ip_subnet"))
    net.dhcp_enabled = _bool(nc.get("dhcpd_enabled"))
    if net.enabled is None:
        net.enabled = _bool(nc.get("enabled"))


def _band(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return f"{value:g}"


def _lldp_port(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value >= 1 else None
    if isinstance(value, str) and (m := _LLDP_PORT.fullmatch(value.strip())):
        return int(m.group(1)) or None
    return None


def _port_idxs(member: JSON) -> list[int]:
    return _ints(member.get("portIdxs", member.get("portIndexes")))


def _str(value: Any) -> str | None:
    if isinstance(value, str):
        return value or None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _pos(value: Any) -> int | None:
    n = _int(value)
    return n if n is not None and n >= 1 else None


def _ints(value: Any) -> list[int]:
    return [n for v in _list(value) if (n := _pos(v)) is not None]


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def _mac(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return normalize_mac(value)
    except ValueError:
        return None


def _dict(value: Any) -> JSON:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dicts(value: Any) -> Iterable[JSON]:
    return [v for v in _list(value) if isinstance(v, dict)]
