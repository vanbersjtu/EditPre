#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
placeholderall.py

一键遍历输入根目录下的所有子文件夹，执行三步流程：
1) 生成初始占位（不触发 VLM，仅保留尺寸/位置）
2) 全局批量 caption：VLM 仅输出英文 caption（max_tokens=512），英文关键词筛可疑图表，
   再二次 VLM 判定是否为纯图表，并写入全局缓存
3) Replace 阶段：将 caption/is_chart 回填到 SVG，占位大小保持与原图一致

全局输出：
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
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Tuple, Optional

from pipeline.svg_image_placeholder import (
    collect_unique_images,
    generate_captions,
    process_svg,
    load_config,
    RateLimiter,
    call_vlm_with_retries,
    parse_json_from_text,
    coerce_bool,
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


def extract_caption(text: str) -> str:
    if not text:
        return "图片占位"
    data = parse_json_from_text(text)
    if isinstance(data, dict):
        caption = (
            data.get("caption")
            or data.get("description")
            or data.get("prompt")
            or data.get("text")
            or ""
        )
        caption = str(caption).strip()
        if caption:
            return caption
    cleaned = str(text).strip()
    return cleaned if cleaned else "图片占位"


def extract_is_chart(text: str) -> bool:
    if not text:
        return False
    data = parse_json_from_text(text)
    if isinstance(data, dict):
        return coerce_bool(
            data.get("is_chart")
            if "is_chart" in data
            else data.get("isChart")
            if "isChart" in data
            else data.get("chart")
        )
    return False


def apply_image_token(prompt: str, image_token: str) -> str:
    token = (image_token or "").strip()
    if not token:
        return prompt
    return f"{token}\n{prompt}"


@lru_cache(maxsize=4)
def _load_vllm_engine(
    model_source: str,
    max_model_len: Optional[int],
    max_batched_tokens: Optional[int],
    tensor_parallel_size: Optional[int],
    enable_chunked_prefill: bool,
):
    from vllm import LLM
    from transformers import AutoProcessor

    engine_kwargs: Dict[str, object] = {
        "model": model_source,
        "trust_remote_code": True,
        "enforce_eager": True,          # 跳过 torch.compile，避免 collective_fusion 导入 bug
        "disable_log_stats": True,
    }
    if enable_chunked_prefill:
        engine_kwargs["enable_chunked_prefill"] = True
    if max_model_len:
        engine_kwargs["max_model_len"] = max_model_len
    if max_batched_tokens:
        engine_kwargs["max_num_batched_tokens"] = max_batched_tokens
    if tensor_parallel_size:
        engine_kwargs["tensor_parallel_size"] = tensor_parallel_size

    llm = LLM(**engine_kwargs)
    processor = AutoProcessor.from_pretrained(model_source, trust_remote_code=True)
    return llm, processor


def run_vllm_batch(
    items: List[Dict[str, str]],
    model_source: str,
    temperature: float,
    max_tokens: int,
    batch_size: int,
    concurrency: int,
    max_model_len: Optional[int],
    max_batched_tokens: Optional[int],
    tensor_parallel_size: Optional[int],
    enable_chunked_prefill: bool,
    ray_address: str,
    ray_log_to_driver: bool,
    progress_label: str = "",
) -> List[Dict[str, object]]:
    if not items:
        return []

    try:
        from vllm import SamplingParams
        from qwen_vl_utils import process_vision_info
        from PIL import Image
    except Exception as exc:
        raise RuntimeError("vLLM + qwen_vl_utils + pillow are required for batch inference.") from exc

    llm, processor = _load_vllm_engine(
        model_source,
        max_model_len,
        max_batched_tokens,
        tensor_parallel_size,
        enable_chunked_prefill,
    )
    patch_size = processor.image_processor.patch_size

    sampling_params = SamplingParams(temperature=temperature, max_tokens=max_tokens)

    outputs: List[Dict[str, object]] = []
    step = max(1, batch_size)
    total_items = len(items)
    for start in range(0, total_items, step):
        batch = items[start : start + step]
        inputs: List[Dict[str, object]] = []
        hashes: List[str] = []

        for row in batch:
            image_path = str(row.get("image_path", ""))
            prompt = str(row.get("prompt", ""))
            if image_path:
                image_item: Dict[str, object] = {"type": "image", "image": image_path}
                resized_height: Optional[int] = None
                resized_width: Optional[int] = None
                image_obj = None
                try:
                    with Image.open(image_path) as img:
                        width, height = img.size
                        max_edge = max(width, height)
                        if max_edge > 0:
                            scale = min(1.0, 1024 / max_edge)
                            resized_width = max(1, int(round(width * scale)))
                            resized_height = max(1, int(round(height * scale)))
                        image_obj = img.convert("RGB")
                except Exception:
                    resized_height = None
                    resized_width = None
                    image_obj = None

                if image_obj is not None:
                    image_item["image"] = image_obj
                if resized_height and resized_width:
                    image_item["resized_height"] = resized_height
                    image_item["resized_width"] = resized_width

                content = [
                    image_item,
                    {"type": "text", "text": prompt},
                ]
            else:
                content = [{"type": "text", "text": prompt}]

            messages = [{"role": "user", "content": content}]
            text_prompt = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            image_inputs, video_inputs, video_kwargs = process_vision_info(
                messages,
                image_patch_size=patch_size,
                return_video_kwargs=True,
                return_video_metadata=True,
            )

            mm_data: Dict[str, object] = {}
            if image_inputs is not None:
                mm_data["image"] = image_inputs
            if video_inputs is not None:
                mm_data["video"] = video_inputs

            payload: Dict[str, object] = {
                "prompt": text_prompt,
                "multi_modal_data": mm_data,
            }
            if video_kwargs:
                payload["mm_processor_kwargs"] = video_kwargs

            inputs.append(payload)
            hashes.append(str(row.get("image_hash", "")))

        results = llm.generate(inputs, sampling_params=sampling_params)
        for h, result in zip(hashes, results):
            text = ""
            outputs_list = getattr(result, "outputs", None)
            if outputs_list:
                text = getattr(outputs_list[0], "text", "") or ""
            outputs.append({"image_hash": h, "generated_text": text})
        if progress_label:
            processed = min(start + step, total_items)
            pct = (processed / total_items * 100.0) if total_items else 100.0
            print(
                f"[placeholderall] {progress_label}: {processed}/{total_items} ({pct:.1f}%)"
            )

    return outputs


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
    stage_label: str = "Replace",
) -> None:
    """
    占位/回填阶段：基于已经准备好的 caption_cache/chart_cache/image_cache。
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

    stage_label = stage_label or "Replace"

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
            f"[placeholderall] {stage_label}: {processed_svgs}/{total_svgs} SVGs, "
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


def is_suspicious_chart_caption(text: str) -> bool:
    """根据 caption 关键词判断是否为“可疑图表”，仅用于筛选需要二次 VLM 判别的图片。"""
    if not text:
        return False
    lowered = text.lower()
    keywords = [
        "chart",
        "bar chart",
        "line chart",
        "pie chart",
        "donut chart",
        "doughnut chart",
        "ring chart",
        "area chart",
        "stacked bar",
        "stacked area",
        "histogram",
        "scatter plot",
        "scatter chart",
        "bubble chart",
        "heatmap",
        "heat map",
        "timeline",
        "time series",
        "graph",
        "x-axis",
        "y-axis",
        "x axis",
        "y axis",
        "horizontal axis",
        "vertical axis",
        "data points",
        "trend line",
    ]
    return any(k in lowered for k in keywords)


def detect_charts_with_vlm(
    hashes: List[str],
    hash_to_image: Dict[str, Dict[str, str]],
    base_url: str,
    api_key: str,
    model: str,
    timeout: int,
    retries: int,
    qps: float,
    sleep_sec: float,
    workers: int,
) -> Dict[str, bool]:
    """
    使用 VLM 对可疑图片进行“是否为纯图表”的二次判断。
    返回 hash -> is_chart 布尔值（只覆盖传入的 hashes）。
    """
    if not base_url or not api_key or not model:
        # 无 VLM 配置时，不做二次判定
        return {}

    pending = [h for h in hashes if h in hash_to_image]
    total = len(pending)
    if total == 0:
        return {}

    limiter = RateLimiter(qps) if qps > 0 else None
    result: Dict[str, bool] = {}

    def worker(h: str) -> Tuple[str, bool]:
        info = hash_to_image.get(h) or {}
        img_path = info.get("path")
        mime = info.get("mime", "image/png")
        if not img_path:
            return h, False
        try:
            data = Path(img_path).read_bytes()
        except Exception:
            return h, False

        # 专门的“图表判断”提示词：只关心 is_chart
        prompt = (
            "你是图像类型判定助手。请仅根据图片内容判断这是否是“纯数据可视化图表”，"
            "例如：柱状图、折线图、饼图、散点图、面积图、雷达图、直方图等。"
            "如果是纯图表（主要内容是数据可视化），请输出严格 JSON："
            '{"is_chart": true}；否则输出 {"is_chart": false}。'
            "只输出 JSON，不要输出任何多余文字。"
        )

        resp = call_vlm_with_retries(
            data,
            mime,
            base_url,
            api_key,
            model,
            prompt,
            # 判定只需要很短的输出
            max_tokens=64,
            temperature=0.0,
            timeout=timeout,
            retries=retries,
            limiter=limiter,
            post_sleep=sleep_sec,
        )
        if not resp:
            return h, False
        parsed = parse_json_from_text(resp)
        if isinstance(parsed, dict):
            flag = coerce_bool(
                parsed.get("is_chart")
                if "is_chart" in parsed
                else parsed.get("isChart")
                if "isChart" in parsed
                else parsed.get("chart")
            )
            return h, bool(flag)
        # 解析失败则保守认为不是图表
        return h, False

    if workers <= 1:
        for idx, h in enumerate(pending, start=1):
            hh, is_chart = worker(h)
            result[hh] = is_chart
            print(f"[placeholderall] Chart VLM: {idx}/{total}")
    else:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = [executor.submit(worker, h) for h in pending]
            done = 0
            for fut in as_completed(futures):
                hh, is_chart = fut.result()
                result[hh] = is_chart
                done += 1
                print(f"[placeholderall] Chart VLM: {done}/{total}")

    return result


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
        "--vlm-backend",
        choices=["api", "vllm-batch"],
        default="api",
        help="VLM 调用后端（api 或 vllm-batch）。",
    )
    parser.add_argument("--vllm-model", default="", help="vLLM batch 模式模型路径（默认沿用 --model）。")
    parser.add_argument("--vllm-batch-size", type=int, default=64, help="vLLM batch 推理 batch size。")
    parser.add_argument("--vllm-concurrency", type=int, default=1, help="vLLM batch 并行副本数。")
    parser.add_argument(
        "--vllm-tensor-parallel-size",
        type=int,
        default=None,
        help="vLLM tensor parallel size（多卡模型并行）。",
    )
    parser.add_argument("--vllm-max-model-len", type=int, default=None, help="vLLM max_model_len。")
    parser.add_argument("--vllm-max-batched-tokens", type=int, default=None, help="vLLM max_num_batched_tokens。")
    parser.add_argument("--vllm-enable-chunked-prefill", action="store_true", help="启用 vLLM chunked prefill。")
    parser.add_argument("--ray-address", default="", help="Ray 集群地址（如 auto）。")
    parser.add_argument("--ray-log-to-driver", action="store_true", help="Ray 日志输出到 driver。")
    parser.add_argument(
        "--image-token",
        default="<|image_1|>",
        help="多模态 prompt 的图像占位 token（仅 API 后端使用，vllm-batch 会忽略）。",
    )

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
    parser.add_argument(
        "--stage",
        nargs="+",
        choices=["placeholder", "caption", "chart", "all"],
        default=["placeholder", "caption", "chart"],
        help="仅运行指定阶段（placeholder/caption/chart），默认全部。",
    )

    args = parser.parse_args()

    stage_tokens = args.stage or ["placeholder", "caption", "chart"]
    if "all" in stage_tokens:
        selected_stages = ["placeholder", "caption", "chart"]
    else:
        order = ["placeholder", "caption", "chart"]
        selected_stages = [name for name in order if name in stage_tokens]
    if not selected_stages:
        selected_stages = ["placeholder", "caption", "chart"]
    run_placeholder = "placeholder" in selected_stages
    run_caption = "caption" in selected_stages
    run_chart = "chart" in selected_stages
    print(f"[placeholderall] 运行阶段: {', '.join(selected_stages)}")

    in_root = Path(args.input).expanduser().resolve()
    out_root = Path(args.output).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    svg_dirs = find_svg_dirs(in_root)
    if not svg_dirs:
        print("[placeholderall] No SVG files found under input root.")
        return

    print(f"[placeholderall] 将处理 {len(svg_dirs)} 个目录（包含 SVG）。")

    def log_progress(label: str, done: int, total: int) -> None:
        if total <= 0:
            pct = 100.0
        else:
            pct = done / total * 100.0
        print(f"[placeholderall] {label} 进度: {done}/{total} ({pct:.1f}%)")

    # 读取 config 与基础 VLM 配置（与 svg_image_placeholder 保持一致）
    config_path = Path(args.config) if args.config else None
    config = load_config(config_path if config_path and config_path.exists() else None)
    base_url = args.base_url or str(config.get("base_url", "")) or os.getenv("OPENAI_BASE_URL", "")
    api_key = args.api_key or str(config.get("api_key", "")) or os.getenv("OPENAI_API_KEY", "")
    api_model = args.model or str(config.get("vlm_model", "")) or os.getenv("OPENAI_MODEL", "")
    vllm_model = args.vllm_model or api_model
    vlm_backend = args.vlm_backend
    caption_max_tokens = args.max_tokens if args.max_tokens is not None else 512
    temperature = args.temperature if args.temperature is not None else float(config.get("vlm_temperature", 0.2))
    timeout = args.timeout if args.timeout is not None else int(config.get("vlm_timeout", 60))
    sleep_sec = args.sleep if args.sleep is not None else float(config.get("vlm_sleep", 0.0))
    workers = args.workers if args.workers is not None else int(config.get("vlm_workers", 8))
    qps = args.qps if args.qps is not None else float(config.get("vlm_qps", 4.0))
    retries = args.retries if args.retries is not None else int(config.get("vlm_retries", 2))

    if vlm_backend == "vllm-batch" and not vllm_model:
        raise SystemExit("[placeholderall] vllm-batch 需要提供 --vllm-model 或 --model。")

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

    # 预处理：遍历各子目录，收集 unique_images 与目录级 image_cache
    all_unique_images: Dict[str, Dict[str, str]] = {}
    dir_image_caches: Dict[Path, Dict[str, Dict[str, str]]] = {}

    results: List[Tuple[Path, Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]]] = []
    total_dirs = len(svg_dirs)
    if args.folder_workers <= 1:
        for idx, d in enumerate(svg_dirs, start=1):
            res = run_for_dir_collect(d, out_root / d.relative_to(in_root), caption_cache_global, chart_cache_global)
            results.append(res)
            log_progress("Collect", idx, total_dirs)
    else:
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
            done = 0
            for fut in as_completed(futures):
                svg_dir, unique_images, image_cache = fut.result()
                results.append((svg_dir, unique_images, image_cache))
                done += 1
                log_progress("Collect", done, total_dirs)

    for svg_dir, unique_images, image_cache in results:
        dir_image_caches[svg_dir] = image_cache
        for h, info in unique_images.items():
            if h not in caption_cache_global:
                all_unique_images.setdefault(h, info)

    for image_cache in dir_image_caches.values():
        for info in image_cache.values():
            h = info.get("hash")
            if not h or str(h).startswith("missing:"):
                continue
            if h in caption_cache_global or h in all_unique_images:
                continue
            all_unique_images[h] = {
                "mime": info.get("mime", "application/octet-stream"),
                "path": info.get("path", ""),
                "has_alpha": info.get("has_alpha", False),
            }

    if run_caption:
        print(f"[placeholderall] 全局待 caption 图片数（去重后）: {len(all_unique_images)}")
    else:
        print("[placeholderall] 已跳过 caption 阶段，跳过待 caption 统计。")

    def run_replace_stage(
        stage_label: str,
        caption_cache: Dict[str, str],
        chart_cache: Dict[str, bool],
    ) -> None:
        total_dirs_local = len(svg_dirs)

        def stage_worker(svg_dir: Path) -> None:
            rel = svg_dir.relative_to(in_root)
            out_dir = out_root / rel
            image_cache = dir_image_caches.get(svg_dir, {})
            print(f"[placeholderall] {stage_label} 目录: {svg_dir} -> {out_dir}")
            run_for_dir_replace(
                svg_dir,
                out_dir,
                image_cache,
                caption_cache,
                chart_cache,
                stage_label=stage_label,
            )

        if args.folder_workers <= 1:
            for idx, d in enumerate(svg_dirs, start=1):
                stage_worker(d)
                log_progress(stage_label, idx, total_dirs_local)
        else:
            with ThreadPoolExecutor(max_workers=args.folder_workers) as executor:
                futures = {executor.submit(stage_worker, d): d for d in svg_dirs}
                done = 0
                for fut in as_completed(futures):
                    d = futures[fut]
                    try:
                        fut.result()
                    except Exception as exc:
                        print(f"[placeholderall] 子目录 {stage_label} 失败: {d} -> {exc}")
                    finally:
                        done += 1
                        log_progress(stage_label, done, total_dirs_local)

    # 阶段1：生成初始占位（不触发 VLM，使用已有缓存）
    if run_placeholder:
        stage1_caption_cache = dict(caption_cache_global)
        stage1_chart_cache = dict(chart_cache_global)
        run_replace_stage("Placeholder", stage1_caption_cache, stage1_chart_cache)
    else:
        print("[placeholderall] 跳过 Placeholder 阶段 (--stage 未包含 placeholder)")

    # 阶段2：全局批量 caption（此处只负责 caption，本阶段不更新图表判断）
    if run_caption:
        if all_unique_images:
            prompt = args.prompt or (
                "请根据图片输出严格的 JSON："
                "{\"caption\":\"...\"}。"
                "caption 用英文详尽描述画面，适合作为生图提示词。只输出 JSON。"
            )
            if vlm_backend == "api":
                # 这里传入一个临时 chart_cache，避免在 caption 阶段就写入图表判断结果
                dummy_chart_cache: Dict[str, bool] = {}
                generate_captions(
                    all_unique_images,
                    caption_cache_global,
                    dummy_chart_cache,
                    base_url,
                    api_key,
                    api_model,
                    prompt,
                    caption_max_tokens,
                    temperature,
                    timeout,
                    retries,
                    workers,
                    qps,
                    sleep_sec,
                )
            else:
                caption_prompt = prompt
                caption_items: List[Dict[str, str]] = []
                for h, info in all_unique_images.items():
                    img_path = info.get("path", "")
                    if not img_path or not Path(img_path).exists():
                        caption_cache_global[h] = "图片占位"
                        continue
                    caption_items.append(
                        {"image_hash": h, "image_path": img_path, "prompt": caption_prompt}
                    )
                outputs = run_vllm_batch(
                    caption_items,
                    model_source=vllm_model,
                    temperature=temperature,
                    max_tokens=caption_max_tokens,
                    batch_size=args.vllm_batch_size,
                    concurrency=args.vllm_concurrency,
                    max_model_len=args.vllm_max_model_len,
                    max_batched_tokens=args.vllm_max_batched_tokens,
                    tensor_parallel_size=args.vllm_tensor_parallel_size,
                    enable_chunked_prefill=args.vllm_enable_chunked_prefill,
                    ray_address=args.ray_address,
                    ray_log_to_driver=args.ray_log_to_driver,
                    progress_label="Caption VLM",
                )
                for row in outputs:
                    h = str(row.get("image_hash", ""))
                    if not h:
                        continue
                    caption_cache_global[h] = extract_caption(str(row.get("generated_text", "")))
        else:
            print("[placeholderall] 无需 caption：没有新图片需要处理。")

    else:
        print("[placeholderall] 跳过 Caption 阶段 (--stage 未包含 caption)")

    # 基于 caption 关键词，筛选“可疑图表”图片 hash
    if run_chart:
        # 仅对这些 hash 再走一次 VLM，专门进行图表判断
        hash_to_image: Dict[str, Dict[str, str]] = {}
        for image_cache in dir_image_caches.values():
            for info in image_cache.values():
                h = info.get("hash")
                if not h:
                    continue
                if h not in hash_to_image:
                    hash_to_image[h] = info
        for h, info in all_unique_images.items():
            if h not in hash_to_image:
                hash_to_image[h] = info
    
        suspicious_hashes: List[str] = []
        for h, cap in caption_cache_global.items():
            if not cap:
                continue
            if not is_suspicious_chart_caption(cap):
                continue
            # 非 force 模式下，如果已有图表判断结果，则跳过，避免重复推理
            if not args.force and h in chart_cache_global:
                continue
            suspicious_hashes.append(h)
    
        print(f"[placeholderall] 可疑图表图片数: {len(suspicious_hashes)}")
    
        if vlm_backend == "api":
            chart_updates = detect_charts_with_vlm(
                suspicious_hashes,
                hash_to_image,
                base_url=base_url,
                api_key=api_key,
                model=api_model,
                timeout=timeout,
                retries=retries,
                qps=qps,
                sleep_sec=sleep_sec,
                workers=workers,
            )
        else:
            chart_prompt = (
                "你是图像类型判定助手。请仅根据图片内容判断这是否是“纯数据可视化图表”，"
                "例如：柱状图、折线图、饼图、散点图、面积图、雷达图、直方图等。"
                "如果是纯图表（主要内容是数据可视化），请输出严格 JSON："
                '{"is_chart": true}；否则输出 {"is_chart": false}。'
                "只输出 JSON，不要输出任何多余文字。"
            )
            chart_prompt = chart_prompt
            chart_items: List[Dict[str, str]] = []
            for h in suspicious_hashes:
                info = hash_to_image.get(h) or {}
                img_path = info.get("path", "")
                if not img_path or not Path(img_path).exists():
                    chart_cache_global[h] = False
                    continue
                chart_items.append({"image_hash": h, "image_path": img_path, "prompt": chart_prompt})
            outputs = run_vllm_batch(
                chart_items,
                model_source=vllm_model,
                temperature=0.0,
                max_tokens=64,
                batch_size=args.vllm_batch_size,
                concurrency=args.vllm_concurrency,
                max_model_len=args.vllm_max_model_len,
                max_batched_tokens=args.vllm_max_batched_tokens,
                tensor_parallel_size=args.vllm_tensor_parallel_size,
                enable_chunked_prefill=args.vllm_enable_chunked_prefill,
                ray_address=args.ray_address,
                ray_log_to_driver=args.ray_log_to_driver,
                progress_label="Chart VLM",
            )
            chart_updates = {
                str(row.get("image_hash", "")): extract_is_chart(str(row.get("generated_text", "")))
                for row in outputs
                if row.get("image_hash")
            }
    
        # 先将没有任何判断结果的 hash 默认标记为 False，再用 VLM 结果覆盖
        for h in caption_cache_global.keys():
            chart_cache_global.setdefault(h, False)
        for h, flag in chart_updates.items():
            chart_cache_global[h] = bool(flag)
    
        # 阶段3：为每个子目录执行占位替换（replace），并写目录级缓存
        run_replace_stage("Replace", caption_cache_global, chart_cache_global)
    
    else:
        print("[placeholderall] 跳过 Chart 阶段 (--stage 未包含 chart)")

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
