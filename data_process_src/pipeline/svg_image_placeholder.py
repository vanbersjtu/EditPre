#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Replace SVG <image> / <use> (image refs) with same-size placeholders and VLM captions.
"""

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import random
import re
import time
import threading
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Optional, Tuple, List

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"


def tag_name(elem: ET.Element) -> str:
    return elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag


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


def get_href(elem: ET.Element) -> str:
    return elem.get(f"{{{XLINK_NS}}}href", "") or elem.get("href", "")


def build_parent_map(root: ET.Element) -> Dict[ET.Element, ET.Element]:
    parent_map: Dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        for child in parent:
            parent_map[child] = parent
    return parent_map


def build_id_map(root: ET.Element) -> Dict[str, ET.Element]:
    id_map: Dict[str, ET.Element] = {}
    for elem in root.iter():
        elem_id = elem.get("id")
        if elem_id:
            id_map[elem_id] = elem
    return id_map


def is_in_defs(elem: ET.Element, parent_map: Dict[ET.Element, ET.Element]) -> bool:
    cur = elem
    while cur in parent_map:
        cur = parent_map[cur]
        if tag_name(cur) == "defs":
            return True
    return False


def parse_data_uri(href: str) -> Optional[Tuple[str, bytes]]:
    if not href.startswith("data:"):
        return None
    header, data = href.split(",", 1)
    mime = header[5:].split(";")[0] if ";" in header else header[5:]
    if ";base64" in header:
        try:
            return mime, base64.b64decode(data)
        except Exception:
            return None
    return None


def load_image_bytes(href: str, svg_dir: Path) -> Optional[Tuple[str, bytes, str]]:
    if href.startswith("data:"):
        parsed = parse_data_uri(href)
        if parsed:
            mime, data = parsed
            return mime, data, "data_uri"
        return None
    if href.startswith("http://") or href.startswith("https://"):
        try:
            with urllib.request.urlopen(href, timeout=30) as resp:
                data = resp.read()
                mime = resp.headers.get_content_type()
                return mime, data, "url"
        except Exception:
            return None
    path = (svg_dir / href).resolve()
    if path.exists():
        mime, _ = mimetypes.guess_type(str(path))
        try:
            data = path.read_bytes()
            return (mime or "application/octet-stream"), data, "file"
        except Exception:
            return None
    return None


def source_key_from_href(href: str, svg_dir: Path) -> str:
    if href.startswith("data:"):
        parsed = parse_data_uri(href)
        if parsed:
            _, data = parsed
            return "data:" + hashlib.sha1(data).hexdigest()
        return "data:invalid"
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return str((svg_dir / href).resolve())


def call_vlm(
    image_bytes: bytes,
    mime: str,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    timeout: int,
) -> Optional[str]:
    img_b64 = base64.b64encode(image_bytes).decode("utf-8")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are an image captioner."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{img_b64}"},
                    },
                ],
            },
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    url = base_url.rstrip("/") + "/v1/chat/completions"
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
    except Exception:
        return None
    try:
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def parse_json_from_text(text: str) -> Optional[Dict[str, object]]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n", "", cleaned).strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    match = re.search(r"\{.*\}", cleaned, re.S)
    if match:
        candidate = match.group(0)
        try:
            return json.loads(candidate)
        except Exception:
            pass
    try:
        return json.loads(cleaned)
    except Exception:
        return None


def coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "y", "1", "是", "对")
    return False


def parse_vlm_response(text: str) -> Tuple[str, bool]:
    if not text:
        return "图片占位", False
    data = parse_json_from_text(text)
    if isinstance(data, dict):
        caption = data.get("caption") or data.get("description") or data.get("prompt") or data.get("text") or ""
        caption = str(caption).strip()
        is_chart = coerce_bool(
            data.get("is_chart")
            if "is_chart" in data
            else data.get("isChart")
            if "isChart" in data
            else data.get("chart")
        )
        if not is_chart and isinstance(data.get("type"), str):
            type_val = str(data.get("type"))
            if "chart" in type_val.lower() or "图表" in type_val:
                is_chart = True
        if not is_chart and caption:
            if is_chart_caption(caption):
                is_chart = True
        if not caption:
            caption = text.strip()
        return caption, is_chart
    return text.strip(), is_chart_caption(text)


def load_config(path: Optional[Path]) -> Dict[str, object]:
    if not path or not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class RateLimiter:
    def __init__(self, qps: float):
        self.min_interval = 1.0 / qps if qps > 0 else 0.0
        self.lock = threading.Lock()
        self.next_time = time.monotonic()

    def acquire(self) -> None:
        if self.min_interval <= 0:
            return
        with self.lock:
            now = time.monotonic()
            if now < self.next_time:
                time.sleep(self.next_time - now)
            self.next_time = max(now, self.next_time) + self.min_interval


def call_vlm_with_retries(
    image_bytes: bytes,
    mime: str,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    timeout: int,
    retries: int,
    limiter: Optional[RateLimiter],
    post_sleep: float,
) -> Optional[str]:
    for attempt in range(retries + 1):
        if limiter:
            limiter.acquire()
        caption = call_vlm(
            image_bytes,
            mime,
            base_url,
            api_key,
            model,
            prompt,
            max_tokens,
            temperature,
            timeout,
        )
        if post_sleep:
            time.sleep(post_sleep)
        if caption:
            return caption
        if attempt < retries:
            backoff = min(2 ** attempt, 10) + random.random() * 0.3
            time.sleep(backoff)
    return None


def png_has_alpha(data: bytes) -> Optional[bool]:
    if len(data) < 26 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    if data[12:16] != b"IHDR":
        return None
    color_type = data[25]
    if color_type in (4, 6):
        return True
    # For indexed/truecolor/grayscale, transparency can be stored in tRNS chunk.
    return b"tRNS" in data


def compute_font_size(w: float, h: float) -> float:
    if w <= 0 or h <= 0:
        return 12.0
    return max(12.0, min(24.0, min(w, h) / 6.0))


def truncate_text(text: str, max_len: int = 60) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def is_chart_caption(text: str) -> bool:
    if not text:
        return False
    cleaned = re.sub(r"<\|/?(begin|end)_of_box\|>", "", text).lower()
    keywords = [
        "图表",
        "柱状",
        "折线",
        "饼图",
        "chart",
        "bar chart",
        "line chart",
        "pie chart",
        "diagram",
    ]
    return any(k in cleaned for k in keywords)


def build_placeholder(
    ns_prefix: str,
    x: float,
    y: float,
    w: float,
    h: float,
    caption: str,
    is_chart: bool,
    transform: Optional[str],
    extra_attrs: Dict[str, str],
    placeholder_id: str,
    has_alpha: bool,
) -> ET.Element:
    g = ET.Element(f"{ns_prefix}g")
    g.set("id", placeholder_id)
    g.set("data-role", "image-placeholder")
    g.set("data-caption", caption)
    if is_chart:
        g.set("data-is-chart", "true")
    if transform:
        g.set("transform", transform)
    for key in ("clip-path", "opacity", "filter", "mask"):
        if key in extra_attrs:
            g.set(key, extra_attrs[key])

    rect = ET.Element(f"{ns_prefix}rect")
    rect.set("x", format_num(x))
    rect.set("y", format_num(y))
    rect.set("width", format_num(w))
    rect.set("height", format_num(h))
    if has_alpha:
        rect.set("fill", "none")
    else:
        rect.set("fill", "#F0F0F0")
    rect.set("stroke", "#888888")
    rect.set("stroke-dasharray", "6 4")

    text = ET.Element(f"{ns_prefix}text")
    text.set("x", format_num(x + w / 2.0))
    text.set("y", format_num(y + h / 2.0))
    text.set("font-family", "sans-serif")
    text.set("font-size", format_num(compute_font_size(w, h)))
    text.set("fill", "#444444")
    text.set("text-anchor", "middle")
    text.set("dominant-baseline", "middle")
    text.text = truncate_text(caption)

    g.append(rect)
    g.append(text)
    return g


def resolve_use_size(use_elem: ET.Element, ref_elem: Optional[ET.Element]) -> Tuple[float, float, float, float]:
    x = safe_float(use_elem.get("x", 0))
    y = safe_float(use_elem.get("y", 0))
    w_attr = use_elem.get("width")
    h_attr = use_elem.get("height")
    w = safe_float(w_attr, 0)
    h = safe_float(h_attr, 0)
    needs_ref = (w == 0 or h == 0) or (w_attr and "%" in w_attr) or (h_attr and "%" in h_attr)
    if needs_ref and ref_elem is not None:
        ref_w = safe_float(ref_elem.get("width", 0))
        ref_h = safe_float(ref_elem.get("height", 0))
        if ref_w:
            w = ref_w
        if ref_h:
            h = ref_h
    if w == 0 or h == 0:
        w = w or 100.0
        h = h or 100.0
    return x, y, w, h


def resolve_image_size(img_elem: ET.Element) -> Tuple[float, float, float, float]:
    x = safe_float(img_elem.get("x", 0))
    y = safe_float(img_elem.get("y", 0))
    w = safe_float(img_elem.get("width", 0))
    h = safe_float(img_elem.get("height", 0))
    if w == 0 or h == 0:
        w = w or 100.0
        h = h or 100.0
    return x, y, w, h


def hash_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def collect_unique_images(
    svg_files: List[Path],
    extracted_dir: Path,
    image_cache: Dict[str, Dict[str, str]],
) -> Dict[str, Dict[str, str]]:
    unique_images: Dict[str, Dict[str, str]] = {}
    for svg in svg_files:
        try:
            tree = ET.parse(svg)
        except Exception:
            continue
        root = tree.getroot()
        parent_map = build_parent_map(root)
        id_map = build_id_map(root)
        svg_dir = svg.parent
        for elem in root.iter():
            tag = tag_name(elem)
            if tag == "image":
                if is_in_defs(elem, parent_map):
                    continue
                href = get_href(elem)
            elif tag == "use":
                href = get_href(elem)
                if not href.startswith("#"):
                    continue
                ref = id_map.get(href[1:])
                if ref is None or tag_name(ref) != "image":
                    continue
                href = get_href(ref)
            else:
                continue

            source_key = source_key_from_href(href, svg_dir)
            if source_key in image_cache:
                continue
            loaded = load_image_bytes(href, svg_dir)
            if loaded:
                mime, data, src_type = loaded
                has_alpha = False
                if mime == "image/png":
                    alpha = png_has_alpha(data)
                    has_alpha = bool(alpha) if alpha is not None else False
                img_hash = hash_bytes(data)
                ext = mimetypes.guess_extension(mime) or ".img"
                extracted_dir.mkdir(parents=True, exist_ok=True)
                img_path = extracted_dir / f"{img_hash}{ext}"
                if not img_path.exists():
                    img_path.write_bytes(data)
                image_cache[source_key] = {
                    "hash": img_hash,
                    "mime": mime,
                    "path": str(img_path),
                    "source_type": src_type,
                    "has_alpha": has_alpha,
                }
                if img_hash not in unique_images:
                    unique_images[img_hash] = {
                        "mime": mime,
                        "path": str(img_path),
                        "has_alpha": has_alpha,
                    }
            else:
                missing_hash = "missing:" + hashlib.sha1(source_key.encode("utf-8")).hexdigest()
                image_cache[source_key] = {
                    "hash": missing_hash,
                    "mime": "application/octet-stream",
                    "path": "",
                    "source_type": "unknown",
                    "has_alpha": False,
                }
    return unique_images


def generate_captions(
    unique_images: Dict[str, Dict[str, str]],
    caption_cache: Dict[str, str],
    chart_cache: Dict[str, bool],
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    timeout: int,
    retries: int,
    workers: int,
    qps: float,
    post_sleep: float,
) -> None:
    pending = [(h, info) for h, info in unique_images.items() if h not in caption_cache]
    total = len(pending)
    if total == 0:
        return
    if not base_url or not api_key or not model:
        for h, _ in pending:
            caption_cache[h] = "图片占位"
            chart_cache[h] = False
        return

    limiter = RateLimiter(qps) if qps > 0 else None
    lock = threading.Lock()
    done = 0

    def worker(item: Tuple[str, Dict[str, str]]) -> Tuple[str, str]:
        img_hash, info = item
        try:
            data = Path(info["path"]).read_bytes()
            caption = call_vlm_with_retries(
                data,
                info["mime"],
                base_url,
                api_key,
                model,
                prompt,
                max_tokens,
                temperature,
                timeout,
                retries,
                limiter,
                post_sleep,
            )
            if not caption:
                return img_hash, "图片占位"
            return img_hash, caption
        except Exception:
            return img_hash, "图片占位"

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(worker, item) for item in pending]
        for future in as_completed(futures):
            img_hash, caption = future.result()
            with lock:
                parsed_caption, is_chart = parse_vlm_response(caption)
                caption_cache[img_hash] = parsed_caption
                chart_cache[img_hash] = bool(is_chart)
                done += 1
                print(f"Captions: {done}/{total}")


def process_svg(
    svg_path: Path,
    output_path: Path,
    extracted_dir: Path,
    caption_cache: Dict[str, str],
    chart_cache: Dict[str, bool],
    image_cache: Dict[str, Dict[str, str]],
    mappings: List[Dict[str, str]],
) -> None:
    tree = ET.parse(svg_path)
    root = tree.getroot()
    ns_prefix = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""

    parent_map = build_parent_map(root)
    id_map = build_id_map(root)

    placeholders: List[Tuple[ET.Element, ET.Element]] = []
    to_remove: List[ET.Element] = []
    placeholder_index = 0
    svg_dir = svg_path.parent

    for elem in list(root.iter()):
        tag = tag_name(elem)
        if tag == "image":
            if is_in_defs(elem, parent_map):
                href = get_href(elem)
                if href.startswith("data:"):
                    to_remove.append(elem)
                continue
            ref_elem = None
        elif tag == "use":
            href = get_href(elem)
            if not href.startswith("#"):
                continue
            ref_elem = id_map.get(href[1:])
            if ref_elem is None or tag_name(ref_elem) != "image":
                continue
        else:
            continue

        if tag == "use":
            ref_href = get_href(ref_elem) if ref_elem is not None else ""
            href = ref_href
        else:
            href = get_href(elem)

        source_key = source_key_from_href(href, svg_dir)
        cache_entry = image_cache.get(source_key)
        if cache_entry is None:
            loaded = load_image_bytes(href, svg_dir)
            if loaded:
                mime, data, src_type = loaded
                has_alpha = False
                if mime == "image/png":
                    alpha = png_has_alpha(data)
                    has_alpha = bool(alpha) if alpha is not None else False
                img_hash = hash_bytes(data)
                ext = mimetypes.guess_extension(mime) or ".img"
                extracted_dir.mkdir(parents=True, exist_ok=True)
                img_path = extracted_dir / f"{img_hash}{ext}"
                if not img_path.exists():
                    img_path.write_bytes(data)
                cache_entry = {
                    "hash": img_hash,
                    "mime": mime,
                    "path": str(img_path),
                    "source_type": src_type,
                    "has_alpha": has_alpha,
                }
                image_cache[source_key] = cache_entry
                caption_cache.setdefault(img_hash, "图片占位")
                chart_cache.setdefault(img_hash, False)
            else:
                missing_hash = "missing:" + hashlib.sha1(source_key.encode("utf-8")).hexdigest()
                cache_entry = {
                    "hash": missing_hash,
                    "mime": "application/octet-stream",
                    "path": "",
                    "source_type": "unknown",
                    "has_alpha": False,
                }
                image_cache[source_key] = cache_entry
                caption_cache.setdefault(missing_hash, "图片占位")
                chart_cache.setdefault(missing_hash, False)

        img_hash = cache_entry.get("hash", "")
        caption = caption_cache.get(img_hash, "图片占位")
        is_chart = chart_cache.get(img_hash, is_chart_caption(caption))
        has_alpha = bool(cache_entry.get("has_alpha", False))
        transform = elem.get("transform")
        extra_attrs = dict(elem.attrib)
        placeholder_index += 1
        placeholder_id = f"image_placeholder_{placeholder_index}"

        if tag == "use":
            x, y, w, h = resolve_use_size(elem, ref_elem)
        else:
            x, y, w, h = resolve_image_size(elem)

        placeholder = build_placeholder(
            ns_prefix,
            x,
            y,
            w,
            h,
            caption,
            is_chart,
            transform,
            extra_attrs,
            placeholder_id,
            has_alpha,
        )

        placeholders.append((elem, placeholder))
        if tag == "use" and ref_elem is not None and get_href(ref_elem).startswith("data:"):
            to_remove.append(ref_elem)

        mappings.append(
            {
                "svg_file": svg_path.name,
                "placeholder_id": placeholder_id,
                "source_key": source_key,
                "image_hash": img_hash,
                "source_type": image_cache[source_key]["source_type"],
                "image_path": image_cache[source_key]["path"],
                "width": str(w),
                "height": str(h),
                "has_alpha": str(has_alpha),
                "caption": caption,
                "is_chart": bool(is_chart),
            }
        )

    for elem, placeholder in placeholders:
        parent = parent_map.get(elem)
        if parent is None:
            continue
        children = list(parent)
        try:
            index = children.index(elem)
        except ValueError:
            index = len(children)
        parent.remove(elem)
        parent.insert(index, placeholder)

    for elem in to_remove:
        parent = parent_map.get(elem)
        if parent is not None and elem in list(parent):
            parent.remove(elem)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)


def count_images(svg_path: Path) -> int:
    try:
        tree = ET.parse(svg_path)
    except Exception:
        return 0
    root = tree.getroot()
    parent_map = build_parent_map(root)
    id_map = build_id_map(root)
    count = 0
    for elem in root.iter():
        tag = tag_name(elem)
        if tag == "image":
            if not is_in_defs(elem, parent_map):
                count += 1
        elif tag == "use":
            href = get_href(elem)
            if href.startswith("#"):
                ref = id_map.get(href[1:])
                if ref is not None and tag_name(ref) == "image":
                    count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description="Replace SVG images with placeholders and VLM captions.")
    parser.add_argument("--input", required=True, help="Input directory containing SVGs.")
    parser.add_argument("--output", required=True, help="Output directory for placeholder SVGs.")
    parser.add_argument("--config", default="config.json", help="Optional config json path.")
    parser.add_argument("--base-url", default="", help="OpenAI-compatible base URL.")
    parser.add_argument("--api-key", default="", help="API key.")
    parser.add_argument("--model", default="", help="VLM model name.")
    parser.add_argument(
        "--prompt",
        default=(
            "请根据图片输出严格的 JSON："
            "{\"caption\":\"...\",\"is_chart\":true/false}。"
            "caption 用中文详尽描述画面，适合作为生图提示词。只输出 JSON。"
        ),
        help="Prompt for image captioning.",
    )
    parser.add_argument("--max-tokens", type=int, default=None, help="Max tokens for VLM response.")
    parser.add_argument("--temperature", type=float, default=None, help="VLM temperature.")
    parser.add_argument("--timeout", type=int, default=None, help="Request timeout in seconds.")
    parser.add_argument("--sleep", type=float, default=None, help="Sleep seconds after each VLM request.")
    parser.add_argument("--workers", type=int, default=None, help="Parallel VLM workers.")
    parser.add_argument("--qps", type=float, default=None, help="Global requests per second limit.")
    parser.add_argument("--retries", type=int, default=None, help="VLM retry count.")
    args = parser.parse_args()

    input_dir = Path(args.input)
    output_dir = Path(args.output)
    extracted_dir = output_dir / "extracted_images"
    mappings: List[Dict[str, str]] = []
    caption_cache: Dict[str, str] = {}
    chart_cache: Dict[str, bool] = {}
    image_cache: Dict[str, Dict[str, str]] = {}

    config = load_config(Path(args.config) if args.config else None)
    base_url = args.base_url or str(config.get("base_url", "")) or os.getenv("OPENAI_BASE_URL", "")
    api_key = args.api_key or str(config.get("api_key", "")) or os.getenv("OPENAI_API_KEY", "")
    model = args.model or str(config.get("vlm_model", "")) or os.getenv("OPENAI_MODEL", "")
    max_tokens = args.max_tokens if args.max_tokens is not None else int(config.get("vlm_max_tokens", 200))
    temperature = args.temperature if args.temperature is not None else float(config.get("vlm_temperature", 0.2))
    timeout = args.timeout if args.timeout is not None else int(config.get("vlm_timeout", 60))
    sleep_sec = args.sleep if args.sleep is not None else float(config.get("vlm_sleep", 0.0))
    workers = args.workers if args.workers is not None else int(config.get("vlm_workers", 6))
    qps = args.qps if args.qps is not None else float(config.get("vlm_qps", 2.0))
    retries = args.retries if args.retries is not None else int(config.get("vlm_retries", 2))

    svg_files = [p for p in input_dir.glob("*.SVG") if not p.name.startswith("._")]
    if not svg_files:
        print("No SVG files found.")
        return

    total_svgs = len(svg_files)
    total_images = sum(count_images(p) for p in svg_files)
    processed_svgs = 0
    processed_images = 0

    unique_images = collect_unique_images(svg_files, extracted_dir, image_cache)
    if unique_images:
        print(f"Unique images: {len(unique_images)}")
    generate_captions(
        unique_images,
        caption_cache,
        chart_cache,
        base_url,
        api_key,
        model,
        args.prompt,
        max_tokens,
        temperature,
        timeout,
        retries,
        workers,
        qps,
        sleep_sec,
    )

    for svg in sorted(svg_files):
        out_svg = output_dir / svg.name
        before_count = len(mappings)
        process_svg(
            svg,
            out_svg,
            extracted_dir,
            caption_cache,
            chart_cache,
            image_cache,
            mappings,
        )
        processed_svgs += 1
        processed_images += max(0, len(mappings) - before_count)
        progress = f"Progress: {processed_svgs}/{total_svgs} SVGs, {processed_images}/{total_images} images"
        print(progress)

    mapping_path = output_dir / "image_placeholders.json"
    with mapping_path.open("w", encoding="utf-8") as f:
        json.dump(mappings, f, ensure_ascii=False, indent=2)
    print(f"Mapping written: {mapping_path}")


if __name__ == "__main__":
    main()
