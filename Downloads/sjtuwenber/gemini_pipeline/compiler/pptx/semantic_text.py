"""Semantic text extraction and reconstruction helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import re
import xml.etree.ElementTree as ET

from pptx.enum.text import MSO_VERTICAL_ANCHOR, PP_ALIGN

from .text_style import (
    apply_text_run_style,
    font_family_is_theme,
    pick_font_name,
    set_run_ea_font,
    set_run_font_size_from_px,
)
from ..utils.colors import parse_color
from ..utils.lengths import normalize_rotation, parse_length
from ..utils.svg_helpers import tag_name
from ..utils.text import CJK_RE, has_cjk
from ..utils.transforms import TransformMatrix, parse_transform

PUNCT_NO_SPACE_BEFORE = set(",.;:!?%)]}»。，、：；？！％）】》")
PUNCT_NO_SPACE_AFTER = set("([{\"'“‘")


def group_text_lines(items: List[Dict[str, object]], line_tol: float) -> List[Dict[str, object]]:
    """Group text items into lines based on y-coordinate proximity."""
    if not items:
        return []
    items = sorted(items, key=lambda it: (it["y"], it["x"]))
    lines: List[Dict[str, object]] = []
    current: List[Dict[str, object]] = []
    current_y = items[0]["y"]
    for item in items:
        if abs(item["y"] - current_y) <= line_tol:
            current.append(item)
        else:
            lines.append({"y": current_y, "items": current})
            current = [item]
            current_y = item["y"]
    if current:
        lines.append({"y": current_y, "items": current})
    return lines


def should_insert_space(prev_item: Dict[str, object], curr_item: Dict[str, object]) -> bool:
    prev_text = str(prev_item.get("text", ""))
    curr_text = str(curr_item.get("text", ""))
    if not prev_text or not curr_text:
        return False
    if prev_text[-1].isspace() or curr_text[0].isspace():
        return False
    if has_cjk(prev_text) or has_cjk(curr_text):
        return False
    if curr_text[0] in PUNCT_NO_SPACE_BEFORE:
        return False
    if prev_text[-1] in PUNCT_NO_SPACE_AFTER:
        return False
    if not re.search(r"[A-Za-z0-9]", prev_text) or not re.search(r"[A-Za-z0-9]", curr_text):
        return False
    prev_x = float(prev_item.get("x") or 0.0)
    curr_x = float(curr_item.get("x") or 0.0)
    size = max(float(prev_item.get("font_size") or 0.0), float(curr_item.get("font_size") or 0.0))
    gap = curr_x - prev_x
    if size <= 0.0:
        return gap > 1.0
    return gap > size * 0.6


def estimate_text_width_px(text: str, font_size: float, letter_spacing: float = 0.0) -> float:
    """Rough text advance estimation in SVG px."""
    if font_size <= 0:
        font_size = 16.0
    if not text:
        return font_size * 0.6

    width = 0.0
    for ch in text:
        if ch.isspace():
            width += font_size * 0.33
        elif CJK_RE.search(ch):
            width += font_size * 1.0
        elif ch in "ilI1|":
            width += font_size * 0.32
        elif ch in "mwMW@#%&":
            width += font_size * 0.86
        else:
            width += font_size * 0.56

    if len(text) > 1 and letter_spacing > 0:
        width += letter_spacing * (len(text) - 1)
    return max(width, font_size * 0.6)


def baseline_to_top_offset_px(font_size: float, text: str) -> float:
    """Approximate baseline-to-top distance for PPTX textbox placement."""
    if font_size <= 0:
        font_size = 16.0
    ratio = 0.90 if has_cjk(text) else 0.82
    return font_size * ratio


def estimate_line_height_px(items: List[Dict[str, object]]) -> float:
    if not items:
        return 0.0
    max_font = max(float(it.get("font_size") or 0.0) for it in items)
    if max_font <= 0.0:
        max_font = 16.0
    return max_font * 1.18


def should_middle_anchor_textbox(
    box_height_px: float,
    lines: List[Dict[str, object]],
) -> bool:
    if box_height_px <= 0 or not lines:
        return False
    content_height = sum(estimate_line_height_px(line.get("items") or []) for line in lines)
    if content_height <= 0:
        return False
    return box_height_px >= content_height * 1.22


def extract_semantic_textboxes(
    svg_path: Path,
) -> Tuple[Dict[str, float], List[Dict[str, object]], Dict[str, Dict[str, object]]]:
    """Extract textboxes from semantic layer in SVG file."""
    tree = ET.parse(svg_path)
    root = tree.getroot()
    width = parse_length(root.get("width"))
    height = parse_length(root.get("height"))
    canvas = {"w": width, "h": height}

    parent_map: Dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        for child in parent:
            parent_map[child] = parent

    transform_cache: Dict[ET.Element, TransformMatrix] = {}
    auto_group_ids: Dict[ET.Element, str] = {}

    def global_transform(elem: ET.Element) -> TransformMatrix:
        if elem in transform_cache:
            return transform_cache[elem]

        chain: List[ET.Element] = []
        cur = elem
        while cur is not None:
            chain.append(cur)
            cur = parent_map.get(cur)
        chain.reverse()

        mat = TransformMatrix.identity()
        for node in chain:
            t_str = node.get("transform", "")
            if t_str:
                mat = mat.multiply(parse_transform(t_str))
        transform_cache[elem] = mat
        return mat

    def nearest_text_group(elem: ET.Element) -> Optional[ET.Element]:
        cur = parent_map.get(elem)
        while cur is not None:
            if tag_name(cur) == "g" and cur.get("data-type") == "text-group":
                return cur
            cur = parent_map.get(cur)
        return None

    def collect_text_items(container: ET.Element, container_transform: TransformMatrix) -> List[Dict[str, object]]:
        items: List[Dict[str, object]] = []
        for child in container:
            if tag_name(child) != "text":
                continue

            text_transform = parse_transform(child.get("transform", ""))
            total_transform = container_transform.multiply(text_transform)
            sx, sy = total_transform.get_scale()
            font_scale = sy if sy > 0 else 1.0
            rotation = normalize_rotation(total_transform.get_rotation_degrees())

            base_style = {
                "font_size": parse_length(child.get("font-size")) * font_scale,
                "font_family": child.get("font-family"),
                "font_theme": font_family_is_theme(child.get("font-family")),
                "fill": child.get("fill"),
                "font_weight": child.get("font-weight"),
                "letter_spacing": parse_length(child.get("letter-spacing"), 0.0),
                "text_anchor": child.get("text-anchor"),
                "rotation": rotation,
            }

            tspans = [c for c in list(child) if tag_name(c) == "tspan"]
            if tspans:
                cur_x = parse_length(child.get("x"), 0.0)
                cur_y = parse_length(child.get("y"), 0.0)

                prefix_text = (child.text or "").replace("\u00a0", " ")
                if prefix_text.strip():
                    px, py = total_transform.transform_point(cur_x, cur_y)
                    items.append({"text": prefix_text.strip(), "x": px, "y": py, **base_style})
                    cur_x += estimate_text_width_px(
                        prefix_text.strip(),
                        float(base_style.get("font_size") or 16.0),
                        float(base_style.get("letter_spacing") or 0.0),
                    )

                for tspan in tspans:
                    if tspan.get("x") is not None and str(tspan.get("x")).strip() != "":
                        try:
                            cur_x = parse_length(tspan.get("x"))
                        except Exception:
                            pass
                    if tspan.get("y") is not None and str(tspan.get("y")).strip() != "":
                        try:
                            cur_y = parse_length(tspan.get("y"))
                        except Exception:
                            pass
                    dy = tspan.get("dy")
                    if dy is not None and str(dy).strip() != "":
                        cur_y += parse_length(dy, 0.0)

                    tspan_style = dict(base_style)
                    if tspan.get("font-size"):
                        tspan_style["font_size"] = parse_length(tspan.get("font-size")) * font_scale
                    if tspan.get("font-family"):
                        tspan_style["font_family"] = tspan.get("font-family")
                        tspan_style["font_theme"] = font_family_is_theme(tspan.get("font-family"))
                    if tspan.get("fill"):
                        tspan_style["fill"] = tspan.get("fill")
                    if tspan.get("font-weight"):
                        tspan_style["font_weight"] = tspan.get("font-weight")
                    if tspan.get("letter-spacing"):
                        tspan_style["letter_spacing"] = parse_length(tspan.get("letter-spacing"), 0.0)
                    if tspan.get("text-anchor"):
                        tspan_style["text_anchor"] = tspan.get("text-anchor")

                    tspan_text = (tspan.text or "").replace("\u00a0", " ")
                    if tspan_text.strip():
                        tx, ty = total_transform.transform_point(cur_x, cur_y)
                        items.append({"text": tspan_text.strip(), "x": tx, "y": ty, **tspan_style})
                        cur_x += estimate_text_width_px(
                            tspan_text.strip(),
                            float(tspan_style.get("font_size") or 16.0),
                            float(tspan_style.get("letter_spacing") or 0.0),
                        )

                    tail_text = (tspan.tail or "").replace("\u00a0", " ")
                    if tail_text.strip():
                        tx2, ty2 = total_transform.transform_point(cur_x, cur_y)
                        items.append({"text": tail_text.strip(), "x": tx2, "y": ty2, **base_style})
                        cur_x += estimate_text_width_px(
                            tail_text.strip(),
                            float(base_style.get("font_size") or 16.0),
                            float(base_style.get("letter_spacing") or 0.0),
                        )
                continue

            content = (child.text or "").replace("\u00a0", " ").strip()
            if not content:
                continue
            local_x = parse_length(child.get("x"), 0.0)
            local_y = parse_length(child.get("y"), 0.0)
            gx, gy = total_transform.transform_point(local_x, local_y)
            items.append({"text": content, "x": gx, "y": gy, **base_style})
        return items

    def infer_box_rotation(items: List[Dict[str, object]]) -> float:
        rotations = []
        for item in items:
            rot = item.get("rotation")
            if isinstance(rot, (int, float)):
                rot_val = normalize_rotation(float(rot))
                if abs(rot_val) >= 1.0:
                    rotations.append(rot_val)
        if not rotations:
            return 0.0
        rotations.sort()
        return rotations[len(rotations) // 2]

    def _get_len_any(elem: ET.Element, *names: str, default: float = 0.0) -> float:
        for name in names:
            v = elem.get(name)
            if v is not None and str(v).strip() != "":
                return parse_length(v, default)
        return default

    def get_bbox_from_rect(elem: ET.Element) -> Dict[str, float]:
        for child in elem:
            if tag_name(child) == "rect" and child.get("class") == "tb-bbox":
                return {
                    "x": _get_len_any(child, "x", default=0.0),
                    "y": _get_len_any(child, "y", default=0.0),
                    "w": _get_len_any(child, "width", default=0.0),
                    "h": _get_len_any(child, "height", default=0.0),
                }
        return {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}

    def transform_bbox(x: float, y: float, w: float, h: float, mat: TransformMatrix) -> Dict[str, float]:
        cx, cy = mat.transform_point(x + w / 2.0, y + h / 2.0)
        sx, sy = mat.get_scale()
        bw = abs(w * sx)
        bh = abs(h * sy)
        if bw <= 0:
            bw = abs(w)
        if bh <= 0:
            bh = abs(h)
        return {
            "x": cx - bw / 2.0,
            "y": cy - bh / 2.0,
            "w": bw,
            "h": bh,
            "rotation": normalize_rotation(mat.get_rotation_degrees()),
        }

    group_metas: Dict[str, Dict[str, object]] = {}
    for ge in root.iter():
        if tag_name(ge) != "g":
            continue
        if ge.get("data-type") != "text-group":
            continue
        group_id = (ge.get("id") or "").strip()
        if not group_id:
            group_id = auto_group_ids.setdefault(ge, f"text-group-{len(auto_group_ids) + 1}")
        g_transform = global_transform(ge)
        gb = {
            "x": _get_len_any(ge, "data-x", "x", default=0.0),
            "y": _get_len_any(ge, "data-y", "y", default=0.0),
            "w": _get_len_any(ge, "data-w", "width", default=0.0),
            "h": _get_len_any(ge, "data-h", "height", default=0.0),
        }
        if gb["w"] <= 0 or gb["h"] <= 0:
            rect_bbox = get_bbox_from_rect(ge)
            if rect_bbox["w"] > 0 and rect_bbox["h"] > 0:
                gb = rect_bbox
        if gb["w"] <= 0 or gb["h"] <= 0:
            continue
        transformed = transform_bbox(float(gb["x"]), float(gb["y"]), float(gb["w"]), float(gb["h"]), g_transform)
        try:
            order_val = float((ge.get("data-order") or "").strip())
        except Exception:
            order_val = 1e9
        group_metas[group_id] = {
            "id": group_id,
            "x": transformed["x"],
            "y": transformed["y"],
            "w": transformed["w"],
            "h": transformed["h"],
            "order": order_val,
            "role": (ge.get("data-role") or "").strip(),
            "visual_group_id": (ge.get("data-visual-group") or "").strip(),
        }

    textboxes = []
    for g in root.iter():
        if tag_name(g) != "g":
            continue
        if g.get("data-type") != "textbox":
            continue
        g_transform = global_transform(g)
        texts = collect_text_items(g, g_transform)
        bbox = {
            "x": _get_len_any(g, "data-x", "x", default=0.0),
            "y": _get_len_any(g, "data-y", "y", default=0.0),
            "w": _get_len_any(g, "data-w", "width", default=0.0),
            "h": _get_len_any(g, "data-h", "height", default=0.0),
        }
        if bbox["w"] <= 0 or bbox["h"] <= 0:
            rect_bbox = get_bbox_from_rect(g)
            if rect_bbox["w"] > 0 and rect_bbox["h"] > 0:
                bbox = rect_bbox

        transformed_bbox = transform_bbox(float(bbox["x"]), float(bbox["y"]), float(bbox["w"]), float(bbox["h"]), g_transform)
        text_rot = infer_box_rotation(texts)
        final_rotation = text_rot if abs(text_rot) >= 1.0 else transformed_bbox["rotation"]
        group_elem = nearest_text_group(g)
        group_id = ""
        group_role = ""
        group_order = 1e9
        visual_group_id = ""
        if group_elem is not None:
            group_id = (group_elem.get("id") or "").strip()
            if not group_id:
                group_id = auto_group_ids.setdefault(group_elem, f"text-group-{len(auto_group_ids) + 1}")
            group_role = (group_elem.get("data-role") or "").strip()
            visual_group_id = (group_elem.get("data-visual-group") or "").strip()
            try:
                group_order = float((group_elem.get("data-order") or "").strip())
            except Exception:
                group_order = 1e9

        textboxes.append(
            {
                "id": g.get("id") or "",
                "x": transformed_bbox["x"],
                "y": transformed_bbox["y"],
                "w": transformed_bbox["w"],
                "h": transformed_bbox["h"],
                "role": g.get("data-role") or "",
                "texts": texts,
                "rotation": final_rotation,
                "group_id": group_id,
                "group_role": group_role,
                "group_order": group_order,
                "visual_group_id": visual_group_id,
            }
        )

    unions: Dict[str, Dict[str, float]] = {}
    for tb in textboxes:
        gid = str(tb.get("group_id") or "").strip()
        if not gid:
            continue
        x = float(tb.get("x") or 0.0)
        y = float(tb.get("y") or 0.0)
        w = float(tb.get("w") or 0.0)
        h = float(tb.get("h") or 0.0)
        if w <= 0 or h <= 0:
            continue
        cur = unions.get(gid)
        if cur is None:
            unions[gid] = {"x1": x, "y1": y, "x2": x + w, "y2": y + h}
        else:
            cur["x1"] = min(cur["x1"], x)
            cur["y1"] = min(cur["y1"], y)
            cur["x2"] = max(cur["x2"], x + w)
            cur["y2"] = max(cur["y2"], y + h)

    for gid, u in unions.items():
        if gid in group_metas and group_metas[gid].get("w", 0) and group_metas[gid].get("h", 0):
            continue
        order_val = 1e9
        role_val = ""
        visual_gid = ""
        for tb in textboxes:
            if str(tb.get("group_id") or "") != gid:
                continue
            try:
                order_val = min(order_val, float(tb.get("group_order") or 1e9))
            except Exception:
                pass
            role_val = role_val or str(tb.get("group_role") or "")
            visual_gid = visual_gid or str(tb.get("visual_group_id") or "")
        group_metas[gid] = {
            "id": gid,
            "x": u["x1"],
            "y": u["y1"],
            "w": max(1.0, u["x2"] - u["x1"]),
            "h": max(1.0, u["y2"] - u["y1"]),
            "order": order_val,
            "role": role_val,
            "visual_group_id": visual_gid,
        }

    return canvas, textboxes, group_metas


def add_semantic_textbox(
    slide,
    tb: Dict[str, object],
    converter: Any,
    line_tol: float,
    box_pad: float,
    cjk_font: str,
    width_expand: float = 1.0,
) -> Optional[Any]:
    """Add one semantic textbox."""
    pad = max(0.0, float(box_pad))
    orig_x = float(tb["x"])
    orig_y = float(tb["y"])
    orig_w = float(tb["w"])
    orig_h = float(tb["h"])

    base_x = orig_x - pad
    base_y = orig_y - pad
    base_w = orig_w + 2 * pad
    base_h = orig_h + 2 * pad

    try:
        width_expand = float(width_expand)
    except Exception:
        width_expand = 1.0
    if width_expand and abs(width_expand - 1.0) > 1e-6:
        new_w = base_w * width_expand
        base_x -= (new_w - base_w) / 2.0
        base_w = new_w
    if base_w <= 0 or base_h <= 0:
        return None

    rotation = normalize_rotation(float(tb.get("rotation", 0.0) or 0.0))
    swap_dims = abs(abs(rotation) - 90.0) <= 2.0

    if swap_dims:
        width_px = base_h
        height_px = base_w
        center_x = base_x + base_w / 2.0
        center_y = base_y + base_h / 2.0
        left_px = center_x - width_px / 2.0
        top_px = center_y - height_px / 2.0
    else:
        width_px = base_w
        height_px = base_h
        left_px = base_x
        top_px = base_y

    left_emu = converter.to_emu_x(left_px)
    top_emu = converter.to_emu_y(top_px)
    width_emu = converter.to_emu_width(width_px)
    height_emu = converter.to_emu_height(height_px)
    shape = slide.shapes.add_textbox(left_emu, top_emu, width_emu, height_emu)

    if abs(rotation) >= 1.0:
        shape.rotation = rotation
    if tb.get("id"):
        shape.name = str(tb["id"])

    tf = shape.text_frame
    tf.clear()
    tf.word_wrap = False
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0

    text_items = tb.get("texts", [])
    lines = group_text_lines(text_items, line_tol=line_tol)
    tf.vertical_anchor = (
        MSO_VERTICAL_ANCHOR.MIDDLE
        if should_middle_anchor_textbox(height_px, lines)
        else MSO_VERTICAL_ANCHOR.TOP
    )
    for idx, line in enumerate(lines):
        line_items = sorted(line["items"], key=lambda it: it["x"])
        p = tf.paragraphs[0] if idx == 0 else tf.add_paragraph()
        p.text = ""
        p.space_before = 0
        p.space_after = 0
        if not line_items:
            continue

        prev_item: Optional[Dict[str, object]] = None
        for item in line_items:
            text_val = str(item.get("text", ""))
            if not text_val:
                continue
            if prev_item and should_insert_space(prev_item, item):
                run_space = p.add_run()
                run_space.text = " "
            run = p.add_run()
            run.text = text_val
            apply_text_run_style(run, item, text_val, cjk_font, font_scale=converter.font_scale_factor())
            prev_item = item

        anchor = line_items[0].get("text_anchor")
        if anchor == "middle":
            p.alignment = PP_ALIGN.CENTER
        elif anchor == "end":
            p.alignment = PP_ALIGN.RIGHT
        else:
            p.alignment = PP_ALIGN.LEFT
    return shape


def add_semantic_text_items_absolute(
    slide,
    tb: Dict[str, object],
    converter: Any,
    cjk_font: str,
    text_pad: float = 1.0,
) -> List[Any]:
    """Add semantic text items as independent absolute-position textboxes."""
    created: List[Any] = []
    try:
        pad = max(0.0, float(text_pad))
    except Exception:
        pad = 1.0

    items = sorted(
        [it for it in (tb.get("texts") or []) if str(it.get("text", "")).strip()],
        key=lambda it: (float(it.get("y") or 0.0), float(it.get("x") or 0.0)),
    )

    for item in items:
        text = str(item.get("text", "")).replace("\u00a0", " ").strip()
        if not text:
            continue
        x = float(item.get("x") or 0.0)
        y = float(item.get("y") or 0.0)
        font_size = float(item.get("font_size") or 16.0)
        letter_spacing = float(item.get("letter_spacing") or 0.0)
        anchor = str(item.get("text_anchor") or "start").strip().lower()

        inner_w = estimate_text_width_px(text, font_size, letter_spacing)
        inner_h = max(font_size * 1.25, 1.0)
        base_left = x
        if anchor == "middle":
            base_left = x - inner_w / 2.0
        elif anchor == "end":
            base_left = x - inner_w
        base_top = y - baseline_to_top_offset_px(font_size, text)

        left_emu = converter.to_emu_x(base_left - pad)
        top_emu = converter.to_emu_y(base_top - pad)
        width_emu = converter.to_emu_width(inner_w + 2.0 * pad)
        height_emu = converter.to_emu_height(inner_h + 2.0 * pad)

        shape = slide.shapes.add_textbox(left_emu, top_emu, width_emu, height_emu)
        rotation = normalize_rotation(float(item.get("rotation") or 0.0))
        if abs(rotation) >= 1.0:
            shape.rotation = rotation

        tf = shape.text_frame
        tf.clear()
        tf.word_wrap = False
        tf.margin_left = 0
        tf.margin_right = 0
        tf.margin_top = 0
        tf.margin_bottom = 0
        tf.vertical_anchor = MSO_VERTICAL_ANCHOR.TOP

        p = tf.paragraphs[0]
        p.text = text
        p.space_before = 0
        p.space_after = 0
        if anchor == "middle":
            p.alignment = PP_ALIGN.CENTER
        elif anchor == "end":
            p.alignment = PP_ALIGN.RIGHT
        else:
            p.alignment = PP_ALIGN.LEFT

        run = p.runs[0] if p.runs else p.add_run()
        set_run_font_size_from_px(run, font_size, scale=converter.font_scale_factor())

        use_theme_font = bool(item.get("font_theme"))
        font_name = None
        if not use_theme_font:
            font_name = pick_font_name(item.get("font_family"), text, cjk_font)
            if font_name:
                run.font.name = font_name
        if has_cjk(text) and not use_theme_font:
            ea_font = font_name or cjk_font
            if ea_font:
                set_run_ea_font(run, ea_font)

        color = parse_color(item.get("fill"))
        if color:
            run.font.color.rgb = color

        weight = str(item.get("font_weight") or "").strip().lower()
        if weight in ("bold", "700", "800", "900"):
            run.font.bold = True
        created.append(shape)

    return created
