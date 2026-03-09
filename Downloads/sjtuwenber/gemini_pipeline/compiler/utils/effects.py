"""SVG filter helpers (currently focused on simple drop shadows)."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from typing import Any, Dict

from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement

from .colors import parse_color
from .lengths import parse_length, parse_opacity
from .svg_helpers import tag_name


def extract_simple_drop_shadow_filters(root: ET.Element) -> Dict[str, Dict[str, float]]:
    """Extract feDropShadow definitions keyed by filter id."""
    filters: Dict[str, Dict[str, float]] = {}
    for elem in root.iter():
        if tag_name(elem) != "filter":
            continue
        fid = (elem.get("id") or "").strip()
        if not fid:
            continue
        ds = None
        for child in elem:
            if tag_name(child) == "feDropShadow":
                ds = child
                break
        if ds is None:
            continue

        dx = parse_length(ds.get("dx"), 0.0)
        dy = parse_length(ds.get("dy"), 0.0)
        blur = parse_length(ds.get("stdDeviation"), 0.0)
        color = (ds.get("flood-color") or "#000000").strip()
        opacity = parse_opacity(ds.get("flood-opacity"))
        rgb = parse_color(color)
        if rgb is None:
            continue

        filters[fid] = {
            "dx": float(dx),
            "dy": float(dy),
            "std": float(blur),
            "r": float(rgb[0]),
            "g": float(rgb[1]),
            "b": float(rgb[2]),
            "opacity": float(opacity),
        }
    return filters


def apply_svg_filter_shadow_if_needed(
    shape: Any,
    elem: ET.Element,
    converter: Any,
    filters: Dict[str, Dict[str, float]],
) -> None:
    """Apply feDropShadow (`filter=url(#id)`) to a PPT shape's OOXML effect list."""
    filter_ref = (elem.get("filter") or "").strip()
    if not (filter_ref.startswith("url(#") and filter_ref.endswith(")")):
        return
    fid = filter_ref[5:-1].strip()
    if not fid:
        return
    spec = filters.get(fid)
    if not spec:
        return

    try:
        dx = float(spec.get("dx", 0.0))
        dy = float(spec.get("dy", 0.0))
        std = float(spec.get("std", 0.0))
        dist = math.sqrt(dx * dx + dy * dy)

        # OOXML dir: 1 degree = 60000 units.
        dir_deg = (math.degrees(math.atan2(dy, dx)) + 360.0) % 360.0
        dir_ooxml = str(int(round(dir_deg * 60000.0)))
        blur_rad = max(0, converter.to_emu_length(std * 2.0))
        dist_emu = max(0, converter.to_emu_length(dist))

        sp = shape.element
        sp_pr = sp.spPr
        if sp_pr is None:
            return

        for child in list(sp_pr):
            if child.tag in (qn("a:effectLst"), qn("a:effectDag")):
                sp_pr.remove(child)

        effect_lst = OxmlElement("a:effectLst")
        outer = OxmlElement("a:outerShdw")
        outer.set("blurRad", str(int(blur_rad)))
        outer.set("dist", str(int(dist_emu)))
        outer.set("dir", dir_ooxml)
        outer.set("algn", "ctr")
        outer.set("rotWithShape", "0")

        srgb = OxmlElement("a:srgbClr")
        r = int(max(0, min(255, round(spec.get("r", 0.0)))))
        g = int(max(0, min(255, round(spec.get("g", 0.0)))))
        b = int(max(0, min(255, round(spec.get("b", 0.0)))))
        srgb.set("val", f"{r:02X}{g:02X}{b:02X}")

        alpha = OxmlElement("a:alpha")
        alpha_val = int(max(0, min(100000, round(float(spec.get("opacity", 1.0)) * 100000.0))))
        alpha.set("val", str(alpha_val))
        srgb.append(alpha)
        outer.append(srgb)
        effect_lst.append(outer)
        sp_pr.append(effect_lst)
    except Exception:
        return
