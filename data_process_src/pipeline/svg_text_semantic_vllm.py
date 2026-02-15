#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
svg_text_semantic_vllm.py

使用本地 vLLM 引擎（例如 Qwen3-Coder-30B-A3B-Instruct）对 SVG 文本进行语义分组，
保持与原 `svg_text_semantic.py` 完全一致的输出格式与目录结构：

- 语义 SVG 输出到 `--output` 目录（同名 .SVG）
- 元数据：
  - `meta/items/<name>.json`
  - `meta/plans/<name>.json`
  - `meta/raw/<name>.txt` / `<name>_retry.txt`
  - `meta/failed_tasks.json`

区别在于：
- 不再通过 HTTP API 调用 LLM，而是在本机直接用 vLLM 进行批量推理
- 支持对同一目录下多个 SVG 一次性 batch 发送到 vLLM，提高 GPU 吞吐
"""

import argparse
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import xml.etree.ElementTree as ET

from vllm import LLM, SamplingParams
from transformers import AutoTokenizer

from .svg_text_semantic import (  # type: ignore
    ROLE_SET,
    FAILED_TASKS_NAME,
    SVG_NS,
    load_config,
    parse_json_from_text,
    ensure_text_ids,
    extract_items_with_playwright,
    get_canvas_size,
    read_text_xml,
    normalize_tree_plan,
    apply_plan_to_svg,
)


class VLLMSemanticEngine:
    """封装 vLLM 引擎与批量推理逻辑。"""

    def __init__(
        self,
        model_path: str,
        tp: int,
        max_tokens: int,
        temperature: float,
        max_model_len: int,
        gpu_mem_util: float,
        max_num_seqs: int,
        max_batched_tokens: int,
    ) -> None:
        self.max_tokens = max_tokens
        self.temperature = temperature

        # 初始化 vLLM 引擎
        self.llm = LLM(
            model=model_path,
            tensor_parallel_size=tp,
            trust_remote_code=True,
            max_model_len=max_model_len,
            gpu_memory_utilization=gpu_mem_util,
            max_num_seqs=max_num_seqs,
            max_num_batched_tokens=max_batched_tokens,
        )

        self.sampling = SamplingParams(
            temperature=temperature,
            top_p=0.9,
            max_tokens=max_tokens,
        )

        try:
            self.tokenizer = self.llm.get_tokenizer()
        except Exception:
            self.tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)

    def generate_batch(self, prompts: List[str]) -> List[str]:
        """对一组 prompt 进行推理，返回每个 prompt 的生成文本。"""
        if not prompts:
            return []
        outputs = self.llm.generate(prompts, self.sampling)
        results: List[str] = []
        for out in outputs:
            if not out.outputs:
                results.append("")
            else:
                results.append(out.outputs[0].text)
        return results

    def build_chat_prompt(self, system_prompt: str, user_prompt: str) -> str:
        tokenizer = getattr(self, "tokenizer", None)
        if tokenizer is None:
            raise RuntimeError("Tokenizer not initialized.")
        apply_chat_template = getattr(tokenizer, "apply_chat_template", None)
        if apply_chat_template is None:
            raise RuntimeError("Tokenizer does not support chat template.")
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return apply_chat_template(messages, tokenize=False, add_generation_prompt=True)

    def generate_with_retry(
        self,
        prompts: List[str],
        max_tokens: int,
        retry_max_tokens: int,
    ) -> List[Tuple[str, Optional[str]]]:
        """
        对 prompts 批量推理：
        - 先用 max_tokens 生成一次
        - 对解析失败的条目，再用 retry_max_tokens 重试一次
        返回列表：(raw_text, error_msg or None)
        """
        n = len(prompts)
        if n == 0:
            return []

        # 一次性生成初始结果
        self.sampling.max_tokens = max_tokens
        first_texts = self.generate_batch(prompts)

        results: List[Tuple[str, Optional[str]]] = [("", None) for _ in range(n)]
        need_retry_indices: List[int] = []
        retry_prompts: List[str] = []

        for i, text in enumerate(first_texts):
            parsed = parse_json_from_text(text or "")
            if parsed is not None:
                results[i] = (text, None)
            else:
                need_retry_indices.append(i)
                retry_prompts.append(prompts[i])

        if not need_retry_indices:
            return results

        # 对解析失败的条目做一次高 max_tokens 重试
        self.sampling.max_tokens = retry_max_tokens
        retry_texts = self.generate_batch(retry_prompts)

        for idx, text in zip(need_retry_indices, retry_texts):
            parsed = parse_json_from_text(text or "")
            if parsed is not None:
                results[idx] = (text, None)
            else:
                results[idx] = (text or "", "invalid JSON after retry")

        return results


def build_prompt(role_list: List[str]) -> str:
    """复用原脚本中的 prompt 设计，保持行为一致。"""
    roles = ", ".join(role_list)
    return (
        "你是 PPT 幻灯片的文本语义标注器。输入是从 SVG 中提取的 text items，"
        "每个 item 包含文本、bbox 与样式（fontSize/fontWeight/fill/fontFamily 等）。"
        "你的目标是把这些 items 分组成多层级的结构："
        "最底层是 textbox（文本框），上层可以是 group（文本块/版块），可以形成任意深度树。"
        f"角色集合仅允许这些：{roles}。"
        "必须输出严格 JSON，结构如下："
        "{\"nodes\":[...],\"root\":\"node-id\"}。"
        "其中 nodes 是节点列表，每个节点类型必须是 textbox 或 group："
        "- textbox 节点字段：id,type=\"textbox\",role,order,item_ids,confidence(可选)"
        "- group 节点字段：id,type=\"group\",role,order,children,confidence(可选)"
        "root 是根节点 id，代表整个文本组合的顶层 group。"
        "分组规则（必须遵守）："
        "1) 同一个 textbox 内的 items 必须字号相同（同一 fontSize）。"
        "2) 如果多行文本属于同一段落或同一标题块，且字号相同、上下相邻、对齐一致，"
        "   必须合并为一个 textbox（允许多行）。"
        "3) subtitle 只能在字号明显小于 title 时使用；"
        "   如果字号相同，就不要拆成 title + subtitle，必须合并为一个多行标题 textbox。"
        "4) 每个 bullet point 应该至少拆成两个 textbox（小标题 + 正文），"
        "   并将它们放进一个 group 里（bullet block）。"
        "5) 若存在主标题/副标题，应与所有 bullet groups 一起归入更高层的 group。"
        "6) 阅读顺序：先上后下，同一行从左到右。"
        "输出约束："
        "每个 item_id 必须出现且只能出现 1 次，不能遗漏；"
        "如果无法归组，必须单独放入 textbox；"
        "children 只能引用 nodes 中存在的 id；"
        "不要产生循环引用；"
        "只输出 nodes 与 root 字段，不要输出额外字段；"
        "只输出一个 JSON，不要重复多份。"
    )


def slim_item(item: Dict[str, object]) -> Dict[str, object]:
    """
    精简单个 item，只保留语义分组所需的核心字段。
    保留：id, text, bbox (x,y,w,h 保留2位小数), style.fontSize, style.fontWeight,
         style.textAnchor, style.fontFamily, style.fill, style.opacity
    删除：ctm (对语义分组无用，最终输出 SVG 时也不需要),
         style 其他字段 (letterSpacing, dominantBaseline), text_xml
    """
    if not isinstance(item, dict):
        return item

    slimmed = {
        "id": item.get("id"),
        "text": item.get("text"),
    }

    # 保留简化的 bbox（保留 2 位小数，减少 token）
    bbox = item.get("bbox")
    if isinstance(bbox, dict):
        slimmed["bbox"] = {
            "x": round(float(bbox.get("x", 0)), 2) if bbox.get("x") is not None else 0.0,
            "y": round(float(bbox.get("y", 0)), 2) if bbox.get("y") is not None else 0.0,
            "w": round(float(bbox.get("w", 0)), 2) if bbox.get("w") is not None else 0.0,
            "h": round(float(bbox.get("h", 0)), 2) if bbox.get("h") is not None else 0.0,
        }

    # 只保留关键的 style 字段（语义分组需要）
    style = item.get("style")
    if isinstance(style, dict):
        slimmed_style = {}
        if style.get("fontSize"):
            slimmed_style["fontSize"] = style["fontSize"]
        if style.get("fontWeight"):
            slimmed_style["fontWeight"] = style["fontWeight"]
        if style.get("textAnchor"):
            slimmed_style["textAnchor"] = style["textAnchor"]
        if style.get("fontFamily"):
            slimmed_style["fontFamily"] = style["fontFamily"]
        if style.get("fill"):
            slimmed_style["fill"] = style["fill"]
        if style.get("opacity"):
            slimmed_style["opacity"] = style["opacity"]
        if slimmed_style:
            slimmed["style"] = slimmed_style

    return slimmed


def slim_items_doc(items_doc: Dict[str, object]) -> Dict[str, object]:
    """
    精简 items_doc，对每个 item 应用瘦身。
    """
    if not isinstance(items_doc, dict):
        return items_doc

    items = items_doc.get("items")
    if not isinstance(items, list):
        return items_doc

    slimmed_items = [slim_item(item) for item in items if isinstance(item, dict)]

    return {
        "canvas": items_doc.get("canvas"),
        "items": slimmed_items,
    }


def extract_items_for_svg(
    svg_path: Path,
    meta_items_dir: Path,
) -> Dict[str, object]:
    """
    对单个 SVG 抽取 text items；如已有 meta/items JSON 则复用（增量）。
    返回瘦身后的 items_doc = {"canvas": {...}, "items": [...]}
    注意：保存到磁盘的版本已经是瘦身后的，大幅减少 token 数。
    """
    meta_items_dir.mkdir(parents=True, exist_ok=True)
    items_path = meta_items_dir / (svg_path.stem + ".json")
    if items_path.exists():
        try:
            with items_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "items" in data:
                # 如果读取的是旧版本（包含 text_xml 或 style 里有很多冗余字段），也做一次瘦身
                # 检查是否需要瘦身（简单判断：如果第一个 item 有 text_xml 或 style 里有 fontFamily/fill 等冗余字段）
                if data.get("items") and isinstance(data["items"], list) and len(data["items"]) > 0:
                    first_item = data["items"][0]
                    if isinstance(first_item, dict):
                        needs_slim = False
                        if "text_xml" in first_item:
                            needs_slim = True
                        else:
                            style = first_item.get("style")
                            if isinstance(style, dict) and ("fontFamily" in style or "fill" in style or "opacity" in style):
                                needs_slim = True
                        if needs_slim:
                            # 旧版本，瘦身后重新保存
                            data = slim_items_doc(data)
                            items_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                return data
        except Exception:
            pass

    tree = ET.parse(svg_path)
    root = tree.getroot()
    ET.register_namespace("", SVG_NS)
    id_map = ensure_text_ids(root)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".svg") as tmp:
        tmp_path = Path(tmp.name)
        tree.write(tmp_path, encoding="utf-8", xml_declaration=True)

    items = extract_items_with_playwright(tmp_path)
    tmp_path.unlink(missing_ok=True)

    # 不再添加 text_xml（瘦身后不需要）
    # for item in items:
    #     item_id = item.get("id")
    #     elem = id_map.get(item_id)
    #     if elem is not None:
    #         item["text_xml"] = read_text_xml(elem)

    items_doc = {
        "canvas": get_canvas_size(root),
        "items": items,
    }

    # 瘦身后再保存
    items_doc = slim_items_doc(items_doc)
    items_path.write_text(json.dumps(items_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return items_doc


def process_directory_with_vllm(
    input_dir: Path,
    output_dir: Path,
    meta_dir: Path,
    engine: VLLMSemanticEngine,
    system_prompt: str,
    user_prompt_template: str,
    max_tokens: int,
    retries: int,
) -> None:
    """
    对一个目录下所有 SVG 使用同一个 vLLM 引擎进行语义分组。
    保持与原脚本相同的增量/失败记录行为。
    """
    meta_items_dir = meta_dir / "items"
    meta_plans_dir = meta_dir / "plans"
    meta_raw_dir = meta_dir / "raw"

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
    tasks: List[Dict[str, object]] = []

    for svg_path in sorted(svg_files):
        out_svg = output_dir / svg_path.name
        if svg_path.name not in failed_names and out_svg.exists():
            # 已成功完成的 SVG 直接跳过（增量）
            continue

        items_doc = extract_items_for_svg(svg_path, meta_items_dir)
        tasks.append(
            {
                "svg_path": svg_path,
                "out_svg": out_svg,
                "items_doc": items_doc,
            }
        )

    if not tasks:
        print("No SVG files need processing.")
        return

    total = len(tasks)
    print(f"Semantic (vLLM): {total} SVGs to process.")

    # 构造 prompts
    prompts: List[str] = []
    for t in tasks:
        items_doc = t["items_doc"]  # type: ignore[assignment]
        user_prompt = user_prompt_template + "\n\nINPUT_JSON:\n" + json.dumps(items_doc, ensure_ascii=False)
        text = engine.build_chat_prompt(system_prompt, user_prompt)
        prompts.append(text)

    # 批量调用 vLLM，并带一次 retry 逻辑
    retry_max_tokens = max(max_tokens * 2, 2000)
    results = engine.generate_with_retry(prompts, max_tokens=max_tokens, retry_max_tokens=retry_max_tokens)

    processed = 0
    failures: Dict[str, str] = {}

    for t, (raw_text, err) in zip(tasks, results):
        svg_path: Path = t["svg_path"]  # type: ignore[assignment]
        out_svg: Path = t["out_svg"]  # type: ignore[assignment]
        items_doc = t["items_doc"]  # type: ignore[assignment]
        name = svg_path.stem

        items = items_doc.get("items") if isinstance(items_doc, dict) else None
        if not isinstance(items, list):
            items = []

        # 写 raw 响应
        meta_raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = meta_raw_dir / f"{name}.txt"
        raw_path.write_text(raw_text or (err or ""), encoding="utf-8")
        if err:
            failures[svg_path.name] = err
            processed += 1
            print(f"Semantic (vLLM): {processed}/{total} (FAILED: {svg_path.name})")
            continue

        parsed = parse_json_from_text(raw_text or "")
        if not parsed:
            failures[svg_path.name] = "invalid JSON after retry"
            processed += 1
            print(f"Semantic (vLLM): {processed}/{total} (FAILED: {svg_path.name})")
            continue

        item_ids = [it.get("id") for it in items if isinstance(it, dict) and it.get("id")]
        plan = normalize_tree_plan(parsed or {}, item_ids)  # type: ignore[arg-type]

        # 写 plan
        meta_plans_dir.mkdir(parents=True, exist_ok=True)
        plan_path = meta_plans_dir / f"{name}.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

        # 应用 plan 到 SVG
        tree = ET.parse(svg_path)
        root = tree.getroot()
        ET.register_namespace("", SVG_NS)

        apply_plan_to_svg(tree, items, plan, pad=0.0, keep_ids=False)
        out_svg.parent.mkdir(parents=True, exist_ok=True)
        tree.write(out_svg, encoding="utf-8", xml_declaration=True)

        processed += 1
        print(f"Semantic (vLLM): {processed}/{total}")

    # 写 failed_tasks.json（与原版保持一致）
    failed_tasks_path = meta_dir / FAILED_TASKS_NAME
    if failures:
        failed_items = [{"svg": name, "error": err} for name, err in sorted(failures.items())]
        failed_tasks_path.parent.mkdir(parents=True, exist_ok=True)
        failed_tasks_path.write_text(json.dumps(failed_items, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Semantic (vLLM) failures: {len(failures)} (saved to {failed_tasks_path})")
    else:
        if failed_tasks_path.exists():
            failed_tasks_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Semantic grouping for SVG text using local vLLM.")
    parser.add_argument("--input", required=True, help="Input directory with SVGs.")
    parser.add_argument("--output", required=True, help="Output directory for semantic SVGs.")
    parser.add_argument("--meta", default="", help="Metadata directory for items/plans (default: output/meta).")
    parser.add_argument("--config", default="config.json", help="Config json path (only for text params).")

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

    args = parser.parse_args()

    input_dir = Path(args.input)
    output_dir = Path(args.output)
    meta_dir = Path(args.meta) if args.meta else output_dir / "meta"

    config = load_config(Path(args.config) if args.config else None)
    config_tokens = int(config.get("text_max_tokens", 1200))
    max_tokens = args.max_tokens if args.max_tokens is not None else max(config_tokens, 2000)
    temperature = args.temperature if args.temperature is not None else float(config.get("text_temperature", 0.2))

    # 初始化 vLLM 引擎
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

    system_prompt = "You are an SVG text semantic annotator. Output JSON only."
    user_prompt_template = build_prompt(ROLE_SET)

    process_directory_with_vllm(
        input_dir=input_dir,
        output_dir=output_dir,
        meta_dir=meta_dir,
        engine=engine,
        system_prompt=system_prompt,
        user_prompt_template=user_prompt_template,
        max_tokens=max_tokens,
        retries=args.retries,
    )


if __name__ == "__main__":
    main()
