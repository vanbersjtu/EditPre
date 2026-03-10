#!/usr/bin/env python3
"""
单张图片 PNG -> SVG 转换脚本
使用 Gemini API 将单张 PNG 图片转换为 SVG
"""
import base64
import json
import os
import sys
from pathlib import Path

# 添加父目录到路径，以便导入 gemini_svg_pipeline
sys.path.insert(0, str(Path(__file__).parent))
from gemini_svg_pipeline import (
    load_image_as_base64,
    build_request_body,
    call_gemini,
    extract_svg,
    PROMPT_TEXT,
)

# 配置
API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = "gemini-3-pro-preview"
INPUT_PNG = Path(__file__).parent / "input" / "image.png"
OUTPUT_SVG = Path(__file__).parent / "output" / "svg" / "image.svg"
RETRIES = 5
TIMEOUT = 300


def main():
    # 检查输入文件
    if not INPUT_PNG.exists():
        print(f"错误：找不到输入文件 {INPUT_PNG}")
        sys.exit(1)
    
    print(f"处理文件：{INPUT_PNG}")
    print(f"使用模型：{MODEL}")
    print(f"API Key: {API_KEY[:10]}...{API_KEY[-5:]}")
    
    # 构建 API 端点
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    
    # 加载图片
    print("\n[1/3] 加载图片...")
    img_b64 = load_image_as_base64(INPUT_PNG)
    print(f"  图片大小：{len(img_b64)} bytes (base64)")
    
    # 构建请求体
    print("\n[2/3] 构建请求...")
    body = build_request_body(PROMPT_TEXT, img_b64)
    print(f"  max_output_tokens: {body['generation_config']['max_output_tokens']}")
    
    # 调用 Gemini API
    print(f"\n[3/3] 调用 Gemini API (retries={RETRIES}, timeout={TIMEOUT})...")
    try:
        text = call_gemini(
            api_key=API_KEY,
            endpoint=endpoint,
            model=MODEL,
            body=body,
            retries=RETRIES,
            timeout=TIMEOUT,
        )
        print("  ✓ API 调用成功")
    except Exception as e:
        print(f"  ✗ API 调用失败：{e}")
        sys.exit(1)
    
    # 提取 SVG
    print("\n[4/4] 提取 SVG...")
    svg = extract_svg(text)
    
    # 保存 SVG
    OUTPUT_SVG.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_SVG, "w", encoding="utf-8") as f:
        f.write(svg)
    
    print(f"\n✓ 完成！")
    print(f"  SVG 已保存到：{OUTPUT_SVG}")
    
    # 显示 SVG 文件信息
    svg_size = len(svg)
    print(f"  SVG 大小：{svg_size:,} bytes")
    print(f"  SVG 行数：{len(svg.splitlines()):,}")


if __name__ == "__main__":
    main()
