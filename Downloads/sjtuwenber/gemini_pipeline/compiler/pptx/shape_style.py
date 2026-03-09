"""Shape fill/stroke style helpers."""

from __future__ import annotations

from typing import Any, Optional

from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Pt

from ..pptx.coordinates import CoordinateConverter
from ..utils.colors import parse_color
from ..utils.lengths import parse_length


def apply_fill_to_shape(shape: Any, fill_color: Optional[str], opacity: float = 1.0) -> None:
    """Apply fill color to a PPTX shape."""
    color = parse_color(fill_color)
    if color is None or fill_color in ("none", "transparent"):
        shape.fill.background()
    else:
        shape.fill.solid()
        shape.fill.fore_color.rgb = color
        # Note: Opacity requires OXML manipulation for full support.


def apply_stroke_to_shape(
    shape: Any,
    stroke_color: Optional[str],
    stroke_width: Optional[str],
    converter: CoordinateConverter,
    opacity: float = 1.0,
    stroke_linecap: Optional[str] = None,
    stroke_linejoin: Optional[str] = None,
) -> None:
    """Apply stroke to a PPTX shape."""
    color = parse_color(stroke_color)
    if color is None or stroke_color in ("none", "transparent"):
        shape.line.fill.background()
    else:
        shape.line.color.rgb = color
        if stroke_width:
            width_px = parse_length(stroke_width, 1.0)
            # Convert to EMU, then to Pt for line width.
            shape.line.width = Pt(width_px * 0.75)  # Approximate px to pt.
        # Improve edge appearance by preserving SVG cap/join style on OOXML line.
        try:
            ln = shape.line._get_or_add_ln()
            cap_raw = (stroke_linecap or "").strip().lower()
            cap_map = {"round": "rnd", "square": "sq", "butt": "flat"}
            cap_val = cap_map.get(cap_raw)
            if cap_val:
                ln.set("cap", cap_val)
            join_raw = (stroke_linejoin or "").strip().lower()
            for child in list(ln):
                if child.tag in (qn("a:round"), qn("a:bevel"), qn("a:miter")):
                    ln.remove(child)
            if join_raw == "round":
                ln.append(OxmlElement("a:round"))
            elif join_raw == "bevel":
                ln.append(OxmlElement("a:bevel"))
            elif join_raw in ("miter", "miter-clip", "arcs"):
                ln.append(OxmlElement("a:miter"))
        except Exception:
            pass
        # Note: Opacity requires OXML manipulation for full support.

