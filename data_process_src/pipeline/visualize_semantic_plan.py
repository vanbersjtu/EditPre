#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Embed plan.json bounding boxes into SVG for visualization.
"""

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple


SVG_NS = "http://www.w3.org/2000/svg"
GROUP_COLORS = ["#1f77b4", "#2ca02c", "#9467bd", "#8c564b", "#17becf"]
TEXTBOX_COLOR = "#d62728"


def format_num(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def load_json(path: Path) -> Optional[Dict[str, object]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def union_bbox(bboxes: Iterable[Dict[str, float]]) -> Optional[Dict[str, float]]:
    boxes = [b for b in bboxes if isinstance(b, dict)]
    if not boxes:
        return None
    xs = [b.get("x", 0.0) for b in boxes]
    ys = [b.get("y", 0.0) for b in boxes]
    x2 = [b.get("x", 0.0) + b.get("w", 0.0) for b in boxes]
    y2 = [b.get("y", 0.0) + b.get("h", 0.0) for b in boxes]
    return {
        "x": min(xs),
        "y": min(ys),
        "w": max(x2) - min(xs),
        "h": max(y2) - min(ys),
    }


def normalize_plan(plan: Dict[str, object]) -> Dict[str, object]:
    nodes = plan.get("nodes") if isinstance(plan.get("nodes"), list) else None
    if nodes:
        return plan
    textboxes = plan.get("textboxes") if isinstance(plan.get("textboxes"), list) else []
    nodes = []
    for idx, tb in enumerate(textboxes, 1):
        if not isinstance(tb, dict):
            continue
        node_id = tb.get("id") if isinstance(tb.get("id"), str) else None
        if not node_id:
            node_id = tb.get("tb_id") if isinstance(tb.get("tb_id"), str) else f"tb-{idx:03d}"
        nodes.append(
            {
                "id": node_id,
                "type": "textbox",
                "role": tb.get("role", "unknown"),
                "item_ids": tb.get("item_ids", []),
            }
        )
    root = plan.get("root") if isinstance(plan.get("root"), str) else None
    if not root:
        root = "g-root"
        nodes.append({"id": root, "type": "group", "role": "group", "children": [n["id"] for n in nodes]})
    return {"nodes": nodes, "root": root}


def compute_bboxes(
    plan: Dict[str, object],
    items_by_id: Dict[str, Dict[str, float]],
) -> Tuple[Dict[str, Dict[str, float]], Dict[str, int]]:
    node_list = plan.get("nodes") if isinstance(plan.get("nodes"), list) else []
    node_map = {n.get("id"): n for n in node_list if isinstance(n, dict) and isinstance(n.get("id"), str)}
    root_id = plan.get("root") if isinstance(plan.get("root"), str) else None

    cache: Dict[str, Dict[str, float]] = {}

    def compute(node_id: str) -> Optional[Dict[str, float]]:
        if node_id in cache:
            return cache[node_id]
        node = node_map.get(node_id)
        if not node:
            return None
        if node.get("type") == "textbox":
            item_ids = node.get("item_ids") if isinstance(node.get("item_ids"), list) else []
            boxes = [items_by_id[iid] for iid in item_ids if iid in items_by_id]
            bbox = union_bbox(boxes)
        else:
            child_ids = node.get("children") if isinstance(node.get("children"), list) else []
            boxes = [compute(cid) for cid in child_ids]
            bbox = union_bbox([b for b in boxes if b])
        if bbox:
            cache[node_id] = bbox
        return bbox

    if root_id:
        compute(root_id)
    else:
        for node_id in node_map:
            compute(node_id)

    depths: Dict[str, int] = {}

    def walk(node_id: str, depth: int) -> None:
        if node_id in depths:
            return
        depths[node_id] = depth
        node = node_map.get(node_id)
        if not node:
            return
        if node.get("type") == "group":
            for child_id in node.get("children", []):
                if isinstance(child_id, str):
                    walk(child_id, depth + 1)

    if root_id and root_id in node_map:
        walk(root_id, 0)
    else:
        for node_id in node_map:
            walk(node_id, 0)

    return cache, depths


def add_rect(
    parent: ET.Element,
    ns_prefix: str,
    bbox: Dict[str, float],
    color: str,
    width: int,
    dash: Optional[str],
) -> None:
    rect = ET.Element(f"{ns_prefix}rect")
    rect.set("x", format_num(float(bbox.get("x", 0.0))))
    rect.set("y", format_num(float(bbox.get("y", 0.0))))
    rect.set("width", format_num(float(bbox.get("w", 0.0))))
    rect.set("height", format_num(float(bbox.get("h", 0.0))))
    rect.set("fill", "none")
    rect.set("stroke", color)
    rect.set("stroke-width", str(width))
    if dash:
        rect.set("stroke-dasharray", dash)
    parent.append(rect)


def add_label(parent: ET.Element, ns_prefix: str, bbox: Dict[str, float], text: str, color: str) -> None:
    label = ET.Element(f"{ns_prefix}text")
    label.set("x", format_num(float(bbox.get("x", 0.0) + 4)))
    label.set("y", format_num(float(bbox.get("y", 0.0) + 14)))
    label.set("fill", color)
    label.set("font-size", "12")
    label.text = text
    parent.append(label)


def render_svg(
    svg_path: Path,
    plan: Dict[str, object],
    boxes: Dict[str, Dict[str, float]],
    depths: Dict[str, int],
    output_path: Path,
    draw_groups: bool,
    draw_textboxes: bool,
    show_labels: bool,
) -> None:
    tree = ET.parse(svg_path)
    root = tree.getroot()
    ET.register_namespace("", SVG_NS)
    ns_prefix = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""

    viz_group = ET.Element(f"{ns_prefix}g")
    viz_group.set("id", "visualization-layer")
    viz_group.set("data-type", "visualization-layer")

    node_list = plan.get("nodes") if isinstance(plan.get("nodes"), list) else []
    nodes = [n for n in node_list if isinstance(n, dict) and isinstance(n.get("id"), str)]

    if draw_groups:
        for node in nodes:
            if node.get("type") != "group":
                continue
            node_id = node.get("id")
            bbox = boxes.get(node_id)
            if not bbox:
                continue
            depth = depths.get(node_id, 0)
            color = GROUP_COLORS[depth % len(GROUP_COLORS)]
            add_rect(viz_group, ns_prefix, bbox, color, 2, "6 4")
            if show_labels:
                role = node.get("role") if isinstance(node.get("role"), str) else ""
                add_label(viz_group, ns_prefix, bbox, f"group/{role}/{node_id}", color)

    if draw_textboxes:
        for node in nodes:
            if node.get("type") != "textbox":
                continue
            node_id = node.get("id")
            bbox = boxes.get(node_id)
            if not bbox:
                continue
            add_rect(viz_group, ns_prefix, bbox, TEXTBOX_COLOR, 3, None)
            if show_labels:
                role = node.get("role") if isinstance(node.get("role"), str) else ""
                add_label(viz_group, ns_prefix, bbox, f"textbox/{role}/{node_id}", TEXTBOX_COLOR)

    root.append(viz_group)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)


def process_ppt_dir(ppt_dir: Path, draw_groups: bool, draw_textboxes: bool, show_labels: bool) -> None:
    plans_dir = ppt_dir / "meta" / "plans"
    items_dir = ppt_dir / "meta" / "items"
    if not plans_dir.exists() or not items_dir.exists():
        return
    output_dir = ppt_dir / "visem_SVG"
    output_dir.mkdir(parents=True, exist_ok=True)

    for plan_path in sorted(plans_dir.glob("*.json")):
        svg_path = ppt_dir / f"{plan_path.stem}.SVG"
        if not svg_path.exists():
            svg_path = ppt_dir / f"{plan_path.stem}.svg"
        if not svg_path.exists():
            continue
        items_path = items_dir / plan_path.name
        items_doc = load_json(items_path)
        plan_doc = load_json(plan_path)
        if not items_doc or not plan_doc:
            continue
        plan = normalize_plan(plan_doc)
        items = items_doc.get("items") if isinstance(items_doc.get("items"), list) else []
        items_by_id = {
            it.get("id"): it.get("bbox")
            for it in items
            if isinstance(it, dict) and isinstance(it.get("id"), str) and isinstance(it.get("bbox"), dict)
        }
        boxes, depths = compute_bboxes(plan, items_by_id)
        output_path = output_dir / f"{plan_path.stem}.SVG"
        render_svg(
            svg_path,
            plan,
            boxes,
            depths,
            output_path,
            draw_groups,
            draw_textboxes,
            show_labels,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Visualize semantic plan bboxes in SVG.")
    parser.add_argument("--input", required=True, help="Semantic output root or a single PPT folder.")
    parser.add_argument("--no-groups", action="store_true", help="Do not draw group boxes.")
    parser.add_argument("--no-textboxes", action="store_true", help="Do not draw textbox boxes.")
    parser.add_argument("--no-labels", action="store_true", help="Do not draw labels.")
    args = parser.parse_args()

    root = Path(args.input)
    draw_groups = not args.no_groups
    draw_textboxes = not args.no_textboxes
    show_labels = not args.no_labels

    if (root / "meta" / "plans").exists():
        process_ppt_dir(root, draw_groups, draw_textboxes, show_labels)
        return

    for ppt_dir in sorted(root.iterdir()):
        if not ppt_dir.is_dir():
            continue
        if (ppt_dir / "meta" / "plans").exists():
            process_ppt_dir(ppt_dir, draw_groups, draw_textboxes, show_labels)


if __name__ == "__main__":
    main()
