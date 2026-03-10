#!/usr/bin/env python3
"""
SVG -> PPTX 转换脚本
使用 source-crop 模式填充图片占位符
"""
import sys
import subprocess
from pathlib import Path

# 配置
INPUT_SVG_DIR = Path(__file__).parent / "output" / "svg" / "test_3.1"
OUTPUT_PPTX = Path(__file__).parent / "output" / "pptx" / "test_3.1_sourcecrop_redraw_notext.pptx"
SOURCE_IMAGE_DIR = Path(__file__).parent / "input" / "test_3.1"
IMAGE_MODEL = "gemini-3.1-flash-image-preview"

def main():
    cmd = [
        sys.executable,
        str(Path(__file__).parent / "svg_to_pptx_pro.py"),
        "--input", str(INPUT_SVG_DIR),
        "--output", str(OUTPUT_PPTX),
        "--refill-placeholders",
        "--refill-mode", "source-crop",
        "--source-image-dir", str(SOURCE_IMAGE_DIR),
        "--source-crop-redraw-no-text",
        "--image-model", IMAGE_MODEL,
        "--image-api-key", "$GEMINI_API_KEY",
    ]
    
    print("=" * 80)
    print("SVG -> PPTX 转换")
    print("=" * 80)
    print(f"\n输入目录：{INPUT_SVG_DIR}")
    print(f"输出文件：{OUTPUT_PPTX}")
    print(f"图片目录：{SOURCE_IMAGE_DIR}")
    print(f"图片模型：{IMAGE_MODEL}")
    print(f"填充模式：source-crop (裁剪源图并重绘)")
    print("\n开始转换...\n")
    
    # 使用 shell=True 以支持环境变量
    shell_cmd = " ".join(cmd)
    shell_cmd = shell_cmd.replace('"$GEMINI_API_KEY"', "$GEMINI_API_KEY")
    
    subprocess.run(shell_cmd, shell=True, check=True)
    
    print("\n" + "=" * 80)
    print("转换完成！")
    print(f"PPTX 已保存到：{OUTPUT_PPTX}")
    print("=" * 80)

if __name__ == "__main__":
    main()
