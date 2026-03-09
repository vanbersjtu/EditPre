"""Visual grouping metadata helpers."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Dict

from ..utils.svg_helpers import tag_name


def extract_visual_group_meta(root: ET.Element) -> Dict[str, Dict[str, object]]:
    """Extract visual-group metadata declared in visual-layer."""
    metas: Dict[str, Dict[str, object]] = {}
    encounter_idx = 0
    for elem in root.iter():
        if tag_name(elem) != "g":
            continue
        if elem.get("data-type") != "visual-group":
            continue
        encounter_idx += 1
        gid = (elem.get("id") or elem.get("data-visual-group") or "").strip()
        if not gid:
            continue
        raw_order = (elem.get("data-order") or "").strip()
        if raw_order:
            try:
                order_val = float(raw_order)
            except Exception:
                order_val = float(encounter_idx)
        else:
            # If data-order is absent, preserve the original SVG declaration order.
            order_val = float(encounter_idx)
        role_val = (elem.get("data-role") or "").strip()
        if role_val == "background" or "background" in gid.lower():
            order_val = min(order_val, -1000000.0)

        metas[gid] = {
            "id": gid,
            "order": order_val,
            "role": role_val,
        }
    return metas

