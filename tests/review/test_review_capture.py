"""Capture proof (SCR-06): console identity in the console.json that `capture` writes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from unifi_carto import collect as collect_mod
from unifi_carto.build import build_inventory
from unifi_carto.cli import main
from unifi_carto.raw import ConsoleRef, OfficialSnapshot


def test_scr06_console_identity_in_capture_is_scrubbed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Runs the real `capture` command (collection faked) and checks the console.json it writes (Amendment 2.14).
    console = ConsoleRef(
        id="9C05D6000000000007A3387E0000000063EC9853:1234567890",
        name="hq",
        access="local",
        local_url="https://unifi.acme-hq.example",
        model="UDM-Pro",
        firmware="4.1.13",
    )
    snap = OfficialSnapshot(console=console, site={"id": "s1", "name": "Main"}, app_version="10.6.106")
    inv = build_inventory(
        [snap], {}, collector_version="0", generated_at=datetime(2026, 1, 1, tzinfo=UTC), include_clients=False
    )
    monkeypatch.setattr(collect_mod, "collect", lambda *a, **kw: collect_mod.Result(inv, 0, [snap], {}))
    assert main(["capture", "-o", str(tmp_path), "--salt", "review-salt"]) == 0
    files = list(tmp_path.rglob("console.json"))
    assert len(files) == 1
    out = files[0].read_text()
    assert "7A3387E" not in out
    assert "acme-hq" not in out
    assert '"hq"' not in out
    written = json.loads(out)
    assert "local_url" not in written and "host_id" not in written
    assert (written["model"], written["firmware"]) == ("UDM-Pro", "4.1.13")
    assert ConsoleRef(**written).id == "console-1"
