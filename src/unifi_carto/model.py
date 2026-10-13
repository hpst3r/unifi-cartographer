"""Inventory model (schema_version "1").

This module is the frozen contract between collectors, the builder and renderers.
Field semantics: docs/MODEL.md. Run ``python -m unifi_carto.model`` to print the JSON Schema.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION: Literal["1"] = "1"

_MAC_HEX = re.compile(r"[0-9a-f]{12}")


def normalize_mac(value: str) -> str:
    """Return a MAC as lowercase colon-separated hex (``aa:bb:cc:dd:ee:ff``).

    Accepts colon, dash, dot or no separators. Raises ValueError for anything else.
    """
    hexdigits = re.sub(r"[^0-9a-fA-F]", "", value).lower()
    if not _MAC_HEX.fullmatch(hexdigits):
        raise ValueError(f"not a MAC address: {value!r}")
    return ":".join(hexdigits[i : i + 2] for i in range(0, 12, 2))


def device_key(official_id: str | None, mac: str) -> str:
    """Stable device id: the official UUID when known, else ``mac:<normalized mac>``."""
    return official_id if official_id else f"mac:{normalize_mac(mac)}"


class Source(StrEnum):
    OFFICIAL = "official"
    LEGACY = "legacy"
    LLDP = "lldp"
    SITE_MANAGER = "site_manager"
    INFERRED = "inferred"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False, use_enum_values=False)


class _Sourced(_Base):
    sources: list[Source] = Field(default_factory=list)

    @field_validator("sources")
    @classmethod
    def _sort_unique(cls, v: list[Source]) -> list[Source]:
        return sorted(set(v), key=lambda s: s.value)


class CollectionWarning(_Base):
    code: str  # e.g. endpoint_unavailable, legacy_auth_refused, legacy_skipped_connector, parse_error
    message: str
    console: str | None = None
    site: str | None = None


class Console(_Base):
    id: str  # Site Manager host id when known, else the configured console name
    name: str
    access: Literal["local", "connector"]
    local_url: str | None = None
    model: str | None = None
    firmware: str | None = None
    network_version: str | None = None  # from /v1/info
    legacy: Literal["api_key", "login", "unavailable", "disabled"] = "unavailable"


class Site(_Base):
    id: str  # official site UUID
    console_id: str
    name: str
    internal_reference: str | None = None  # legacy short name (e.g. "default")


class PoE(_Base):
    enabled: bool | None = None
    standard: str | None = None  # 802.3af / 802.3at / 802.3bt
    state: str | None = None  # UP / DOWN / LIMITED / UNKNOWN


class Sfp(_Base):
    vendor: str | None = None
    part: str | None = None
    serial: str | None = None  # scrubbed in fixtures


class Port(_Sourced):
    idx: int = Field(ge=1)
    name: str | None = None
    connector: str | None = None  # RJ45 / SFP / SFPPLUS / SFP28 / QSFP28 (official)
    media: str | None = None  # legacy media string, e.g. GE, 10G, SFP+
    max_speed_mbps: int | None = None
    speed_mbps: int | None = None
    state: Literal["up", "down", "unknown"] = "unknown"
    enabled: bool | None = None
    poe: PoE | None = None
    sfp: Sfp | None = None
    is_uplink: bool | None = None
    lag_id: str | None = None
    profile: str | None = None  # port profile name (legacy portconf)
    native_vlan: int | None = None  # None = unknown
    tagged_vlans: list[int] | Literal["all"] | None = None  # None = unknown


class Radio(_Base):
    frequency_ghz: float | None = None
    channel: int | None = None
    channel_width_mhz: int | None = None
    wlan_standard: str | None = None


class Device(_Sourced):
    id: str  # see device_key()
    site_id: str
    mac: str
    name: str | None = None
    model: str | None = None
    role: Literal["gateway", "switch", "access_point", "other"] = "other"
    ip: str | None = None
    firmware: str | None = None
    state: str | None = None  # ONLINE / OFFLINE / ... as reported
    uplink_device_id: str | None = None  # parent in the topology tree (official)
    ports: list[Port] = Field(default_factory=list)
    radios: list[Radio] = Field(default_factory=list)

    @field_validator("mac")
    @classmethod
    def _mac(cls, v: str) -> str:
        return normalize_mac(v)


class ExternalPeer(_Base):
    """A neighbor seen via LLDP (or a legacy uplink MAC) that is not a managed device."""

    chassis_id: str | None = None
    port_id: str | None = None
    system_name: str | None = None


class LinkEnd(_Base):
    device_id: str | None = None
    port_idx: int | None = Field(default=None, ge=1)
    external: ExternalPeer | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> LinkEnd:
        if (self.device_id is None) == (self.external is None):
            raise ValueError("LinkEnd needs exactly one of device_id or external")
        return self


class Link(_Base):
    id: str  # deterministic: "<a.device_id>:<a.port_idx or ?>-<b...>"
    site_id: str
    a: LinkEnd  # downstream end (device whose uplink this is, or LLDP local side)
    b: LinkEnd  # upstream end
    kind: Literal["wired", "wireless", "mesh", "unknown"] = "unknown"
    speed_mbps: int | None = None
    lag_id: str | None = None
    source: Source


class LagMember(_Base):
    device_id: str
    port_idxs: list[int]


class Lag(_Base):
    id: str
    site_id: str
    kind: Literal["local", "multi_chassis", "switch_stack"]
    members: list[LagMember]


class Network(_Sourced):
    id: str
    site_id: str
    name: str
    vlan_id: int | None = None
    purpose: str | None = None  # corporate / guest / vlan-only / wan ... (legacy) or management (official)
    subnet: str | None = None  # CIDR of the gateway interface, e.g. 10.0.10.1/24
    dhcp_enabled: bool | None = None
    enabled: bool | None = None


class Wlan(_Sourced):
    id: str
    site_id: str
    name: str
    enabled: bool | None = None
    network_id: str | None = None
    security: str | None = None
    bands: list[str] | None = None


class Wan(_Base):
    id: str
    site_id: str
    name: str | None = None
    raw: dict[str, object] = Field(default_factory=dict)  # official WAN object, unmodeled fields


class Client(_Base):
    id: str
    site_id: str
    mac: str
    name: str | None = None
    ip: str | None = None
    type: str | None = None  # WIRED / WIRELESS / VPN / TELEPORT
    uplink_device_id: str | None = None
    switch_port: int | None = None  # legacy only
    vlan: int | None = None
    sources: list[Source] = Field(default_factory=list)

    @field_validator("mac")
    @classmethod
    def _mac(cls, v: str) -> str:
        return normalize_mac(v)


class Inventory(_Base):
    schema_version: Literal["1"] = SCHEMA_VERSION
    generated_at: datetime
    collector_version: str
    consoles: list[Console] = Field(default_factory=list)
    sites: list[Site] = Field(default_factory=list)
    devices: list[Device] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    lags: list[Lag] = Field(default_factory=list)
    networks: list[Network] = Field(default_factory=list)
    wlans: list[Wlan] = Field(default_factory=list)
    wans: list[Wan] = Field(default_factory=list)
    clients: list[Client] | None = None  # None = not collected (--clients not given)
    warnings: list[CollectionWarning] = Field(default_factory=list)


def json_schema() -> dict[str, object]:
    return Inventory.model_json_schema()


if __name__ == "__main__":
    print(json.dumps(json_schema(), indent=2, sort_keys=True))
