#!/usr/bin/env python3
"""
单独生成缺失的 SVG 文件
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_KEY = os.environ.get("OPENAI_API_KEY", "")
API_BASE = "https://cdn.12ai.org/v1"
MODEL = "gemini-3.1-pro-preview"

sys.path.insert(0, str(Path(__file__).parent))
from gemini_svg_pipeline import PROMPT_TEXT, extract_svg


def load_image_as_base64(path: Path) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def build_request(prompt: str, image_b64: str) -> dict:
    return {
        "model": MODEL,
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": prompt.strip()},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}}
            ]}
        ],
        "max_tokens": 32768,
        "temperature": 0.2,
    }


def call_api(body: dict, retries: int = 5) -> str:
    url = f"{API_BASE}/chat/completions"
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url=url, data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
        method="POST"
    )
    
    for i in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                data = json.loads(r.read().decode("utf-8"))
            if "error" in data:
                raise RuntimeError(f"API error: {data['error']}")
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            if i < retries:
                print(f"  重试 {i}/{retries}...")
                time.sleep(2 ** i)
            else:
                raise e


def main():
    INPUT_DIR = Path(__file__).parent / "input" / "test_3.1"
    OUTPUT_DIR = Path(__file__).parent / "output" / "svg" / "test_3.1"
    
    missing = ["image copy 4.png", "image copy.png"]
    
    for name in missing:
        png_path = INPUT_DIR / name
        svg_path = OUTPUT_DIR / f"{Path(name).stem}.svg"
        
        print(f"处理: {name}")
        
        try:
            img_b64 = load_image_as_base64(png_path)
            body = build_request(PROMPT_TEXT, img_b64)
            text = call_api(body)
            svg = extract_svg(text)
            
            with open(svg_path, "w", encoding="utf-8") as f:
                f.write(svg)
            
            print(f"  ✓ 成功: {svg_path.name} ({len(svg)} bytes)")
        except Exception as e:
            print(f"  ✗ 失败: {e}")


if __name__ == "__main__":
    main()
