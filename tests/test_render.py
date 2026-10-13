"""Tests for unifi_carto.render (draw.io renderer)."""

from __future__ import annotations

import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from unifi_carto.model import Inventory, Site
from unifi_carto.render import render_drawio
from unifi_carto.render._site import fmt_ports, fmt_speed
from unifi_carto.render._xml import slug

FIXTURES = Path(__file__).parent / "fixtures" / "render"
NAMES = ["site-a", "site-a-official", "large"]


def load(name: str) -> Inventory:
    return Inventory.model_validate_json((FIXTURES / f"{name}.json").read_text())


def diagrams(xml: str) -> list[ET.Element]:
    root = ET.fromstring(xml)
    assert root.tag == "mxfile"
    return root.findall("diagram")


def page(xml: str, name: str) -> ET.Element:
    found = [d for d in diagrams(xml) if d.get("name") == name]
    assert len(found) == 1, [d.get("name") for d in diagrams(xml)]
    return found[0]


def cells(d: ET.Element) -> list[ET.Element]:
    return d.findall("./mxGraphModel/root/mxCell")


def edges(d: ET.Element) -> list[ET.Element]:
    """Connected edges (legend sample lines have no source/target)."""
    return [c for c in cells(d) if c.get("edge") == "1" and c.get("source")]


@pytest.fixture(scope="module", params=NAMES)
def rendered(request: pytest.FixtureRequest) -> tuple[Inventory, str]:
    inv = load(request.param)
    return inv, render_drawio(inv)


def test_parses_and_page_names(rendered: tuple[Inventory, str]) -> None:
    _inv, xml = rendered
    names = [d.get("name") for d in diagrams(xml)]
    assert names[0] == "HQ - Logical"
    assert names[-2:] == ["HQ - Cable schedule", "HQ - Inventory"]
    cable_pages = names[1:-2]
    assert cable_pages == ["HQ - Cables"] or all(re.fullmatch(r"HQ - Cables \(\d/\d\)", n or "") for n in cable_pages)
    for d in diagrams(xml):
        model = d.find("mxGraphModel")
        assert model is not None and model.get("adaptiveColors") == "auto"


def test_deterministic(rendered: tuple[Inventory, str]) -> None:
    inv, xml = rendered
    assert render_drawio(inv) == xml
    assert render_drawio(Inventory.model_validate_json(inv.model_dump_json())) == xml


def test_no_comments_and_unique_ids(rendered: tuple[Inventory, str]) -> None:
    _inv, xml = rendered
    assert "<!--" not in xml
    seen: set[str] = set()
    diagram_ids = [d.get("id") for d in diagrams(xml)]
    assert len(diagram_ids) == len(set(diagram_ids))
    for d in diagrams(xml):
        ids = [c.get("id") for c in cells(d)]
        assert len(ids) == len(set(ids)), "duplicate cell id in one page"
        content = {i for i in ids if i not in ("0", "1")}
        assert not content & seen, "cell id reused across pages"
        seen |= {i for i in content if i}
        assert all("html=1" in (c.get("style") or "") for c in cells(d) if c.get("style"))


def test_every_edge_references_existing_cells(rendered: tuple[Inventory, str]) -> None:
    _inv, xml = rendered
    for d in diagrams(xml):
        ids = {c.get("id") for c in cells(d)}
        for e in edges(d):
            assert e.get("source") in ids and e.get("target") in ids
            geo = e.find("mxGeometry")
            assert geo is not None and geo.get("relative") == "1"


def test_every_link_once_in_logical(rendered: tuple[Inventory, str]) -> None:
    inv, xml = rendered
    logical = page(xml, "HQ - Logical")
    edge_ids = [e.get("id") or "" for e in edges(logical)]
    covered: dict[str, int] = {}
    for lk in inv.links:
        bundle = [x for x in inv.links if lk.lag_id and x.lag_id == lk.lag_id]
        first = min(bundle or [lk], key=lambda x: x.id)  # LAG members share one edge
        key = f"-e-{slug(first.id)}"
        matches = [i for i in edge_ids if i.endswith(key)]
        assert len(matches) == 1, (lk.id, matches)
        covered[matches[0]] = covered.get(matches[0], 0) + 1
    assert set(covered) == set(edge_ids), "logical page has edges that are not links"


def test_lag_drawn_as_one_bold_edge() -> None:
    xml = render_drawio(load("site-a"))
    logical = page(xml, "HQ - Logical")
    lag = [e for e in edges(logical) if (e.get("value") or "").startswith("LAG")]
    assert len(lag) == 1
    assert lag[0].get("value") == "LAG p49-50 - p1-2"
    assert "strokeWidth=3" in (lag[0].get("style") or "")
    mesh = [e for e in edges(logical) if e.get("value") == "mesh"]
    assert len(mesh) == 1 and "dashed=1" in (mesh[0].get("style") or "")
    values = {e.get("value") for e in edges(logical)}
    assert {"p8 - p11", "p49 - p3", "p1 - p5", "p10 - eno1"} <= values


def test_every_link_once_in_schedule(rendered: tuple[Inventory, str]) -> None:
    inv, xml = rendered
    sched = page(xml, "HQ - Cable schedule")
    table = next(c.get("value") or "" for c in cells(sched) if (c.get("id") or "").endswith("-table"))
    ids = re.findall(r"<b>([CW]-\d+)</b>", table)
    assert len(ids) == len(inv.links) == len(set(ids))
    n_wireless = sum(1 for lk in inv.links if lk.kind in ("wireless", "mesh"))
    assert sum(1 for i in ids if i.startswith("W-")) == n_wireless
    assert [i for i in ids if i.startswith("C-")] == [f"C-{n:03d}" for n in range(1, len(inv.links) - n_wireless + 1)]


def test_cables_drawn_once_with_lane_routing(rendered: tuple[Inventory, str]) -> None:
    inv, xml = rendered
    cable_pages = [d for d in diagrams(xml) if "Cables" in (d.get("name") or "")]
    drawn = [e for d in cable_pages for e in edges(d)]
    devices = {d.id for d in inv.devices}
    expected = [
        lk
        for lk in inv.links
        if lk.kind not in ("wireless", "mesh")
        and lk.a.port_idx is not None
        and lk.b.port_idx is not None
        and lk.a.device_id in devices
        and lk.b.device_id in devices
    ]
    assert len(drawn) == len(expected)
    for lk in expected:
        assert sum(1 for e in drawn if (e.get("id") or "").endswith(f"-c-{slug(lk.id)}")) == 1
    for e in drawn:
        style = e.get("style") or ""
        assert "jumpStyle=arc" in style and "edgeStyle=none" in style
        pts = e.findall("./mxGeometry/Array/mxPoint")
        assert len(pts) >= 2
        assert len({p.get("y") for p in pts}) >= 1


def test_official_only_notes_missing_ports() -> None:
    xml = render_drawio(load("site-a-official"))
    logical = page(xml, "HQ - Logical")
    values = " ".join(c.get("value") or "" for c in cells(logical))
    assert "5 links without port numbers - legacy data unavailable" in values
    assert all(e.get("value") == "? - ?" for e in edges(logical))
    cables = page(xml, "HQ - Cables")
    assert not edges(cables)
    assert "No links with port numbers on both ends" in " ".join(c.get("value") or "" for c in cells(cables))


def test_large_splits_cable_pages() -> None:
    xml = render_drawio(load("large"))
    names = [d.get("name") for d in diagrams(xml)]
    assert "HQ - Cables (1/2)" in names and "HQ - Cables (2/2)" in names
    for name in ("HQ - Cables (1/2)", "HQ - Cables (2/2)"):
        d = page(xml, name)
        switches = [c for c in cells(d) if "&middot; switch</font>" in (c.get("value") or "")]
        assert 0 < len(switches) <= 6
        assert len(edges(d)) <= 40


def test_escapes_markup_in_names() -> None:
    inv = load("site-a")
    inv.devices[0].name = "gw & <core>"
    inv.sites[0] = Site(id=inv.sites[0].id, console_id=inv.sites[0].console_id, name="HQ & <lab>")
    xml = render_drawio(inv)
    assert "gw &amp;amp; &amp;lt;core&amp;gt;" in xml  # HTML-escaped label inside an XML attribute
    names = [d.get("name") for d in diagrams(xml)]
    assert "HQ & <lab> - Logical" in names
    values = [c.get("value") or "" for d in diagrams(xml) for c in cells(d)]
    assert any("gw &amp; &lt;core&gt;" in v for v in values)
    assert not any("<core>" in v for v in values)


def test_unnamed_device_uses_model_and_mac_suffix() -> None:
    inv = load("site-a")
    ap = next(d for d in inv.devices if d.name == "ap-lobby")
    ap.name = None
    xml = render_drawio(inv, pages=["logical"])
    assert f"U6-Pro {ap.mac[-5:]}" in xml


def test_pages_and_site_filter() -> None:
    inv = load("site-a")
    xml = render_drawio(inv, pages=["schedule", "rack-inventory"])
    assert [d.get("name") for d in diagrams(xml)] == ["HQ - Cable schedule", "HQ - Inventory"]
    assert render_drawio(inv, site_ids=[inv.sites[0].id]) == render_drawio(inv)
    with pytest.raises(ValueError, match="unknown site"):
        render_drawio(inv, site_ids=["nope"])
    with pytest.raises(ValueError, match="unknown page"):
        render_drawio(inv, pages=["rack"])


def test_multi_site_and_empty() -> None:
    a = load("site-a")
    b = load("large")
    other = "6a1d0000-0000-4000-8000-00000000b001"
    for d in b.devices:
        d.site_id = other
    for lk in b.links:
        lk.site_id = other
    for n in b.networks:
        n.site_id = other
    merged = a.model_copy(
        update={
            "sites": [*a.sites, Site(id=other, console_id=a.consoles[0].id, name="Branch")],
            "devices": a.devices + b.devices,
            "links": a.links + b.links,
            "networks": a.networks + b.networks,
        }
    )
    names = [d.get("name") for d in diagrams(render_drawio(merged, pages=["logical"]))]
    assert names == ["Branch - Logical", "HQ - Logical"]
    empty = Inventory(generated_at=a.generated_at, collector_version="x")
    assert len(diagrams(render_drawio(empty))) == 1


def test_formatters() -> None:
    assert fmt_ports([49, 50]) == "p49-50"
    assert fmt_ports([1, 3, 4, 5]) == "p1,3-5"
    assert fmt_ports([None]) == "?"
    assert fmt_speed(10000) == "10G" and fmt_speed(2500) == "2.5G" and fmt_speed(100) == "100M"
    assert fmt_speed(None) == "?"


# Optional visual integration check with a draw.io CLI wrapper exposing `check` and `draft`
# (e.g. the drawio-diagrams agent skill's drawio_helper.py). Opt-in via environment.
HELPER = os.environ.get("UNIFI_CARTO_DRAWIO_HELPER", "")


@pytest.mark.skipif(
    os.environ.get("UNIFI_CARTO_DRAWIO_IT") != "1" or not HELPER,
    reason="set UNIFI_CARTO_DRAWIO_IT=1 and UNIFI_CARTO_DRAWIO_HELPER=/path/to/drawio_helper.py",
)
@pytest.mark.parametrize("name", NAMES)
def test_drawio_helper_check_and_draft(name: str, tmp_path: Path) -> None:
    out = Path(os.environ.get("UNIFI_CARTO_DRAWIO_OUT", str(tmp_path)))
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"{name}.drawio"
    target.write_text(render_drawio(load(name)))
    check = subprocess.run(["python3", HELPER, "check", str(target)], capture_output=True, text=True)
    assert check.returncode == 0, check.stdout + check.stderr
    assert check.stdout.startswith("OK")
    draft = subprocess.run(
        ["python3", HELPER, "draft", str(target), "--all-pages"], capture_output=True, text=True, timeout=600
    )
    assert draft.returncode == 0, draft.stdout + draft.stderr
    pngs = re.findall(r"DRAFT: (\S+\.png)", draft.stdout)
    assert len(pngs) == len(diagrams(target.read_text()))
    assert all(Path(p if os.path.isabs(p) else out / p).stat().st_size > 0 for p in pngs)


def test_control_characters_are_stripped_from_every_rendered_string() -> None:
    inv = load("site-a")
    inv.sites[0].name = "Main\x1b[0m\x00"
    for d in inv.devices:
        d.name = f"{d.name}\x07\tbay\n2\r\x0b\ud800"
    xml = render_drawio(inv)
    root = ET.fromstring(xml)
    texts = [v for el in root.iter() for v in el.attrib.values()]
    assert not [t for t in texts if re.search("[\x00-\x1f\ud800-\udfff]", t)]
    assert any(d.get("name") == "Main[0m - Logical" for d in diagrams(xml))
    assert any("bay 2 " in t for t in texts)  # tab/newline/CR became spaces
