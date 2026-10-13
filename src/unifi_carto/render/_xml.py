"""Minimal mxGraph page builder on top of ``xml.etree`` (attribute escaping is done by ElementTree)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence

_SLUG = re.compile(r"[^A-Za-z0-9_.-]+")
_SPACE = re.compile(r"[\t\n\r]")
# C0 controls, lone surrogates and U+FFFE/U+FFFF are not allowed in XML 1.0 documents.
_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")

Point = tuple[float, float]


def slug(value: str) -> str:
    """Reduce a model id to characters that are safe and readable in a cell id."""
    return _SLUG.sub("-", value).strip("-") or "x"


def clean(value: str) -> str:
    """Make device-supplied text safe in XML: tab/newline become spaces, other C0 controls are dropped."""
    return _INVALID.sub("", _SPACE.sub(" ", value))


def num(value: float) -> str:
    """Format a coordinate: integers without a decimal point, else one decimal."""
    r = round(value, 1)
    return str(int(r)) if r == int(r) else f"{r:.1f}"


class Page:
    """One ``<diagram>``: collects cells, hands out unique ids and tracks the drawing extent."""

    def __init__(self, diagram_id: str, name: str, prefix: str) -> None:
        self.diagram_id = diagram_id
        self.name = clean(name)
        self.prefix = prefix
        self._cells: list[ET.Element] = []
        self._ids: set[str] = {"0", "1"}
        self.max_x = 0.0
        self.max_y = 0.0

    def new_id(self, key: str) -> str:
        base = f"{self.prefix}-{slug(key)}"
        cid, n = base, 2
        while cid in self._ids:
            cid, n = f"{base}-{n}", n + 1
        self._ids.add(cid)
        return cid

    def _extend(self, x: float, y: float) -> None:
        self.max_x = max(self.max_x, x)
        self.max_y = max(self.max_y, y)

    def vertex(
        self, key: str, value: str, style: str, x: float, y: float, w: float, h: float, parent: str = "1"
    ) -> str:
        cid = self.new_id(key)
        cell = ET.Element("mxCell", {"id": cid, "value": clean(value), "style": style, "vertex": "1", "parent": parent})
        ET.SubElement(
            cell, "mxGeometry", {"x": num(x), "y": num(y), "width": num(w), "height": num(h), "as": "geometry"}
        )
        self._cells.append(cell)
        if parent == "1":
            self._extend(x + w, y + h)
        return cid

    def edge(
        self,
        key: str,
        style: str,
        source: str,
        target: str,
        *,
        value: str = "",
        parent: str = "1",
        points: Sequence[Point] = (),
        label_pos: float | None = None,
    ) -> str:
        cid = self.new_id(key)
        cell = ET.Element(
            "mxCell",
            {
                "id": cid,
                "value": clean(value),
                "style": style,
                "edge": "1",
                "parent": parent,
                "source": source,
                "target": target,
            },
        )
        attrs = {"relative": "1", "as": "geometry"}
        if label_pos is not None:
            attrs = {"x": f"{round(label_pos, 3):g}", **attrs}
        geo = ET.SubElement(cell, "mxGeometry", attrs)
        if points:
            arr = ET.SubElement(geo, "Array", {"as": "points"})
            for x, y in points:
                ET.SubElement(arr, "mxPoint", {"x": num(x), "y": num(y)})
                self._extend(x, y)
        self._cells.append(cell)
        return cid

    def line(self, key: str, style: str, p1: Point, p2: Point, parent: str = "1") -> str:
        """A free-standing edge with explicit endpoints (legend sample lines)."""
        cid = self.new_id(key)
        cell = ET.Element("mxCell", {"id": cid, "value": "", "style": style, "edge": "1", "parent": parent})
        geo = ET.SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})
        ET.SubElement(geo, "mxPoint", {"x": num(p1[0]), "y": num(p1[1]), "as": "sourcePoint"})
        ET.SubElement(geo, "mxPoint", {"x": num(p2[0]), "y": num(p2[1]), "as": "targetPoint"})
        self._cells.append(cell)
        return cid

    def element(self) -> ET.Element:
        diagram = ET.Element("diagram", {"id": self.diagram_id, "name": self.name})
        model = ET.SubElement(
            diagram,
            "mxGraphModel",
            {
                "adaptiveColors": "auto",
                "grid": "1",
                "gridSize": "10",
                "page": "1",
                "pageWidth": num(max(850.0, self.max_x + 40)),
                "pageHeight": num(max(1100.0, self.max_y + 40)),
            },
        )
        root = ET.SubElement(model, "root")
        ET.SubElement(root, "mxCell", {"id": "0"})
        ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})
        root.extend(self._cells)
        return diagram
