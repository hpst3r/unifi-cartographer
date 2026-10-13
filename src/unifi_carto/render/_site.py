"""Per-site view of an Inventory: nodes, layout tree, tiers, link groups, cable ids and shared formatting."""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape

from unifi_carto.model import Console, Device, ExternalPeer, Inventory, Link, LinkEnd, Network, Port, Site

ROLE_RANK = {"gateway": 0, "switch": 1, "other": 2, "access_point": 3}
EXTERNAL_RANK = 4
ROLE_TEXT = {"gateway": "gateway", "switch": "switch", "access_point": "access point", "other": "other"}
WIRELESS_KINDS = ("wireless", "mesh")

GRAY = "#555555"


def esc(text: str) -> str:
    return escape(text, quote=False)


def gray(text: str, size: int = 11) -> str:
    return f'<font style="font-size:{size}px" color="{GRAY}">{esc(text)}</font>'


def fmt_speed(mbps: int | None) -> str:
    if mbps is None:
        return "?"
    if mbps >= 1000 and mbps % 100 == 0:
        return f"{mbps / 1000:g}G"
    return f"{mbps}M"


def fmt_ports(idxs: list[int | None]) -> str:
    """``[49, 50]`` -> ``p49-50``; ``[1, 3]`` -> ``p1,3``; any unknown -> ``?``."""
    if not idxs or any(i is None for i in idxs):
        return "?"
    known = sorted({i for i in idxs if i is not None})
    runs: list[str] = []
    start = prev = known[0]
    for i in [*known[1:], None]:
        if i is not None and i == prev + 1:
            prev = i
            continue
        runs.append(str(start) if start == prev else f"{start}-{prev}")
        if i is not None:
            start = prev = i
    return "p" + ",".join(runs)


def external_ident(peer: ExternalPeer) -> str:
    return (peer.chassis_id or peer.system_name or peer.port_id or "unknown").lower()


@dataclass
class Node:
    key: str
    device: Device | None = None
    external: ExternalPeer | None = None
    missing_id: str | None = None
    parent: str | None = None
    children: list[str] = field(default_factory=list)
    tier: int = 0
    x: float = 0.0  # center x in the logical layout (also used as an ordering key)

    @property
    def rank(self) -> int:
        return ROLE_RANK[self.device.role] if self.device else EXTERNAL_RANK

    @property
    def role(self) -> str:
        return self.device.role if self.device else "external"

    @property
    def name(self) -> str:
        if self.device:
            return device_name(self.device)
        if self.external:
            p = self.external
            return p.system_name or p.chassis_id or p.port_id or "LLDP neighbor"
        return f"unknown device {self.missing_id}"


def device_name(d: Device) -> str:
    if d.name:
        return d.name
    return f"{d.model or ROLE_TEXT[d.role]} {d.mac[-5:]}"


@dataclass
class LinkGroup:
    """Links drawn as one logical edge: a LAG bundle between two nodes, or a single link."""

    key: str
    links: list[Link]
    a_key: str
    b_key: str
    lag_id: str | None
    kind: str

    @property
    def a_ports(self) -> list[int | None]:
        return [lk.a.port_idx for lk in self.links]

    @property
    def b_ports(self) -> list[int | None]:
        return [lk.b.port_idx for lk in self.links]

    def label(self) -> str:
        """``p49 - p1``, ``p49 - ?``, ``LAG p49-50 - p1-2``; LLDP peers show their port id."""
        if self.kind in WIRELESS_KINDS:
            return self.kind
        a = fmt_ports(self.a_ports)
        peer = self.links[0].b.external
        b = (peer.port_id or "?") if peer is not None else fmt_ports(self.b_ports)
        if self.lag_id:
            return f"LAG {a} - {b}"
        return f"{a} - {b}"


class SiteView:
    def __init__(self, inv: Inventory, site: Site) -> None:
        self.inv = inv
        self.site = site
        self.console: Console | None = next((c for c in inv.consoles if c.id == site.console_id), None)
        self.devices: dict[str, Device] = {
            d.id: d for d in sorted(inv.devices, key=lambda d: d.id) if d.site_id == site.id
        }
        self.links: list[Link] = sorted((lk for lk in inv.links if lk.site_id == site.id), key=lambda lk: lk.id)
        self.networks: list[Network] = sorted(
            (n for n in inv.networks if n.site_id == site.id), key=lambda n: (n.vlan_id or 0, n.name, n.id)
        )
        self.nodes: dict[str, Node] = {}
        for d in self.devices.values():
            self.nodes[f"d:{d.id}"] = Node(key=f"d:{d.id}", device=d)
        for lk in self.links:
            for end in (lk.a, lk.b):
                k = self.end_key(end)
                if k not in self.nodes:
                    self.nodes[k] = Node(key=k, external=end.external, missing_id=end.device_id)
        self._build_tree()
        self._layout()
        self.order: list[str] = sorted(self.nodes, key=lambda k: (self.nodes[k].tier, self.nodes[k].x, k))
        self._pos = {k: i for i, k in enumerate(self.order)}
        self.groups: list[LinkGroup] = self._group_links()
        self.cable_ids: dict[str, str] = self._number_cables()

    # ------------------------------------------------------------------ keys and lookups
    def end_key(self, end: LinkEnd) -> str:
        if end.device_id is not None:
            return f"d:{end.device_id}" if end.device_id in self.devices else f"m:{end.device_id}"
        assert end.external is not None
        return f"x:{external_ident(end.external)}"

    def port(self, end: LinkEnd) -> Port | None:
        if end.device_id is None or end.port_idx is None or end.device_id not in self.devices:
            return None
        return next((p for p in self.devices[end.device_id].ports if p.idx == end.port_idx), None)

    def end_text(self, end: LinkEnd) -> str:
        """``device:port (port name)`` for tables."""
        node = self.nodes[self.end_key(end)]
        if end.external is not None:
            peer = end.external
            extra = f" ({peer.chassis_id})" if peer.chassis_id and peer.chassis_id != node.name else ""
            return f"{node.name}{extra}:{peer.port_id or '?'} (LLDP)"
        port = self.port(end)
        pname = f" ({port.name})" if port is not None and port.name else ""
        idx = f"p{end.port_idx}" if end.port_idx is not None else "?"
        return f"{node.name}:{idx}{pname}"

    def network_name(self, vlan: int) -> str | None:
        return next((n.name for n in self.networks if n.vlan_id == vlan), None)

    # ------------------------------------------------------------------ tree
    def _build_tree(self) -> None:
        for k, node in self.nodes.items():
            if node.device is not None:
                up = node.device.uplink_device_id
                if up is not None and up in self.devices and up != node.device.id:
                    node.parent = f"d:{up}"
                    continue
                cands = sorted(
                    (lk.source == "lldp", lk.id, self.end_key(lk.b))
                    for lk in self.links
                    if self.end_key(lk.a) == k and lk.b.device_id in self.devices and self.end_key(lk.b) != k
                )
                node.parent = cands[0][2] if cands else None
            else:
                for lk in self.links:
                    ka, kb = self.end_key(lk.a), self.end_key(lk.b)
                    other = kb if ka == k else ka if kb == k else None
                    if other is not None and other.startswith("d:"):
                        node.parent = other
                        break
        # break cycles: walk up from every node; a revisit cuts the closing edge
        for k in sorted(self.nodes):
            seen: set[str] = set()
            cur: str | None = k
            while cur is not None:
                if cur in seen:
                    self.nodes[cur].parent = None
                    break
                seen.add(cur)
                cur = self.nodes[cur].parent
        for k in sorted(self.nodes):
            p = self.nodes[k].parent
            if p is not None:
                self.nodes[p].children.append(k)
        for node in self.nodes.values():
            node.children.sort(key=self._sort_key)

    def _sort_key(self, k: str) -> tuple[int, str, str]:
        n = self.nodes[k]
        return (n.rank, n.name.lower(), k)

    def _depth(self, k: str) -> int:
        d, cur = 0, self.nodes[k]
        while cur.parent is not None:
            d, cur = d + 1, self.nodes[cur.parent]
        return d + (0 if cur.role == "gateway" else 1)

    def _has_ap_ancestor(self, k: str) -> bool:
        p = self.nodes[k].parent
        while p is not None:
            if self.nodes[p].role == "access_point":
                return True
            p = self.nodes[p].parent
        return False

    def roots(self) -> list[str]:
        return sorted((k for k, n in self.nodes.items() if n.parent is None), key=self._sort_key)

    def _layout(self) -> None:
        wired = [
            self._depth(k)
            for k, n in self.nodes.items()
            if n.role in ("gateway", "switch", "other") and not self._has_ap_ancestor(k)
        ]
        self.ap_base = max([1, *(d + 1 for d in wired)])

        # Children sit one tier below their uplink. APs are leaves, so they end up at the bottom of their
        # subtree; an AP on an aggregation switch stays next to the access switches instead of being pushed
        # to the AP row, where its link would have to cross the access tier.
        def assign(k: str, tier: int) -> None:
            node = self.nodes[k]
            node.tier = tier
            for c in node.children:
                assign(c, tier + 1)

        for r in self.roots():
            role = self.nodes[r].role
            assign(r, 0 if role == "gateway" else self.ap_base if role == "access_point" else 1)

    def place(self, col_w: float, left: float) -> float:
        """Tidy tree placement: leaves get one column each, parents center over their children.

        Returns the right edge. Sets ``Node.x`` (center)."""

        def width(k: str) -> float:
            return max(col_w, sum(width(c) for c in self.nodes[k].children))

        def put(k: str, x0: float) -> None:
            node = self.nodes[k]
            if node.children:
                cur = x0
                for c in node.children:
                    put(c, cur)
                    cur += width(c)
                node.x = (self.nodes[node.children[0]].x + self.nodes[node.children[-1]].x) / 2
            else:
                node.x = x0 + col_w / 2

        cur = left
        for r in self.roots():
            put(r, cur)
            cur += width(r)
        return cur

    # ------------------------------------------------------------------ links
    def link_order(self, lk: Link) -> tuple[int, int, int, int, str]:
        return (
            self._pos[self.end_key(lk.b)],
            lk.b.port_idx or 0,
            self._pos[self.end_key(lk.a)],
            lk.a.port_idx or 0,
            lk.id,
        )

    def _group_links(self) -> list[LinkGroup]:
        groups: dict[str, LinkGroup] = {}
        for lk in sorted(self.links, key=self.link_order):
            ka, kb = self.end_key(lk.a), self.end_key(lk.b)
            gk = f"lag|{lk.lag_id}|{ka}|{kb}" if lk.lag_id else f"link|{lk.id}"
            if gk in groups:
                groups[gk].links.append(lk)
            else:
                groups[gk] = LinkGroup(key=gk, links=[lk], a_key=ka, b_key=kb, lag_id=lk.lag_id, kind=lk.kind)
        return list(groups.values())

    def group_of(self, lk: Link) -> LinkGroup:
        return next(g for g in self.groups if lk in g.links)

    def _number_cables(self) -> dict[str, str]:
        ids: dict[str, str] = {}
        cable = wifi = 0
        for lk in sorted(self.links, key=self.link_order):
            if lk.kind in WIRELESS_KINDS:
                wifi += 1
                ids[lk.id] = f"W-{wifi:02d}"
            else:
                cable += 1
                ids[lk.id] = f"C-{cable:03d}"
        return ids

    def category(self, lk: Link) -> str:
        if lk.kind in WIRELESS_KINDS:
            return "wireless"
        if lk.lag_id:
            return "lag"
        roles = {self.nodes[self.end_key(e)].role for e in (lk.a, lk.b)}
        if "gateway" in roles:
            return "gateway"
        if "access_point" in roles:
            return "ap"
        if roles == {"switch"}:
            return "trunk"
        return "other"

    def drawable(self, lk: Link) -> bool:
        """A link the Cables page can draw: physical, both ends managed devices with known ports."""
        return (
            lk.kind not in WIRELESS_KINDS
            and lk.a.device_id in self.devices
            and lk.b.device_id in self.devices
            and lk.a.device_id != lk.b.device_id
            and lk.a.port_idx is not None
            and lk.b.port_idx is not None
        )

    def missing_port_links(self) -> int:
        return sum(
            1
            for lk in self.links
            if lk.kind not in WIRELESS_KINDS
            and (lk.a.port_idx is None or (lk.b.external is None and lk.b.port_idx is None))
        )

    def subtitle(self) -> str:
        con = self.console.name if self.console else self.site.console_id
        when = self.inv.generated_at.strftime("%Y-%m-%d %H:%M %Z").strip()
        return (
            f"Console {esc(con)} &middot; site {esc(self.site.name)} &middot; {len(self.devices)} devices, "
            f"{len(self.links)} links &middot; inventory generated {esc(when)} by unifi-cartographer "
            f"{esc(self.inv.collector_version)}"
        )


# Cable purpose palette: (color, width px, legend text, color name)
CATEGORIES: dict[str, tuple[str, int, str, str]] = {
    "gateway": ("#C62828", 3, "Uplink to the gateway", "red"),
    "trunk": ("#1565C0", 3, "Inter-switch trunk", "blue"),
    "lag": ("#7B1FA2", 3, "LAG member (aggregated link)", "purple"),
    "ap": ("#2E7D32", 2, "Access point uplink", "green"),
    "other": ("#757575", 2, "LLDP neighbor / other device", "gray"),
    "wireless": ("#757575", 2, "Wireless or mesh uplink (no cable)", "gray, dotted"),
}
CATEGORY_ORDER = ["gateway", "trunk", "lag", "ap", "other"]
