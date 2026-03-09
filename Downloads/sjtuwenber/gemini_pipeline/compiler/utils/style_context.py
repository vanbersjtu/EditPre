"""Style inheritance context for SVG element traversal."""

from __future__ import annotations

from dataclasses import dataclass
import xml.etree.ElementTree as ET
from typing import Dict, Optional

from .lengths import parse_opacity


def parse_style_attr(style_attr: str) -> Dict[str, str]:
    """Parse `style='k:v; ...'` into a normalized dict."""
    style_dict: Dict[str, str] = {}
    for part in (style_attr or "").split(";"):
        if ":" not in part:
            continue
        key, val = part.split(":", 1)
        key = key.strip().lower()
        if key:
            style_dict[key] = val.strip()
    return style_dict


@dataclass
class StyleContext:
    """Track inherited style properties through SVG hierarchy."""

    fill: Optional[str] = None
    stroke: Optional[str] = None
    stroke_width: Optional[str] = None
    opacity: float = 1.0
    fill_opacity: float = 1.0
    stroke_opacity: float = 1.0
    font_family: Optional[str] = None
    font_size: Optional[str] = None
    font_weight: Optional[str] = None
    text_anchor: Optional[str] = None
    stroke_linecap: Optional[str] = None
    stroke_linejoin: Optional[str] = None

    def copy(self) -> "StyleContext":
        return StyleContext(
            fill=self.fill,
            stroke=self.stroke,
            stroke_width=self.stroke_width,
            opacity=self.opacity,
            fill_opacity=self.fill_opacity,
            stroke_opacity=self.stroke_opacity,
            font_family=self.font_family,
            font_size=self.font_size,
            font_weight=self.font_weight,
            text_anchor=self.text_anchor,
            stroke_linecap=self.stroke_linecap,
            stroke_linejoin=self.stroke_linejoin,
        )

    def update_from_element(self, elem: ET.Element) -> "StyleContext":
        """Create a child context with inline style + direct attribute overrides."""
        ctx = self.copy()
        style_dict = parse_style_attr(elem.get("style", ""))

        def get_style(name: str, attr_name: Optional[str] = None) -> Optional[str]:
            attr_name = attr_name or name
            return elem.get(attr_name) or style_dict.get(name)

        if get_style("fill"):
            ctx.fill = get_style("fill")
        if get_style("stroke"):
            ctx.stroke = get_style("stroke")
        if get_style("stroke-width"):
            ctx.stroke_width = get_style("stroke-width")
        if get_style("opacity"):
            ctx.opacity = parse_opacity(get_style("opacity")) * ctx.opacity
        if get_style("fill-opacity"):
            ctx.fill_opacity = parse_opacity(get_style("fill-opacity"))
        if get_style("stroke-opacity"):
            ctx.stroke_opacity = parse_opacity(get_style("stroke-opacity"))
        if get_style("font-family"):
            ctx.font_family = get_style("font-family")
        if get_style("font-size"):
            ctx.font_size = get_style("font-size")
        if get_style("font-weight"):
            ctx.font_weight = get_style("font-weight")
        if get_style("text-anchor"):
            ctx.text_anchor = get_style("text-anchor")
        if get_style("stroke-linecap"):
            ctx.stroke_linecap = get_style("stroke-linecap")
        if get_style("stroke-linejoin"):
            ctx.stroke_linejoin = get_style("stroke-linejoin")
        return ctx
