#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

"""
Generate images for SVG image placeholders using Qwen/Qwen-Image-2512.

Workflow:
1) Scan SVGs for <g data-type="image-placeholder" ...> (or data-role variant)
2) Read data-caption as prompt
3) Generate image with Qwen-Image-2512 (Diffusers)
4) Save generated image files
5) Export image_placeholders.json for svg_to_pptx_pro.py --placeholders
"""

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

try:
    import torch
    from diffusers import DiffusionPipeline
except Exception:  # noqa: BLE001
    torch = None
    DiffusionPipeline = None


SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

# Recommended output sizes from the model card.
SIZE_PRESETS: List[Tuple[int, int]] = [
    (1328, 1328),  # 1:1
    (1664, 928),   # 16:9
    (1472, 1140),  # 4:3
    (1140, 1472),  # 3:4
    (928, 1664),   # 9:16
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate placeholder images from SVG captions with Qwen-Image-2512."
    )
    parser.add_argument(
        "--svg-input",
        default="/Users/xiaoxiaobo/Downloads/sjtuwenber/gemini_pipeline/output/svg",
        help="Input SVG file or directory.",
    )
    parser.add_argument(
        "--output-image-dir",
        default="/Users/xiaoxiaobo/Downloads/sjtuwenber/gemini_pipeline/output/generated_images",
        help="Directory to save generated images.",
    )
    parser.add_argument(
        "--placeholders-json",
        default="/Users/xiaoxiaobo/Downloads/sjtuwenber/gemini_pipeline/output/image_placeholders.json",
        help="Output mapping JSON for svg_to_pptx_pro.py --placeholders.",
    )
    parser.add_argument(
        "--model-id",
        default="Qwen/Qwen-Image-2512",
        help="Diffusers model id (default: Qwen/Qwen-Image-2512).",
    )
    parser.add_argument(
        "--device",
        default="auto",
        choices=["auto", "cuda", "cpu", "mps"],
        help="Inference device.",
    )
    parser.add_argument(
        "--dtype",
        default="auto",
        choices=["auto", "float16", "bfloat16", "float32"],
        help="Torch dtype.",
    )
    parser.add_argument(
        "--num-inference-steps",
        type=int,
        default=50,
        help="Diffusion steps.",
    )
    parser.add_argument(
        "--true-cfg-scale",
        type=float,
        default=4.0,
        help="true_cfg_scale for Qwen image pipeline.",
    )
    parser.add_argument(
        "--negative-prompt",
        default="",
        help="Negative prompt.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=2026,
        help="Random seed base (seed + index).",
    )
    parser.add_argument(
        "--max-placeholders",
        type=int,
        default=0,
        help="Max placeholders to process (0 means all).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing generated images.",
    )
    parser.add_argument(
        "--include-charts",
        action="store_true",
        help="Also generate images for data-is-chart=true placeholders.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only parse placeholders and write JSON, do not load model or generate images.",
    )
    parser.add_argument(
        "--hf-token",
        default=os.environ.get("HF_TOKEN", ""),
        help="Hugging Face token (optional, can also use HF_TOKEN env).",
    )
    return parser.parse_args()


def tag_name(elem: ET.Element) -> str:
    return elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag


def parse_length(val: Optional[str], default: float = 0.0) -> float:
    if not val:
        return default
    raw = val.strip()
    for suffix in ("px", "pt", "mm", "cm", "in", "em", "ex"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
            break
    try:
        return float(raw)
    except Exception:
        return default


def natural_sort_key(path: Path) -> List[Any]:
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", path.name)]


def iter_svg_paths(svg_input: Path) -> List[Path]:
    if svg_input.is_file() and svg_input.suffix.lower() == ".svg":
        return [svg_input]
    if svg_input.is_dir():
        paths = [
            p for p in svg_input.rglob("*")
            if p.is_file() and p.suffix.lower() == ".svg" and not p.name.startswith("._")
        ]
        return sorted(paths, key=lambda p: natural_sort_key(p))
    return []


def extract_placeholder_size(elem: ET.Element) -> Tuple[float, float]:
    for child in list(elem):
        t = tag_name(child)
        if t in ("rect", "image"):
            w = parse_length(child.get("width"), 0.0)
            h = parse_length(child.get("height"), 0.0)
            if w > 0 and h > 0:
                return w, h
        if t == "circle":
            r = parse_length(child.get("r"), 0.0)
            if r > 0:
                return 2 * r, 2 * r
        if t == "ellipse":
            rx = parse_length(child.get("rx"), 0.0)
            ry = parse_length(child.get("ry"), 0.0)
            if rx > 0 and ry > 0:
                return 2 * rx, 2 * ry
    return 1328.0, 1328.0


def choose_size(src_w: float, src_h: float) -> Tuple[int, int]:
    if src_w <= 0 or src_h <= 0:
        return SIZE_PRESETS[0]
    ratio = src_w / src_h
    return min(SIZE_PRESETS, key=lambda wh: abs((wh[0] / wh[1]) - ratio))


def sanitize_name(text: str, fallback: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", text.strip())
    s = s.strip("._")
    return s or fallback


def parse_svg_placeholders(svg_path: Path) -> List[Dict[str, Any]]:
    tree = ET.parse(svg_path)
    root = tree.getroot()
    placeholders: List[Dict[str, Any]] = []
    counter = 0

    for elem in root.iter():
        if tag_name(elem) != "g":
            continue
        role = elem.get("data-role")
        typ = elem.get("data-type")
        if role != "image-placeholder" and typ != "image-placeholder":
            continue

        counter += 1
        placeholder_id = elem.get("id", "").strip() or f"placeholder-{counter}"
        caption = (elem.get("data-caption") or "").strip()
        is_chart = str(elem.get("data-is-chart", "false")).strip().lower() == "true"
        w, h = extract_placeholder_size(elem)

        placeholders.append(
            {
                "placeholder_id": placeholder_id,
                "caption": caption,
                "is_chart": is_chart,
                "src_w": w,
                "src_h": h,
            }
        )
    return placeholders


def resolve_device(device: str) -> str:
    if torch is None:
        return "cpu"
    if device != "auto":
        return device
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def resolve_dtype(dtype: str, device: str) -> torch.dtype:
    if torch is None:
        raise RuntimeError(
            "Missing dependencies. Please install: pip install diffusers torch accelerate transformers sentencepiece"
        )
    if dtype == "float16":
        return torch.float16
    if dtype == "bfloat16":
        return torch.bfloat16
    if dtype == "float32":
        return torch.float32
    if device == "cuda":
        return torch.bfloat16
    return torch.float32


def load_pipeline(model_id: str, device: str, dtype: torch.dtype, hf_token: str) -> DiffusionPipeline:
    if DiffusionPipeline is None:
        raise RuntimeError(
            "Missing dependencies. Please install: pip install diffusers torch accelerate transformers sentencepiece"
        )
    kwargs: Dict[str, Any] = {"torch_dtype": dtype}
    if hf_token:
        kwargs["token"] = hf_token
    pipe = DiffusionPipeline.from_pretrained(model_id, **kwargs)
    if device == "cuda":
        pipe.to("cuda")
    elif device == "mps":
        pipe.to("mps")
    else:
        pipe.to("cpu")
    return pipe


def generate_one(
    pipe: Any,
    prompt: str,
    width: int,
    height: int,
    num_inference_steps: int,
    true_cfg_scale: float,
    negative_prompt: str,
    seed: int,
    device: str,
):
    generator = torch.Generator(device=device if device in ("cuda", "cpu") else "cpu").manual_seed(seed)
    result = pipe(
        prompt=prompt,
        negative_prompt=negative_prompt if negative_prompt else None,
        width=width,
        height=height,
        num_inference_steps=num_inference_steps,
        true_cfg_scale=true_cfg_scale,
        generator=generator,
    )
    return result.images[0]


def main() -> None:
    args = parse_args()

    svg_input = Path(args.svg_input).expanduser().resolve()
    output_image_dir = Path(args.output_image_dir).expanduser().resolve()
    placeholders_json = Path(args.placeholders_json).expanduser().resolve()

    svg_paths = iter_svg_paths(svg_input)
    if not svg_paths:
        raise FileNotFoundError(f"No SVG files found: {svg_input}")

    if args.dry_run:
        device = "cpu"
        pipe = None
        print("Dry-run mode: skip model loading and image generation.")
    else:
        device = resolve_device(args.device)
        dtype = resolve_dtype(args.dtype, device)
        print(f"Loading model {args.model_id} on {device} ({dtype})")
        pipe = load_pipeline(args.model_id, device, dtype, args.hf_token)

    output_image_dir.mkdir(parents=True, exist_ok=True)
    placeholders_json.parent.mkdir(parents=True, exist_ok=True)

    records: List[Dict[str, Any]] = []
    generated = 0
    skipped = 0

    max_ph = max(0, int(args.max_placeholders))
    processed = 0

    for svg_path in svg_paths:
        rel_svg = svg_path.relative_to(svg_input) if svg_input.is_dir() else Path(svg_path.name)
        placeholders = parse_svg_placeholders(svg_path)
        if not placeholders:
            continue

        print(f"\n[{svg_path.name}] placeholders={len(placeholders)}")
        for idx, ph in enumerate(placeholders, start=1):
            if max_ph and processed >= max_ph:
                break
            processed += 1

            placeholder_id = ph["placeholder_id"]
            caption = ph["caption"]
            is_chart = ph["is_chart"]
            src_w = float(ph["src_w"])
            src_h = float(ph["src_h"])
            w, h = choose_size(src_w, src_h)

            record: Dict[str, Any] = {
                "svg_file": svg_path.name,
                "svg_path": str(svg_path.resolve()),
                "svg_relpath": str(rel_svg),
                "placeholder_id": placeholder_id,
                "caption": caption,
                "is_chart": is_chart,
            }

            if is_chart and not args.include_charts:
                records.append(record)
                skipped += 1
                print(f"  - skip chart: {placeholder_id}")
                continue

            if not caption:
                records.append(record)
                skipped += 1
                print(f"  - skip empty caption: {placeholder_id}")
                continue

            safe_svg_name = sanitize_name(svg_path.stem, "slide")
            safe_ph_id = sanitize_name(placeholder_id, f"ph_{idx}")
            image_dir = output_image_dir / rel_svg.parent / safe_svg_name
            image_dir.mkdir(parents=True, exist_ok=True)
            image_path = image_dir / f"{safe_ph_id}.png"

            if image_path.exists() and not args.overwrite:
                record["image_path"] = str(image_path)
                records.append(record)
                skipped += 1
                print(f"  - reuse: {placeholder_id} -> {image_path}")
                continue

            if args.dry_run:
                record["image_path"] = str(image_path)
                records.append(record)
                skipped += 1
                print(f"  - dry-run: {placeholder_id} -> {image_path}")
                continue

            print(f"  - gen: {placeholder_id} ({w}x{h})")
            image = generate_one(
                pipe=pipe,
                prompt=caption,
                width=w,
                height=h,
                num_inference_steps=args.num_inference_steps,
                true_cfg_scale=args.true_cfg_scale,
                negative_prompt=args.negative_prompt,
                seed=args.seed + processed,
                device=device,
            )
            image.save(image_path)

            record["image_path"] = str(image_path)
            records.append(record)
            generated += 1

        if max_ph and processed >= max_ph:
            break

    with open(placeholders_json, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    print("\nDone.")
    print(f"Generated: {generated}")
    print(f"Skipped: {skipped}")
    print(f"Placeholders JSON: {placeholders_json}")


if __name__ == "__main__":
    main()
