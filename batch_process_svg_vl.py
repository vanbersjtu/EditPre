#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Batch-run svg_text_semantic_vl over all PPT subfolders.
"""

import os
import subprocess
import sys
from pathlib import Path

# Update these paths if needed.
INPUT_ROOT = Path("/mnt/cache/liwenbo/PPT2SVG-SlideSVG/tes_foler_placeholder")
IMAGE_ROOT = Path("/mnt/cache/liwenbo/PPT2SVG-SlideSVG/test_foler_png")
OUTPUT_ROOT = Path("/mnt/cache/liwenbo/PPT2SVG-SlideSVG/tes_foler_semantic_vl_new")
PIPELINE_ROOT = Path("/mnt/cache/liwenbo/PPT2SVG-SlideSVG/data_process_src")

# vLLM HTTP API settings (OpenAI-compatible).
# Note: BASE_URL should NOT include the trailing /v1.
VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "http://10.119.21.254:8080")
VLLM_API_KEY = os.getenv("VLLM_API_KEY", "EMPTY")
VLLM_MODEL = os.getenv(
    "VLLM_MODEL",
    "/mnt/cache/liwenbo/data/model/Qwen/Qwen3-VL-30B-A3B-Instruct",
)
FORCE_REPROCESS = os.getenv("FORCE_REPROCESS", "0").lower() in {"1", "true", "yes"}


def main() -> None:
    if not INPUT_ROOT.exists():
        print(f"错误：输入目录不存在: {INPUT_ROOT}")
        sys.exit(1)
    if not IMAGE_ROOT.exists():
        print(f"错误：截图目录不存在: {IMAGE_ROOT}")
        sys.exit(1)
    if not PIPELINE_ROOT.exists():
        print(f"错误：pipeline 目录不存在: {PIPELINE_ROOT}")
        sys.exit(1)

    folders = [d for d in INPUT_ROOT.iterdir() if d.is_dir() and not d.name.startswith(".")]
    print(f"找到 {len(folders)} 个文件夹")

    python_exe = sys.executable

    for idx, folder in enumerate(folders, 1):
        output_dir = OUTPUT_ROOT / folder.name
        output_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n{'='*80}")
        print(f"处理 {idx}/{len(folders)}: {folder.name}")
        print(f"输出目录: {output_dir}")
        print(f"{'='*80}")

        cmd = [
            python_exe,
            "-m",
            "pipeline.svg_text_semantic_vl",
            "--input",
            str(folder),
            "--output",
            str(output_dir),
            "--image-root",
            str(IMAGE_ROOT),
        ]
        if VLLM_BASE_URL:
            cmd += ["--base-url", VLLM_BASE_URL]
        if VLLM_API_KEY:
            cmd += ["--api-key", VLLM_API_KEY]
        if VLLM_MODEL:
            cmd += ["--model", VLLM_MODEL]
        if FORCE_REPROCESS:
            cmd += ["--force"]

        print(f"运行命令: {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd,
                cwd=PIPELINE_ROOT,
                capture_output=True,
                text=True,
                timeout=3600,
            )
            if result.returncode == 0:
                print(f"✓ 成功处理: {folder.name}")
            else:
                print(f"✗ 处理失败: {folder.name}")
                if result.stderr:
                    print(f"错误输出:\n{result.stderr}")
        except subprocess.TimeoutExpired:
            print(f"✗ 超时: {folder.name}")
        except Exception as exc:
            print(f"✗ 异常: {folder.name}")
            print(f"错误: {exc}")

    print(f"\n{'='*80}")
    print("处理完成！")
    print(f"{'='*80}")


if __name__ == "__main__":
    main()
