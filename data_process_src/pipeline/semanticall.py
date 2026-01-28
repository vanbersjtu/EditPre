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
    """构造调用 `python -m pipeline.svg_text_semantic` 的命令行。"""
    cmd = [
        sys.executable,
        "-m",
        "pipeline.svg_text_semantic",
        "--input",
        str(svg_dir),
        "--output",
        str(out_dir),
    ]

    if args.meta:
        cmd += ["--meta", str(Path(args.meta).expanduser().resolve())]
    if args.config:
        cmd += ["--config", args.config]
    if args.base_url:
        cmd += ["--base-url", args.base_url]
    if args.api_key:
        cmd += ["--api-key", args.api_key]
    if args.model:
        cmd += ["--model", args.model]
    if args.max_tokens is not None:
        cmd += ["--max-tokens", str(args.max_tokens)]
    if args.temperature is not None:
        cmd += ["--temperature", str(args.temperature)]
    if args.timeout is not None:
        cmd += ["--timeout", str(args.timeout)]
    if args.workers is not None:
        cmd += ["--workers", str(args.workers)]
    if args.qps is not None:
        cmd += ["--qps", str(args.qps)]
    if args.retries is not None:
        cmd += ["--retries", str(args.retries)]
    if args.pad is not None:
        cmd += ["--pad", str(args.pad)]
    if args.keep_ids:
        cmd += ["--keep-ids"]
    if args.prompt:
        cmd += ["--prompt", args.prompt]
    if args.enforce_font_size:
        cmd += ["--enforce-font-size"]
    if args.font_size_tol is not None:
        cmd += ["--font-size-tol", str(args.font_size_tol)]
    if args.require_success:
        cmd += ["--require-success"]

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
        description="Run svg_text_semantic on all subdirectories (semantic text grouping)."
    )
    parser.add_argument("--input", required=True, help="输入根目录（占位符 SVG 的根），包含多个子目录。")
    parser.add_argument("--output", required=True, help="输出根目录，将按相同子目录结构写入语义 SVG。")
    parser.add_argument(
        "--meta",
        default="",
        help="可选：统一的 meta 根目录（items/plans/raw）。若为空则每个子目录各自使用 output/meta。",
    )
    parser.add_argument("--config", default="", help="传给 svg_text_semantic 的 config 路径（可选）。")

    # 透传给 svg_text_semantic 的 LLM 相关参数（HTTP / OpenAI 兼容接口）
    parser.add_argument("--base-url", default="", help="OpenAI-compatible base URL.")
    parser.add_argument("--api-key", default="", help="API key.")
    parser.add_argument("--model", default="", help="Text model name.")
    parser.add_argument("--max-tokens", type=int, default=None, help="LLM 输出最大 token 数。")
    parser.add_argument("--temperature", type=float, default=None, help="LLM 采样温度。")
    parser.add_argument("--timeout", type=int, default=None, help="请求超时时间（秒）。")
    parser.add_argument("--workers", type=int, default=None, help="单目录内部并行 worker 数。")
    parser.add_argument("--qps", type=float, default=None, help="全局 QPS 限制。")
    parser.add_argument("--retries", type=int, default=None, help="失败重试次数。")
    parser.add_argument("--pad", type=float, default=None, help="textbox/group 外框 padding。")
    parser.add_argument("--keep-ids", action="store_true", help="保留 data-extract-id。")
    parser.add_argument("--prompt", default="", help="自定义 LLM prompt。")
    parser.add_argument(
        "--enforce-font-size",
        action="store_true",
        help="强制同一 textbox 内字号归一逻辑（转发给 svg_text_semantic）。",
    )
    parser.add_argument(
        "--font-size-tol",
        type=float,
        default=None,
        help="字体容差，建议 0.2 左右（不填则沿用 svg_text_semantic 默认值）。",
    )
    parser.add_argument(
        "--require-success",
        action="store_true",
        help="若某子目录存在失败项，则让子进程以非零退出（便于上层检测）。",
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

