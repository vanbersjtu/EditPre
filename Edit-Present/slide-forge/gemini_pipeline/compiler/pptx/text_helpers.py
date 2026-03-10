"""Text helper utilities shared by SVG text reconstruction paths."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Dict, List

from ..utils.svg_helpers import tag_name
from ..utils.text import CJK_RE, has_cjk


def read_text_content(elem: ET.Element) -> str:
    """Read text content from SVG text element including tspans."""
    parts: List[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in elem.iter():
        if child is elem:
            continue
        if tag_name(child) == "tspan" and child.text:
            parts.append(child.text)
    return "".join(parts).replace("\u00a0", " ").strip()


def group_text_lines(items: List[Dict[str, object]], line_tol: float) -> List[Dict[str, object]]:
    """Group text items into lines by y coordinate."""
    if not items:
        return []

    sorted_items = sorted(items, key=lambda it: float(it.get("y", 0.0)))
    groups: List[List[Dict[str, object]]] = []
    current: List[Dict[str, object]] = []
    current_y: float = 0.0

    for item in sorted_items:
        y = float(item.get("y", 0.0))
        if not current:
            current = [item]
            current_y = y
            continue
        if abs(y - current_y) <= line_tol:
            current.append(item)
            current_y = (current_y * (len(current) - 1) + y) / len(current)
        else:
            groups.append(current)
            current = [item]
            current_y = y
    if current:
        groups.append(current)

    out: List[Dict[str, object]] = []
    for group in groups:
        out.append({"items": sorted(group, key=lambda it: float(it.get("x", 0.0)))})
    return out


def should_insert_space(prev_item: Dict[str, object], curr_item: Dict[str, object]) -> bool:
    """Determine whether to insert a synthetic space between adjacent text runs."""
    prev_text = str(prev_item.get("text", "") or "")
    curr_text = str(curr_item.get("text", "") or "")

    if not prev_text or not curr_text:
        return False
    if prev_text.endswith(" ") or curr_text.startswith(" "):
        return False

    # Never auto-insert spaces for CJK neighboring text.
    if has_cjk(prev_text) or has_cjk(curr_text):
        return False

    # For punctuation boundaries, keep compact by default.
    if prev_text[-1] in ",.;:!?)]}" or curr_text[0] in ",.;:!?)]}":
        return False

    # If either side isn't alnum-like, avoid forcing spaces.
    if not re.search(r"[A-Za-z0-9]", prev_text) or not re.search(r"[A-Za-z0-9]", curr_text):
        return False

    prev_x = float(prev_item.get("x", 0.0))
    prev_w = float(prev_item.get("w", 0.0))
    curr_x = float(curr_item.get("x", 0.0))
    gap = curr_x - (prev_x + prev_w)

    prev_font = float(prev_item.get("font_size", 16.0) or 16.0)
    curr_font = float(curr_item.get("font_size", prev_font) or prev_font)
    fs = max(1.0, (prev_font + curr_font) / 2.0)

    # Tight threshold: require a visible gap to insert synthetic space.
    return gap >= max(2.0, 0.22 * fs)


def assemble_line_text(items: List[Dict[str, object]]) -> str:
    """Assemble a line text from ordered text items with synthetic spaces."""
    if not items:
        return ""

    parts: List[str] = []
    prev_item: Dict[str, object] = {}
    for idx, item in enumerate(items):
        txt = str(item.get("text", "") or "")
        if idx > 0 and prev_item and should_insert_space(prev_item, item):
            parts.append(" ")
        parts.append(txt)
        prev_item = item
    return "".join(parts)


def estimate_text_width_px(text: str, font_size: float, letter_spacing: float = 0.0) -> float:
    """Estimate line width in px with coarse per-script heuristics."""
    if not text:
        return 0.0

    width = 0.0
    for ch in text:
        if ch == " ":
            width += font_size * 0.33
        elif CJK_RE.search(ch):
            width += font_size * 0.95
        elif ch.isupper():
            width += font_size * 0.67
        elif ch.isdigit():
            width += font_size * 0.58
        elif ch in "ilI|":
            width += font_size * 0.30
        elif ch in "mwMW":
            width += font_size * 0.85
        elif ch in ",.;:'\"`":
            width += font_size * 0.26
        elif ch in "()[]{}":
            width += font_size * 0.34
        else:
            width += font_size * 0.56

    if len(text) > 1 and letter_spacing:
        width += letter_spacing * (len(text) - 1)

    return width


def baseline_to_top_offset_px(font_size: float, text: str) -> float:
    """Estimate baseline-to-top offset in px for SVG baseline anchoring."""
    # Empirical baseline ratio: CJK tends closer to full em box.
    ratio = 0.90 if has_cjk(text) else 0.82
    return font_size * ratio

