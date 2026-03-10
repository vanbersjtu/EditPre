"""svgBlip helpers for high-fidelity icon/region rendering."""

from __future__ import annotations

import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..constants import (
    CT_NS,
    REL_IMAGE,
    REL_NS,
    SVG_BLIP_EXT_URI,
    SVG_BLIP_NS,
    TRANSPARENT_PNG_BYTES,
)
from ..utils.lengths import parse_length
from ..utils.svg_helpers import tag_name


def elem_is_hidden_bbox_rect(elem: ET.Element) -> bool:
    """Return True if element is a hidden bbox helper rect."""
    if tag_name(elem) != "rect":
        return False
    fill = (elem.get("fill") or "").strip().lower()
    stroke = (elem.get("stroke") or "").strip().lower()
    cls = (elem.get("class") or "").strip().lower()
    if cls in ("tb-bbox", "vg-bbox"):
        return True
    return fill in ("none", "transparent", "") and stroke in ("none", "transparent", "")


def group_local_bbox_from_hidden_rect(elem: ET.Element) -> Optional[Tuple[float, float, float, float]]:
    """Extract local bbox from hidden helper rect in a group."""
    for child in elem:
        if not elem_is_hidden_bbox_rect(child):
            continue
        w = parse_length(child.get("width"), 0.0)
        h = parse_length(child.get("height"), 0.0)
        if w <= 0 or h <= 0:
            continue
        x = parse_length(child.get("x"), 0.0)
        y = parse_length(child.get("y"), 0.0)
        return (x, y, w, h)
    return None


def transform_bbox(mat: Any, x: float, y: float, w: float, h: float) -> Tuple[float, float, float, float]:
    """Transform local bbox corners with matrix and return axis-aligned bbox."""
    pts = [
        mat.transform_point(x, y),
        mat.transform_point(x + w, y),
        mat.transform_point(x + w, y + h),
        mat.transform_point(x, y + h),
    ]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    return (min_x, min_y, max_x - min_x, max_y - min_y)


def count_group_graphics(elem: ET.Element) -> Dict[str, int]:
    """Count common primitive nodes in group subtree."""
    keys = ("path", "rect", "line", "circle", "ellipse", "polyline", "polygon", "text")
    cnt = {k: 0 for k in keys}
    for node in elem.iter():
        t = tag_name(node)
        if t in cnt:
            cnt[t] += 1
    return cnt


def should_render_group_as_svgblip(elem: ET.Element) -> bool:
    """Heuristic for complex-icon groups: render as svgBlip for fidelity."""
    if tag_name(elem) != "g":
        return False
    dtype = (elem.get("data-type") or "").strip().lower()
    if dtype in (
        "visual-layer",
        "visual-group",
        "semantic-layer",
        "text-group",
        "textbox",
        "image-placeholder",
    ):
        return False
    bbox = group_local_bbox_from_hidden_rect(elem)
    if not bbox:
        return False
    _, _, w, h = bbox
    if w <= 0 or h <= 0:
        return False
    # Target small/medium icon clusters, avoid whole-card replacement.
    if w > 260 or h > 220:
        return False
    cnt = count_group_graphics(elem)
    prim = sum(cnt.values())
    if prim < 10:
        return False
    if (cnt["path"] + cnt["polyline"] + cnt["polygon"]) < 3:
        return False
    return True


def build_svg_region_snippet(root: ET.Element, x: float, y: float, w: float, h: float) -> bytes:
    """Create cropped SVG snippet by viewBox for svgBlip embedding."""
    clone_root = ET.fromstring(ET.tostring(root, encoding="utf-8"))
    for child in list(clone_root):
        t = tag_name(child)
        if t != "g":
            continue
        if child.get("id") == "semantic-layer" or child.get("data-type") == "semantic-layer":
            clone_root.remove(child)
    clone_root.set("viewBox", f"{x:.4f} {y:.4f} {w:.4f} {h:.4f}")
    clone_root.set("width", f"{w:.4f}")
    clone_root.set("height", f"{h:.4f}")
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
    return ET.tostring(clone_root, encoding="utf-8", xml_declaration=True)


def add_svgblip_region_picture(
    slide: Any,
    left_px: float,
    top_px: float,
    width_px: float,
    height_px: float,
    converter: Any,
    shape_name: str,
) -> Optional[Any]:
    """Insert temporary picture shell to be upgraded to svgBlip in post-process."""
    if width_px <= 0 or height_px <= 0:
        return None
    left = converter.to_emu_x(left_px)
    top = converter.to_emu_y(top_px)
    width = max(1, converter.to_emu_width(width_px))
    height = max(1, converter.to_emu_height(height_px))
    with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
        tmp.write(TRANSPARENT_PNG_BYTES)
        tmp_path = tmp.name
    try:
        shape = slide.shapes.add_picture(tmp_path, left, top, width, height)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    shape.name = shape_name
    return shape


def inject_svg_blips_into_pptx(pptx_path: Path, jobs: List[Dict[str, Any]]) -> None:
    """Post-process PPTX to attach svgBlip relationships to picture shells."""
    if not jobs:
        return

    ET.register_namespace("", CT_NS)
    ET.register_namespace("", REL_NS)
    ET.register_namespace("a", "http://schemas.openxmlformats.org/drawingml/2006/main")
    ET.register_namespace("r", "http://schemas.openxmlformats.org/officeDocument/2006/relationships")
    ET.register_namespace("p", "http://schemas.openxmlformats.org/presentationml/2006/main")
    ET.register_namespace("asvg", SVG_BLIP_NS)

    with zipfile.ZipFile(pptx_path, "r") as zin:
        files: Dict[str, bytes] = {name: zin.read(name) for name in zin.namelist()}

    ct_name = "[Content_Types].xml"
    if ct_name in files:
        ct_root = ET.fromstring(files[ct_name])
        has_svg = False
        for node in ct_root.findall(f"{{{CT_NS}}}Default"):
            if (node.get("Extension") or "").lower() == "svg":
                has_svg = True
                break
        if not has_svg:
            d = ET.Element(f"{{{CT_NS}}}Default")
            d.set("Extension", "svg")
            d.set("ContentType", "image/svg+xml")
            ct_root.append(d)
            files[ct_name] = ET.tostring(ct_root, encoding="utf-8", xml_declaration=True)

    rel_ns = {"rel": REL_NS}
    slide_ns = {
        "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }

    for idx, job in enumerate(jobs, 1):
        slide_index = int(job.get("slide_index", 0) or 0)
        shape_name = str(job.get("shape_name") or "")
        svg_bytes = job.get("svg_bytes")
        if slide_index <= 0 or not shape_name or not isinstance(svg_bytes, (bytes, bytearray)):
            continue

        slide_name = f"ppt/slides/slide{slide_index}.xml"
        rels_name = f"ppt/slides/_rels/slide{slide_index}.xml.rels"
        if slide_name not in files or rels_name not in files:
            continue

        media_name = f"ppt/media/svgblip_{slide_index}_{idx}.svg"
        files[media_name] = bytes(svg_bytes)

        rels_root = ET.fromstring(files[rels_name])
        max_rid = 0
        for rel in rels_root.findall("rel:Relationship", rel_ns):
            rid = rel.get("Id", "")
            if rid.startswith("rId"):
                try:
                    max_rid = max(max_rid, int(rid[3:]))
                except Exception:
                    pass
        new_rid = f"rId{max_rid + 1}"
        new_rel = ET.Element(f"{{{REL_NS}}}Relationship")
        new_rel.set("Id", new_rid)
        new_rel.set("Type", REL_IMAGE)
        new_rel.set("Target", f"../media/{Path(media_name).name}")
        rels_root.append(new_rel)
        files[rels_name] = ET.tostring(rels_root, encoding="utf-8", xml_declaration=True)

        slide_root = ET.fromstring(files[slide_name])
        pic_nodes = slide_root.findall(".//p:pic", slide_ns)
        target_pic = None
        for pic in pic_nodes:
            c_nv_pr = pic.find("./p:nvPicPr/p:cNvPr", slide_ns)
            if c_nv_pr is not None and (c_nv_pr.get("name") or "") == shape_name:
                target_pic = pic
                break
        if target_pic is None:
            continue
        blip = target_pic.find("./p:blipFill/a:blip", slide_ns)
        if blip is None:
            continue
        ext_lst = blip.find("./a:extLst", slide_ns)
        if ext_lst is None:
            ext_lst = ET.SubElement(blip, "{http://schemas.openxmlformats.org/drawingml/2006/main}extLst")
        ext = None
        for node in list(ext_lst):
            if node.tag == "{http://schemas.openxmlformats.org/drawingml/2006/main}ext" and node.get("uri") == SVG_BLIP_EXT_URI:
                ext = node
                break
        if ext is None:
            ext = ET.SubElement(ext_lst, "{http://schemas.openxmlformats.org/drawingml/2006/main}ext")
            ext.set("uri", SVG_BLIP_EXT_URI)
        for node in list(ext):
            ext.remove(node)
        svg_blip = ET.SubElement(ext, f"{{{SVG_BLIP_NS}}}svgBlip")
        svg_blip.set("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed", new_rid)
        files[slide_name] = ET.tostring(slide_root, encoding="utf-8", xml_declaration=True)

    tmp_out = pptx_path.with_suffix(".svgblip.tmp.pptx")
    with zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in files.items():
            zout.writestr(name, data)
    tmp_out.replace(pptx_path)

