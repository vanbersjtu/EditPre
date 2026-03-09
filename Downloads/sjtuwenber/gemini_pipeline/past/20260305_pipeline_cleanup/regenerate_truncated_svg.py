#!/usr/bin/env python3
"""
重新生成被截断的 SVG 文件
使用更高的 max_tokens 限制
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# 配置
API_KEY = os.environ.get("GEMINI_API_KEY", "")
API_BASE = "https://generativelanguage.googleapis.com/v1beta"
MODEL = "gemini-3.1-pro-preview"
# 要重新生成的图片
INPUT_PNG = Path(__file__).parent / "input" / "test_3.1" / "image copy 7.png"
OUTPUT_SVG = Path(__file__).parent / "output" / "svg" / "test_3.1" / "image copy 7.svg"
MAX_TOKENS = 65536  # 增加到最大值
TIMEOUT = 600  # 增加超时时间

sys.path.insert(0, str(Path(__file__).parent))
from gemini_svg_pipeline import PROMPT_TEXT, extract_svg


def load_image_as_base64(path: Path) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def build_openai_request(prompt_text: str, image_b64: str) -> dict:
    return {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text.strip()},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                ],
            }
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.2,
    }


def call_openai_api(api_base: str, api_key: str, body: dict, timeout: int) -> str:
    url = f"{api_base}/chat/completions"
    
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
    data = json.loads(raw)
    
    if "error" in data:
        err = data["error"]
        raise RuntimeError(f"API error: {err.get('message', 'unknown')}")
    
    content = data["choices"][0]["message"]["content"]
    if not content.strip():
        raise RuntimeError("API 返回空文本")
    
    return content


def main():
    print("=" * 80)
    print("重新生成被截断的 SVG 文件")
    print("=" * 80)
    
    if not INPUT_PNG.exists():
        print(f"错误：找不到输入文件 {INPUT_PNG}")
        sys.exit(1)
    
    print(f"\n输入：{INPUT_PNG}")
    print(f"输出：{OUTPUT_SVG}")
    print(f"max_tokens: {MAX_TOKENS}")
    print(f"timeout: {TIMEOUT}s")
    
    # 加载图片
    print("\n[1/3] 加载图片...")
    img_b64 = load_image_as_base64(INPUT_PNG)
    print(f"  图片大小：{len(img_b64):,} bytes")
    
    # 构建请求
    print("\n[2/3] 构建请求...")
    body = build_openai_request(PROMPT_TEXT, img_b64)
    
    # 调用 API
    print("\n[3/3] 调用 API...")
    try:
        text = call_openai_api(API_BASE, API_KEY, body, TIMEOUT)
        print("  ✓ API 调用成功")
    except Exception as e:
        print(f"  ✗ API 调用失败：{e}")
        sys.exit(1)
    
    # 提取 SVG
    print("\n[4/4] 提取 SVG...")
    svg = extract_svg(text)
    
    # 保存
    with open(OUTPUT_SVG, "w", encoding="utf-8") as f:
        f.write(svg)
    
    print(f"\n✓ 完成！")
    print(f"  SVG 已保存到：{OUTPUT_SVG}")
    print(f"  SVG 大小：{len(svg):,} bytes")
    print(f"  SVG 行数：{len(svg.splitlines()):,}")
    
    # 验证是否完整
    if svg.strip().endswith("</svg>"):
        print("  ✓ SVG 文件完整（有闭合标签）")
    else:
        print("  ✗ 警告：SVG 文件可能被截断（缺少</svg>闭合标签）")


if __name__ == "__main__":
    main()
