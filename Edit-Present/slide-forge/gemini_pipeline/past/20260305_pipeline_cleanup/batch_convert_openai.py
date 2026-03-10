#!/usr/bin/env python3
"""
批量 PNG -> SVG 转换脚本
使用 OpenAI 兼容 API 格式调用 Gemini 3.1 Pro Preview
使用线程池并行处理所有 PNG 图片
"""
import base64
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime
from threading import Lock

# 默认配置
PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_API_BASE = "https://cdn.12ai.org/v1"
DEFAULT_MODEL = "gemini-3.1-pro-preview"
MODEL = DEFAULT_MODEL
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config" / "runtime_api_config.json"
DEFAULT_INPUT_DIR = PROJECT_DIR / "input" / "test_3.1"
DEFAULT_OUTPUT_SVG_DIR = PROJECT_DIR / "output" / "svg" / "test_3.1"
DEFAULT_RETRIES = 5
DEFAULT_TIMEOUT = 300
DEFAULT_MAX_CONCURRENT = 8  # 并行处理的图片数

# 提示词（从 gemini_svg_pipeline 导入）
sys.path.insert(0, str(PROJECT_DIR))
from gemini_svg_pipeline import (
    PROMPT_TEXT,
    extract_svg,
    natural_sort_key,
    sanitize_placeholder_groups,
)

# 线程安全锁
print_lock = Lock()
stats_lock = Lock()


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
                with print_lock:
                    print(f"    .. 请求失败，{sleep_sec}s 后重试 ({i}/{retries})")
                time.sleep(sleep_sec)
            else:
                with print_lock:
                    print(f"    .. 请求失败 ({i}/{retries}): {str(e)[:100]}")
    
    raise RuntimeError(f"重试 {retries} 次后仍然失败：{last_error}")


def process_single_image(
    png_path: Path,
    output_svg_dir: Path,
    api_base: str,
    api_key: str,
    model: str,
    retries: int,
    timeout: int,
    idx: int,
    total: int,
) -> dict:
    """处理单张图片"""
    result = {
        "input": str(png_path),
        "output": None,
        "success": False,
        "error": None,
        "svg_size": 0,
        "svg_lines": 0,
        "duration": 0,
    }
    
    start_time = time.time()
    
    try:
        # 加载图片
        img_b64 = load_image_as_base64(png_path)
        
        # 构建请求体
        body = build_openai_request(PROMPT_TEXT, img_b64)
        body["model"] = model
        
        # 调用 API
        text = call_with_retries(api_base, api_key, body, retries, timeout)
        
        # 提取 SVG
        svg = extract_svg(text)
        svg = sanitize_placeholder_groups(svg)
        
        # 保存 SVG
        output_svg = output_svg_dir / f"{png_path.stem}.svg"
        output_svg.parent.mkdir(parents=True, exist_ok=True)
        with open(output_svg, "w", encoding="utf-8") as f:
            f.write(svg)
        
        result["output"] = str(output_svg)
        result["success"] = True
        result["svg_size"] = len(svg)
        result["svg_lines"] = len(svg.splitlines())
        
    except Exception as e:
        result["error"] = str(e)
    
    result["duration"] = time.time() - start_time
    
    # 线程安全打印
    with print_lock:
        if result["success"]:
            print(f"  ✓ [{idx}/{total}] {png_path.name} 成功 -> {result['svg_size']:,} bytes, {result['svg_lines']:,} 行 ({result['duration']:.1f}s)")
        else:
            print(f"  ✗ [{idx}/{total}] {png_path.name} 失败: {result['error'][:80]}")
    
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量 PNG -> SVG 转换工具 (OpenAI 兼容 API - 并行模式)")
    parser.add_argument("--input-dir", default=str(DEFAULT_INPUT_DIR), help="输入 PNG 目录")
    parser.add_argument("--output-svg-dir", default=str(DEFAULT_OUTPUT_SVG_DIR), help="输出 SVG 目录")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"运行配置文件路径（默认: {DEFAULT_CONFIG_PATH}）",
    )
    parser.add_argument("--api-key", default="", help="OpenAI 兼容 API Key（默认读取 OPENAI_API_KEY）")
    parser.add_argument("--api-base", default="", help="OpenAI 兼容 API Base（默认从 config 读取）")
    parser.add_argument("--model", default="", help="模型名（默认从 config 读取）")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="失败重试次数")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="请求超时秒数")
    parser.add_argument("--max-concurrent", type=int, default=DEFAULT_MAX_CONCURRENT, help="并发线程数")
    return parser


def main():
    args = build_parser().parse_args()

    runtime_cfg = {}
    config_path = Path(args.config).expanduser().resolve() if args.config else None
    if config_path and config_path.exists():
        try:
            runtime_cfg = json.loads(config_path.read_text(encoding="utf-8"))
            print(f"已加载配置: {config_path}")
        except Exception as e:
            print(f"警告：读取配置失败 {config_path}: {e}")

    api_key = (
        args.api_key
        or str(runtime_cfg.get("OPENAI_API_KEY") or "").strip()
        or os.environ.get("OPENAI_API_KEY", "")
    )
    if not api_key:
        print("错误：缺少 API key，请传 --api-key 或设置 OPENAI_API_KEY")
        sys.exit(1)

    input_dir = Path(args.input_dir).expanduser().resolve()
    output_svg_dir = Path(args.output_svg_dir).expanduser().resolve()
    api_base = (
        args.api_base.strip()
        or str(runtime_cfg.get("DEFAULT_API_BASE") or "").strip()
        or DEFAULT_API_BASE
    )
    model = (
        args.model.strip()
        or str(runtime_cfg.get("DEFAULT_MODEL") or "").strip()
        or DEFAULT_MODEL
    )
    retries = args.retries
    timeout = args.timeout
    max_concurrent = max(1, args.max_concurrent)

    print("=" * 80)
    print("批量 PNG -> SVG 转换工具 (OpenAI 兼容 API - 并行模式)")
    print("=" * 80)
    
    # 检查输入目录
    if not input_dir.exists():
        print(f"\n错误：找不到输入目录 {input_dir}")
        sys.exit(1)
    
    # 收集所有 PNG 文件
    png_files = sorted(
        [p for p in input_dir.rglob("*.png") if not p.name.startswith("._")],
        key=lambda p: natural_sort_key(str(p.relative_to(input_dir))),
    )
    
    if not png_files:
        print(f"\n未找到 PNG 文件：{input_dir}")
        sys.exit(0)
    
    print(f"\n输入目录：{input_dir}")
    print(f"输出目录：{output_svg_dir}")
    print(f"发现 {len(png_files)} 张 PNG 图片")
    print(f"使用模型：{model}")
    print(f"API 端点：{api_base}/chat/completions")
    print(f"并行数量：{max_concurrent}")
    print(f"重试次数：{retries}, 超时：{timeout}s")
    
    # 处理统计
    stats = {
        "total": len(png_files),
        "success": 0,
        "failed": 0,
        "total_size": 0,
        "total_duration": 0,
    }
    
    results = []
    start_time = time.time()
    
    # 批量处理 - 使用线程池并行
    print("\n" + "=" * 80)
    print("开始并行处理...")
    print("=" * 80)
    
    with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
        # 提交所有任务
        future_to_png = {
            executor.submit(
                process_single_image,
                png_path,
                output_svg_dir,
                api_base,
                api_key,
                model,
                retries,
                timeout,
                idx,
                len(png_files),
            ): png_path
            for idx, png_path in enumerate(png_files, 1)
        }
        
        # 收集结果
        for future in as_completed(future_to_png):
            png_path = future_to_png[future]
            try:
                result = future.result()
                results.append(result)
                
                # 更新统计
                with stats_lock:
                    if result["success"]:
                        stats["success"] += 1
                        stats["total_size"] += result["svg_size"]
                    else:
                        stats["failed"] += 1
                    stats["total_duration"] += result["duration"]
                    
                    # 显示进度
                    done = stats["success"] + stats["failed"]
                    print(f"\n进度: {done}/{stats['total']} 完成, {stats['success']} 成功, {stats['failed']} 失败")
                    
            except Exception as e:
                with print_lock:
                    print(f"  ✗ [{png_path.name}] 异常: {e}")
    
    total_time = time.time() - start_time
    
    # 打印统计
    print("\n" + "=" * 80)
    print("处理完成！统计信息：")
    print("=" * 80)
    print(f"总图片数：{stats['total']}")
    print(f"成功：{stats['success']} ({stats['success']/stats['total']*100:.1f}%)")
    print(f"失败：{stats['failed']} ({stats['failed']/stats['total']*100:.1f}%)")
    print(f"总 SVG 大小：{stats['total_size']:,} bytes ({stats['total_size']/1024:.1f} KB)")
    print(f"总耗时：{total_time:.1f}s")
    print(f"理论并行节省时间：{stats['total_duration'] - total_time:.1f}s")
    
    # 保存处理报告
    output_svg_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_svg_dir / f"processing_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_data = {
        "timestamp": datetime.now().isoformat(),
        "input_dir": str(input_dir),
        "output_dir": str(output_svg_dir),
        "model": model,
        "parallel": max_concurrent,
        "stats": stats,
        "results": results,
    }
    
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, ensure_ascii=False, indent=2)
    
    print(f"\n处理报告已保存到：{report_path}")
    print("=" * 80)


if __name__ == "__main__":
    main()
