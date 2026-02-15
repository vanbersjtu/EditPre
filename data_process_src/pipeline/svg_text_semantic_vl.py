#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Semantic grouping for SVG text using a PPT screenshot (OpenAI-compatible VLM).
"""

import argparse
import base64
import io
import json
import os
import random
import tempfile
import time
import threading
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    from PIL import Image
except Exception:  # pragma: no cover - optional dependency
    Image = None

from .svg_text_semantic import (
    FAILED_TASKS_NAME,
    ROLE_SET,
    SVG_NS,
    RateLimiter,
    apply_plan_to_svg,
    build_prompt,
    ensure_text_ids,
    extract_items_with_playwright,
    get_canvas_size,
    load_config,
    merge_adjacent_textboxes,
    normalize_tree_plan,
    parse_json_from_text,
    read_text_xml,
    sync_playwright,
)

TARGET_IMAGE_SIZE = (1280, 720)
# Fill these defaults for quick local runs (CLI/config/env still override).
DEFAULT_BASE_URL = "https://api.siliconflow.cn"
DEFAULT_API_KEY = "sk-jbgfrmskloebvrrfskymtouxikkjffaezbcenyjfsmuorqzt"
DEFAULT_MODEL = "Qwen/Qwen3-VL-30B-A3B-Instruct"
GROUP_COLORS = ["#1f77b4", "#2ca02c", "#9467bd", "#8c564b", "#17becf"]
TEXTBOX_COLOR = "#d62728"
STYLE_KEYS = ("fontFamily", "fontSize", "fontWeight", "fontStyle")


def format_num(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def compact_style(style: Optional[Dict[str, object]]) -> Dict[str, object]:
    if not isinstance(style, dict):
        return {}
    compacted: Dict[str, object] = {}
    for key in STYLE_KEYS:
        value = style.get(key)
        if value is None:
            continue
        compacted[key] = value
    return compacted


def round_bbox(bbox: Dict[str, object]) -> Dict[str, float]:
    rounded: Dict[str, float] = {}
    for key in ("x", "y", "w", "h"):
        try:
            value = float(bbox.get(key, 0.0))
        except Exception:
            value = 0.0
        rounded[key] = round(value, 1)
    return rounded


def build_llm_items(
    items_doc: Dict[str, object],
) -> Tuple[Dict[str, object], Dict[str, List[str]]]:
    items = items_doc.get("items") if isinstance(items_doc.get("items"), list) else []
    item_map = {
        it.get("id"): it
        for it in items
        if isinstance(it, dict) and isinstance(it.get("id"), str)
    }
    if not item_map:
        return {"canvas": items_doc.get("canvas", {}), "items": []}, {}

    pseudo_plan = {
        "nodes": [
            {
                "id": item_id,
                "type": "textbox",
                "role": "unknown",
                "order": idx,
                "item_ids": [item_id],
                "confidence": 1.0,
            }
            for idx, item_id in enumerate(item_map.keys())
        ],
        "root": "g-root",
    }
    merged_plan = merge_adjacent_textboxes(pseudo_plan, items)
    merged_nodes = [
        n
        for n in merged_plan.get("nodes", [])
        if isinstance(n, dict) and n.get("type") == "textbox" and isinstance(n.get("id"), str)
    ]

    merged_map: Dict[str, List[str]] = {}
    llm_items: List[Dict[str, object]] = []
    for node in merged_nodes:
        node_id = node.get("id")
        if not isinstance(node_id, str):
            continue
        item_ids = [iid for iid in node.get("item_ids", []) if isinstance(iid, str) and iid in item_map]
        if not item_ids:
            continue
        merged_map[node_id] = item_ids

        texts = []
        bboxes = []
        for iid in item_ids:
            item = item_map.get(iid, {})
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
            bbox = item.get("bbox")
            if isinstance(bbox, dict):
                bboxes.append(bbox)

        merged_bbox = union_bbox(bboxes) if bboxes else None
        style = compact_style(item_map[item_ids[0]].get("style"))

        llm_item: Dict[str, object] = {"id": node_id}
        if texts:
            llm_item["text"] = "\n".join(texts)
        if merged_bbox:
            llm_item["bbox"] = round_bbox(merged_bbox)
        if style:
            llm_item["style"] = style
        llm_items.append(llm_item)

    return {"canvas": items_doc.get("canvas", {}), "items": llm_items}, merged_map


def expand_plan_item_ids(
    plan: Dict[str, object],
    merged_map: Dict[str, List[str]],
) -> Dict[str, object]:
    if not merged_map:
        return plan
    node_list = plan.get("nodes")
    if not isinstance(node_list, list):
        return plan
    for node in node_list:
        if not isinstance(node, dict) or node.get("type") != "textbox":
            continue
        item_ids = node.get("item_ids")
        if not isinstance(item_ids, list):
            continue
        expanded: List[str] = []
        seen = set()
        for iid in item_ids:
            if not isinstance(iid, str):
                continue
            replacement = merged_map.get(iid, [iid])
            for sub_id in replacement:
                if sub_id not in seen:
                    expanded.append(sub_id)
                    seen.add(sub_id)
        node["item_ids"] = expanded
    unassigned = plan.get("unassigned")
    if isinstance(unassigned, list):
        expanded_unassigned: List[str] = []
        seen = set()
        for iid in unassigned:
            if not isinstance(iid, str):
                continue
            replacement = merged_map.get(iid, [iid])
            for sub_id in replacement:
                if sub_id not in seen:
                    expanded_unassigned.append(sub_id)
                    seen.add(sub_id)
        plan["unassigned"] = expanded_unassigned
    return plan


def find_image_path(svg_path: Path, input_dir: Path, image_root: Path) -> Optional[Path]:
    try:
        rel_path = svg_path.relative_to(input_dir)
    except ValueError:
        rel_path = Path(svg_path.name)
    stem = rel_path.with_suffix("")
    base_candidates = [image_root]
    if input_dir.name:
        base_candidates.append(image_root / input_dir.name)
    for ext in (".png", ".PNG", ".jpg", ".jpeg", ".JPG", ".JPEG"):
        for base in base_candidates:
            candidate = base / stem.with_suffix(ext)
            if candidate.exists():
                return candidate
    return None


def load_image_base64(path: Path, target_size: Tuple[int, int]) -> Tuple[Optional[str], Optional[str]]:
    if Image is None:
        return None, "PIL is not available; install pillow to enable image resize."
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            if hasattr(Image, "Resampling"):
                resample = Image.Resampling.LANCZOS
            else:  # pragma: no cover
                resample = Image.LANCZOS
            img = img.resize(target_size, resample=resample)
            buffer = io.BytesIO()
            img.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("utf-8"), None
    except Exception as exc:
        return None, f"Failed to load image: {exc}"


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


def compute_plan_bboxes(
    plan: Dict[str, object],
    items_by_id: Dict[str, Dict[str, float]],
) -> Tuple[Dict[str, Dict[str, float]], Dict[str, int]]:
    node_list = plan.get("nodes") if isinstance(plan.get("nodes"), list) else []
    node_map = {n.get("id"): n for n in node_list if isinstance(n, dict) and isinstance(n.get("id"), str)}
    root_id = plan.get("root") if isinstance(plan.get("root"), str) else None

    cache: Dict[str, Dict[str, float]] = {}
    visiting: set = set()

    def compute(node_id: str) -> Optional[Dict[str, float]]:
        if node_id in cache:
            return cache[node_id]
        if node_id in visiting:
            return None
        node = node_map.get(node_id)
        if not node:
            return None
        visiting.add(node_id)
        if node.get("type") == "textbox":
            item_ids = node.get("item_ids") if isinstance(node.get("item_ids"), list) else []
            boxes = [items_by_id[iid] for iid in item_ids if iid in items_by_id]
            bbox = union_bbox(boxes)
        else:
            child_ids = node.get("children") if isinstance(node.get("children"), list) else []
            boxes = [compute(cid) for cid in child_ids]
            bbox = union_bbox([b for b in boxes if b])
        visiting.remove(node_id)
        if bbox:
            cache[node_id] = bbox
        return bbox

    if root_id:
        compute(root_id)
    else:
        for node_id in node_map:
            compute(node_id)

    depths: Dict[str, int] = {}

    visiting_depths: set = set()

    def walk(node_id: str, depth: int) -> None:
        if node_id in depths:
            return
        if node_id in visiting_depths:
            return
        visiting_depths.add(node_id)
        depths[node_id] = depth
        node = node_map.get(node_id)
        if not node:
            visiting_depths.remove(node_id)
            return
        if node.get("type") == "group":
            for child_id in node.get("children", []):
                if isinstance(child_id, str):
                    walk(child_id, depth + 1)
        visiting_depths.remove(node_id)

    if root_id and root_id in node_map:
        walk(root_id, 0)
    else:
        for node_id in node_map:
            walk(node_id, 0)

    return cache, depths


def add_overlay_rect(
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


def add_overlay_label(parent: ET.Element, ns_prefix: str, bbox: Dict[str, float], text: str, color: str) -> None:
    label = ET.Element(f"{ns_prefix}text")
    label.set("x", format_num(float(bbox.get("x", 0.0) + 4)))
    label.set("y", format_num(float(bbox.get("y", 0.0) + 14)))
    label.set("fill", color)
    label.set("font-size", "12")
    label.text = text
    parent.append(label)


def write_visualization_svg(
    tree: ET.ElementTree,
    plan: Dict[str, object],
    items: List[Dict[str, object]],
    output_path: Path,
    show_labels: bool,
) -> None:
    root_copy = ET.fromstring(ET.tostring(tree.getroot()))
    viz_tree = ET.ElementTree(root_copy)
    ET.register_namespace("", SVG_NS)
    ns_prefix = root_copy.tag.split("}")[0] + "}" if root_copy.tag.startswith("{") else ""

    viz_group = ET.Element(f"{ns_prefix}g")
    viz_group.set("id", "visualization-layer")
    viz_group.set("data-type", "visualization-layer")

    node_list = plan.get("nodes") if isinstance(plan.get("nodes"), list) else []
    nodes = [n for n in node_list if isinstance(n, dict) and isinstance(n.get("id"), str)]
    items_by_id = {
        it.get("id"): it.get("bbox")
        for it in items
        if isinstance(it, dict) and isinstance(it.get("id"), str) and isinstance(it.get("bbox"), dict)
    }
    boxes, depths = compute_plan_bboxes(plan, items_by_id)

    for node in nodes:
        node_type = node.get("type")
        node_id = node.get("id")
        if node_type == "group":
            bbox = boxes.get(node_id)
            if not bbox:
                continue
            color = GROUP_COLORS[depths.get(node_id, 0) % len(GROUP_COLORS)]
            add_overlay_rect(viz_group, ns_prefix, bbox, color, 2, "6 4")
            if show_labels:
                role = node.get("role") if isinstance(node.get("role"), str) else ""
                add_overlay_label(viz_group, ns_prefix, bbox, f"group/{role}/{node_id}", color)
        elif node_type == "textbox":
            bbox = boxes.get(node_id)
            if not bbox:
                continue
            add_overlay_rect(viz_group, ns_prefix, bbox, TEXTBOX_COLOR, 3, None)
            if show_labels:
                role = node.get("role") if isinstance(node.get("role"), str) else ""
                add_overlay_label(viz_group, ns_prefix, bbox, f"textbox/{role}/{node_id}", TEXTBOX_COLOR)

    root_copy.append(viz_group)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    viz_tree.write(output_path, encoding="utf-8", xml_declaration=True)


def build_vl_prompt(
    base_prompt: str,
    canvas: Dict[str, object],
    image_size: Tuple[int, int],
) -> str:
    canvas_w = canvas.get("w", 0)
    canvas_h = canvas.get("h", 0)
    return (
        base_prompt
        + f"\n\n补充说明：你还会收到该页 PPT 的截图（已缩放到 {image_size[0]}x{image_size[1]}）。"
        "截图仅用于理解视觉层级、对齐、间距与版块关系；"
        "文本内容与坐标以 INPUT_JSON 为准，不要从截图中 OCR 文本。"
        "INPUT_JSON 中的 text 可能包含换行（表示同一文本框的多行），"
        "并且只保留 id/text/bbox/style（字体相关）等必要字段。"
        f"SVG 画布尺寸为 {canvas_w}x{canvas_h}。"
    )


def call_vision_llm(
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    items: Dict[str, object],
    image_b64: str,
    max_tokens: int,
    temperature: float,
    timeout: int,
) -> Tuple[Optional[str], Optional[str]]:
    url = base_url.rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are an SVG text semantic annotator. Output JSON only.",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                    },
                    {
                        "type": "text",
                        "text": "INPUT_JSON:\n" + json.dumps(items, ensure_ascii=False),
                    },
                ],
            },
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        err_msg = str(exc)
        try:
            if hasattr(exc, "read"):
                err_body = exc.read().decode("utf-8", errors="ignore")
                if err_body:
                    err_msg = err_msg + "\n" + err_body
        except Exception:
            pass
        return None, err_msg
    try:
        return data["choices"][0]["message"]["content"].strip(), None
    except Exception:
        return None, "Invalid response format."


def call_vision_llm_with_retries(
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    items: Dict[str, object],
    image_b64: str,
    max_tokens: int,
    temperature: float,
    timeout: int,
    retries: int,
    limiter: Optional[RateLimiter],
) -> Tuple[Optional[str], Optional[str]]:
    last_error: Optional[str] = None
    for attempt in range(retries + 1):
        if limiter:
            limiter.acquire()
        resp, err = call_vision_llm(
            base_url,
            api_key,
            model,
            prompt,
            items,
            image_b64,
            max_tokens,
            temperature,
            timeout,
        )
        if resp:
            return resp, None
        if err:
            last_error = err
        if attempt < retries:
            backoff = min(2 ** attempt, 10) + random.random() * 0.3
            time.sleep(backoff)
    return None, last_error


def process_svg(
    svg_path: Path,
    output_path: Path,
    input_dir: Path,
    image_root: Path,
    image_size: Tuple[int, int],
    meta_items_dir: Path,
    meta_plans_dir: Path,
    meta_raw_dir: Path,
    base_url: str,
    api_key: str,
    model: str,
    max_tokens: int,
    temperature: float,
    timeout: int,
    retries: int,
    limiter: Optional[RateLimiter],
    pad: float,
    keep_ids: bool,
    prompt: str,
    visualize: bool,
    viz_labels: bool,
) -> Tuple[bool, Optional[str]]:
    tree = ET.parse(svg_path)
    root = tree.getroot()
    ET.register_namespace("", SVG_NS)
    id_map = ensure_text_ids(root)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".svg") as tmp:
        tmp_path = Path(tmp.name)
        tree.write(tmp_path, encoding="utf-8", xml_declaration=True)

    items = extract_items_with_playwright(tmp_path)
    try:
        tmp_path.unlink()
    except FileNotFoundError:
        pass

    for item in items:
        item_id = item.get("id")
        elem = id_map.get(item_id)
        if elem is not None:
            item["text_xml"] = read_text_xml(elem)

    items_doc = {
        "canvas": get_canvas_size(root),
        "items": items,
    }
    llm_items_doc, llm_merged_map = build_llm_items(items_doc)

    meta_items_dir.mkdir(parents=True, exist_ok=True)
    meta_plans_dir.mkdir(parents=True, exist_ok=True)
    items_path = meta_items_dir / (svg_path.stem + ".json")
    items_path.write_text(json.dumps(llm_items_doc, ensure_ascii=False, indent=2), encoding="utf-8")

    if not base_url or not api_key or not model:
        plan = {"textboxes": [], "unassigned": [it.get("id") for it in items if it.get("id")]}
    else:
        image_path = find_image_path(svg_path, input_dir, image_root)
        if image_path is None:
            return False, f"Missing image for {svg_path.name}"
        image_b64, image_err = load_image_base64(image_path, image_size)
        if image_err or not image_b64:
            return False, image_err or "Failed to load image"

        prompt_with_image = build_vl_prompt(prompt, items_doc.get("canvas", {}), image_size)
        response, error = call_vision_llm_with_retries(
            base_url,
            api_key,
            model,
            prompt_with_image,
            llm_items_doc,
            image_b64,
            max_tokens,
            temperature,
            timeout,
            retries,
            limiter,
        )
        meta_raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = meta_raw_dir / (svg_path.stem + ".txt")
        if response:
            raw_path.write_text(response, encoding="utf-8")
        elif error:
            raw_path.write_text(error, encoding="utf-8")

        parsed = parse_json_from_text(response or "")
        if not parsed:
            retry_max_tokens = max_tokens
            retry_timeout = max(timeout * 2, timeout)
            response_retry, error_retry = call_vision_llm_with_retries(
                base_url,
                api_key,
                model,
                prompt_with_image,
                llm_items_doc,
                image_b64,
                retry_max_tokens,
                temperature,
                retry_timeout,
                retries,
                limiter,
            )
            raw_retry_path = meta_raw_dir / (svg_path.stem + "_retry.txt")
            if response_retry:
                raw_retry_path.write_text(response_retry, encoding="utf-8")
            elif error_retry:
                raw_retry_path.write_text(error_retry, encoding="utf-8")

            parsed = parse_json_from_text(response_retry or "")
            if not parsed:
                combined_error = error_retry or error or "invalid JSON after retry"
                return False, combined_error

        if not parsed:
            return False, error or "invalid JSON"

        plan = parsed or {}
        plan = expand_plan_item_ids(plan, llm_merged_map)
        plan = normalize_tree_plan(plan, [it["id"] for it in items if it.get("id")])
        plan = merge_adjacent_textboxes(plan, items)

    plan_path = meta_plans_dir / (svg_path.stem + ".json")
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    apply_plan_to_svg(tree, items, plan, pad, keep_ids)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    if visualize:
        viz_dir = output_path.parent / "visem_SVG"
        viz_path = viz_dir / output_path.name
        write_visualization_svg(tree, plan, items, viz_path, viz_labels)
    return True, None


def main() -> None:
    parser = argparse.ArgumentParser(description="Semantic grouping for SVG text (vision).")
    parser.add_argument("--input", required=True, help="Input directory with SVGs.")
    parser.add_argument("--output", required=True, help="Output directory for semantic SVGs.")
    parser.add_argument("--image-root", required=True, help="Root directory containing PPT screenshots.")
    parser.add_argument("--meta", default="", help="Metadata directory for items/plans (default: output/meta).")
    parser.add_argument("--config", default="config.json", help="Config json path.")
    parser.add_argument("--base-url", default="", help="OpenAI-compatible base URL.")
    parser.add_argument("--api-key", default="", help="API key.")
    parser.add_argument("--model", default="", help="Vision model name.")
    parser.add_argument("--max-tokens", type=int, default=None, help="Max tokens for LLM response.")
    parser.add_argument("--temperature", type=float, default=None, help="LLM temperature.")
    parser.add_argument("--timeout", type=int, default=None, help="Request timeout in seconds.")
    parser.add_argument("--workers", type=int, default=None, help="Parallel workers (LLM).")
    parser.add_argument("--qps", type=float, default=None, help="Global requests per second limit.")
    parser.add_argument("--retries", type=int, default=None, help="LLM retry count.")
    parser.add_argument("--pad", type=float, default=0.0, help="BBox padding.")
    parser.add_argument("--keep-ids", action="store_true", help="Keep data-extract-id in output SVGs.")
    parser.add_argument("--prompt", default="", help="Override LLM prompt.")
    parser.add_argument("--require-success", action="store_true", help="Exit non-zero if any failures occurred.")
    parser.add_argument("--image-width", type=int, default=TARGET_IMAGE_SIZE[0], help="Resize width.")
    parser.add_argument("--image-height", type=int, default=TARGET_IMAGE_SIZE[1], help="Resize height.")
    parser.add_argument("--no-viz", action="store_true", help="Disable SVG bbox visualization output.")
    parser.add_argument("--viz-labels", action="store_true", help="Draw labels on visualization layer.")
    parser.add_argument("--force", action="store_true", help="Reprocess all SVGs even if outputs exist.")
    args = parser.parse_args()

    if sync_playwright is None:
        raise SystemExit("playwright not installed. Run: pip install playwright && playwright install chromium")

    input_dir = Path(args.input)
    output_dir = Path(args.output)
    image_root = Path(args.image_root)
    meta_dir = Path(args.meta) if args.meta else output_dir / "meta"
    meta_items_dir = meta_dir / "items"
    meta_plans_dir = meta_dir / "plans"
    meta_raw_dir = meta_dir / "raw"
    image_size = (args.image_width, args.image_height)

    config = load_config(Path(args.config) if args.config else None)
    base_url = args.base_url or DEFAULT_BASE_URL
    if not base_url:
        base_url = (
            str(config.get("vlm_base_url", ""))
            or str(config.get("base_url", ""))
            or os.getenv("OPENAI_BASE_URL", "")
        )
    api_key = args.api_key or DEFAULT_API_KEY
    if not api_key:
        api_key = (
            str(config.get("vlm_api_key", ""))
            or str(config.get("api_key", ""))
            or os.getenv("OPENAI_API_KEY", "")
        )
    model = args.model or DEFAULT_MODEL
    if not model:
        model = (
            str(config.get("vlm_model", ""))
            or str(config.get("text_model", ""))
            or os.getenv("OPENAI_MODEL", "")
        )
    max_tokens = (
        args.max_tokens
        if args.max_tokens is not None
        else max(
            int(config.get("vlm_max_tokens", 0)),
            int(config.get("text_max_tokens", 1200)),
            2048,
        )
    )
    temperature = (
        args.temperature
        if args.temperature is not None
        else float(config.get("vlm_temperature", config.get("text_temperature", 0.2)))
    )
    timeout = (
        args.timeout
        if args.timeout is not None
        else max(
            int(config.get("vlm_timeout", 0) or 0),
            int(config.get("text_timeout", 60)),
            180,
        )
    )
    workers = (
        args.workers
        if args.workers is not None
        else int(config.get("vlm_workers", config.get("text_workers", 1)))
    )
    qps = (
        args.qps
        if args.qps is not None
        else float(config.get("vlm_qps", config.get("text_qps", 0.5)))
    )
    retries = (
        args.retries
        if args.retries is not None
        else max(int(config.get("vlm_retries", 0)), int(config.get("text_retries", 2)), 4)
    )

    prompt = args.prompt or build_prompt(ROLE_SET)

    svg_files = [p for p in input_dir.glob("*.SVG") if not p.name.startswith("._")]
    if not svg_files:
        print("No SVG files found.")
        return
    failed_tasks_path = meta_dir / FAILED_TASKS_NAME
    failed_prev: Dict[str, str] = {}
    if failed_tasks_path.exists():
        try:
            data = json.loads(failed_tasks_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and isinstance(item.get("svg"), str):
                        failed_prev[item["svg"]] = str(item.get("error") or "")
            elif isinstance(data, dict):
                failed_prev = {str(k): str(v) for k, v in data.items()}
        except Exception:
            failed_prev = {}

    failed_names = set(failed_prev.keys())
    if args.force:
        todo = sorted(svg_files)
    else:
        todo = []
        for svg_path in sorted(svg_files):
            out_svg = output_dir / svg_path.name
            if svg_path.name in failed_names or not out_svg.exists():
                todo.append(svg_path)
    if not todo:
        print("No SVG files need processing.")
        return

    limiter = RateLimiter(qps) if qps > 0 else None
    processed = 0
    total = len(todo)
    failures: Dict[str, str] = {}
    lock = threading.Lock()

    def worker(svg_path: Path) -> None:
        out_svg = output_dir / svg_path.name
        ok, err = process_svg(
            svg_path,
            out_svg,
            input_dir,
            image_root,
            image_size,
            meta_items_dir,
            meta_plans_dir,
            meta_raw_dir,
            base_url,
            api_key,
            model,
            max_tokens,
            temperature,
            timeout,
            retries,
            limiter,
            args.pad,
            args.keep_ids,
            prompt,
            not args.no_viz,
            args.viz_labels,
        )
        if not ok:
            with lock:
                failures[svg_path.name] = err or "unknown error"

    if workers <= 1:
        for svg_path in todo:
            worker(svg_path)
            processed += 1
            print(f"Semantic: {processed}/{total}")
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(worker, svg_path) for svg_path in todo]
            for future in as_completed(futures):
                future.result()
                processed += 1
                print(f"Semantic: {processed}/{total}")

    if failures:
        failed_data = [{"svg": name, "error": err} for name, err in failures.items()]
        failed_tasks_path.write_text(json.dumps(failed_data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Failed tasks written to {failed_tasks_path}")
        if args.require_success:
            raise SystemExit(1)
    else:
        if failed_tasks_path.exists():
            failed_tasks_path.unlink()


if __name__ == "__main__":
    main()
