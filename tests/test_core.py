from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from unifi_carto.config import load_config
from unifi_carto.errors import ConfigError
from unifi_carto.model import (
    Device,
    Inventory,
    Link,
    LinkEnd,
    Port,
    Source,
    device_key,
    json_schema,
    normalize_mac,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "raw",
    ["AA:BB:CC:DD:EE:FF", "aa-bb-cc-dd-ee-ff", "aabb.ccdd.eeff", "AABBCCDDEEFF"],
)
def test_normalize_mac(raw: str) -> None:
    assert normalize_mac(raw) == "aa:bb:cc:dd:ee:ff"


def test_normalize_mac_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        normalize_mac("not-a-mac")


def test_device_key() -> None:
    assert device_key("uuid-1", "AA:BB:CC:DD:EE:FF") == "uuid-1"
    assert device_key(None, "AA:BB:CC:DD:EE:FF") == "mac:aa:bb:cc:dd:ee:ff"


def test_sources_sorted_unique() -> None:
    p = Port(idx=1, sources=[Source.LEGACY, Source.OFFICIAL, Source.LEGACY])
    assert p.sources == [Source.LEGACY, Source.OFFICIAL]


def test_link_end_exactly_one() -> None:
    with pytest.raises(ValidationError):
        LinkEnd()
    with pytest.raises(ValidationError):
        LinkEnd(device_id="x", external={"chassis_id": "c"})
    assert LinkEnd(device_id="x", port_idx=3).port_idx == 3


def test_roundtrip() -> None:
    inv = Inventory(
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        collector_version="test",
        devices=[Device(id="d1", site_id="s1", mac="02:00:00:00:00:01", ports=[Port(idx=1)])],
        links=[
            Link(
                id="d2:1-d1:24",
                site_id="s1",
                a=LinkEnd(device_id="d2", port_idx=1),
                b=LinkEnd(device_id="d1", port_idx=24),
                source=Source.LEGACY,
            )
        ],
    )
    again = Inventory.model_validate_json(inv.model_dump_json())
    assert again == inv


def test_schema_is_checked_in_and_current() -> None:
    checked_in = json.loads((ROOT / "schema" / "inventory-v1.schema.json").read_text())
    assert checked_in == json_schema(), "run: python -m unifi_carto.model > schema/inventory-v1.schema.json"


def test_config_refuses_inline_secret(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text('[[console]]\nname = "hq"\napi_key = "s3cret"\n')
    with pytest.raises(ConfigError, match="inline secret"):
        load_config(cfg)


def test_config_secret_file_permissions(tmp_path: Path) -> None:
    secret = tmp_path / "key"
    secret.write_text("abc\n")
    os.chmod(secret, 0o644)
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[[console]]\nname = "hq"\nlocal_url = "https://10.0.0.1"\napi_key_file = "{secret}"\n')
    c = load_config(cfg).consoles[0]
    with pytest.raises(ConfigError, match="chmod 600"):
        c.api_key.resolve("hq api key")
    os.chmod(secret, 0o600)
    assert c.api_key.resolve("hq api key") == "abc"


def test_config_env_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text('[site_manager]\napi_key_env = "SMK"\n')
    conf = load_config(cfg)
    with pytest.raises(ConfigError, match="SMK"):
        conf.site_manager_key.resolve("site manager key")
    monkeypatch.setenv("SMK", "k")
    assert conf.site_manager_key.resolve("site manager key") == "k"


def test_config_requires_https(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text('[[console]]\nname = "hq"\nlocal_url = "http://10.0.0.1"\n')
    with pytest.raises(ConfigError, match="https"):
        load_config(cfg)
