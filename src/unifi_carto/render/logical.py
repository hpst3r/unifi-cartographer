"""Logical page: devices in tiers (gateway, switches by uplink depth, APs), links labeled with ports."""

from __future__ import annotations

from typing import NamedTuple

from unifi_carto.render._site import WIRELESS_KINDS, LinkGroup, Node, SiteView, esc, gray
from unifi_carto.render._xml import Page

COL_W = 210  # one leaf column: icon plus the label to its right
TIER_H = 160  # icon 50 + 110 for the bus and the link label
TOP = 130
LEFT = 40

ICON = "sketch=0;html=1;aspect=fixed;pointerEvents=1;fillColor=#FAFAFA;strokeColor=#005073;"
LABEL_RIGHT = (
    "labelPosition=right;verticalLabelPosition=middle;align=left;verticalAlign=middle;spacingLeft=6;fontSize=12;"
)
SHAPES = {
    "gateway": ("shape=mxgraph.cisco19.rect;prIcon=firewall;", 64, 50),
    "l3_switch": ("shape=mxgraph.cisco19.rect;prIcon=l3_switch;", 50, 50),
    "l2_switch": ("shape=mxgraph.cisco19.rect;prIcon=l2_switch;", 50, 50),
    "access_point": ("shape=mxgraph.cisco19.wireless_access_point2;", 50, 50),
}
GENERIC = "rounded=1;arcSize=20;html=1;dashed=1;fillColor=#FAFAFA;strokeColor=#005073;"
EXTERNAL = (
    "rounded=1;arcSize=10;html=1;whiteSpace=wrap;fillColor=#EEEEEE;strokeColor=#9E9E9E;fontColor=#616161;fontSize=10;"
)
EXT_W, EXT_H = 120, 36

EDGE = "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=none;labelBackgroundColor=#ffffff;fontSize=11;"
STRAIGHT = "edgeStyle=none;html=1;endArrow=none;labelBackgroundColor=#ffffff;fontSize=11;"
LAG = "strokeWidth=3;"
WIRELESS = "dashed=1;dashPattern=1 4;strokeWidth=2;"
LLDP = "strokeColor=#9E9E9E;fontColor=#616161;"
SAME_TIER = "exitX=0.75;exitY=1;exitDx=0;exitDy=0;entryX=0.25;entryY=1;entryDx=0;entryDy=0;jettySize=24;"
PIN_DOWN = "exitX=0.5;exitY=1;exitDx=0;exitDy=0;entryX=0.5;entryY=0;entryDx=0;entryDy=0;"

TXT = "text;html=1;align=left;verticalAlign=middle;"
LEGEND = "swimlane;startSize=24;html=1;rounded=1;fillColor=#f5f5f5;strokeColor=#666666;swimlaneFillColor=#ffffff;"
VLAN_COLORS = [
    ("#d5e8d4", "#82b366"),
    ("#fff2cc", "#d6b656"),
    ("#e1d5e7", "#9673a6"),
    ("#dae8fc", "#6c8ebf"),
    ("#f8cecc", "#b85450"),
    ("#ffe6cc", "#d79b00"),
]


def _shape(view: SiteView, node: Node) -> tuple[str, float, float]:
    d = node.device
    if d is None:
        return EXTERNAL, EXT_W, EXT_H
    if d.role == "gateway":
        s, w, h = SHAPES["gateway"]
    elif d.role == "switch":
        downstream = any(view.nodes[c].role == "switch" for c in node.children)
        s, w, h = SHAPES["l3_switch" if downstream else "l2_switch"]
    elif d.role == "access_point":
        s, w, h = SHAPES["access_point"]
    else:
        return GENERIC + LABEL_RIGHT, 50, 50
    return ICON + s + LABEL_RIGHT, w, h


def _label(node: Node) -> str:
    d = node.device
    if d is None:
        if node.external is not None:
            peer = node.external
            sub = f"<br>{gray(peer.chassis_id, 9)}" if peer.chassis_id and peer.chassis_id != node.name else ""
            return f"{esc(node.name)}{sub}<br>{gray('LLDP neighbor', 9)}"
        return esc(node.name)
    parts = [esc(node.name)]
    if d.ip:
        parts.append(gray(d.ip))
    if d.model:
        parts.append(gray(d.model, 10))
    return "<br>".join(parts)


def render_logical(page: Page, view: SiteView) -> None:
    page.vertex("title", f"<b>{esc(view.site.name)} - logical topology</b>", TXT + "fontSize=18;", LEFT, 20, 900, 30)
    page.vertex("sub", view.subtitle(), TXT + "fontSize=12;fontColor=#555555;", LEFT, 50, 1100, 20)
    missing = view.missing_port_links()
    if missing:
        page.vertex(
            "note-ports",
            f"<b>{missing} link{'s' if missing != 1 else ''} without port numbers - legacy data unavailable</b> "
            "(official API only; ports shown as ?).",
            TXT + "fontSize=12;fontColor=#B85450;",
            LEFT,
            72,
            900,
            20,
        )

    right = view.place(COL_W, LEFT)
    geo: dict[str, Geo] = {}
    for k in view.order:
        node = view.nodes[k]
        style, w, h = _shape(view, node)
        x, y = node.x - w / 2, TOP + node.tier * TIER_H + (50 - h) / 2
        geo[k] = Geo(page.vertex(k, _label(node), style, x, y, w, h), x, y, w, h)

    for g in view.groups:
        _edge(page, view, g, geo)

    _legend(page, view, right + 100)


class Geo(NamedTuple):
    cell: str
    x: float
    y: float
    w: float
    h: float


def _edge(page: Page, view: SiteView, g: LinkGroup, geo: dict[str, Geo]) -> None:
    a, b = view.nodes[g.a_key], view.nodes[g.b_key]
    # draw from the layout parent down to the child when this is a tree edge
    if a.parent == b.key:
        up, down = b, a
    elif b.parent == a.key:
        up, down = a, b
    else:
        up, down = b, a
    style = EDGE
    extra = ""
    if g.lag_id:
        extra += LAG
    if g.kind in WIRELESS_KINDS:
        extra += WIRELESS
    if down.device is None or up.device is None:
        extra += LLDP
    label = g.label()
    label_pos: float | None = None
    if down.parent == up.key and down.tier > up.tier:
        sx, sy = up.x, geo[up.key].y + geo[up.key].h
        tx, ty = down.x, geo[down.key].y
        span = ty - sy
        if down.tier == up.tier + 1:
            style += PIN_DOWN
            if label:
                # put the label on the child's own vertical drop (last half of the gap)
                length = span + abs(tx - sx)
                at = span / 2 + abs(tx - sx) + span / 4
                label_pos = 2 * at / length - 1 if length > 0 else 0
        else:
            style = STRAIGHT + PIN_DOWN
            if label:
                label_pos = 0.6
    elif down.tier == up.tier:
        # same tier (e.g. an inter-switch LAG): a U below both icons, clear of the labels on their right
        if up.x > down.x:
            up, down = down, up
        style += SAME_TIER
    page.edge(
        f"e-{g.links[0].id}",
        style + extra,
        geo[up.key].cell,
        geo[down.key].cell,
        value=esc(label),
        label_pos=label_pos,
    )


def _legend(page: Page, view: SiteView, x: float) -> None:
    nets = [n for n in view.networks if (n.purpose or "").lower() != "wan"]
    h = 30 + 4 * 28 + 34 + (len(nets) * 42 if nets else 30) + 16
    lg = page.vertex("legend", "Legend", LEGEND, x, TOP - 20, 330, h)
    styles = [
        ("Wired link, labeled downstream port - upstream port", ""),
        ("LAG (802.3ad), one edge per bundle", LAG),
        ("Wireless / mesh uplink", WIRELESS),
        ("LLDP neighbor (not managed)", LLDP),
    ]
    y = 44.0
    for i, (text, st) in enumerate(styles):
        page.line(f"lg-l{i}", "endArrow=none;html=1;" + st, (15, y), (65, y), lg)
        page.vertex(f"lg-t{i}", esc(text), TXT + "fontSize=11;whiteSpace=wrap;", 75, y - 12, 245, 24, lg)
        y += 28
    page.vertex("lg-vlans", "<b>Networks (VLANs)</b>", TXT + "fontSize=12;", 15, y, 300, 24, lg)
    y += 30
    if not nets:
        page.vertex("lg-none", "none reported", TXT + "fontSize=11;fontColor=#555555;", 15, y, 300, 24, lg)
    ci = 0
    for n in nets:
        if n.vlan_id is None or n.vlan_id == 1:
            fill, stroke = "#f5f5f5", "#666666"
        else:
            fill, stroke = VLAN_COLORS[ci % len(VLAN_COLORS)]
            ci += 1
        vlan = f"VLAN {n.vlan_id}" if n.vlan_id is not None else "VLAN ?"
        value = f"{vlan} {esc(n.name)}<br>{gray(n.subnet or 'subnet ?')}"
        page.vertex(
            f"lg-net-{n.id}",
            value,
            f"rounded=1;whiteSpace=wrap;html=1;align=left;spacingLeft=8;fontSize=12;fillColor={fill};strokeColor={stroke};",
            15,
            y,
            300,
            36,
            lg,
        )
        y += 42
