"""Length/opacity/angle parsing helpers."""

from typing import Optional


def parse_length(val: Optional[str], default: float = 0.0) -> float:
    """Parse SVG length value (px, pt, mm, cm, in, %)."""
    if not val:
        return default
    raw = val.strip()

    if raw.endswith("%"):
        try:
            return float(raw[:-1]) / 100.0 * default if default else 0.0
        except ValueError:
            return default

    for suffix in ("px", "pt", "mm", "cm", "in", "em", "ex"):
        if raw.endswith(suffix):
            raw = raw[:-len(suffix)]
            break

    try:
        return float(raw)
    except ValueError:
        return default


def parse_opacity(val: Optional[str]) -> float:
    """Parse opacity value (0.0 to 1.0)."""
    if not val:
        return 1.0
    try:
        opacity = float(val.strip())
        return max(0.0, min(1.0, opacity))
    except ValueError:
        return 1.0


def normalize_rotation(angle: float) -> float:
    """Normalize rotation angle to -180..180 degrees."""
    while angle <= -180:
        angle += 360
    while angle > 180:
        angle -= 360
    return angle
