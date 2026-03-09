"""PPT text run style helpers for SVG semantic/text rendering."""

from __future__ import annotations

from typing import Dict, Optional

from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Pt

from ..utils.colors import parse_color
from ..utils.text import GENERIC_FONTS, has_cjk


def font_family_is_theme(font_family: Optional[str]) -> bool:
    if not font_family:
        return False
    ff = str(font_family).strip().lower()
    if not ff:
        return False
    return ff in {"theme", "minor", "major", "+mn-lt", "+mj-lt", "inherit", "unset", "initial"}


def pick_font_name(font_family: Optional[str], text: str, cjk_font: str) -> Optional[str]:
    if font_family:
        for raw in str(font_family).split(","):
            token = raw.strip().strip("'\"")
            if not token:
                continue
            token_lower = token.lower()
            if token_lower in GENERIC_FONTS:
                continue
            if "msfontservice" in token_lower:
                continue
            if any(c.isalpha() for c in token):
                return token
    if has_cjk(text) and cjk_font:
        return cjk_font
    return None


def set_run_ea_font(run, font_name: str) -> None:
    """Set East Asian font for a text run."""
    if not font_name:
        return
    r_pr = run._r.get_or_add_rPr()
    ea = r_pr.find(qn("a:ea"))
    if ea is None:
        ea = OxmlElement("a:ea")
        r_pr.append(ea)
    ea.set("typeface", font_name)


def set_run_font_size_from_px(run, font_size_px: float, scale: float = 1.0) -> None:
    """Set run font size from SVG px with safety clamp for python-pptx limits."""
    try:
        px = float(font_size_px or 0.0)
    except Exception:
        return
    if px <= 0:
        return
    try:
        scale = float(scale or 1.0)
    except Exception:
        scale = 1.0
    if scale <= 0:
        scale = 1.0
    pt = max(1.0, px * 0.75 * scale)
    run.font.size = Pt(pt)


def apply_text_run_style(
    run,
    style: Dict[str, object],
    text: str,
    cjk_font: str,
    font_scale: float = 1.0,
) -> None:
    """Apply font family/size/color/weight style to a PPT text run."""
    font_size = float(style.get("font_size") or 0.0)
    if font_size > 0:
        set_run_font_size_from_px(run, font_size, scale=font_scale)

    use_theme_font = bool(style.get("font_theme"))
    font_name = None
    if not use_theme_font:
        font_name = pick_font_name(style.get("font_family"), text, cjk_font)
        if font_name:
            run.font.name = font_name
    if has_cjk(text) and not use_theme_font:
        ea_font = font_name or cjk_font
        if ea_font:
            set_run_ea_font(run, ea_font)

    color = parse_color(style.get("fill"))
    if color:
        run.font.color.rgb = color

    weight = str(style.get("font_weight") or "").strip().lower()
    if weight in ("bold", "700", "800", "900"):
        run.font.bold = True
