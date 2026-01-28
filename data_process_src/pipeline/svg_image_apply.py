#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Apply generated images to placeholder SVGs (base64 embedding).
"""

import argparse
import base64
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Optional, Tuple

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"


def safe_float(val: Optional[str], default: float = 0.0) -> float:
    if val is None:
        return default
    val_str = str(val).replace("%", "").strip()
    try:
        return float(val_str) if val_str else default
    except (ValueError, TypeError):
        return default


def format_num(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def build_image_element(
    ns_prefix: str,
    x: float,
    y: float,
    w: float,
    h: float,
    data_uri: str,
    fit: str,
) -> ET.Element:
    image = ET.Element(f"{ns_prefix}image")
    image.set("x", format_num(x))
    image.set("y", format_num(y))
    image.set("width", format_num(w))
    image.set("height", format_num(h))
    if fit == "cover":
        image.set("preserveAspectRatio", "xMidYMid slice")
    else:
        image.set("preserveAspectRatio", "xMidYMid meet")
    image.set(f"{{{XLINK_NS}}}href", data_uri)
    return image


def load_manifest(path: Path) -> Dict[Tuple[str, str], str]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    mapping: Dict[Tuple[str, str], str] = {}
    for item in data:
        svg_file = item.get("svg_file")
        placeholder_id = item.get("placeholder_id")
        image_file = item.get("image_file")
        if svg_file and placeholder_id and image_file:
            mapping[(svg_file, placeholder_id)] = image_file
    return mapping


def load_config(path: Optional[Path]) -> Dict[str, object]:
    if not path or not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_placeholder_geometry(svg_path: Path) -> Dict[str, Dict[str, object]]:
    if not svg_path.exists():
        return {}
    root = ET.parse(svg_path).getroot()
    geometry: Dict[str, Dict[str, object]] = {}
    for g in root.iter():
        if g.tag.endswith("g") and g.get("data-role") == "image-placeholder":
            placeholder_id = g.get("id")
            if not placeholder_id:
                continue
            rect = None
            image = None
            for ch in list(g):
                if ch.tag.endswith("rect"):
                    rect = ch
                    break
                if ch.tag.endswith("image"):
                    image = ch
                    break
            source = rect if rect is not None else image
            if source is None:
                continue
            geometry[placeholder_id] = {
                "x": safe_float(source.get("x"), 0.0),
                "y": safe_float(source.get("y"), 0.0),
                "w": safe_float(source.get("width"), 0.0),
                "h": safe_float(source.get("height"), 0.0),
                "transform": g.get("transform"),
            }
    return geometry


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply generated images to placeholder SVGs.")
    parser.add_argument("--input", required=True, help="Placeholder directory.")
    parser.add_argument("--output", required=True, help="Output directory for refilled SVGs.")
    parser.add_argument("--generated", default="", help="Generated images directory.")
    parser.add_argument("--manifest", default="", help="Generated manifest json path.")
    parser.add_argument("--config", default="config.json", help="Optional config json path.")
    parser.add_argument("--placeholder-ref", default="", help="Reference placeholder SVG directory.")
    parser.add_argument("--fit", default=None, help="Image fit mode: cover or contain.")
    args = parser.parse_args()

    input_dir = Path(args.input)
    output_dir = Path(args.output)
    ref_dir = Path(args.placeholder_ref) if args.placeholder_ref else None
    if ref_dir is None:
        for name in ("placeholder_output", "placeholder"):
            candidate = input_dir.parent / name
            if candidate.exists() and candidate.is_dir() and candidate != input_dir:
                ref_dir = candidate
                break
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_dir = Path(args.generated) if args.generated else output_dir / "generated_images"
    manifest_path = Path(args.manifest) if args.manifest else output_dir / "generated_manifest.json"

    config = load_config(Path(args.config) if args.config else None)
    fit = args.fit or str(config.get("fit", "cover"))
    fit = fit if fit in ("cover", "contain") else "cover"

    manifest = load_manifest(manifest_path)
    if not manifest:
        print("Manifest file not found or empty.")
        return

    svg_files = [p for p in input_dir.glob("*.SVG") if not p.name.startswith("._")]
    processed = 0
    total = len(svg_files)
    for svg in sorted(svg_files):
        tree = ET.parse(svg)
        root = tree.getroot()
        ns_prefix = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
        ref_geometry: Dict[str, Dict[str, object]] = {}
        if ref_dir is not None:
            ref_svg = ref_dir / svg.name
            ref_geometry = load_placeholder_geometry(ref_svg)
        replaced = 0
        for g in list(root.iter()):
            if g.tag.endswith("g") and g.get("data-role") == "image-placeholder":
                placeholder_id = g.get("id")
                if not placeholder_id:
                    continue
                key = (svg.name, placeholder_id)
                image_file = manifest.get(key)
                if not image_file:
                    continue
                img_path = generated_dir / image_file
                if not img_path.exists():
                    continue
                rect = None
                existing_image = None
                for ch in list(g):
                    if ch.tag.endswith("rect"):
                        rect = ch
                        break
                    if ch.tag.endswith("image"):
                        existing_image = ch
                        break
                if rect is None:
                    ref_info = ref_geometry.get(placeholder_id)
                    if ref_info:
                        x = float(ref_info["x"])
                        y = float(ref_info["y"])
                        w = float(ref_info["w"])
                        h = float(ref_info["h"])
                        ref_transform = ref_info.get("transform")
                    elif existing_image is not None:
                        x = safe_float(existing_image.get("x"), 0.0)
                        y = safe_float(existing_image.get("y"), 0.0)
                        w = safe_float(existing_image.get("width"), 0.0)
                        h = safe_float(existing_image.get("height"), 0.0)
                        ref_transform = None
                    else:
                        continue
                else:
                    x = safe_float(rect.get("x"), 0.0)
                    y = safe_float(rect.get("y"), 0.0)
                    w = safe_float(rect.get("width"), 0.0)
                    h = safe_float(rect.get("height"), 0.0)
                    ref_transform = ref_geometry.get(placeholder_id, {}).get("transform")
                data_uri = "data:image/png;base64," + base64.b64encode(img_path.read_bytes()).decode("utf-8")
                image = build_image_element(ns_prefix, x, y, w, h, data_uri, fit)
                new_g = ET.Element(f"{ns_prefix}g")
                if placeholder_id:
                    new_g.set("id", placeholder_id)
                for attr, val in g.attrib.items():
                    if attr.startswith("data-"):
                        new_g.set(attr, val)
                transform_value = g.attrib.get("transform") or ref_transform
                if transform_value:
                    new_g.set("transform", transform_value)
                for attr in ("clip-path", "opacity", "filter", "mask"):
                    if attr in g.attrib:
                        new_g.set(attr, g.attrib[attr])
                new_g.append(image)
                parent = None
                for p in root.iter():
                    if g in list(p):
                        parent = p
                        break
                if parent is None:
                    continue
                children = list(parent)
                index = children.index(g)
                parent.remove(g)
                parent.insert(index, new_g)
                replaced += 1
        out_svg = output_dir / svg.name
        tree.write(out_svg, encoding="utf-8", xml_declaration=True)
        processed += 1
        print(f"Applied: {processed}/{total} {out_svg} (replaced {replaced})")


if __name__ == "__main__":
    main()
