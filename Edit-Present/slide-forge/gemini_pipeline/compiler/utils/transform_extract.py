"""Helpers to extract simple rotation/translation from transform strings."""

from __future__ import annotations

import math
import re
from typing import Optional, Tuple

from .lengths import normalize_rotation
from .transforms import MATRIX_RE, TRANSLATE_RE


def parse_transform_rotation(transform: Optional[str]) -> Optional[float]:
    """Extract rotation angle from SVG transform attribute."""
    if not transform:
        return None
    if "rotate(" in transform:
        match = re.search(r"rotate\s*\(\s*([^)]+)\s*\)", transform)
        if match:
            parts = [p for p in re.split(r"[\s,]+", match.group(1).strip()) if p]
            if parts:
                try:
                    return normalize_rotation(float(parts[0]))
                except Exception:
                    return None
    match = MATRIX_RE.search(transform)
    if match:
        parts = [p for p in re.split(r"[\s,]+", match.group(1).strip()) if p]
        if len(parts) >= 4:
            try:
                a = float(parts[0])
                b = float(parts[1])
                if abs(a) < 1e-12 and abs(b) < 1e-12:
                    return None
                angle = math.degrees(math.atan2(b, a))
                return normalize_rotation(angle)
            except Exception:
                return None
    return None


def parse_transform_xy(transform: Optional[str]) -> Tuple[float, float]:
    """Extract translation (x, y) from SVG transform attribute."""
    if not transform:
        return (0.0, 0.0)
    match = MATRIX_RE.search(transform)
    if match:
        parts = [p for p in re.split(r"[\s,]+", match.group(1).strip()) if p]
        if len(parts) == 6:
            try:
                return (float(parts[4]), float(parts[5]))
            except Exception:
                return (0.0, 0.0)
    match = TRANSLATE_RE.search(transform)
    if match:
        parts = [p for p in re.split(r"[\s,]+", match.group(1).strip()) if p]
        if parts:
            try:
                x = float(parts[0])
                y = float(parts[1]) if len(parts) > 1 else 0.0
                return (x, y)
            except Exception:
                return (0.0, 0.0)
    return (0.0, 0.0)

