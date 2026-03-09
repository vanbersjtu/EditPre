"""Image-placeholder extraction and insertion helpers."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import re
import tempfile
from typing import Any, Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

from ..constants import SVG_NS, XLINK_NS
from ..utils.lengths import parse_length
from ..utils.svg_helpers import tag_name

try:
    import cairosvg

    HAS_CAIROSVG = True
except Exception:
    cairosvg = None
    HAS_CAIROSVG = False

try:
    from PIL import Image

    HAS_PIL = True
except Exception:
    Image = None
    HAS_PIL = False


def parse_matrix_simple(transform: str) -> Tuple[float, float, float, float, float, float]:
    """Parse matrix(a b c d e f) transform, returns (a, b, c, d, e, f)."""
    match = re.search(r"matrix\(([^)]+)\)", transform)
    if match:
        parts = [p for p in re.split(r"[ ,]+", match.group(1).strip()) if p]
        if len(parts) == 6:
            try:
                return tuple(float(p) for p in parts)
            except Exception:
                pass
    return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def parse_scale_simple(transform: str) -> Tuple[float, float]:
    """Parse scale(sx sy) transform, returns (sx, sy)."""
    match = re.search(r"scale\(([^)]+)\)", transform)
    if match:
        parts = [p for p in re.split(r"[ ,]+", match.group(1).strip()) if p]
        if parts:
            try:
                sx = float(parts[0])
                sy = float(parts[1]) if len(parts) > 1 else sx
                return (sx, sy)
            except Exception:
                pass
    return (1.0, 1.0)


def parse_transform_xy(transform: Optional[str]) -> Tuple[float, float]:
    """Extract translation (x, y) from transform string."""
    if not transform:
        return (0.0, 0.0)
    match = re.search(r"matrix\(([^)]+)\)", transform)
    if match:
        parts = [p for p in re.split(r"[ ,]+", match.group(1).strip()) if p]
        if len(parts) == 6:
            try:
                return (float(parts[4]), float(parts[5]))
            except Exception:
                return (0.0, 0.0)
    match = re.search(r"translate\(([^)]+)\)", transform)
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


def apply_transform_chain(
    x: float,
    y: float,
    w: float,
    h: float,
    transforms: List[str],
) -> Tuple[float, float, float, float]:
    """Apply a list of SVG transforms (element -> root) to a rect."""
    for t in transforms:
        sx, sy = parse_scale_simple(t)
        x *= sx
        y *= sy
        w *= sx
        h *= sy

        a, b, c, d, e, f = parse_matrix_simple(t)
        if not (a == 1.0 and b == 0.0 and c == 0.0 and d == 1.0 and e == 0.0 and f == 0.0):
            new_x = a * x + c * y + e
            new_y = b * x + d * y + f
            x, y = new_x, new_y
            w *= a
            h *= d

        if "translate(" in t and "matrix(" not in t:
            tx, ty = parse_transform_xy(t)
            x += tx
            y += ty
    return x, y, w, h


def clip_path_is_rect(clip_elem: ET.Element) -> bool:
    """Return True if clipPath contains a single rect child and nothing else."""
    children = [child for child in list(clip_elem) if isinstance(child.tag, str)]
    if len(children) != 1:
        return False
    return tag_name(children[0]) == "rect"


def rasterize_clipped_placeholder(
    placeholder: Dict[str, Any],
    converter: Any,
) -> Optional[Tuple[str, Tuple[int, int, int, int]]]:
    """Rasterize a non-rect clipped placeholder to a temporary PNG."""
    if not HAS_CAIROSVG:
        print("Warning: cairosvg not available; skipping non-rect clip rasterization.")
        return None
    if not HAS_PIL:
        print("Warning: PIL not available; skipping non-rect clip rasterization.")
        return None

    svg_width = int(round(converter.svg_width))
    svg_height = int(round(converter.svg_height))
    if svg_width <= 0 or svg_height <= 0:
        return None

    svg_root = ET.Element(
        "svg",
        {
            "xmlns": SVG_NS,
            "xmlns:xlink": XLINK_NS,
            "width": str(svg_width),
            "height": str(svg_height),
            "viewBox": f"0 0 {svg_width} {svg_height}",
        },
    )
    defs = ET.SubElement(svg_root, "defs")
    for clip_xml in placeholder.get("clip_defs", {}).values():
        try:
            defs.append(ET.fromstring(clip_xml))
        except ET.ParseError:
            continue

    parent = svg_root
    for wrapper in placeholder.get("clip_chain", []):
        group = ET.SubElement(parent, "g")
        if wrapper.get("transform"):
            group.set("transform", wrapper["transform"])
        if wrapper.get("clip_path"):
            group.set("clip-path", wrapper["clip_path"])
        if wrapper.get("opacity"):
            group.set("opacity", wrapper["opacity"])
        parent = group

    elem_xml = placeholder.get("elem_xml")
    if not elem_xml:
        return None
    try:
        parent.append(ET.fromstring(elem_xml))
    except ET.ParseError:
        return None

    svg_bytes = ET.tostring(svg_root, encoding="utf-8", xml_declaration=True)
    try:
        png_bytes = cairosvg.svg2png(
            bytestring=svg_bytes,
            output_width=svg_width,
            output_height=svg_height,
        )
    except Exception:
        return None

    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    alpha = img.split()[-1]
    bbox = alpha.getbbox()
    if not bbox:
        return None

    cropped = img.crop(bbox)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
        cropped.save(tmp, format="PNG")
        tmp_path = tmp.name
    return tmp_path, bbox


def classify_image_placeholder_group(elem: ET.Element) -> str:
    """Classify image-placeholder group behavior."""
    force_placeholder = str(elem.get("data-force-placeholder") or "").strip().lower()
    if force_placeholder in ("1", "true", "yes", "on"):
        return "placeholder"

    has_image_href = False
    has_vector_content = False
    for child in elem.iter():
        if child is elem:
            continue
        t = tag_name(child)
        if t == "image":
            href = (child.get(f"{{{XLINK_NS}}}href") or child.get("href") or "").strip()
            if href:
                has_image_href = True
                break
        elif t in ("path", "circle", "ellipse", "line", "polyline", "polygon", "text"):
            has_vector_content = True

    if has_image_href:
        return "placeholder"
    if has_vector_content:
        return "vectorized"
    return "placeholder"


def parse_placeholder_remove_bg(elem: ET.Element) -> Optional[bool]:
    """Parse per-placeholder remove-bg preference from SVG attrs."""
    for key in ("data-remove-bg", "data-rgba", "data-needs-rgba"):
        raw = str(elem.get(key) or "").strip().lower()
        if not raw:
            continue
        if raw in ("1", "true", "yes", "on"):
            return True
        if raw in ("0", "false", "no", "off"):
            return False
    return None


def parse_placeholder_remove_bg_mode(elem: ET.Element) -> Optional[str]:
    """Parse per-placeholder remove-bg mode from SVG attrs."""
    for key in ("data-remove-bg-mode", "data-rgba-mode", "data-cutout-mode"):
        raw = str(elem.get(key) or "").strip().lower()
        if not raw:
            continue
        if raw in ("flat", "chroma", "key"):
            return "flat"
        if raw in ("photo", "rembg", "model"):
            return "photo"
        if raw in ("auto",):
            return "auto"
    return None


def parse_chart_spec(elem: ET.Element) -> Optional[Any]:
    """Parse data-chart-spec JSON from placeholder, fallback to raw string."""
    raw = str(elem.get("data-chart-spec") or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return raw


def parse_placeholder_text_policy(elem: ET.Element) -> str:
    """Parse per-placeholder text policy: editable|raster."""
    raw = str(elem.get("data-text-policy") or "").strip().lower()
    if raw in ("editable", "raster"):
        return raw
    return "editable"


def extract_image_placeholders(svg_path: Path) -> List[Dict[str, Any]]:
    """Extract image placeholders with transform/clip-resolved bboxes."""
    tree = ET.parse(svg_path)
    root = tree.getroot()

    parent_map: Dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        for child in parent:
            parent_map[child] = parent

    placeholders: List[Dict[str, Any]] = []

    def nearest_visual_group_id(node: Optional[ET.Element]) -> str:
        cur = node
        while cur is not None:
            if tag_name(cur) == "g" and cur.get("data-type") == "visual-group":
                return (cur.get("id") or cur.get("data-visual-group") or "").strip()
            cur = parent_map.get(cur)
        return ""

    clip_rects_map: Dict[str, Dict[str, float]] = {}
    clip_units: Dict[str, str] = {}
    clip_is_rect: Dict[str, bool] = {}
    clip_xml_map: Dict[str, str] = {}

    for elem in root.iter():
        if tag_name(elem) != "clipPath":
            continue
        clip_id = elem.get("id")
        if not clip_id:
            continue
        clip_units[clip_id] = elem.get("clipPathUnits", "userSpaceOnUse")
        clip_is_rect[clip_id] = clip_path_is_rect(elem)
        clip_xml_map[clip_id] = ET.tostring(elem, encoding="utf-8").decode("utf-8")
        if clip_is_rect[clip_id]:
            rect = None
            for child in list(elem):
                if tag_name(child) == "rect":
                    rect = child
                    break
            if rect is None:
                continue
            clip_rects_map[clip_id] = {
                "x": parse_length(rect.get("x")),
                "y": parse_length(rect.get("y")),
                "w": parse_length(rect.get("width")),
                "h": parse_length(rect.get("height")),
            }

    placeholder_counter = 0
    for elem in root.iter():
        if tag_name(elem) != "g":
            continue
        if elem.get("data-role") != "image-placeholder" and elem.get("data-type") != "image-placeholder":
            continue
        if classify_image_placeholder_group(elem) == "vectorized":
            continue

        placeholder_counter += 1
        placeholder_id = elem.get("id", "").strip() or f"placeholder-{placeholder_counter}"
        caption = elem.get("data-caption", "")
        is_chart = str(elem.get("data-is-chart", "false")).strip().lower() == "true"
        chart_spec = parse_chart_spec(elem)
        remove_bg = parse_placeholder_remove_bg(elem)
        remove_bg_mode = parse_placeholder_remove_bg_mode(elem)
        text_policy = parse_placeholder_text_policy(elem)
        visual_group_id = ((elem.get("data-visual-group") or "").strip() or nearest_visual_group_id(parent_map.get(elem)))

        rect_x, rect_y, rect_w, rect_h = 0.0, 0.0, 0.0, 0.0
        image_elem = None
        for child in elem:
            child_tag = tag_name(child)
            if child_tag in ("rect", "image"):
                rect_x = parse_length(child.get("x"))
                rect_y = parse_length(child.get("y"))
                rect_w = parse_length(child.get("width"))
                rect_h = parse_length(child.get("height"))
                if child_tag == "image":
                    image_elem = child
                break

        transforms: List[str] = []
        current = elem
        while current is not None:
            t = current.get("transform", "")
            if t:
                transforms.append(t)
            current = parent_map.get(current)
        x, y, w, h = apply_transform_chain(rect_x, rect_y, rect_w, rect_h, transforms)

        clip_chain: List[Dict[str, Optional[str]]] = []
        clip_rects: List[Tuple[float, float, float, float]] = []
        current = parent_map.get(elem)
        while current is not None:
            clip_ref = current.get("clip-path", "")
            transform = current.get("transform", "")
            opacity = current.get("opacity", "")
            if clip_ref or transform or opacity:
                clip_chain.append({"clip_path": clip_ref or None, "transform": transform or None, "opacity": opacity or None})
            if clip_ref.startswith("url(#") and clip_ref.endswith(")"):
                clip_id = clip_ref[5:-1]
                units = clip_units.get(clip_id, "userSpaceOnUse")
                if clip_is_rect.get(clip_id) and units == "userSpaceOnUse":
                    rect = clip_rects_map.get(clip_id)
                    if not rect:
                        current = parent_map.get(current)
                        continue
                    t_chain: List[str] = []
                    cur2 = current
                    while cur2 is not None:
                        t_val = cur2.get("transform", "")
                        if t_val:
                            t_chain.append(t_val)
                        cur2 = parent_map.get(cur2)
                    cx, cy, cw, ch = apply_transform_chain(rect["x"], rect["y"], rect["w"], rect["h"], t_chain)
                    clip_rects.append((cx, cy, cw, ch))
            current = parent_map.get(current)

        clip_chain = list(reversed(clip_chain))
        clip_box = None
        if clip_rects:
            left = max(r[0] for r in clip_rects)
            top = max(r[1] for r in clip_rects)
            right = min(r[0] + r[2] for r in clip_rects)
            bottom = min(r[1] + r[3] for r in clip_rects)
            if right > left and bottom > top:
                clip_box = {"x": left, "y": top, "w": right - left, "h": bottom - top}

        clip_ids = []
        needs_raster = False
        for wrapper in clip_chain:
            clip_path = wrapper.get("clip_path") or ""
            if clip_path.startswith("url(#") and clip_path.endswith(")"):
                clip_id = clip_path[5:-1]
                clip_ids.append(clip_id)
                if not clip_is_rect.get(clip_id, False):
                    needs_raster = True
        clip_defs = {clip_id: clip_xml_map[clip_id] for clip_id in clip_ids if clip_id in clip_xml_map}

        placeholders.append(
            {
                "id": placeholder_id,
                "caption": caption,
                "is_chart": is_chart,
                "chart_spec": chart_spec,
                "remove_bg": remove_bg,
                "remove_bg_mode": remove_bg_mode,
                "text_policy": text_policy,
                "visual_group_id": visual_group_id,
                "x": x,
                "y": y,
                "w": w,
                "h": h,
                "clip": clip_box,
                "clip_chain": clip_chain,
                "clip_defs": clip_defs,
                "clip_non_rect": needs_raster,
                "elem_xml": ET.tostring(elem, encoding="utf-8").decode("utf-8"),
                "image_elem": image_elem,
            }
        )
    return placeholders


def add_image_placeholder(
    slide,
    placeholder: Dict[str, Any],
    converter: Any,
    svg_path: Optional[Path] = None,
    clip_to_canvas: bool = True,
) -> Optional[Any]:
    """Add an image placeholder using pre-calculated position and clip data."""
    if not HAS_PIL:
        return None

    orig_x = placeholder.get("x", 0)
    orig_y = placeholder.get("y", 0)
    orig_w = placeholder.get("w", 0)
    orig_h = placeholder.get("h", 0)
    image_elem = placeholder.get("image_elem")
    entry = placeholder.get("entry") if isinstance(placeholder.get("entry"), dict) else {}
    mapped_image_path = str(entry.get("image_path", "")).strip() if entry else ""
    fit_mode = str((entry.get("fit_mode") if entry else "") or placeholder.get("fit_mode") or "stretch").strip().lower()
    if fit_mode not in ("stretch", "contain", "cover"):
        fit_mode = "stretch"
    if orig_w <= 0 or orig_h <= 0:
        return None

    if placeholder.get("clip_non_rect"):
        raster = rasterize_clipped_placeholder(placeholder, converter)
        if raster:
            tmp_path, bbox = raster
            left_emu = converter.to_emu_x(bbox[0])
            top_emu = converter.to_emu_y(bbox[1])
            width_emu = converter.to_emu_width(bbox[2] - bbox[0])
            height_emu = converter.to_emu_height(bbox[3] - bbox[1])
            try:
                return slide.shapes.add_picture(tmp_path, left_emu, top_emu, width_emu, height_emu)
            finally:
                Path(tmp_path).unlink(missing_ok=True)

    href = ""
    href_from_entry = False
    if image_elem is not None:
        href = image_elem.get(f"{{{XLINK_NS}}}href") or image_elem.get("href", "")
    if not href and mapped_image_path:
        href = mapped_image_path
        href_from_entry = True
    if not href:
        return None

    x, y, w, h = orig_x, orig_y, orig_w, orig_h
    crop_fractions = None
    clip_box = placeholder.get("clip")
    if clip_to_canvas:
        canvas_w = converter.svg_width
        canvas_h = converter.svg_height
        visible_left = orig_x
        visible_top = orig_y
        visible_right = orig_x + orig_w
        visible_bottom = orig_y + orig_h
        if isinstance(clip_box, dict):
            try:
                clip_left = float(clip_box.get("x", visible_left))
                clip_top = float(clip_box.get("y", visible_top))
                clip_right = clip_left + float(clip_box.get("w", 0.0))
                clip_bottom = clip_top + float(clip_box.get("h", 0.0))
                visible_left = max(visible_left, clip_left)
                visible_top = max(visible_top, clip_top)
                visible_right = min(visible_right, clip_right)
                visible_bottom = min(visible_bottom, clip_bottom)
            except Exception:
                pass
        visible_left = max(0, visible_left)
        visible_top = max(0, visible_top)
        visible_right = min(canvas_w, visible_right)
        visible_bottom = min(canvas_h, visible_bottom)
        if visible_right <= visible_left or visible_bottom <= visible_top:
            return None

        crop_left = (visible_left - orig_x) / orig_w if orig_w > 0 else 0
        crop_top = (visible_top - orig_y) / orig_h if orig_h > 0 else 0
        crop_right = (orig_x + orig_w - visible_right) / orig_w if orig_w > 0 else 0
        crop_bottom = (orig_y + orig_h - visible_bottom) / orig_h if orig_h > 0 else 0
        if crop_left > 0.001 or crop_top > 0.001 or crop_right > 0.001 or crop_bottom > 0.001:
            crop_fractions = (crop_left, crop_top, crop_right, crop_bottom)
        x = visible_left
        y = visible_top
        w = visible_right - visible_left
        h = visible_bottom - visible_top

    left_emu = converter.to_emu_x(x)
    top_emu = converter.to_emu_y(y)
    width_emu = converter.to_emu_width(w)
    height_emu = converter.to_emu_height(h)

    if image_elem is not None:
        svg_img_w = parse_length(image_elem.get("width"), 0.0)
        svg_img_h = parse_length(image_elem.get("height"), 0.0)
        preserve_aspect = image_elem.get("preserveAspectRatio", "xMidYMid")
    else:
        svg_img_w = orig_w
        svg_img_h = orig_h
        preserve_aspect = "none"

    def add_picture_with_crop(image_path_or_stream, left, top, width, height, crop=None):
        if isinstance(image_path_or_stream, bytes):
            img = Image.open(io.BytesIO(image_path_or_stream))
        else:
            img = Image.open(image_path_or_stream)

        img_w, img_h = img.size
        if crop and svg_img_w > 0 and svg_img_h > 0:
            crop_l, crop_t, crop_r, crop_b = crop
            scale_by_width = svg_img_w / img_w
            scale_by_height = svg_img_h / img_h
            par_scale = max(scale_by_width, scale_by_height) if "slice" in preserve_aspect else min(scale_by_width, scale_by_height)
            scaled_w = img_w * par_scale
            scaled_h = img_h * par_scale
            offset_x = (scaled_w - svg_img_w) / 2
            offset_y = (scaled_h - svg_img_h) / 2

            vis_x_in_view = crop_l * svg_img_w
            vis_y_in_view = crop_t * svg_img_h
            vis_w_in_view = svg_img_w * (1 - crop_l - crop_r)
            vis_h_in_view = svg_img_h * (1 - crop_t - crop_b)

            vis_x_in_scaled = offset_x + vis_x_in_view
            vis_y_in_scaled = offset_y + vis_y_in_view
            vis_w_in_scaled = vis_w_in_view
            vis_h_in_scaled = vis_h_in_view

            left_px = int(vis_x_in_scaled / par_scale)
            top_px = int(vis_y_in_scaled / par_scale)
            right_px = int((vis_x_in_scaled + vis_w_in_scaled) / par_scale)
            bottom_px = int((vis_y_in_scaled + vis_h_in_scaled) / par_scale)

            left_px = max(0, min(left_px, img_w - 1))
            top_px = max(0, min(top_px, img_h - 1))
            right_px = max(left_px + 1, min(right_px, img_w))
            bottom_px = max(top_px + 1, min(bottom_px, img_h))
            img = img.crop((left_px, top_px, right_px, bottom_px))
        elif crop:
            crop_l, crop_t, crop_r, crop_b = crop
            left_px = int(crop_l * img_w)
            top_px = int(crop_t * img_h)
            right_px = int(img_w * (1 - crop_r))
            bottom_px = int(img_h * (1 - crop_b))

            left_px = max(0, min(left_px, img_w - 1))
            top_px = max(0, min(top_px, img_h - 1))
            right_px = max(left_px + 1, min(right_px, img_w))
            bottom_px = max(top_px + 1, min(bottom_px, img_h))
            img = img.crop((left_px, top_px, right_px, bottom_px))

        draw_left, draw_top, draw_width, draw_height = int(left), int(top), int(width), int(height)
        img_w, img_h = img.size
        if img_w > 0 and img_h > 0 and draw_width > 0 and draw_height > 0:
            target_ratio = draw_width / draw_height
            img_ratio = img_w / img_h
            if fit_mode == "contain":
                scale = min(draw_width / img_w, draw_height / img_h)
                out_w = max(1, int(round(img_w * scale)))
                out_h = max(1, int(round(img_h * scale)))
                draw_left = int(round(left + (draw_width - out_w) / 2.0))
                draw_top = int(round(top + (draw_height - out_h) / 2.0))
                draw_width, draw_height = out_w, out_h
            elif fit_mode == "cover":
                if img_ratio > target_ratio:
                    new_w = max(1, int(round(img_h * target_ratio)))
                    x0 = max(0, (img_w - new_w) // 2)
                    img = img.crop((x0, 0, x0 + new_w, img_h))
                elif img_ratio < target_ratio:
                    new_h = max(1, int(round(img_w / target_ratio)))
                    y0 = max(0, (img_h - new_h) // 2)
                    img = img.crop((0, y0, img_w, y0 + new_h))
                draw_left, draw_top, draw_width, draw_height = int(left), int(top), int(width), int(height)

        ext = ".png"
        img_format = "PNG"
        if hasattr(img, "format") and img.format:
            if img.format.lower() in ("jpeg", "jpg"):
                ext = ".jpg"
                img_format = "JPEG"

        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            img.save(tmp, format=img_format)
            tmp_path = tmp.name
        try:
            return slide.shapes.add_picture(tmp_path, draw_left, draw_top, draw_width, draw_height)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    if href.startswith("data:"):
        match = re.match(r"data:([^;,]+)?(?:;base64)?,(.+)", href, re.DOTALL)
        if not match:
            return None
        data = match.group(2)
        try:
            image_data = base64.b64decode(data)
        except Exception:
            return None
        return add_picture_with_crop(image_data, left_emu, top_emu, width_emu, height_emu, crop_fractions)

    if svg_path or href:
        href_path = Path(href).expanduser()
        candidates: List[Path] = []
        if href_path.is_absolute():
            candidates.append(href_path)
        else:
            # Generated/cache paths are often provided as cwd-relative strings.
            if href_from_entry:
                candidates.append(Path.cwd() / href_path)
            # Keep SVG-relative resolution for true SVG href links.
            if svg_path is not None:
                candidates.append(svg_path.parent / href_path)
            candidates.append(href_path)
        for image_path in candidates:
            if image_path.exists():
                return add_picture_with_crop(
                    image_path, left_emu, top_emu, width_emu, height_emu, crop_fractions
                )
    return None
