#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generate images for placeholders and store them on disk.
"""

import argparse
import base64
import json
import os
import random
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from typing import Dict, Optional, Tuple, List

from PIL import Image

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

QWEN_IMAGE_SIZES = [
    (1328, 1328),
    (1664, 928),
    (928, 1664),
    (1472, 1140),
    (1140, 1472),
    (1584, 1056),
    (1056, 1584),
]
KOLOR_IMAGE_SIZES = [
    (1024, 1024),
    (960, 1280),
    (768, 1024),
    (720, 1440),
    (720, 1280),
]


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


def clean_caption(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"<\|/?(begin|end)_of_box\|>", "", text)
    cleaned = cleaned.replace("\n", " ").strip()
    return re.sub(r"\s+", " ", cleaned)


def coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "y", "1", "是", "对")
    return False


def is_chart_caption(text: str) -> bool:
    lowered = text.lower()
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
    return any(k in lowered for k in keywords)


def augment_prompt(prompt: str, is_chart: bool) -> str:
    if not is_chart:
        return prompt
    return (
        f"{prompt}. flat vector chart, transparent background, "
        "no shadow, no texture, clean edges"
    )


def pick_image_size(
    width: float,
    height: float,
    sizes: List[Tuple[int, int]],
) -> Tuple[str, Tuple[int, int]]:
    if width <= 0 or height <= 0:
        size = sizes[0]
        return f"{size[0]}x{size[1]}", size
    target_ratio = width / height
    best = min(
        sizes,
        key=lambda s: (abs((s[0] / s[1]) - target_ratio), abs(s[0] - width) + abs(s[1] - height)),
    )
    return f"{best[0]}x{best[1]}", best


def get_image_sizes(model: str) -> List[Tuple[int, int]]:
    model_lower = model.lower()
    if "kolor" in model_lower:
        return KOLOR_IMAGE_SIZES
    return QWEN_IMAGE_SIZES


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


def load_config(path: Optional[Path]) -> Dict[str, object]:
    if not path or not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_mapping(mapping_path: Path) -> Dict[Tuple[str, str], Dict[str, str]]:
    mapping: Dict[Tuple[str, str], Dict[str, str]] = {}
    if not mapping_path.exists():
        return mapping
    with mapping_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    for item in data:
        svg_file = item.get("svg_file")
        placeholder_id = item.get("placeholder_id")
        if svg_file and placeholder_id:
            mapping[(svg_file, placeholder_id)] = item
    return mapping


def png_has_alpha(data: bytes) -> Optional[bool]:
    if len(data) < 26 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    if data[12:16] != b"IHDR":
        return None
    color_type = data[25]
    if color_type in (4, 6):
        return True
    return b"tRNS" in data


def build_generation_tasks(
    mapping: Dict[Tuple[str, str], Dict[str, str]],
    model: str,
) -> List[Dict[str, object]]:
    sizes = get_image_sizes(model)
    tasks = []
    for (svg_file, placeholder_id), info in mapping.items():
        width = safe_float(info.get("width"), 0.0)
        height = safe_float(info.get("height"), 0.0)
        size_str, size_tuple = pick_image_size(width, height, sizes)
        prompt = clean_caption(info.get("caption", "")) or "图片"
        if "is_chart" in info:
            is_chart = coerce_bool(info.get("is_chart"))
        else:
            is_chart = is_chart_caption(prompt)
        has_alpha = str(info.get("has_alpha", "")).lower() == "true"
        image_path = info.get("image_path", "")
        if (not has_alpha) and image_path and Path(image_path).exists():
            try:
                raw = Path(image_path).read_bytes()
                alpha = png_has_alpha(raw)
                has_alpha = bool(alpha) if alpha is not None else False
            except Exception:
                has_alpha = False
        tasks.append(
            {
                "svg_file": svg_file,
                "placeholder_id": placeholder_id,
                "prompt": prompt,
                "is_chart": is_chart,
                "width": width,
                "height": height,
                "image_size": size_str,
                "image_size_tuple": size_tuple,
                "has_alpha": has_alpha,
                "image_path": image_path,
            }
        )
    return tasks


def call_image_generation(
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    image_size: str,
    timeout: int,
) -> Optional[bytes]:
    url = base_url.rstrip("/") + "/v1/images/generations"
    payload = {
        "model": model,
        "prompt": prompt,
        "image_size": image_size,
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
    except Exception:
        return None

    if isinstance(data, dict):
        items = data.get("data") or data.get("images") or []
        if items:
            item = items[0]
            if isinstance(item, dict):
                if "b64_json" in item:
                    return base64.b64decode(item["b64_json"])
                if "url" in item:
                    try:
                        with urllib.request.urlopen(item["url"], timeout=timeout) as resp:
                            return resp.read()
                    except Exception:
                        return None
            if isinstance(item, str):
                try:
                    return base64.b64decode(item)
                except Exception:
                    return None
    return None


def call_image_with_retries(
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    image_size: str,
    timeout: int,
    retries: int,
    limiter: Optional[RateLimiter],
) -> Optional[bytes]:
    for attempt in range(retries + 1):
        if limiter:
            limiter.acquire()
        data = call_image_generation(base_url, api_key, model, prompt, image_size, timeout)
        if data:
            return data
        if attempt < retries:
            backoff = min(2 ** attempt, 10) + random.random() * 0.3
            time.sleep(backoff)
    return None


def apply_alpha_mask(generated: bytes, mask_path: Path, size: Tuple[int, int]) -> bytes:
    gen_img = Image.open(BytesIO(generated)).convert("RGBA")
    gen_img = gen_img.resize(size, Image.LANCZOS)
    mask_img = Image.open(mask_path).convert("RGBA")
    mask = mask_img.split()[-1]
    mask = mask.resize(size, Image.LANCZOS)
    gen_img.putalpha(mask)
    out = BytesIO()
    gen_img.save(out, format="PNG")
    return out.getvalue()


def ensure_png(data: bytes, size: Tuple[int, int]) -> bytes:
    img = Image.open(BytesIO(data)).convert("RGBA")
    img = img.resize(size, Image.LANCZOS)
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def remove_solid_background(data: bytes, threshold: int = 20) -> bytes:
    img = Image.open(BytesIO(data)).convert("RGBA")
    w, h = img.size
    pixels = img.load()
    corners = [
        pixels[0, 0],
        pixels[w - 1, 0],
        pixels[0, h - 1],
        pixels[w - 1, h - 1],
    ]
    avg = tuple(sum(c[i] for c in corners) // 4 for i in range(3))
    for y in range(h):
        for x in range(w):
            r, g, b, a = pixels[x, y]
            if abs(r - avg[0]) <= threshold and abs(g - avg[1]) <= threshold and abs(b - avg[2]) <= threshold:
                pixels[x, y] = (r, g, b, 0)
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate images for placeholders.")
    parser.add_argument("--input", required=True, help="Placeholder directory.")
    parser.add_argument("--output", required=True, help="Output directory for generated images.")
    parser.add_argument("--mapping", default="", help="Mapping json path (default: input/image_placeholders.json).")
    parser.add_argument("--config", default="config.json", help="Optional config json path.")
    parser.add_argument("--base-url", default="", help="OpenAI-compatible base URL.")
    parser.add_argument("--api-key", default="", help="API key.")
    parser.add_argument("--model", default="", help="Image model name.")
    parser.add_argument("--timeout", type=int, default=None, help="Request timeout in seconds.")
    parser.add_argument("--workers", type=int, default=None, help="Parallel workers for image generation.")
    parser.add_argument("--qps", type=float, default=None, help="Global requests per second limit.")
    parser.add_argument("--retries", type=int, default=None, help="Retry count.")
    parser.add_argument("--force", action="store_true", help="Force regenerate images.")
    args = parser.parse_args()

    input_dir = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_dir = output_dir / "generated_images"
    generated_dir.mkdir(parents=True, exist_ok=True)

    mapping_path = Path(args.mapping) if args.mapping else input_dir / "image_placeholders.json"
    config = load_config(Path(args.config) if args.config else None)
    base_url = args.base_url or str(config.get("base_url", "")) or os.getenv("OPENAI_BASE_URL", "")
    api_key = args.api_key or str(config.get("api_key", "")) or os.getenv("OPENAI_API_KEY", "")
    model = args.model or str(config.get("model", "")) or os.getenv("OPENAI_MODEL", "")
    timeout = args.timeout if args.timeout is not None else int(config.get("timeout", 120))
    workers = args.workers if args.workers is not None else int(config.get("workers", 4))
    qps = args.qps if args.qps is not None else float(config.get("qps", 2.0))
    retries = args.retries if args.retries is not None else int(config.get("retries", 2))

    mapping = load_mapping(mapping_path)
    if not mapping:
        print("Mapping file not found or empty.")
        return

    tasks = build_generation_tasks(mapping, model)
    tasklist_path = output_dir / "generation_tasks.json"
    with tasklist_path.open("w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)
    print(f"Tasklist written: {tasklist_path}")
    print(f"Placeholders: {len(tasks)}")

    limiter = RateLimiter(qps) if qps > 0 else None
    results: Dict[Tuple[str, str], str] = {}
    lock = threading.Lock()
    total = len(tasks)
    done = 0

    def worker(task: Dict[str, object]) -> Tuple[Tuple[str, str], Optional[str]]:
        svg_file = str(task["svg_file"])
        placeholder_id = str(task["placeholder_id"])
        prompt = augment_prompt(str(task["prompt"]), bool(task.get("is_chart", False)))
        size_str = str(task["image_size"])
        size = tuple(task["image_size_tuple"])  # type: ignore
        out_name = f"{svg_file}_{placeholder_id}.png"
        out_path = generated_dir / out_name
        if out_path.exists() and not args.force:
            return (svg_file, placeholder_id), out_name
        data = call_image_with_retries(
            base_url,
            api_key,
            model,
            prompt,
            size_str,
            timeout,
            retries,
            limiter,
        )
        if not data:
            return (svg_file, placeholder_id), None
        has_alpha = bool(task["has_alpha"])
        image_path = str(task.get("image_path") or "")
        try:
            if has_alpha and image_path and Path(image_path).exists():
                data = apply_alpha_mask(data, Path(image_path), size)
            else:
                data = ensure_png(data, size)
                if bool(task.get("is_chart", False)):
                    data = remove_solid_background(data)
        except Exception:
            return (svg_file, placeholder_id), None
        out_path.write_bytes(data)
        return (svg_file, placeholder_id), out_name

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(worker, task) for task in tasks]
        for future in as_completed(futures):
            key, out_name = future.result()
            with lock:
                done += 1
                if out_name:
                    results[key] = out_name
                print(f"Generated: {done}/{total}")

    manifest = []
    for (svg_file, placeholder_id), out_name in results.items():
        manifest.append(
            {
                "svg_file": svg_file,
                "placeholder_id": placeholder_id,
                "image_file": out_name,
            }
        )
    manifest_path = output_dir / "generated_manifest.json"
    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"Manifest written: {manifest_path}")


if __name__ == "__main__":
    main()
