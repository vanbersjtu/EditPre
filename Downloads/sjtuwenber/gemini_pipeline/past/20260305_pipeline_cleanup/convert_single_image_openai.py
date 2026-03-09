#!/usr/bin/env python3
"""
单张图片 PNG -> SVG 转换脚本
使用 OpenAI 兼容 API 格式调用 Gemini 3.1 Pro Preview
"""
import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# 配置
API_KEY = os.environ.get("GEMINI_API_KEY", "")
# OpenAI 兼容端点
API_BASE = "https://generativelanguage.googleapis.com/v1beta"
MODEL = "gemini-3.1-pro-preview"
INPUT_PNG = Path(__file__).parent / "input" / "image.png"
OUTPUT_SVG = Path(__file__).parent / "output" / "svg" / "image.svg"
RETRIES = 5
TIMEOUT = 300

# 提示词（从 gemini_svg_pipeline 导入）
sys.path.insert(0, str(Path(__file__).parent))
from gemini_svg_pipeline import PROMPT_TEXT, extract_svg


def load_image_as_base64(path: Path) -> str:
    """加载图片为 base64"""
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def build_openai_request(prompt_text: str, image_b64: str) -> dict:
    """构建 OpenAI 兼容格式的请求体"""
    return {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt_text.strip(),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{image_b64}",
                        },
                    },
                ],
            }
        ],
        "max_tokens": 32768,
        "temperature": 0.2,
    }


def call_openai_api(api_base: str, api_key: str, body: dict, timeout: int = 300) -> str:
    """使用 OpenAI 兼容格式调用 API"""
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
    
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
        data = json.loads(raw)
        
        # OpenAI 格式响应解析
        if "error" in data:
            err = data["error"]
            code = err.get("code")
            msg = err.get("message", "unknown error")
            raise RuntimeError(f"API error ({code}): {msg}")
        
        # 提取响应文本
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("API 返回空响应")
        
        message = choices[0].get("message", {})
        content = message.get("content", "")
        
        if not content.strip():
            raise RuntimeError("API 返回空文本")
        
        return content
        
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        try:
            error_data = json.loads(error_body)
            error_msg = error_data.get("error", {}).get("message", str(e))
        except:
            error_msg = str(e)
        raise RuntimeError(f"HTTP 错误：{error_msg}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络错误：{e}")
    except Exception as e:
        raise RuntimeError(f"请求失败：{e}")


def call_with_retries(api_base: str, api_key: str, body: dict, retries: int, timeout: int) -> str:
    """带重试的 API 调用"""
    last_error = None
    
    for i in range(1, retries + 1):
        try:
            return call_openai_api(api_base, api_key, body, timeout)
        except Exception as e:
            last_error = e
            if i < retries:
                sleep_sec = min(2 ** i, 8)
                print(f"  .. 请求失败，{sleep_sec}s 后重试 ({i}/{retries}): {e}")
                time.sleep(sleep_sec)
            else:
                print(f"  .. 请求失败 ({i}/{retries}): {e}")
    
    raise RuntimeError(f"重试 {retries} 次后仍然失败：{last_error}")


def main():
    # 检查输入文件
    if not INPUT_PNG.exists():
        print(f"错误：找不到输入文件 {INPUT_PNG}")
        sys.exit(1)
    
    print(f"处理文件：{INPUT_PNG}")
    print(f"使用模型：{MODEL}")
    print(f"API 端点：{API_BASE}/chat/completions")
    print(f"API Key: {API_KEY[:10]}...{API_KEY[-5:]}")
    
    # 加载图片
    print("\n[1/4] 加载图片...")
    img_b64 = load_image_as_base64(INPUT_PNG)
    print(f"  图片大小：{len(img_b64):,} bytes (base64)")
    
    # 构建请求体
    print("\n[2/4] 构建 OpenAI 格式请求...")
    body = build_openai_request(PROMPT_TEXT, img_b64)
    print(f"  model: {body['model']}")
    print(f"  max_tokens: {body['max_tokens']}")
    print(f"  temperature: {body['temperature']}")
    
    # 调用 API
    print(f"\n[3/4] 调用 API (retries={RETRIES}, timeout={TIMEOUT})...")
    try:
        text = call_with_retries(API_BASE, API_KEY, body, RETRIES, TIMEOUT)
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
