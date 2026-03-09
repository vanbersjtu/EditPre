"""Basic SVG helper utilities."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, List

NATURAL_SORT_RE = re.compile(r"(\d+)")


def tag_name(elem: ET.Element) -> str:
    """Extract local tag name without namespace."""
    return elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag


def natural_sort_key(path: Path) -> List[Any]:
    """Natural sort key (e.g., 幻灯片1, 幻灯片2, 幻灯片10)."""
    parts = NATURAL_SORT_RE.split(path.name)
    return [int(p) if p.isdigit() else p.lower() for p in parts]
