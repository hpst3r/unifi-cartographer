"""Inventory -> draw.io (``.drawio``) document. Pure: no I/O, deterministic for the same input.

Pages per site (house style of the drawio-diagrams skill): Logical, Cables (port-level, split into
parts when dense), Cable schedule, Inventory.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Sequence

from unifi_carto.model import Inventory, Site
from unifi_carto.render._site import SiteView
from unifi_carto.render._xml import Page
from unifi_carto.render.cables import render_cables
from unifi_carto.render.logical import render_logical
from unifi_carto.render.tables import render_inventory, render_schedule

PAGE_KINDS = ("logical", "cables", "schedule", "inventory")
_ALIASES = {"rack-inventory": "inventory", "cable-schedule": "schedule"}


def _sites(inv: Inventory, site_ids: Sequence[str] | None) -> list[Site]:
    known = {s.id: s for s in inv.sites}
    for site_id in sorted({d.site_id for d in inv.devices} | {lk.site_id for lk in inv.links}):
        if site_id not in known:  # orphan site id: render it under its id rather than dropping devices
            known[site_id] = Site(id=site_id, console_id="?", name=site_id)
    if site_ids is not None:
        unknown = [s for s in site_ids if s not in known]
        if unknown:
            raise ValueError(f"unknown site id(s): {', '.join(unknown)}")
        return [known[s] for s in dict.fromkeys(site_ids)]
    return sorted(known.values(), key=lambda s: (s.name.lower(), s.id))


def render_drawio(
    inv: Inventory,
    *,
    pages: Sequence[str] = PAGE_KINDS,
    site_ids: Sequence[str] | None = None,
) -> str:
    """Render ``inv`` as a complete ``<mxfile>`` document (one set of pages per site)."""
    kinds = [_ALIASES.get(p, p) for p in pages]
    bad = [p for p in kinds if p not in PAGE_KINDS]
    if bad:
        raise ValueError(f"unknown page kind(s): {', '.join(bad)} (choose from {', '.join(PAGE_KINDS)})")
    kinds = list(dict.fromkeys(kinds))

    out: list[Page] = []
    for n, site in enumerate(_sites(inv, site_ids), start=1):
        view = SiteView(inv, site)
        for kind in kinds:
            prefix = f"s{n}{kind[0]}"
            if kind == "logical":
                page = Page(prefix, f"{site.name} - Logical", prefix)
                render_logical(page, view)
                out.append(page)
            elif kind == "cables":
                out.extend(render_cables(view, prefix, f"{site.name} - Cables"))
            elif kind == "schedule":
                page = Page(prefix, f"{site.name} - Cable schedule", prefix)
                render_schedule(page, view)
                out.append(page)
            else:
                page = Page(prefix, f"{site.name} - Inventory", prefix)
                render_inventory(page, view)
                out.append(page)
    if not out:
        page = Page("empty", "Inventory", "e")
        page.vertex("note", "No sites in this inventory.", "text;html=1;align=left;fontSize=14;", 40, 40, 400, 30)
        out.append(page)

    mxfile = ET.Element("mxfile", {"host": "unifi-cartographer", "type": "device"})
    mxfile.extend(p.element() for p in out)
    return ET.tostring(mxfile, encoding="unicode") + "\n"
