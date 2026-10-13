"""Table pages: cable schedule and device inventory (HTML tables in text cells, as in the skill templates)."""

from __future__ import annotations

from unifi_carto.model import Port
from unifi_carto.render._site import CATEGORIES, ROLE_TEXT, WIRELESS_KINDS, SiteView, esc, fmt_ports, fmt_speed
from unifi_carto.render._xml import Page

TXT = "text;html=1;whiteSpace=wrap;strokeColor=none;fillColor=none;"
TD = "border:1px solid #9E9E9E;padding:3px 8px;text-align:left;vertical-align:middle;white-space:nowrap"
TH = TD + ";background:#e8eef5"
ROW_H = 21
LEFT = 40
SOURCE_COLOR = {"legacy": "#2E7D32", "lldp": "#1565C0", "official": "#616161", "inferred": "#B85450"}


def _table(header: list[str], rows: list[list[str]]) -> str:
    head = "<tr>" + "".join(f'<th style="{TH}">{esc(h)}</th>' for h in header) + "</tr>"
    body = "".join("<tr>" + "".join(f'<td style="{TD}">{v}</td>' for v in r) + "</tr>" for r in rows)
    return f'<table style="border-collapse:collapse;font-size:11px">{head}{body}</table>'


def _title(page: Page, view: SiteView, title: str, sub: str) -> None:
    page.vertex(
        "title", f"<b>{esc(view.site.name)} - {esc(title)}</b>", TXT + "align=left;fontSize=20;", LEFT, 16, 1200, 32
    )
    page.vertex(
        "sub", esc(sub) + " " + view.subtitle(), TXT + "align=left;fontSize=12;fontColor=#424242;", LEFT, 48, 1500, 34
    )


def _vlans(view: SiteView, port: Port | None) -> str:
    if port is None:
        return "?"

    def vname(v: int) -> str:
        n = view.network_name(v)
        return f"{v} ({esc(n)})" if n else str(v)

    native = vname(port.native_vlan) if port.native_vlan is not None else "?"
    if port.tagged_vlans is None:
        tagged = "?"
    elif port.tagged_vlans == "all":
        tagged = "all"
    elif not port.tagged_vlans:
        tagged = "none"
    else:
        tagged = ",".join(str(v) for v in sorted(port.tagged_vlans))
    prof = f" &middot; profile {esc(port.profile)}" if port.profile else ""
    return f"native {native} &middot; tagged {tagged}{prof}"


def render_schedule(page: Page, view: SiteView) -> None:
    _title(
        page,
        view,
        "cable schedule",
        "One row per link (LAG members separately), in cable-ID order. A-end = downstream device (or the local "
        "switch for LLDP), B-end = upstream. VLANs are those of the A-end port. Source: legacy = port-accurate "
        "uplink data, lldp = neighbor table, official = parent only (ports unknown).",
    )
    rows: list[list[str]] = []
    for lk in sorted(view.links, key=lambda lk: (view.cable_ids[lk.id][0], view.cable_ids[lk.id])):
        cat = view.category(lk)
        color, width, _text, cname = CATEGORIES[cat]
        border = "dotted" if lk.kind in WIRELESS_KINDS else "solid"
        swatch = (
            f'<span style="display:inline-block;width:26px;height:0px;border-top:{width + 3}px {border} {color};'
            f'vertical-align:middle"></span>&nbsp;{esc(cname)}'
        )
        lag = view.group_of(lk).label() if lk.lag_id else ""
        src = lk.source.value
        drawn = "" if view.drawable(lk) or lk.kind in WIRELESS_KINDS else " &middot; not on Cables page"
        rows.append(
            [
                f"<b>{view.cable_ids[lk.id]}</b>",
                swatch,
                esc(view.end_text(lk.a)),
                esc(view.end_text(lk.b)),
                esc(fmt_speed(lk.speed_mbps)),
                esc(lk.kind),
                esc(lag),
                _vlans(view, view.port(lk.a)),
                f'<font color="{SOURCE_COLOR.get(src, "#616161")}">{esc(src)}</font>{drawn}',
            ]
        )
    header = ["Cable", "Color", "A-end device:port", "B-end device:port", "Speed", "Kind", "LAG", "VLANs (A port)"]
    header.append("Source")
    if not rows:
        rows.append(["-", "", "no links in this site", "", "", "", "", "", ""])
    height = 10 + ROW_H * (len(rows) + 1)
    page.vertex(
        "table", _table(header, rows), TXT + "align=left;verticalAlign=top;overflow=fill;", LEFT, 90, 1500, height
    )


def render_inventory(page: Page, view: SiteView) -> None:
    _title(
        page,
        view,
        "device inventory",
        "Devices in topology order (gateway, switches by uplink depth, access points). Uplink = upstream device:port.",
    )
    rows: list[list[str]] = []
    for k in view.order:
        node = view.nodes[k]
        d = node.device
        if d is None:
            continue
        uplink = "-"
        if node.parent is not None:
            parent = view.nodes[node.parent]
            g = next((g for g in view.groups if g.a_key == k and g.b_key == node.parent), None)
            if g is None:
                uplink = f"{parent.name}:?"
            elif g.kind in WIRELESS_KINDS:
                uplink = f"{parent.name} ({g.kind})"
            else:
                ports = fmt_ports(g.b_ports)
                uplink = f"{parent.name}:{ports}" + (" (LAG)" if g.lag_id else "")
        nports = len(d.ports)
        rows.append(
            [
                f"<b>{esc(node.name)}</b>",
                esc(d.model or "?"),
                esc(ROLE_TEXT[d.role]),
                esc(d.ip or "?"),
                esc(d.mac),
                esc(d.firmware or "?"),
                esc(d.state or "?"),
                str(nports) if nports else "?",
                esc(uplink),
                esc(", ".join(s.value for s in d.sources) or "?"),
            ]
        )
    header = ["Name", "Model", "Role", "IP", "MAC", "Firmware", "State", "Ports", "Uplink (device:port)", "Sources"]
    if not rows:
        rows.append(["-", "", "", "", "", "", "", "", "no devices in this site", ""])
    height = 10 + ROW_H * (len(rows) + 1)
    page.vertex(
        "table", _table(header, rows), TXT + "align=left;verticalAlign=top;overflow=fill;", LEFT, 90, 1300, height
    )
    page.vertex(
        "note-rack",
        "Rack elevation placement stays manual: U positions and heights are not in the inventory. Start from the "
        "drawio-diagrams rack template and use this table for models and addresses.",
        "rounded=1;html=1;whiteSpace=wrap;align=left;verticalAlign=top;fillColor=#FFFDE7;strokeColor=#F9A825;"
        "fontSize=11;spacing=8;",
        LEFT,
        90 + height + 20,
        800,
        46,
    )
