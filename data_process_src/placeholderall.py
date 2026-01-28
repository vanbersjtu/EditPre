#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
placeholderall.py

一键遍历输入根目录下的所有子文件夹，执行三步流程：
1) 提取图片并生成占位（由 svg_image_placeholder 负责）
2) 使用 VLM 为所有图片生成英文 caption（max_tokens 默认 512）
3) 基于 caption 进行图表检测，写出全局：
   - _global_cache/chart_cache.json
   - _global_cache/chart_captions.json

核心逻辑参考 `pipeline/svg_image_placeholder.py`，并增加：
- 全局批量 caption（跨目录去重）
- 目录级与全局级缓存（增量处理，避免重复生成）
"""

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Tuple

from pipeline.svg_image_placeholder import (
    collect_unique_images,
    generate_captions,
    process_svg,
    load_config,
)


def find_svg_dirs(root: Path) -> List[Path]:
    """查找 root 下所有“包含至少一个 .SVG 文件”的子目录（包括 root 本身），排除 `_global_cache`。"""
    svg_dirs: List[Path] = []

    if any(p.suffix == ".SVG" and not p.name.startswith("._") for p in root.glob("*.SVG")):
        svg_dirs.append(root)

    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        if sub.name == "_global_cache":
            continue
        if any(p.suffix == ".SVG" and not p.name.startswith("._") for p in sub.glob("*.SVG")):
            svg_dirs.append(sub)

    return svg_dirs


def load_json(path: Path) -> Dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_json(path: Path, data: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def run_for_dir_collect(
    svg_dir: Path,
    out_dir: Path,
    caption_cache: Dict[str, str],
    chart_cache: Dict[str, bool],
) -> Tuple[Path, Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]]:
    """
    阶段1：从一个子目录中收集所有图片，返回：
    - svg_dir
    - unique_images: 该目录中新出现的 unique image（按 hash 去重）的信息
    - image_cache: 该目录的完整 image_cache（source_key -> 信息）
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    extracted_dir = out_dir / "extracted_images"

    # 读取已有 image_cache（目录级增量）
    image_cache_path = out_dir / "image_cache.json"
    local_image_cache: Dict[str, Dict[str, str]] = load_json(image_cache_path)

    svg_files = [p for p in svg_dir.glob("*.SVG") if not p.name.startswith("._")]
    if not svg_files:
        return svg_dir, {}, local_image_cache

    unique_images = collect_unique_images(svg_files, extracted_dir, local_image_cache)
    return svg_dir, unique_images, local_image_cache


def run_for_dir_replace(
    svg_dir: Path,
    out_dir: Path,
    image_cache: Dict[str, Dict[str, str]],
    caption_cache: Dict[str, str],
    chart_cache: Dict[str, bool],
) -> None:
    """
    阶段3：占位 & 回填（replace），基于已经准备好的 caption_cache/chart_cache/image_cache。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    extracted_dir = out_dir / "extracted_images"

    mappings: List[Dict[str, str]] = []
    svg_files = [p for p in svg_dir.glob("*.SVG") if not p.name.startswith("._")]
    if not svg_files:
        return

    total_svgs = len(svg_files)
    processed_svgs = 0
    total_images = 0

    for svg in sorted(svg_files):
        out_svg = out_dir / svg.name
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
        total_images += max(0, len(mappings) - before_count)
        print(
            f"[placeholderall] Replace: {processed_svgs}/{total_svgs} SVGs, "
            f"{total_images} images (dir={svg_dir.name})"
        )

    # 写目录级映射与缓存，方便后续增量与其它脚本复用
    mapping_path = out_dir / "image_placeholders.json"
    save_json(mapping_path, mappings)

    image_cache_path = out_dir / "image_cache.json"
    save_json(image_cache_path, image_cache)

    # 写目录级 caption_cache / chart_cache（只包含本目录涉及到的 hash）
    used_hashes = {m["image_hash"] for m in mappings if m.get("image_hash")}
    local_caption_cache = {h: caption_cache[h] for h in used_hashes if h in caption_cache}
    local_chart_cache = {h: bool(chart_cache.get(h, False)) for h in used_hashes}

    save_json(out_dir / "caption_cache.json", local_caption_cache)
    save_json(out_dir / "chart_cache.json", local_chart_cache)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="全局占位 + caption + 图表检测（带增量），遍历所有子目录。"
    )
    parser.add_argument("--input", required=True, help="输入根目录，内部包含多个子目录，每个子目录里有 SVG。")
    parser.add_argument("--output", required=True, help="输出根目录，将按相同子目录结构写入占位符 SVG。")
    parser.add_argument("--config", default="config.json", help="传给 svg_image_placeholder 的 config 路径。")

    # 直接透传给 svg_image_placeholder 的 VLM 相关参数
    parser.add_argument("--base-url", default="", help="OpenAI-compatible base URL.")
    parser.add_argument("--api-key", default="", help="API key.")
    parser.add_argument("--model", default="", help="VLM model name.")
    parser.add_argument("--prompt", default="", help="自定义图片 caption 提示词（可选）。")
    parser.add_argument("--max-tokens", type=int, default=None, help="VLM 输出最大 token 数。")
    parser.add_argument("--temperature", type=float, default=None, help="VLM 采样温度。")
    parser.add_argument("--timeout", type=int, default=None, help="每次请求超时时间（秒）。")
    parser.add_argument("--sleep", type=float, default=None, help="每次请求后额外 sleep 秒数。")
    parser.add_argument("--workers", type=int, default=None, help="单目录内部并行的 VLM worker 数。")
    parser.add_argument("--qps", type=float, default=None, help="全局 QPS 限制。")
    parser.add_argument("--retries", type=int, default=None, help="失败重试次数。")

    parser.add_argument(
        "--folder-workers",
        type=int,
        default=4,
        help="按子目录并行的 worker 数（同时处理多少个子目录）。",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重新生成 caption 与图表判断（忽略已有全局缓存）。",
    )

    args = parser.parse_args()

    in_root = Path(args.input).expanduser().resolve()
    out_root = Path(args.output).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    svg_dirs = find_svg_dirs(in_root)
    if not svg_dirs:
        print("[placeholderall] No SVG files found under input root.")
        return

    print(f"[placeholderall] 将处理 {len(svg_dirs)} 个目录（包含 SVG）。")

    # 读取 config 与基础 VLM 配置（与 svg_image_placeholder 保持一致）
    config_path = Path(args.config) if args.config else None
    config = load_config(config_path if config_path and config_path.exists() else None)
    base_url = args.base_url or str(config.get("base_url", "")) or os.getenv("OPENAI_BASE_URL", "")
    api_key = args.api_key or str(config.get("api_key", "")) or os.getenv("OPENAI_API_KEY", "")
    model = args.model or str(config.get("vlm_model", "")) or os.getenv("OPENAI_MODEL", "")
    max_tokens = args.max_tokens if args.max_tokens is not None else int(config.get("vlm_max_tokens", 512))
    temperature = args.temperature if args.temperature is not None else float(config.get("vlm_temperature", 0.2))
    timeout = args.timeout if args.timeout is not None else int(config.get("vlm_timeout", 60))
    sleep_sec = args.sleep if args.sleep is not None else float(config.get("vlm_sleep", 0.0"))
    workers = args.workers if args.workers is not None else int(config.get("vlm_workers", 8))
    qps = args.qps if args.qps is not None else float(config.get("vlm_qps", 4.0))
    retries = args.retries if args.retries is not None else int(config.get("vlm_retries", 2))

    # 全局缓存目录
    global_cache_dir = in_root / "_global_cache"
    global_cache_dir.mkdir(parents=True, exist_ok=True)

    # 全局 caption_cache / chart_cache / chart_captions：用于跨目录增量与分析
    global_caption_path = global_cache_dir / "caption_cache.json"
    global_chart_path = global_cache_dir / "chart_cache.json"
    global_chart_captions_path = global_cache_dir / "chart_captions.json"

    if args.force:
        caption_cache_global: Dict[str, str] = {}
        chart_cache_global: Dict[str, bool] = {}
        chart_captions_global: Dict[str, Dict[str, object]] = {}
    else:
        caption_cache_global = load_json(global_caption_path)
        chart_cache_global = {k: bool(v) for k, v in load_json(global_chart_path).items()}
        chart_captions_global = load_json(global_chart_captions_path)

        # 兼容旧数据：从各子目录的 caption_cache.json / chart_cache.json 中补充
        for d in svg_dirs:
            local_caption = load_json(d / "caption_cache.json")
            for h, c in local_caption.items():
                caption_cache_global.setdefault(h, c)
            local_chart = load_json(d / "chart_cache.json")
            for h, v in local_chart.items():
                if h not in chart_cache_global:
                    chart_cache_global[h] = bool(v)

    # 阶段1：遍历各子目录，收集 unique_images 与目录级 image_cache
    all_unique_images: Dict[str, Dict[str, str]] = {}
    dir_image_caches: Dict[Path, Dict[str, Dict[str, str]]] = {}

    if args.folder_workers <= 1:
        results = [
            run_for_dir_collect(d, out_root / d.relative_to(in_root), caption_cache_global, chart_cache_global)
            for d in svg_dirs
        ]
    else:
        results: List[Tuple[Path, Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]]] = []
        with ThreadPoolExecutor(max_workers=args.folder_workers) as executor:
            futures = {
                executor.submit(
                    run_for_dir_collect,
                    d,
                    out_root / d.relative_to(in_root),
                    caption_cache_global,
                    chart_cache_global,
                ): d
                for d in svg_dirs
            }
            for fut in as_completed(futures):
                svg_dir, unique_images, image_cache = fut.result()
                results.append((svg_dir, unique_images, image_cache))

    for svg_dir, unique_images, image_cache in results:
        dir_image_caches[svg_dir] = image_cache
        for h, info in unique_images.items():
            if h not in caption_cache_global:
                all_unique_images.setdefault(h, info)

    print(f"[placeholderall] 全局待 caption 图片数（去重后）: {len(all_unique_images)}")

    # 阶段2：全局批量 caption + 初步图表判断
    if all_unique_images:
        prompt = args.prompt or (
            "请根据图片输出严格的 JSON："
            "{\"caption\":\"...\",\"is_chart\":true/false}。"
            "caption 用英文详尽描述画面，适合作为生图提示词。只输出 JSON。"
        )
        generate_captions(
            all_unique_images,
            caption_cache_global,
            chart_cache_global,
            base_url,
            api_key,
            model,
            prompt,
            max_tokens,
            temperature,
            timeout,
            retries,
            workers,
            qps,
            sleep_sec,
        )

    # 阶段3：为每个子目录执行占位替换（replace），并写目录级缓存
    def replace_worker(svg_dir: Path) -> None:
        rel = svg_dir.relative_to(in_root)
        out_dir = out_root / rel
        image_cache = dir_image_caches.get(svg_dir, {})
        print(f"[placeholderall] Replace 目录: {svg_dir} -> {out_dir}")
        run_for_dir_replace(svg_dir, out_dir, image_cache, caption_cache_global, chart_cache_global)

    if args.folder_workers <= 1:
        for d in svg_dirs:
            replace_worker(d)
    else:
        with ThreadPoolExecutor(max_workers=args.folder_workers) as executor:
            futures = {executor.submit(replace_worker, d): d for d in svg_dirs}
            for fut in as_completed(futures):
                d = futures[fut]
                try:
                    fut.result()
                except Exception as exc:
                    print(f"[placeholderall] 子目录 replace 失败: {d} -> {exc}")

    # 阶段4：根据全局 caption_cache / chart_cache 写出全局图表缓存
    for h, is_chart in chart_cache_global.items():
        if is_chart:
            if h not in chart_captions_global:
                chart_captions_global[h] = {
                    "hash": h,
                    "caption": caption_cache_global.get(h, ""),
                    "is_chart": True,
                }

    save_json(global_caption_path, caption_cache_global)
    save_json(global_chart_path, chart_cache_global)
    save_json(global_chart_captions_path, chart_captions_global)

    print(
        f"[placeholderall] 全局缓存已更新：caption={len(caption_cache_global)}, "
        f"charts={sum(1 for v in chart_cache_global.values() if v)}"
    )


if __name__ == "__main__":
    main()

