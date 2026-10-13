"""Raw snapshot containers: untouched API JSON, grouped per console/site.

Collectors (integration.py, legacy.py) produce these; build.py consumes them.
Nothing in here interprets vendor fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from unifi_carto.model import CollectionWarning

JSON = dict[str, Any]


@dataclass(frozen=True)
class ConsoleRef:
    """Identity and access path of one console, as resolved by collect.py."""

    id: str  # Site Manager host id, else configured name
    name: str
    access: Literal["local", "connector"]
    local_url: str | None = None
    model: str | None = None
    firmware: str | None = None


@dataclass
class OfficialSnapshot:
    console: ConsoleRef
    site: JSON  # one item of GET /v1/sites
    app_version: str | None  # GET /v1/info -> applicationVersion
    devices: list[JSON] = field(default_factory=list)  # list endpoint items
    device_details: dict[str, JSON] = field(default_factory=dict)  # device id -> detail object
    networks: list[JSON] = field(default_factory=list)  # detail objects when fetched, else list items
    wlans: list[JSON] = field(default_factory=list)
    wans: list[JSON] = field(default_factory=list)
    lags: list[JSON] = field(default_factory=list)
    mc_lag_domains: list[JSON] = field(default_factory=list)
    switch_stacks: list[JSON] = field(default_factory=list)
    clients: list[JSON] | None = None  # None = not requested
    warnings: list[CollectionWarning] = field(default_factory=list)


@dataclass
class LegacySnapshot:
    site_name: str  # legacy short name, e.g. "default"
    auth: Literal["api_key", "login"]
    devices: list[JSON] = field(default_factory=list)  # stat/device data[]
    portconfs: list[JSON] = field(default_factory=list)  # rest/portconf data[]
    networkconfs: list[JSON] = field(default_factory=list)  # rest/networkconf data[]
    wlanconfs: list[JSON] = field(default_factory=list)  # rest/wlanconf data[]
    clients: list[JSON] | None = None  # stat/sta data[]; None = not requested
    warnings: list[CollectionWarning] = field(default_factory=list)
