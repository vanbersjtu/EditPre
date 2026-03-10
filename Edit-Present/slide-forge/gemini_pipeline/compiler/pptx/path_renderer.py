"""SVG path rendering utilities for PPTX shapes."""

from __future__ import annotations

import re
from typing import Any, Callable, List, Optional, Tuple

import xml.etree.ElementTree as ET

from pptx.enum.shapes import MSO_SHAPE
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement

from ..constants import (
    FREEFORM_LOCAL_UNITS,
    PATH_MAX_SAMPLES_PER_SEGMENT,
    PATH_MIN_SAMPLES_PER_SEGMENT,
)

try:
    from svgpathtools import Arc, CubicBezier, Line, QuadraticBezier, parse_path

    HAS_SVGPATHTOOLS = True
except ImportError:
    Arc = CubicBezier = Line = QuadraticBezier = parse_path = None
    HAS_SVGPATHTOOLS = False


def _to_local_coord(value: float, min_val: float, span: float, local_units: int) -> int:
    """Map absolute coordinate to freeform local coordinate with rounding."""
    if span <= 0:
        return 0
    return int(round((value - min_val) / span * local_units))


def _estimate_segment_samples(segment: Any) -> int:
    """Estimate sampling density for svgpathtools segment."""
    try:
        seg_len = float(segment.length(error=1e-2))
    except Exception:
        seg_len = 64.0
    n = int(max(PATH_MIN_SAMPLES_PER_SEGMENT, min(PATH_MAX_SAMPLES_PER_SEGMENT, seg_len / 2.0)))
    return max(PATH_MIN_SAMPLES_PER_SEGMENT, n)


def _append_ooxml_point(parent: Any, x: int, y: int) -> None:
    pt = OxmlElement("a:pt")
    pt.set("x", str(int(x)))
    pt.set("y", str(int(y)))
    parent.append(pt)


def _collect_path_draw_ops(path: Any, transform: Any) -> List[Tuple[str, Tuple[float, ...]]]:
    """Convert svgpathtools path segments into drawing operations."""
    ops: List[Tuple[str, Tuple[float, ...]]] = []
    cur_end: Optional[Tuple[float, float]] = None
    eps = 1e-6

    for segment in path:
        sx, sy = transform.transform_point(segment.start.real, segment.start.imag)
        ex, ey = transform.transform_point(segment.end.real, segment.end.imag)

        if cur_end is None or abs(sx - cur_end[0]) > eps or abs(sy - cur_end[1]) > eps:
            ops.append(("M", (sx, sy)))

        if isinstance(segment, Line):
            ops.append(("L", (ex, ey)))
        elif isinstance(segment, CubicBezier):
            c1x, c1y = transform.transform_point(segment.control1.real, segment.control1.imag)
            c2x, c2y = transform.transform_point(segment.control2.real, segment.control2.imag)
            ops.append(("C", (c1x, c1y, c2x, c2y, ex, ey)))
        elif isinstance(segment, QuadraticBezier):
            c1x, c1y = transform.transform_point(segment.control.real, segment.control.imag)
            ops.append(("Q", (c1x, c1y, ex, ey)))
        elif isinstance(segment, Arc):
            # Arc -> cubic curves to keep smooth vectors in OOXML.
            try:
                n_curve = max(1, min(16, _estimate_segment_samples(segment) // 12))
                for cubic in segment.as_cubic_curves(curves=n_curve):
                    c1x, c1y = transform.transform_point(cubic.control1.real, cubic.control1.imag)
                    c2x, c2y = transform.transform_point(cubic.control2.real, cubic.control2.imag)
                    cex, cey = transform.transform_point(cubic.end.real, cubic.end.imag)
                    ops.append(("C", (c1x, c1y, c2x, c2y, cex, cey)))
            except Exception:
                ops.append(("L", (ex, ey)))
        else:
            ops.append(("L", (ex, ey)))
        cur_end = (ex, ey)
    return ops


def _ops_bbox(ops: List[Tuple[str, Tuple[float, ...]]]) -> Optional[Tuple[float, float, float, float]]:
    xs: List[float] = []
    ys: List[float] = []
    for _, vals in ops:
        for i in range(0, len(vals), 2):
            xs.append(float(vals[i]))
            ys.append(float(vals[i + 1]))
    if not xs or not ys:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _write_ops_to_shape_custgeom(
    shape: Any,
    ops: List[Tuple[str, Tuple[float, ...]]],
    min_x: float,
    min_y: float,
    width: float,
    height: float,
) -> bool:
    if width <= 0 or height <= 0:
        return False
    sp = shape.element
    sp_pr = sp.spPr
    if sp_pr is None:
        return False

    # Replace preset geometry with custom geometry path.
    for child in list(sp_pr):
        if child.tag in (qn("a:prstGeom"), qn("a:custGeom")):
            sp_pr.remove(child)

    cust = OxmlElement("a:custGeom")
    cust.append(OxmlElement("a:avLst"))
    cust.append(OxmlElement("a:gdLst"))
    cust.append(OxmlElement("a:ahLst"))
    cust.append(OxmlElement("a:cxnLst"))
    rect = OxmlElement("a:rect")
    rect.set("l", "l")
    rect.set("t", "t")
    rect.set("r", "r")
    rect.set("b", "b")
    cust.append(rect)

    path_lst = OxmlElement("a:pathLst")
    path = OxmlElement("a:path")
    path_w = FREEFORM_LOCAL_UNITS
    path_h = FREEFORM_LOCAL_UNITS
    path.set("w", str(path_w))
    path.set("h", str(path_h))

    for op, vals in ops:
        if op == "M":
            node = OxmlElement("a:moveTo")
            _append_ooxml_point(
                node,
                _to_local_coord(vals[0], min_x, width, path_w),
                _to_local_coord(vals[1], min_y, height, path_h),
            )
            path.append(node)
        elif op == "L":
            node = OxmlElement("a:lnTo")
            _append_ooxml_point(
                node,
                _to_local_coord(vals[0], min_x, width, path_w),
                _to_local_coord(vals[1], min_y, height, path_h),
            )
            path.append(node)
        elif op == "Q":
            node = OxmlElement("a:quadBezTo")
            _append_ooxml_point(
                node,
                _to_local_coord(vals[0], min_x, width, path_w),
                _to_local_coord(vals[1], min_y, height, path_h),
            )
            _append_ooxml_point(
                node,
                _to_local_coord(vals[2], min_x, width, path_w),
                _to_local_coord(vals[3], min_y, height, path_h),
            )
            path.append(node)
        elif op == "C":
            node = OxmlElement("a:cubicBezTo")
            _append_ooxml_point(
                node,
                _to_local_coord(vals[0], min_x, width, path_w),
                _to_local_coord(vals[1], min_y, height, path_h),
            )
            _append_ooxml_point(
                node,
                _to_local_coord(vals[2], min_x, width, path_w),
                _to_local_coord(vals[3], min_y, height, path_h),
            )
            _append_ooxml_point(
                node,
                _to_local_coord(vals[4], min_x, width, path_w),
                _to_local_coord(vals[5], min_y, height, path_h),
            )
            path.append(node)

    path_lst.append(path)
    cust.append(path_lst)
    sp_pr.append(cust)
    return True


def _apply_path_style(
    *,
    shape: Any,
    elem: ET.Element,
    style: Any,
    converter: Any,
    apply_fill_to_shape: Callable[..., None],
    apply_stroke_to_shape: Callable[..., None],
    apply_svg_filter_shadow_if_needed: Callable[..., None],
) -> None:
    fill = style.fill or elem.get("fill")
    stroke = style.stroke or elem.get("stroke")
    stroke_width = style.stroke_width or elem.get("stroke-width")

    apply_fill_to_shape(shape, fill, style.fill_opacity * style.opacity)
    apply_stroke_to_shape(
        shape,
        stroke,
        stroke_width,
        converter,
        style.stroke_opacity * style.opacity,
        stroke_linecap=(style.stroke_linecap or elem.get("stroke-linecap")),
        stroke_linejoin=(style.stroke_linejoin or elem.get("stroke-linejoin")),
    )
    apply_svg_filter_shadow_if_needed(shape, elem, converter)


def _add_svg_path_with_ooxml_curves(
    slide: Any,
    elem: ET.Element,
    d: str,
    transform: Any,
    style: Any,
    converter: Any,
    apply_fill_to_shape: Callable[..., None],
    apply_stroke_to_shape: Callable[..., None],
    apply_svg_filter_shadow_if_needed: Callable[..., None],
) -> Optional[Any]:
    """Render SVG path by writing OOXML custom geometry curve commands."""
    try:
        path = parse_path(d)
    except Exception:
        return None
    if len(path) == 0:
        return None

    ops = _collect_path_draw_ops(path, transform)
    if not ops:
        return None
    bbox = _ops_bbox(ops)
    if not bbox:
        return None
    min_x, min_y, max_x, max_y = bbox
    width = max(1e-6, max_x - min_x)
    height = max(1e-6, max_y - min_y)

    left_emu = converter.to_emu_x(min_x)
    top_emu = converter.to_emu_y(min_y)
    width_emu = max(1, converter.to_emu_width(width))
    height_emu = max(1, converter.to_emu_height(height))

    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left_emu, top_emu, width_emu, height_emu)
    ok = _write_ops_to_shape_custgeom(shape, ops, min_x, min_y, width, height)
    if not ok:
        return None

    _apply_path_style(
        shape=shape,
        elem=elem,
        style=style,
        converter=converter,
        apply_fill_to_shape=apply_fill_to_shape,
        apply_stroke_to_shape=apply_stroke_to_shape,
        apply_svg_filter_shadow_if_needed=apply_svg_filter_shadow_if_needed,
    )
    return shape


def _add_svg_path_with_simple_parser(
    slide: Any,
    elem: ET.Element,
    d: str,
    transform: Any,
    style: Any,
    converter: Any,
    apply_fill_to_shape: Callable[..., None],
    apply_stroke_to_shape: Callable[..., None],
    apply_svg_filter_shadow_if_needed: Callable[..., None],
) -> Optional[Any]:
    """Simple path parser fallback - handles basic M/L/H/V/C/Q/Z commands."""
    points: List[Tuple[float, float]] = []
    current_x, current_y = 0.0, 0.0
    start_x, start_y = 0.0, 0.0
    is_closed = False

    tokens = re.findall(r"[MLHVCSQTAZmlhvcsqtaz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", d)

    i = 0
    cmd = "M"
    while i < len(tokens):
        token = tokens[i]
        if token.isalpha():
            cmd = token
            i += 1
            if cmd in ("Z", "z"):
                is_closed = True
                current_x, current_y = start_x, start_y
            continue
        try:
            if cmd in ("M", "m"):
                x = float(tokens[i])
                y = float(tokens[i + 1])
                i += 2
                if cmd == "m":
                    x += current_x
                    y += current_y
                current_x, current_y = x, y
                start_x, start_y = x, y
                points.append((x, y))
                cmd = "L" if cmd == "M" else "l"
            elif cmd in ("L", "l"):
                x = float(tokens[i])
                y = float(tokens[i + 1])
                i += 2
                if cmd == "l":
                    x += current_x
                    y += current_y
                current_x, current_y = x, y
                points.append((x, y))
            elif cmd in ("H", "h"):
                x = float(tokens[i])
                i += 1
                if cmd == "h":
                    x += current_x
                current_x = x
                points.append((x, current_y))
            elif cmd in ("V", "v"):
                y = float(tokens[i])
                i += 1
                if cmd == "v":
                    y += current_y
                current_y = y
                points.append((current_x, y))
            elif cmd in ("C", "c"):
                x1 = float(tokens[i])
                y1 = float(tokens[i + 1])
                x2 = float(tokens[i + 2])
                y2 = float(tokens[i + 3])
                x = float(tokens[i + 4])
                y = float(tokens[i + 5])
                i += 6
                if cmd == "c":
                    x1 += current_x
                    y1 += current_y
                    x2 += current_x
                    y2 += current_y
                    x += current_x
                    y += current_y
                steps = 12
                for s_idx in range(1, steps + 1):
                    t = s_idx / steps
                    bx = (1 - t) ** 3 * current_x + 3 * (1 - t) ** 2 * t * x1 + 3 * (1 - t) * t**2 * x2 + t**3 * x
                    by = (1 - t) ** 3 * current_y + 3 * (1 - t) ** 2 * t * y1 + 3 * (1 - t) * t**2 * y2 + t**3 * y
                    points.append((bx, by))
                current_x, current_y = x, y
            elif cmd in ("Q", "q"):
                x1 = float(tokens[i])
                y1 = float(tokens[i + 1])
                x = float(tokens[i + 2])
                y = float(tokens[i + 3])
                i += 4
                if cmd == "q":
                    x1 += current_x
                    y1 += current_y
                    x += current_x
                    y += current_y
                steps = 10
                for s_idx in range(1, steps + 1):
                    t = s_idx / steps
                    bx = (1 - t) ** 2 * current_x + 2 * (1 - t) * t * x1 + t**2 * x
                    by = (1 - t) ** 2 * current_y + 2 * (1 - t) * t * y1 + t**2 * y
                    points.append((bx, by))
                current_x, current_y = x, y
            else:
                i += 1
        except (ValueError, IndexError):
            i += 1
            continue

    if len(points) < 2:
        return None

    transformed = [transform.transform_point(x, y) for x, y in points]
    xs = [p[0] for p in transformed]
    ys = [p[1] for p in transformed]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    width = max_x - min_x or 1
    height = max_y - min_y or 1

    left_emu = converter.to_emu_x(min_x)
    top_emu = converter.to_emu_y(min_y)

    local_units = FREEFORM_LOCAL_UNITS
    scale_x = converter.to_emu_width(width) / local_units if width > 0 else 1
    scale_y = converter.to_emu_height(height) / local_units if height > 0 else 1

    start_local_x = _to_local_coord(transformed[0][0], min_x, width, local_units)
    start_local_y = _to_local_coord(transformed[0][1], min_y, height, local_units)
    builder = slide.shapes.build_freeform(start_local_x, start_local_y, scale=(scale_x, scale_y))

    line_segments = []
    for px, py in transformed[1:]:
        local_x = _to_local_coord(px, min_x, width, local_units)
        local_y = _to_local_coord(py, min_y, height, local_units)
        line_segments.append((local_x, local_y))
    if line_segments:
        builder.add_line_segments(line_segments, close=is_closed)
    shape = builder.convert_to_shape(left_emu, top_emu)

    _apply_path_style(
        shape=shape,
        elem=elem,
        style=style,
        converter=converter,
        apply_fill_to_shape=apply_fill_to_shape,
        apply_stroke_to_shape=apply_stroke_to_shape,
        apply_svg_filter_shadow_if_needed=apply_svg_filter_shadow_if_needed,
    )
    return shape


def _add_svg_path_with_svgpathtools(
    slide: Any,
    elem: ET.Element,
    d: str,
    transform: Any,
    style: Any,
    converter: Any,
    apply_fill_to_shape: Callable[..., None],
    apply_stroke_to_shape: Callable[..., None],
    apply_svg_filter_shadow_if_needed: Callable[..., None],
) -> Optional[Any]:
    """Parse path with svgpathtools and convert to PPTX freeform."""
    try:
        path = parse_path(d)
    except Exception:
        return _add_svg_path_with_simple_parser(
            slide,
            elem,
            d,
            transform,
            style,
            converter,
            apply_fill_to_shape,
            apply_stroke_to_shape,
            apply_svg_filter_shadow_if_needed,
        )

    if len(path) == 0:
        return None

    points = []
    for segment in path:
        num_samples = _estimate_segment_samples(segment)
        for i in range(num_samples + 1):
            t = i / num_samples
            try:
                pt = segment.point(t)
                x, y = pt.real, pt.imag
                tx, ty = transform.transform_point(x, y)
                points.append((tx, ty))
            except Exception:
                continue

    if len(points) < 2:
        return None

    unique_points = [points[0]]
    for pt in points[1:]:
        if abs(pt[0] - unique_points[-1][0]) > 0.1 or abs(pt[1] - unique_points[-1][1]) > 0.1:
            unique_points.append(pt)
    if len(unique_points) < 2:
        return None

    xs = [p[0] for p in unique_points]
    ys = [p[1] for p in unique_points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    width = max_x - min_x or 1
    height = max_y - min_y or 1

    left_emu = converter.to_emu_x(min_x)
    top_emu = converter.to_emu_y(min_y)

    local_units = FREEFORM_LOCAL_UNITS
    scale_x = converter.to_emu_width(width) / local_units if width > 0 else 1
    scale_y = converter.to_emu_height(height) / local_units if height > 0 else 1

    start_local_x = _to_local_coord(unique_points[0][0], min_x, width, local_units)
    start_local_y = _to_local_coord(unique_points[0][1], min_y, height, local_units)
    builder = slide.shapes.build_freeform(start_local_x, start_local_y, scale=(scale_x, scale_y))

    line_segments = []
    for px, py in unique_points[1:]:
        local_x = _to_local_coord(px, min_x, width, local_units)
        local_y = _to_local_coord(py, min_y, height, local_units)
        line_segments.append((local_x, local_y))

    is_closed = d.strip().upper().endswith("Z")
    if line_segments:
        builder.add_line_segments(line_segments, close=is_closed)
    shape = builder.convert_to_shape(left_emu, top_emu)

    _apply_path_style(
        shape=shape,
        elem=elem,
        style=style,
        converter=converter,
        apply_fill_to_shape=apply_fill_to_shape,
        apply_stroke_to_shape=apply_stroke_to_shape,
        apply_svg_filter_shadow_if_needed=apply_svg_filter_shadow_if_needed,
    )
    return shape


def add_svg_path(
    *,
    slide: Any,
    elem: ET.Element,
    transform: Any,
    style: Any,
    converter: Any,
    apply_fill_to_shape: Callable[..., None],
    apply_stroke_to_shape: Callable[..., None],
    apply_svg_filter_shadow_if_needed: Callable[..., None],
) -> Optional[Any]:
    """Add one SVG `<path>` element as editable PPTX shape."""
    d = elem.get("d", "")
    if not d:
        return None

    if HAS_SVGPATHTOOLS and parse_path:
        try:
            shape = _add_svg_path_with_ooxml_curves(
                slide,
                elem,
                d,
                transform,
                style,
                converter,
                apply_fill_to_shape,
                apply_stroke_to_shape,
                apply_svg_filter_shadow_if_needed,
            )
            if shape is not None:
                return shape
        except Exception:
            pass
        return _add_svg_path_with_svgpathtools(
            slide,
            elem,
            d,
            transform,
            style,
            converter,
            apply_fill_to_shape,
            apply_stroke_to_shape,
            apply_svg_filter_shadow_if_needed,
        )

    return _add_svg_path_with_simple_parser(
        slide,
        elem,
        d,
        transform,
        style,
        converter,
        apply_fill_to_shape,
        apply_stroke_to_shape,
        apply_svg_filter_shadow_if_needed,
    )
