"""Synthetic fixture loaders (tests/fixtures/synthetic/, regenerate with make_site_a.py)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from unifi_carto.legacy import unwrap
from unifi_carto.model import CollectionWarning
from unifi_carto.raw import ConsoleRef, LegacySnapshot, OfficialSnapshot

SYNTHETIC = Path(__file__).resolve().parent / "fixtures" / "synthetic"
SITE_A = SYNTHETIC / "site-a"
GENERATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def load_official(site_dir: Path = SITE_A, *, include_clients: bool = False) -> OfficialSnapshot:
    d = site_dir / "official"
    return OfficialSnapshot(
        console=ConsoleRef(**load_json(d / "console.json")),
        site=load_json(d / "site.json"),
        app_version=load_json(d / "info.json")["applicationVersion"],
        devices=load_json(d / "devices.json"),
        device_details=load_json(d / "device_details.json"),
        networks=load_json(d / "networks.json"),
        wlans=load_json(d / "wlans.json"),
        wans=load_json(d / "wans.json"),
        lags=load_json(d / "lags.json"),
        mc_lag_domains=load_json(d / "mc_lag_domains.json"),
        switch_stacks=load_json(d / "switch_stacks.json"),
        clients=load_json(d / "clients.json") if include_clients else None,
    )


def load_legacy(site_dir: Path = SITE_A, *, include_clients: bool = False) -> LegacySnapshot:
    d = site_dir / "legacy"
    return LegacySnapshot(
        site_name="default",
        auth="api_key",
        devices=unwrap(load_json(d / "stat_device.json")),
        portconfs=unwrap(load_json(d / "rest_portconf.json")),
        networkconfs=unwrap(load_json(d / "rest_networkconf.json")),
        wlanconfs=unwrap(load_json(d / "rest_wlanconf.json")),
        clients=unwrap(load_json(d / "stat_sta.json")) if include_clients else None,
    )


def legacy_key(snap: OfficialSnapshot) -> tuple[str, str]:
    return (snap.console.id, snap.site["id"])


@pytest.fixture
def official_a() -> OfficialSnapshot:
    return load_official()


@pytest.fixture
def legacy_a() -> LegacySnapshot:
    return load_legacy()


@pytest.fixture
def legacy_refused_warning() -> CollectionWarning:
    """What collect.py passes as extra_warnings when Legacy.collect_site returned None."""
    return CollectionWarning(
        code="legacy_auth_refused",
        message="legacy API refused the API key and no legacy credentials are configured",
        console="console-hq",
        site="default",
    )
