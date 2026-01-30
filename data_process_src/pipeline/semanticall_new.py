#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
svg_text_semantic_vllm_global.py

全局版本：只初始化一次 vLLM 引擎（Qwen3-Coder-30B-A3B-Instruct），
对输入根目录下的所有 SVG 做文本语义分组，保持与原
`svg_text_semantic.py` 完全一致的输出格式与目录结构：

- 语义 SVG 输出到 `--output` 根目录下，保持与输入相同的子目录结构
- 每个子目录下仍然有各自的：
  - `meta/items/<name>.json`
  - `meta/plans/<name>.json`
  - `meta/visual_group/<name>.json`
  - `meta/raw/<name>.txt` / `<name>_retry.txt`（这里沿用单文件名，不加 retry 后缀）
  - `meta/failed_tasks.json`

与 `svg_text_semantic_vllm.py` 的区别：
- 只初始化一次 vLLM 引擎（全局），批量处理所有子目录下的 SVG
- 通过 tasks 列表统一调度，避免为每个子目录单独起引擎
"""

import argparse
import json
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import xml.etree.ElementTree as ET

from .svg_text_semantic import (  # type: ignore
    ROLE_SET,
    FAILED_TASKS_NAME,
    SVG_NS,
    load_config,
    parse_json_from_text,
    normalize_tree_plan,
    apply_plan_to_svg,
)
from .svg_text_semantic_vllm import (  # type: ignore
    VLLMSemanticEngine,
    extract_items_for_svg,
    build_prompt,
)


def find_all_svgs(root: Path) -> List[Path]:
    """递归查找 root 下所有 .SVG 文件，跳过以 `._` 开头和 `_global_cache` 目录。"""
    svg_files: List[Path] = []
    for p in root.rglob("*.SVG"):
        if p.name.startswith("._"):
            continue
        # 跳过 _global_cache 及其子目录
        if "_global_cache" in p.parts:
            continue
        svg_files.append(p)
    return sorted(svg_files)


def load_failed_map(meta_dir: Path) -> Dict[str, str]:
    """读取某个 meta 目录下的 failed_tasks.json，返回 {svg_name: error}。"""
    failed_tasks_path = meta_dir / FAILED_TASKS_NAME
    if not failed_tasks_path.exists():
        return {}
    try:
        data = json.loads(failed_tasks_path.read_text(encoding="utf-8"))
        failed_prev: Dict[str, str] = {}
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and isinstance(item.get("svg"), str):
                    failed_prev[item["svg"]] = str(item.get("error") or "")
        elif isinstance(data, dict):
            failed_prev = {str(k): str(v) for k, v in data.items()}
        return failed_prev
    except Exception:
        return {}


def save_failed_map(meta_dir: Path, failures: Dict[str, str]) -> None:
    """将 {svg_name: error} 写回 meta_dir 下的 failed_tasks.json，与原结构兼容。"""
    failed_tasks_path = meta_dir / FAILED_TASKS_NAME
    if not failures:
        if failed_tasks_path.exists():
            failed_tasks_path.unlink(missing_ok=True)
        return
    failed_items = [{"svg": name, "error": err} for name, err in sorted(failures.items())]
    failed_tasks_path.parent.mkdir(parents=True, exist_ok=True)
    failed_tasks_path.write_text(json.dumps(failed_items, ensure_ascii=False, indent=2), encoding="utf-8")


def build_visual_group_plan(
    plan: Dict[str, object],
    items: List[Dict[str, object]],
) -> Dict[str, object]:
    nodes_raw = plan.get("nodes") if isinstance(plan, dict) else None
    if not isinstance(nodes_raw, list):
        return {
            "nodes": [],
            "root": plan.get("root") if isinstance(plan, dict) else None,
            "unassigned": plan.get("unassigned") if isinstance(plan, dict) else [],
        }

    id_to_text: Dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str):
            continue
        text_val = item.get("text")
        id_to_text[item_id] = "" if text_val is None else str(text_val)

    nodes_map: Dict[str, Dict[str, object]] = {}
    for node in nodes_raw:
        if not isinstance(node, dict):
            continue
        node_id = node.get("id")
        if not isinstance(node_id, str):
            continue
        nodes_map[node_id] = dict(node)

    for node in nodes_map.values():
        if node.get("type") != "textbox":
            continue
        item_ids = node.get("item_ids")
        if not isinstance(item_ids, list):
            item_ids = []
        items_payload: List[Dict[str, str]] = []
        texts: List[str] = []
        for iid in item_ids:
            if not isinstance(iid, str):
                continue
            text = id_to_text.get(iid, "")
            items_payload.append({"id": iid, "text": text})
            if text:
                texts.append(text)
        node["items"] = items_payload
        node["text"] = "\n".join(texts).strip()

    text_cache: Dict[str, str] = {}

    def collect_text(node_id: str, stack: Set[str]) -> str:
        cached = text_cache.get(node_id)
        if cached is not None:
            return cached
        node = nodes_map.get(node_id)
        if not node:
            text_cache[node_id] = ""
            return ""
        if node.get("type") == "textbox":
            text = str(node.get("text") or "")
            text_cache[node_id] = text
            return text
        if node.get("type") != "group":
            text_cache[node_id] = ""
            return ""
        if node_id in stack:
            text_cache[node_id] = ""
            return ""

        stack.add(node_id)
        children = node.get("children")
        if not isinstance(children, list):
            children = []
        texts: List[str] = []
        for child_id in children:
            if not isinstance(child_id, str):
                continue
            child_text = collect_text(child_id, stack)
            if child_text:
                texts.append(child_text)
        stack.remove(node_id)

        group_text = "\n".join(texts).strip()
        node["text"] = group_text
        text_cache[node_id] = group_text
        return group_text

    for node_id, node in nodes_map.items():
        if node.get("type") == "group":
            collect_text(node_id, set())

    visual_nodes: List[Dict[str, object]] = []
    for node in nodes_raw:
        if not isinstance(node, dict):
            continue
        node_id = node.get("id")
        if isinstance(node_id, str) and node_id in nodes_map:
            visual_nodes.append(nodes_map[node_id])

    return {
        "nodes": visual_nodes,
        "root": plan.get("root"),
        "unassigned": plan.get("unassigned", []),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Global semantic grouping for all SVGs under a root directory using local vLLM."
    )
    parser.add_argument("--input", required=True, help="输入根目录，内部包含多个子目录与 SVG。")
    parser.add_argument("--output", required=True, help="输出根目录，将保留与输入相同的子目录结构。")
    parser.add_argument(
        "--config",
        default="config.json",
        help="文本参数的 config 路径（可选，仅用于 max_tokens / temperature 等）。",
    )

    # vLLM / 模型相关参数
    parser.add_argument(
        "--model",
        default="/mnt/cache/liwenbo/data/model/Qwen/Qwen3-Coder-30B-A3B-Instruct",
        help="本地 vLLM 模型路径（Qwen3-Coder-30B-A3B-Instruct）。",
    )
    parser.add_argument("--tp", type=int, default=4, help="vLLM tensor parallel size（建议与 GPU 数量一致）。")
    parser.add_argument(
        "--gpu-mem-util",
        type=float,
        default=0.85,
        help="vLLM GPU 显存利用率（0-1）。",
    )
    parser.add_argument(
        "--max-model-len",
        type=int,
        default=4096,
        help="vLLM 最大上下文长度（用于控制显存）。",
    )
    parser.add_argument(
        "--max-batched-tokens",
        type=int,
        default=16384,
        help="vLLM 单 batch 最大 token 数，用于提高吞吐。",
    )
    parser.add_argument(
        "--max-num-seqs",
        type=int,
        default=256,
        help="vLLM 引擎内同时保留的最大序列数。",
    )

    # 语义分组 LLM 输出相关
    parser.add_argument("--max-tokens", type=int, default=None, help="LLM 输出最大 token 数（单 SVG）。")
    parser.add_argument("--temperature", type=float, default=None, help="LLM temperature。")
    parser.add_argument(
        "--retries",
        type=int,
        default=1,
        help="解析失败时重试次数（vLLM 内部再次生成）。",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重算所有 SVG，忽略已有输出与 failed_tasks.json。",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=64,
        help="每批处理的 SVG 数量（控制内存占用）。",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="打印 prompt 调试信息（长度预警与预览）。",
    )

    args = parser.parse_args()

    input_root = Path(args.input).expanduser().resolve()
    output_root = Path(args.output).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    # 收集所有 SVG
    svg_files = find_all_svgs(input_root)
    if not svg_files:
        print("No SVG files found under input root.")
        return

    config = load_config(Path(args.config) if args.config else None)
    max_tokens = args.max_tokens if args.max_tokens is not None else int(config.get("text_max_tokens", 1200))
    temperature = args.temperature if args.temperature is not None else float(config.get("text_temperature", 0.2))

    # 初始化 vLLM 引擎（全局仅一次）
    engine = VLLMSemanticEngine(
        model_path=args.model,
        tp=args.tp,
        max_tokens=max_tokens,
        temperature=temperature,
        max_model_len=args.max_model_len,
        gpu_mem_util=args.gpu_mem_util,
        max_num_seqs=args.max_num_seqs,
        max_batched_tokens=args.max_batched_tokens,
    )

    # 与 HTTP 版本保持一致的“系统角色”说明，直接拼进文本 prompt 里
    system_prefix = "You are an SVG text semantic annotator. Output JSON only.\n\n"
    prompt_template = system_prefix + build_prompt(ROLE_SET)

    # 构造全局任务列表
    # 每个任务包含：svg_path / out_svg / meta_dir
    tasks: List[Dict[str, Path]] = []
    failed_cache: Dict[Path, Dict[str, str]] = {}

    for svg_path in svg_files:
        rel = svg_path.relative_to(input_root)
        out_svg = output_root / rel
        svg_out_dir = out_svg.parent
        meta_dir = svg_out_dir / "meta"

        # 加载该 meta_dir 对应的 failed_tasks 映射（缓存）
        if meta_dir not in failed_cache:
            failed_cache[meta_dir] = load_failed_map(meta_dir)
        failed_prev = failed_cache[meta_dir]

        if not args.force:
            # 增量逻辑：如输出已存在且不在 failed_tasks 中，则跳过
            if svg_path.name not in failed_prev and out_svg.exists():
                continue

        tasks.append(
            {
                "svg_path": svg_path,
                "out_svg": out_svg,
                "meta_dir": meta_dir,
            }
        )

    if not tasks:
        print("No SVG files need processing.")
        return

    total = len(tasks)
    print(f"Semantic (vLLM global): {total} SVGs to process.")

    batch_size = max(args.batch_size, 1)
    retry_max_tokens = min(max_tokens * 2, 2000)
    failures_by_meta: Dict[Path, Dict[str, str]] = {m: {} for m in failed_cache.keys()}
    processed = 0

    for start in range(0, total, batch_size):
        batch_tasks = tasks[start : start + batch_size]
        prompts: List[str] = []
        batch_items_docs: List[Dict[str, object]] = []

        for t in batch_tasks:
            svg_path: Path = t["svg_path"]
            meta_dir: Path = t["meta_dir"]
            items_doc = extract_items_for_svg(svg_path, meta_dir / "items")
            batch_items_docs.append(items_doc)
            text = prompt_template + "\n\nINPUT_JSON:\n" + json.dumps(items_doc, ensure_ascii=False)
            prompts.append(text)

        if args.debug:
            print(f"[semanticall] total prompts: {len(prompts)}")
            max_safe_chars = args.max_model_len * 2
            long_prompts = []
            for i, (t, p) in enumerate(zip(batch_tasks, prompts)):
                char_len = len(p)
                svg_path: Path = t["svg_path"]
                if char_len > max_safe_chars * 0.8:
                    long_prompts.append((i, start + i, svg_path.name, char_len))

            if long_prompts:
                print(
                    f"[semanticall] WARNING: Found {len(long_prompts)} potentially long prompts (may exceed max_model_len):"
                )
                for local_idx, global_idx, name, char_len in long_prompts[:10]:
                    print(f"  [{global_idx}] {name}: {char_len} chars")
                    print(f"    Preview: {prompts[local_idx][:200]}...")

            for i, p in enumerate(prompts[:3]):
                print(f"\n[semanticall] prompt[{start + i}] char_len={len(p)}")
                print(p[:600])
                print("-" * 80)

        results = engine.generate_with_retry(prompts, max_tokens=max_tokens, retry_max_tokens=retry_max_tokens)

        for t, items_doc, (raw_text, err) in zip(batch_tasks, batch_items_docs, results):
            svg_path: Path = t["svg_path"]
            out_svg: Path = t["out_svg"]
            meta_dir: Path = t["meta_dir"]
            name = svg_path.stem

            items = items_doc.get("items") if isinstance(items_doc, dict) else None
            if not isinstance(items, list):
                items = []

            meta_raw_dir = meta_dir / "raw"
            meta_plans_dir = meta_dir / "plans"

            meta_raw_dir.mkdir(parents=True, exist_ok=True)
            raw_path = meta_raw_dir / f"{name}.txt"
            raw_path.write_text(raw_text or (err or ""), encoding="utf-8")

            if err:
                failures_by_meta.setdefault(meta_dir, {})[svg_path.name] = err
                processed += 1
                print(f"Semantic (vLLM global): {processed}/{total} (FAILED: {svg_path})")
                continue

            parsed = parse_json_from_text(raw_text or "")
            if not parsed:
                failures_by_meta.setdefault(meta_dir, {})[svg_path.name] = "invalid JSON after retry"
                processed += 1
                print(f"Semantic (vLLM global): {processed}/{total} (FAILED: {svg_path})")
                continue

            item_ids = [it.get("id") for it in items if isinstance(it, dict) and it.get("id")]
            plan = normalize_tree_plan(parsed or {}, item_ids)  # type: ignore[arg-type]

            meta_plans_dir.mkdir(parents=True, exist_ok=True)
            plan_path = meta_plans_dir / f"{name}.json"
            plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

            meta_visual_dir = meta_dir / "visual_group"
            meta_visual_dir.mkdir(parents=True, exist_ok=True)
            visual_plan = build_visual_group_plan(plan, items)
            visual_path = meta_visual_dir / f"{name}.json"
            visual_path.write_text(json.dumps(visual_plan, ensure_ascii=False, indent=2), encoding="utf-8")

            tree = ET.parse(svg_path)
            root = tree.getroot()
            ET.register_namespace("", SVG_NS)
            apply_plan_to_svg(tree, items, plan, pad=0.0, keep_ids=False)
            out_svg.parent.mkdir(parents=True, exist_ok=True)
            tree.write(out_svg, encoding="utf-8", xml_declaration=True)

            processed += 1
            print(f"Semantic (vLLM global): {processed}/{total}")

    # 写回各 meta_dir 下的 failed_tasks.json
    for meta_dir, prev in failed_cache.items():
        new_failures = failures_by_meta.get(meta_dir, {})
        # 若 force，则只保留这次的失败；否则合并旧失败（仍未成功的）
        merged: Dict[str, str] = {}
        if not args.force:
            merged.update(prev)
        merged.update(new_failures)
        save_failed_map(meta_dir, merged)


if __name__ == "__main__":
    main()
