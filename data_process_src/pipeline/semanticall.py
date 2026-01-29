#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
semanticall.py

一键遍历输入根目录下的所有子文件夹，对每个子目录中的 `.SVG` 文件执行
`pipeline.svg_text_semantic`，生成带语义分组的 SVG。

功能定位（兼容你之前的使用习惯）：
- 输入：一个包含多份 PPT 导出的“占位符 SVG 子文件夹”的根目录，例如：
    random2000_placeholder/
      EADxxx_演示文稿A/幻灯片1.SVG ...
      EADyyy_演示文稿B/幻灯片1.SVG ...
- 输出：在指定输出根目录下，保留相同子目录结构，分别生成：
    random2000_semantic/
      EADxxx_演示文稿A/*.SVG + meta/
      EADyyy_演示文稿B/*.SVG + meta/

语义分组与 LLM 调用的细节逻辑由 `pipeline.svg_text_semantic` 决定；
本脚本只负责“递归调度”和“按子目录并行运行”，并保持增量特性：
- `svg_text_semantic` 本身已经支持：
  - 如输出 SVG 已存在且不在 failed_tasks.json 中，会自动跳过
  - failed_tasks.json 记录失败项，方便多次重跑
"""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List
import subprocess


def find_svg_dirs(root: Path) -> List[Path]:
    """查找 root 下所有“包含至少一个 .SVG 文件”的子目录（包括 root 本身）。"""
    svg_dirs: List[Path] = []

    # 先看根目录自身是否有 SVG
    if any(p.suffix == ".SVG" and not p.name.startswith("._") for p in root.glob("*.SVG")):
        svg_dirs.append(root)

    # 再看下一层子目录
    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        if any(p.suffix == ".SVG" and not p.name.startswith("._") for p in sub.glob("*.SVG")):
            svg_dirs.append(sub)

    return svg_dirs


def build_cmd(
    svg_dir: Path,
    out_dir: Path,
    args: argparse.Namespace,
) -> list:
    """构造调用 `python -m pipeline.svg_text_semantic_vllm` 的命令行。"""
    cmd = [
        sys.executable,
        "-m",
        "pipeline.svg_text_semantic_vllm",
        "--input",
        str(svg_dir),
        "--output",
        str(out_dir),
    ]

    # vLLM / 引擎相关参数透传
    if getattr(args, "model", None):
        cmd += ["--model", args.model]
    if getattr(args, "tp", None) is not None:
        cmd += ["--tp", str(args.tp)]
    if getattr(args, "gpu_mem_util", None) is not None:
        cmd += ["--gpu-mem-util", str(args.gpu_mem_util)]
    if getattr(args, "max_model_len", None) is not None:
        cmd += ["--max-model-len", str(args.max_model_len)]
    if getattr(args, "max_batched_tokens", None) is not None:
        cmd += ["--max-batched-tokens", str(args.max_batched_tokens)]
    if getattr(args, "max_num_seqs", None) is not None:
        cmd += ["--max-num-seqs", str(args.max_num_seqs)]

    # 语义 LLM 相关参数透传
    if args.meta:
        cmd += ["--meta", str(Path(args.meta).expanduser().resolve())]
    if args.config:
        cmd += ["--config", args.config]
    if args.max_tokens is not None:
        cmd += ["--max-tokens", str(args.max_tokens)]
    if args.temperature is not None:
        cmd += ["--temperature", str(args.temperature)]
    if args.retries is not None:
        cmd += ["--retries", str(args.retries)]
    if args.prompt:
        cmd += ["--prompt", args.prompt]

    return cmd


def run_for_dir(svg_dir: Path, out_root: Path, in_root: Path, args: argparse.Namespace) -> None:
    rel = svg_dir.relative_to(in_root)
    out_dir = out_root / rel
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = build_cmd(svg_dir, out_dir, args)
    print(f"[semanticall] 处理目录: {svg_dir} -> {out_dir}")
    # 在 data_process_src 目录下运行，保持与直接调用一致的 import/cwd 语义
    data_root = in_root.parent
    subprocess.run(cmd, check=True, cwd=str(data_root))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run svg_text_semantic_vllm on all subdirectories (semantic text grouping with vLLM)."
    )
    parser.add_argument("--input", required=True, help="输入根目录（占位符 SVG 的根），包含多个子目录。")
    parser.add_argument("--output", required=True, help="输出根目录，将按相同子目录结构写入语义 SVG。")
    parser.add_argument(
        "--meta",
        default="",
        help="可选：统一的 meta 根目录（items/plans/raw）。若为空则每个子目录各自使用 output/meta。",
    )
    parser.add_argument("--config", default="", help="文本参数的 config 路径（可选，仅用于 max_tokens / temperature 等）。")

    # 透传给 vLLM 本地引擎的参数（与 svg_text_semantic_vllm 对齐）
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

    parser.add_argument("--max-tokens", type=int, default=None, help="LLM 输出最大 token 数（单 SVG）。")
    parser.add_argument("--temperature", type=float, default=None, help="LLM 采样温度。")
    parser.add_argument(
        "--retries",
        type=int,
        default=1,
        help="解析失败时重试次数（vLLM 内部再次生成）。",
    )

    parser.add_argument(
        "--folder-workers",
        type=int,
        default=4,
        help="按子目录并行的 worker 数（同时处理多少个子目录）。",
    )

    args = parser.parse_args()

    in_root = Path(args.input).expanduser().resolve()
    out_root = Path(args.output).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    svg_dirs = find_svg_dirs(in_root)
    if not svg_dirs:
        print("[semanticall] No SVG files found under input root.")
        return

    print(f"[semanticall] 将处理 {len(svg_dirs)} 个目录（包含 SVG）。")

    if args.folder_workers <= 1:
        for d in svg_dirs:
            run_for_dir(d, out_root, in_root, args)
    else:
        with ThreadPoolExecutor(max_workers=args.folder_workers) as executor:
            futures = {executor.submit(run_for_dir, d, out_root, in_root, args): d for d in svg_dirs}
            for fut in as_completed(futures):
                d = futures[fut]
                try:
                    fut.result()
                except Exception as exc:
                    print(f"[semanticall] 子目录失败: {d} -> {exc}")
                    # 不中断其它目录，方便增量重跑


if __name__ == "__main__":
    main()

