"""Color parsing helpers."""

import re
from typing import Optional

from pptx.dml.color import RGBColor

NAMED_COLORS = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "red": (255, 0, 0),
    "green": (0, 128, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "cyan": (0, 255, 255),
    "magenta": (255, 0, 255),
    "gray": (128, 128, 128),
    "grey": (128, 128, 128),
    "silver": (192, 192, 192),
    "maroon": (128, 0, 0),
    "olive": (128, 128, 0),
    "lime": (0, 255, 0),
    "aqua": (0, 255, 255),
    "teal": (0, 128, 128),
    "navy": (0, 0, 128),
    "fuchsia": (255, 0, 255),
    "purple": (128, 0, 128),
    "orange": (255, 165, 0),
    "pink": (255, 192, 203),
    "brown": (165, 42, 42),
    "transparent": None,
    "none": None,
}


def parse_color(val: Optional[str]) -> Optional[RGBColor]:
    """Parse SVG color value to RGBColor."""
    if not val:
        return None

    v = val.strip().lower()

    if v in ("none", "transparent", "inherit", "currentcolor"):
        return None

    if v in NAMED_COLORS:
        rgb = NAMED_COLORS[v]
        return RGBColor(*rgb) if rgb else None

    if v.startswith("#"):
        hex_color = v[1:]
        try:
            if len(hex_color) == 3:
                r = int(hex_color[0] * 2, 16)
                g = int(hex_color[1] * 2, 16)
                b = int(hex_color[2] * 2, 16)
            elif len(hex_color) == 6:
                r = int(hex_color[0:2], 16)
                g = int(hex_color[2:4], 16)
                b = int(hex_color[4:6], 16)
            elif len(hex_color) == 8:
                r = int(hex_color[0:2], 16)
                g = int(hex_color[2:4], 16)
                b = int(hex_color[4:6], 16)
            else:
                return None
            return RGBColor(r, g, b)
        except ValueError:
            return None

    rgb_match = re.match(r"rgba?\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", v)
    if rgb_match:
        try:
            r = int(rgb_match.group(1))
            g = int(rgb_match.group(2))
            b = int(rgb_match.group(3))
            return RGBColor(min(255, max(0, r)), min(255, max(0, g)), min(255, max(0, b)))
        except ValueError:
            return None

    return None
