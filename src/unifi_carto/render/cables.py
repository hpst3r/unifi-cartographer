"""Cables page: port-level faceplates and lane-routed cables colored by purpose.

Routing follows the drawio-diagrams skill "Port-level cabling" rules: straight segments with
``jumpStyle=arc`` and waypoints on a private horizontal lane per cable in the gap between rows.
A port whose cable leaves on the far side of its faceplate (a bottom-row port going up, a top-row
port going down) jogs sideways inside the faceplate into the gap between port columns first.
Cables that skip a row run through a corridor to the right of all faceplates.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from unifi_carto.model import Device, Link
from unifi_carto.render._site import CATEGORIES, CATEGORY_ORDER, ROLE_TEXT, SiteView, esc, gray
from unifi_carto.render._xml import Page, Point

MAX_SWITCHES = 6
MAX_CABLES = 40

LABEL_W = 190  # faceplate label area (hostname, IP, model, port summary)
LABEL_W_SMALL = 140  # devices with at most 4 ports (APs)
SQ_W, SQ_H = 22, 18
COL_GAP = 14
PITCH = SQ_W + COL_GAP
CLASS_GAP = 16
FREE_W = 30
PANEL_H = 84
TOP_ROW_Y, BOTTOM_ROW_Y = 12, 54
BAND_UP, BAND_DOWN = 38, 46  # jog heights inside the faceplate (between the two port rows)
JOG = 4  # jog distance from the square edge into the column gap
LANE = 10
PANEL_GAP_X = 60
MIN_GAP_Y = 60
LEFT = 40
TOP = 110

TXT = "text;html=1;whiteSpace=wrap;strokeColor=none;fillColor=none;"
PANEL = (
    "rounded=1;arcSize=6;html=1;container=1;collapsible=0;recursiveResize=0;fillColor=#F7F7F7;"
    "strokeColor=#616161;strokeWidth=1.5;verticalAlign=top;align=left;spacingLeft=8;"
)
PORT_FREE = "rounded=0;html=1;fillColor=#EEEEEE;strokeColor=#BDBDBD;fontColor=#9E9E9E;fontSize=8;spacing=0;"
FREE_BLOCK = (
    "rounded=0;html=1;whiteSpace=wrap;fillColor=#EEEEEE;strokeColor=#BDBDBD;fontColor=#9E9E9E;fontSize=8;"
    "spacing=0;horizontal=0;"
)
LEGEND = (
    "swimlane;html=1;startSize=26;fillColor=#F5F5F5;strokeColor=#757575;fontStyle=1;align=left;spacingLeft=8;"
    "container=1;collapsible=0;"
)


def port_used(color: str, elsewhere: bool = False) -> str:
    dash = "dashed=1;dashPattern=3 2;" if elsewhere else ""
    return (
        f"rounded=0;html=1;fillColor=#FFFFFF;strokeColor={color};strokeWidth=2;fontStyle=1;fontSize=9;spacing=0;{dash}"
    )


def port_class(d: Device, idx: int) -> str:
    p = next((p for p in d.ports if p.idx == idx), None)
    if p is None:
        return "?"
    text = f"{p.connector or ''} {p.media or ''}".upper()
    return "sfp" if "SFP" in text or "QSFP" in text else "rj45" if text.strip() else "?"


@dataclass
class Panel:
    key: str
    device: Device
    label_w: float = 0.0
    row: int = 0
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    port_x: dict[int, float] = field(default_factory=dict)  # port idx -> square left x (panel-relative)
    elements: list[tuple[str, int, int, float]] = field(default_factory=list)  # (kind, first, last, rel x)
    cell: str = ""
    port_cells: dict[int, str] = field(default_factory=dict)


@dataclass
class End:
    panel: Panel
    idx: int
    up: bool  # cable leaves upward
    lane_x: float = 0.0  # x of the vertical segment that reaches the lane

    @property
    def top_row(self) -> bool:
        return self.idx % 2 == 1

    @property
    def cx(self) -> float:
        return self.panel.x + self.panel.port_x[self.idx] + SQ_W / 2

    def escape(self, lane_y: float) -> list[Point]:
        """Waypoints from the port to (lane_x, lane_y)."""
        if self.up == self.top_row:
            return [(self.cx, lane_y)]
        band = self.panel.y + (BAND_UP if self.up else BAND_DOWN)
        return [(self.cx, band), (self.lane_x, band), (self.lane_x, lane_y)]


@dataclass
class GapItem:
    key: str
    x1: float
    x2: float
    up: list[float]  # stub x positions that rise from the lane to the row above
    down: list[float]  # stub x positions that drop from the lane to the row below
    lane: int = 0


@dataclass
class Cable:
    link: Link
    cid: str
    cat: str
    a: End
    b: End
    items: list[tuple[int, GapItem]] = field(default_factory=list)  # (gap index, item); 1 or 2 (corridor)
    corridor_x: float | None = None


def _build_panel(page_key: str, d: Device, marked: set[int]) -> Panel:
    nports = max([1, *(p.idx for p in d.ports), *marked])
    panel = Panel(key=page_key, device=d, label_w=LABEL_W if nports > 4 else LABEL_W_SMALL)
    present = {p.idx for p in d.ports} | marked
    if not d.ports:
        present = set(range(1, nports + 1))
    ncols = math.ceil(nports / 2)
    cols = list(range(1, ncols + 1))

    def used(k: int) -> bool:
        return (2 * k - 1) in marked or (2 * k) in marked

    def cls(k: int) -> str:
        return port_class(d, 2 * k - 1)

    runs: list[tuple[bool, str, list[int]]] = []
    for k in cols:
        u, c = used(k), cls(k)
        if runs and runs[-1][0] == u and runs[-1][1] == c and not u:
            runs[-1][2].append(k)
        else:
            runs.append((u, c, [k]))
    x = panel.label_w + 14.0
    prev: str | None = None
    for u, c, ks in runs:
        if prev is not None and c != prev:
            x += CLASS_GAP
        prev = c
        if not u and len(ks) >= 2:
            first, last = 2 * ks[0] - 1, min(2 * ks[-1], nports)
            panel.elements.append(("free", first, last, x))
            x += FREE_W + COL_GAP
            continue
        for k in ks:
            for idx in (2 * k - 1, 2 * k):
                if idx in present:
                    panel.port_x[idx] = x
            panel.elements.append(("col", 2 * k - 1, 2 * k, x))
            x += PITCH
    panel.width = x - COL_GAP + 14
    return panel


def _order_lanes(items: list[GapItem]) -> None:
    """Order lanes in one gap to reduce crossings (linear ordering by insertion moves; deterministic)."""
    n = len(items)
    items.sort(key=lambda it: (it.x1, it.x2, it.key))

    def c(i: GapItem, j: GapItem) -> int:  # crossings when i's lane is above j's
        return sum(1 for x in j.up if i.x1 < x < i.x2) + sum(1 for x in i.down if j.x1 < x < j.x2)

    cost = [[c(i, j) if i is not j else 0 for j in items] for i in items]
    order = list(range(n))
    for _ in range(50):
        improved = False
        for p in range(n):
            item = order[p]
            best_q, best_delta, delta = p, 0, 0
            for q in range(p - 1, -1, -1):  # move up past order[q]
                other = order[q]
                delta += cost[item][other] - cost[other][item]
                if delta < best_delta:
                    best_q, best_delta = q, delta
            delta = 0
            for q in range(p + 1, n):  # move down past order[q]
                other = order[q]
                delta += cost[other][item] - cost[item][other]
                if delta < best_delta:
                    best_q, best_delta = q, delta
            if best_q != p:
                order.pop(p)
                order.insert(best_q, item)
                improved = True
        if not improved:
            break
    for lane, i in enumerate(order):
        items[i].lane = lane


def _pages(view: SiteView, cables: list[Link]) -> list[tuple[set[str], set[str]]]:
    """Split into pages: (device keys, link ids drawn). One page unless it gets too dense."""
    keys = sorted({view.end_key(e) for lk in cables for e in (lk.a, lk.b)}, key=view.order.index)
    switches = [k for k in keys if view.nodes[k].role == "switch"]
    if len(switches) <= MAX_SWITCHES and len(cables) <= MAX_CABLES:
        return [(set(keys), {lk.id for lk in cables})]
    leaf_tier = max(view.nodes[k].tier for k in switches)
    groups: list[set[str]] = []
    grouped: set[str] = set()
    for k in keys:
        if view.nodes[k].role == "switch" and view.nodes[k].tier == leaf_tier:
            members = {k}
            stack = list(view.nodes[k].children)
            while stack:
                c = stack.pop()
                members.add(c)
                stack.extend(view.nodes[c].children)
            members &= set(keys)
            groups.append(members)
            grouped |= members
    core = set(keys) - grouped

    def page_links(devs: set[str], first: bool) -> set[str]:
        out = set()
        for lk in cables:
            ka, kb = view.end_key(lk.a), view.end_key(lk.b)
            if ka in devs and kb in devs and (first or not (ka in core and kb in core)):
                out.add(lk.id)
        return out

    def nswitch(devs: set[str]) -> int:
        return sum(1 for k in devs if view.nodes[k].role == "switch")

    pages: list[tuple[set[str], set[str]]] = []
    cur: set[str] = set()
    for grp in groups:
        trial = core | cur | grp
        if cur and (nswitch(trial) > MAX_SWITCHES or len(page_links(trial, not pages)) > MAX_CABLES):
            devs = core | cur
            pages.append((devs, page_links(devs, not pages)))
            cur = set()
        cur |= grp
    devs = core | cur
    pages.append((devs, page_links(devs, not pages)))
    return pages


def render_cables(view: SiteView, prefix: str, base_name: str) -> list[Page]:
    cables = [lk for lk in view.links if view.drawable(lk)]
    split = _pages(view, cables) if cables else [(set(), set())]
    out: list[Page] = []
    for i, (devs, drawn) in enumerate(split):
        name = base_name if len(split) == 1 else f"{base_name} ({i + 1}/{len(split)})"
        page = Page(f"{prefix}{i + 1}" if len(split) > 1 else prefix, name, f"{prefix}{i + 1}")
        _render_page(page, view, cables, devs, drawn, (i + 1, len(split)))
        out.append(page)
    return out


def _render_page(
    page: Page, view: SiteView, cables: list[Link], devs: set[str], drawn: set[str], part: tuple[int, int]
) -> None:
    title = f"<b>{esc(view.site.name)} - cable layout (port level)</b>"
    if part[1] > 1:
        title += f" &middot; part {part[0]} of {part[1]}"
    page.vertex("title", title, TXT + "align=left;fontSize=20;", LEFT, 16, 1200, 32)
    page.vertex(
        "sub",
        "Every cable with port numbers on both ends, colored by purpose. Ports in physical order (odd top, even "
        "bottom); cabled ports bold in the cable color, free ports gray, runs of free ports collapsed into one "
        "gray block. Cable IDs match the Cable schedule page. " + view.subtitle(),
        TXT + "align=left;fontSize=12;fontColor=#424242;",
        LEFT,
        48,
        1400,
        34,
    )
    skipped = [lk for lk in view.links if not view.drawable(lk)]
    if skipped:
        page.vertex(
            "note-skipped",
            f"<b>{len(skipped)} link{'s' if len(skipped) != 1 else ''} not drawn</b> (wireless/mesh, LLDP neighbors "
            "outside the inventory, or port numbers unknown - legacy data unavailable); see the Cable schedule.",
            TXT + "align=left;fontSize=12;fontColor=#B85450;",
            LEFT,
            82,
            1400,
            20,
        )
    if not drawn:
        page.vertex(
            "empty",
            "No links with port numbers on both ends. Port-level data comes from the legacy API "
            "(local console URL); see the Logical page and the Cable schedule.",
            "rounded=1;html=1;whiteSpace=wrap;align=left;verticalAlign=top;fillColor=#FFFDE7;strokeColor=#F9A825;"
            "fontSize=12;spacing=8;",
            LEFT,
            TOP + 10,
            700,
            60,
        )
        return

    on_page = [lk for lk in cables if lk.id in drawn]
    marked: dict[str, dict[int, tuple[str, bool]]] = {k: {} for k in devs}  # key -> idx -> (color, elsewhere)
    for lk in cables:
        color = CATEGORIES[view.category(lk)][0]
        for link_end in (lk.a, lk.b):
            k = view.end_key(link_end)
            if k in devs and link_end.port_idx is not None:
                marked[k][link_end.port_idx] = (color, lk.id not in drawn)

    keys = sorted(devs, key=lambda k: (view.nodes[k].tier, view.nodes[k].x, k))
    tiers = sorted({view.nodes[k].tier for k in keys})
    panels: dict[str, Panel] = {}
    rows: list[list[Panel]] = [[] for _ in tiers]
    for k in keys:
        dev = view.nodes[k].device
        assert dev is not None
        p = _build_panel(k, dev, set(marked[k]))
        p.row = tiers.index(view.nodes[k].tier)
        panels[k] = p
        rows[p.row].append(p)
    widths = [sum(p.width for p in r) + PANEL_GAP_X * (len(r) - 1) for r in rows]
    max_w = max(widths)
    for row, w in zip(rows, widths, strict=True):
        x = LEFT + (max_w - w) / 2
        for p in row:
            p.x = x
            x += p.width + PANEL_GAP_X
    corridor0 = LEFT + max_w + 40

    # ends, gap items
    gaps: dict[int, list[GapItem]] = {}
    routed: list[Cable] = []
    n_corr = 0
    for lk in sorted(on_page, key=lambda lk: view.cable_ids[lk.id]):
        pa, pb = panels[view.end_key(lk.a)], panels[view.end_key(lk.b)]
        assert lk.a.port_idx is not None and lk.b.port_idx is not None
        a_up = pa.row >= pb.row
        b_up = pb.row >= pa.row
        a, b = End(pa, lk.a.port_idx, a_up), End(pb, lk.b.port_idx, b_up)
        for e in (a, b):
            sq = e.panel.x + e.panel.port_x[e.idx]
            if e.up == e.top_row:
                e.lane_x = e.cx
            elif e.up:
                e.lane_x = sq + SQ_W + JOG
            else:
                e.lane_x = sq - JOG
        cab = Cable(lk, view.cable_ids[lk.id], view.category(lk), a, b)
        if abs(pa.row - pb.row) <= 1:
            g = max(pa.row, pb.row)
            up = [e.lane_x for e in (a, b) if e.panel.row == g - 1]
            down = [e.lane_x for e in (a, b) if e.panel.row == g]
            item = GapItem(cab.cid, min(a.lane_x, b.lane_x), max(a.lane_x, b.lane_x), up, down)
            cab.items.append((g, item))
        else:
            cx = corridor0 + n_corr * 12
            n_corr += 1
            cab.corridor_x = cx
            upper, lower = (a, b) if pa.row < pb.row else (b, a)
            cab.items.append(
                (upper.panel.row + 1, GapItem(cab.cid + "u", min(upper.lane_x, cx), cx, [upper.lane_x], [cx]))
            )
            cab.items.append((lower.panel.row, GapItem(cab.cid + "l", min(lower.lane_x, cx), cx, [cx], [lower.lane_x])))
        for g, item in cab.items:
            gaps.setdefault(g, []).append(item)
        routed.append(cab)
    for items in gaps.values():
        _order_lanes(items)

    # vertical placement
    lane0: dict[int, float] = {}  # y of the first lane in each gap; lanes are centered in the gap
    y = float(TOP)
    for gi in range(len(rows) + 1):
        n = len(gaps.get(gi, []))
        h = max(MIN_GAP_Y if 0 < gi < len(rows) else 0, 24 + n * LANE if n else 0)
        lane0[gi] = y + (h - (n - 1) * LANE) / 2
        y += h
        if gi < len(rows):
            for p in rows[gi]:
                p.y = y
            y += PANEL_H
    bottom = y

    for k in keys:
        _emit_panel(page, view, panels[k], marked[k])

    for cab in routed:
        lane_y = {id(it): lane0[g] + it.lane * LANE for g, it in cab.items}
        if cab.corridor_x is None:
            ly = lane_y[id(cab.items[0][1])]
            pts = cab.a.escape(ly) + list(reversed(cab.b.escape(ly)))
        else:
            upper_first = cab.a.panel.row < cab.b.panel.row
            la = lane_y[id(cab.items[0][1])] if upper_first else lane_y[id(cab.items[1][1])]
            lb = lane_y[id(cab.items[1][1])] if upper_first else lane_y[id(cab.items[0][1])]
            cx = cab.corridor_x
            pts = [*cab.a.escape(la), (cx, la), (cx, lb), *reversed(cab.b.escape(lb))]
        pts = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
        color, width, _t, _n = CATEGORIES[cab.cat]
        style = (
            f"edgeStyle=none;html=1;rounded=1;endArrow=none;startArrow=none;strokeWidth={width};"
            f"strokeColor={color};jumpStyle=arc;jumpSize=10;"
            f"exitX=0.5;exitY={0 if cab.a.up else 1};exitDx=0;exitDy=0;"
            f"entryX=0.5;entryY={0 if cab.b.up else 1};entryDx=0;entryDy=0;"
        )
        page.edge(
            f"c-{cab.link.id}",
            style,
            cab.a.panel.port_cells[cab.a.idx],
            cab.b.panel.port_cells[cab.b.idx],
            points=pts,
        )

    _legend(page, max(bottom, page.max_y) + 40, part[1] > 1)


def _emit_panel(page: Page, view: SiteView, p: Panel, marked: dict[int, tuple[str, bool]]) -> None:
    d = p.device
    p.cell = page.vertex(f"fp-{d.id}", "", PANEL, p.x, p.y, p.width, PANEL_H)
    n_cabled = len(marked)
    nports = max([0, *(pt.idx for pt in d.ports)])
    lines = [f"<b>{esc(view.nodes[p.key].name)}</b>"]
    if d.ip:
        lines.append(gray(d.ip))
    lines.append(f'<font style="font-size:10px">{esc(d.model or "model ?")} &middot; {ROLE_TEXT[d.role]}</font>')
    ports_txt = f"{nports} port{'s' if nports != 1 else ''}" if nports else "ports not reported"
    lines.append(f'<font style="font-size:9px" color="#616161">{ports_txt} &middot; {n_cabled} cabled</font>')
    page.vertex(
        f"fp-{d.id}-lbl",
        "<br>".join(lines),
        TXT + "align=left;verticalAlign=top;fontSize=12;spacing=2;",
        6,
        2,
        p.label_w,
        PANEL_H - 4,
        p.cell,
    )
    for kind, first, last, x in p.elements:
        if kind == "free":
            page.vertex(
                f"fp-{d.id}-free{first}",
                f"{first}-{last}",
                FREE_BLOCK,
                x,
                TOP_ROW_Y,
                FREE_W,
                BOTTOM_ROW_Y + SQ_H - TOP_ROW_Y,
                p.cell,
            )
            continue
        for idx, y in ((first, TOP_ROW_Y), (last, BOTTOM_ROW_Y)):
            if idx not in p.port_x:
                continue
            if idx in marked:
                color, elsewhere = marked[idx]
                style = port_used(color, elsewhere)
            else:
                style = PORT_FREE
            p.port_cells[idx] = page.vertex(f"fp-{d.id}-p{idx}", str(idx), style, x, y, SQ_W, SQ_H, p.cell)


def _legend(page: Page, y: float, split: bool) -> None:
    rows = [k for k in CATEGORY_ORDER]
    h = 26 + len(rows) * 26 + 70 + (20 if split else 0)
    lg = page.vertex("legend", "Cable color code (purpose)", LEGEND, LEFT, y, 760, h)
    for i, k in enumerate(rows):
        color, width, text, cname = CATEGORIES[k]
        yy = 40 + i * 26
        page.line(f"lg-l{k}", f"endArrow=none;html=1;strokeWidth={width};strokeColor={color};", (14, yy), (74, yy), lg)
        page.vertex(
            f"lg-t{k}",
            f"<b>{cname}</b> {color} &middot; {esc(text)}",
            TXT + "align=left;verticalAlign=middle;fontSize=11;spacing=0;",
            86,
            yy - 10,
            660,
            20,
            lg,
        )
    yy = 40 + len(rows) * 26 - 8
    notes = [
        "Line hops = cables crossing, not connected. Gray block = run of free ports (range rotated inside).",
        "Colors mark purpose, not VLAN: use colored jackets where stocked, otherwise colored velcro or label "
        "sleeves at both ends printed with the cable ID (DAC/AOC come in fixed colors).",
    ]
    if split:
        notes.append("Dashed port outline = cabled, but the cable is drawn on another part of this page set.")
    page.vertex(
        "lg-notes",
        "<br>".join(esc(n) for n in notes),
        TXT + "align=left;verticalAlign=top;fontSize=10;fontColor=#616161;spacing=0;",
        14,
        yy,
        730,
        h - yy - 6,
        lg,
    )
