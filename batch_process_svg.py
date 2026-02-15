#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三阶段并行处理 SVG：items 提取、LLM 语义 plan、回填与可视化。
"""

import argparse
import atexit
import json
import re
import subprocess
import sys
import tempfile
import time
import threading
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from multiprocessing import get_context
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from data_process_src.pipeline.svg_text_semantic import (
    SVG_NS,
    ROLE_SET,
    RateLimiter,
    apply_plan_to_svg,
    build_prompt,
    call_text_llm_with_retries,
    ensure_text_ids,
    get_canvas_size,
    merge_adjacent_textboxes,
    normalize_tree_plan,
    parse_json_from_text,
    read_text_xml,
)

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None

INPUT_BASE = Path("/mnt/cache/liwenbo/PPT2SVG-SlideSVG/tes_foler_placeholder")
OUTPUT_ROOT = Path("/mnt/cache/liwenbo/PPT2SVG-SlideSVG/tes_foler_semantic_google")
VIS_SCRIPT = Path("/mnt/cache/liwenbo/PPT2SVG-SlideSVG/data_process_src/pipeline/visualize_semantic_plan.py")

XML_DECL_RE = re.compile(r"<\?xml[^>]*\?>", re.I)
PLAYWRIGHT_JS = r"""
(() => {
  const svg = document.querySelector('svg');
  const items = [];
  if (!svg) return {items};
  const texts = Array.from(svg.querySelectorAll('text[data-extract-id]')).filter(
    el => !el.closest('g[data-role=\"image-placeholder\"]')
  );

  function toRootBBox(el) {
    try {
      const bb = el.getBBox();
      const ctm = el.getCTM();
      if (!ctm) {
        return {x: bb.x, y: bb.y, w: bb.width, h: bb.height, ctm: null};
      }
      function xform(x, y) {
        return {
          x: ctm.a * x + ctm.c * y + ctm.e,
          y: ctm.b * x + ctm.d * y + ctm.f
        };
      }
      const p1 = xform(bb.x, bb.y);
      const p2 = xform(bb.x + bb.width, bb.y);
      const p3 = xform(bb.x, bb.y + bb.height);
      const p4 = xform(bb.x + bb.width, bb.y + bb.height);
      const xs = [p1.x, p2.x, p3.x, p4.x];
      const ys = [p1.y, p2.y, p3.y, p4.y];
      const minX = Math.min(...xs);
      const maxX = Math.max(...xs);
      const minY = Math.min(...ys);
      const maxY = Math.max(...ys);
      return {x: minX, y: minY, w: (maxX - minX), h: (maxY - minY), ctm: ctm};
    } catch (e) {
      return {x: 0, y: 0, w: 0, h: 0, ctm: null};
    }
  }

  function getStyle(el) {
    const cs = window.getComputedStyle(el);
    return {
      fontFamily: cs.fontFamily || '',
      fontSize: cs.fontSize || '',
      fontWeight: cs.fontWeight || '',
      fontStyle: cs.fontStyle || '',
      fill: cs.fill || '',
      opacity: cs.opacity || '',
      letterSpacing: cs.letterSpacing || '',
      textAnchor: cs.textAnchor || '',
      dominantBaseline: cs.dominantBaseline || ''
    };
  }

  for (const el of texts) {
    const id = el.getAttribute('data-extract-id');
    const textContent = (el.textContent || '').replace(/\u00a0/g, ' ').trim();
    const bbox = toRootBBox(el);
    const style = getStyle(el);
    items.push({
      id,
      text: textContent,
      bbox: {x: bbox.x, y: bbox.y, w: bbox.w, h: bbox.h},
      ctm: bbox.ctm ? {
        a: bbox.ctm.a, b: bbox.ctm.b, c: bbox.ctm.c,
        d: bbox.ctm.d, e: bbox.ctm.e, f: bbox.ctm.f
      } : null,
      style
    });
  }
  return {items};
})();
"""

PW_PLAYWRIGHT = None
PW_BROWSER = None
PW_PAGE = None

APIS = [
    {"api_key": "key1"},
    {"api_key": "key2"},
    {"api_key": "key3"},
]

COMMON = {
    "base_url": "https://generativelanguage.googleapis.com",
    "model": "gemini-3-pro-preview",
    "max_tokens": 4096,
    "temperature": 0.2,
    "timeout": 120,
    "retries": 2,
    "qps": 8,
}


def task_key(folder: str, stem: str) -> str:
    return f"{folder}__{stem}"


def load_json(path: Path) -> Optional[Dict[str, object]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def init_stage1_worker() -> None:
    global PW_PLAYWRIGHT, PW_BROWSER, PW_PAGE
    if sync_playwright is None:
        raise RuntimeError("playwright is not installed. Run: pip install playwright && playwright install chromium")
    PW_PLAYWRIGHT = sync_playwright().start()
    PW_BROWSER = PW_PLAYWRIGHT.chromium.launch()
    PW_PAGE = PW_BROWSER.new_page(viewport={"width": 1400, "height": 900})
    atexit.register(close_stage1_worker)


def close_stage1_worker() -> None:
    global PW_PLAYWRIGHT, PW_BROWSER, PW_PAGE
    try:
        if PW_PAGE is not None:
            PW_PAGE.close()
    except Exception:
        pass
    try:
        if PW_BROWSER is not None:
            PW_BROWSER.close()
    except Exception:
        pass
    try:
        if PW_PLAYWRIGHT is not None:
            PW_PLAYWRIGHT.stop()
    except Exception:
        pass
    PW_PAGE = None
    PW_BROWSER = None
    PW_PLAYWRIGHT = None


def extract_items_with_playwright_session(svg_path: Path) -> List[Dict[str, object]]:
    if PW_PAGE is None:
        raise RuntimeError("playwright session not initialized")
    svg_text = svg_path.read_text(encoding="utf-8")
    svg_text = XML_DECL_RE.sub("", svg_text).strip()
    html = (
        "<!doctype html><html><head><meta charset=\"utf-8\"></head>"
        "<body style=\"margin:0;padding:0;background:white;\">"
        f"<div id=\"wrap\">{svg_text}</div></body></html>"
    )
    PW_PAGE.set_content(html, wait_until="load")
    result = PW_PAGE.evaluate(PLAYWRIGHT_JS)
    if isinstance(result, dict):
        return result.get("items", [])
    return []


def read_tasks(tasks_path: Path) -> Tuple[List[Dict[str, str]], set]:
    tasks: List[Dict[str, str]] = []
    seen = set()
    if not tasks_path.exists():
        return tasks, seen
    with tasks_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            folder = item.get("folder")
            stem = item.get("stem")
            if not isinstance(folder, str) or not isinstance(stem, str):
                continue
            key = task_key(folder, stem)
            if key in seen:
                continue
            seen.add(key)
            tasks.append(item)
    return tasks, seen


def stage1_worker(task: Dict[str, str]) -> Tuple[str, str, Optional[str]]:
    folder = task["folder"]
    stem = task["stem"]
    svg_path = Path(task["svg_path"])
    items_path = Path(task["items_path"])
    key = task_key(folder, stem)

    try:
        tree = ET.parse(svg_path)
        root = tree.getroot()
        ET.register_namespace("", SVG_NS)
        id_map = ensure_text_ids(root)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".svg") as tmp:
            tmp_path = Path(tmp.name)
            tree.write(tmp_path, encoding="utf-8", xml_declaration=True)

        items = extract_items_with_playwright_session(tmp_path)
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass

        for item in items:
            item_id = item.get("id")
            elem = id_map.get(item_id)
            if elem is not None:
                item["text_xml"] = read_text_xml(elem)

        items_doc = {
            "canvas": get_canvas_size(root),
            "items": items,
        }
        items_path.parent.mkdir(parents=True, exist_ok=True)
        items_path.write_text(json.dumps(items_doc, ensure_ascii=False, indent=2), encoding="utf-8")
        return key, str(svg_path), None
    except Exception as exc:
        return key, str(svg_path), f"{type(exc).__name__}: {exc}"


def stage3_worker(task: Dict[str, str]) -> Tuple[str, str, Optional[str]]:
    key = task["key"]
    folder_output = task["folder_output"]
    svg_path = Path(task["svg_path"])
    output_svg = Path(task["output_svg"])
    items_path = Path(task["items_path"])
    plan_path = Path(task["plan_path"])
    raw_path = Path(task["raw_path"])
    meta_plans_dir = Path(task["meta_plans_dir"])
    meta_raw_dir = Path(task["meta_raw_dir"])

    try:
        plan = load_json(plan_path)
        if not isinstance(plan, dict):
            return key, folder_output, "plan.json invalid"
        items_doc = load_json(items_path)
        if not isinstance(items_doc, dict):
            return key, folder_output, "items.json invalid"
        items = items_doc.get("items") if isinstance(items_doc.get("items"), list) else []

        tree = ET.parse(svg_path)
        root = tree.getroot()
        ET.register_namespace("", SVG_NS)
        ensure_text_ids(root)
        apply_plan_to_svg(tree, items, plan, pad=0.0, keep_ids=False)

        output_svg.parent.mkdir(parents=True, exist_ok=True)
        tree.write(output_svg, encoding="utf-8", xml_declaration=True)

        meta_plans_dir.mkdir(parents=True, exist_ok=True)
        meta_raw_dir.mkdir(parents=True, exist_ok=True)
        (meta_plans_dir / plan_path.name.split("__", 1)[-1]).write_text(
            json.dumps(plan, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if raw_path.exists():
            raw_text = raw_path.read_text(encoding="utf-8")
            (meta_raw_dir / raw_path.name.split("__", 1)[-1]).write_text(raw_text, encoding="utf-8")
        return key, folder_output, None
    except Exception as exc:
        return key, folder_output, f"{type(exc).__name__}: {exc}"


def visualize_worker(folder_output: str) -> Tuple[str, Optional[str]]:
    try:
        result = subprocess.run(
            [sys.executable, str(VIS_SCRIPT), "--input", folder_output],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return folder_output, result.stderr.strip() or "visualize failed"
        return folder_output, None
    except Exception as exc:
        return folder_output, f"{type(exc).__name__}: {exc}"


def stage1_extract_items(force: bool, workers: int) -> Tuple[int, int, float]:
    global_cache = OUTPUT_ROOT / "global_cache"
    tasks_path = global_cache / "tasks.jsonl"
    global_cache.mkdir(parents=True, exist_ok=True)

    _, existing = read_tasks(tasks_path)
    extraction_tasks: List[Dict[str, str]] = []
    total_svgs = 0
    appended = 0

    with tasks_path.open("a", encoding="utf-8") as f:
        for folder in sorted(INPUT_BASE.iterdir()):
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            output_folder = OUTPUT_ROOT / folder.name
            items_dir = output_folder / "meta" / "items"
            for svg_path in sorted(folder.glob("*.SVG")):
                if svg_path.name.startswith("._"):
                    continue
                stem = svg_path.stem
                key = task_key(folder.name, stem)
                items_path = items_dir / f"{stem}.json"
                task = {
                    "folder": folder.name,
                    "stem": stem,
                    "svg_path": str(svg_path),
                    "items_path": str(items_path),
                }
                total_svgs += 1
                if key not in existing:
                    f.write(json.dumps(task, ensure_ascii=False) + "\n")
                    existing.add(key)
                    appended += 1
                if force or not items_path.exists():
                    extraction_tasks.append(task)

    if not extraction_tasks:
        print("Stage 1: no items need extraction")
        return 0, total_svgs, 0.0

    start = time.time()
    processed = 0
    failures = 0
    ctx = get_context("spawn")
    with ctx.Pool(processes=workers, initializer=init_stage1_worker) as pool:
        for key, svg_path, error in pool.imap_unordered(stage1_worker, extraction_tasks):
            processed += 1
            if error:
                failures += 1
                print(f"Stage 1 error {key}: {error}")
            if processed % 100 == 0 or processed == len(extraction_tasks):
                print(f"Stage 1 progress: {processed}/{len(extraction_tasks)}")

    duration = time.time() - start
    print(f"Stage 1 appended tasks: {appended}, extracted: {processed}, failed: {failures}")
    return processed, total_svgs, duration


def stage2_generate_plans(force: bool, workers_per_key: int) -> Tuple[int, float]:
    global_cache = OUTPUT_ROOT / "global_cache"
    tasks_path = global_cache / "tasks.jsonl"
    tasks, _ = read_tasks(tasks_path)
    if not tasks:
        print("Stage 2: tasks.jsonl is empty")
        return 0, 0.0

    plans_dir = global_cache / "plans"
    raw_dir = global_cache / "raw"
    failed_path = global_cache / "failed.jsonl"
    plans_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    prompt = build_prompt(ROLE_SET)
    limiters = [RateLimiter(COMMON["qps"]) for _ in APIS]
    semaphores = [threading.Semaphore(workers_per_key) for _ in APIS]
    max_tokens = int(COMMON["max_tokens"])

    tasks_for_stage2 = []
    missing_items = 0
    for idx, task in enumerate(tasks):
        folder = task.get("folder")
        stem = task.get("stem")
        if not isinstance(folder, str) or not isinstance(stem, str):
            continue
        items_path_value = task.get("items_path")
        if not isinstance(items_path_value, str) or not Path(items_path_value).exists():
            missing_items += 1
            continue
        plan_path = plans_dir / f"{folder}__{stem}.json"
        raw_path = raw_dir / f"{folder}__{stem}.txt"
        if not force and plan_path.exists():
            continue
        task_copy = dict(task)
        task_copy["plan_path"] = str(plan_path)
        task_copy["raw_path"] = str(raw_path)
        task_copy["api_idx"] = idx % len(APIS)
        tasks_for_stage2.append(task_copy)

    if not tasks_for_stage2:
        if missing_items:
            print(f"Stage 2 skipped {missing_items} tasks with missing items.json")
        print("Stage 2: no plans need generation")
        return 0, 0.0

    if missing_items:
        print(f"Stage 2 skipped {missing_items} tasks with missing items.json")

    total = len(tasks_for_stage2)
    processed = 0
    start = time.time()
    lock = threading.Lock()

    def record_failure(task: Dict[str, str], error: str) -> None:
        entry = {
            "folder": task.get("folder"),
            "stem": task.get("stem"),
            "svg_path": task.get("svg_path"),
            "error": error,
        }
        with lock:
            with failed_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def worker(task: Dict[str, str]) -> Optional[str]:
        api_idx = task["api_idx"]
        limiter = limiters[api_idx]
        api_key = APIS[api_idx]["api_key"]
        items_path = Path(task["items_path"])
        raw_path = Path(task["raw_path"])
        if not items_path.exists():
            return "items.json missing"
        items_doc = load_json(items_path)
        if not isinstance(items_doc, dict):
            return "items.json invalid"
        items = items_doc.get("items") if isinstance(items_doc.get("items"), list) else []
        item_ids = [it.get("id") for it in items if isinstance(it, dict) and isinstance(it.get("id"), str)]

        with semaphores[api_idx]:
            response, error = call_text_llm_with_retries(
                COMMON["base_url"],
                api_key,
                COMMON["model"],
                prompt,
                items_doc,
                max_tokens,
                float(COMMON["temperature"]),
                int(COMMON["timeout"]),
                int(COMMON["retries"]),
                limiter,
            )

        if response:
            raw_path.write_text(response, encoding="utf-8")
        if not response:
            return error or "llm request failed"
        plan = parse_json_from_text(response)
        if not plan:
            with semaphores[api_idx]:
                retry_resp, retry_err = call_text_llm_with_retries(
                    COMMON["base_url"],
                    api_key,
                    COMMON["model"],
                    prompt,
                    items_doc,
                    max_tokens * 2,
                    float(COMMON["temperature"]),
                    int(COMMON["timeout"]),
                    int(COMMON["retries"]),
                    limiter,
                )
            if retry_resp:
                raw_path.write_text(retry_resp, encoding="utf-8")
                plan = parse_json_from_text(retry_resp)
            if not plan:
                return retry_err or "parse json failed"

        normalized = normalize_tree_plan(plan, item_ids)
        merged = merge_adjacent_textboxes(normalized, items)
        plan_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        return None

    max_workers = workers_per_key * len(APIS)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(worker, task): task for task in tasks_for_stage2}
        for future in as_completed(future_map):
            task = future_map[future]
            error = None
            try:
                error = future.result()
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            if error:
                record_failure(task, error)
            with lock:
                processed += 1
                if processed % 50 == 0 or processed == total:
                    print(f"Stage 2 progress: {processed}/{total}")

    duration = time.time() - start
    return processed, duration


def stage3_apply_plans(force: bool, workers: int) -> Tuple[int, float]:
    global_cache = OUTPUT_ROOT / "global_cache"
    tasks_path = global_cache / "tasks.jsonl"
    tasks, _ = read_tasks(tasks_path)
    if not tasks:
        print("Stage 3: tasks.jsonl is empty")
        return 0, 0.0

    plans_dir = global_cache / "plans"
    raw_dir = global_cache / "raw"
    tasks_for_stage3: List[Dict[str, str]] = []
    missing_items = 0

    for task in tasks:
        folder = task.get("folder")
        stem = task.get("stem")
        svg_path = task.get("svg_path")
        items_path = task.get("items_path")
        if not isinstance(folder, str) or not isinstance(stem, str) or not isinstance(svg_path, str):
            continue
        if not isinstance(items_path, str) or not Path(items_path).exists():
            missing_items += 1
            continue
        plan_path = plans_dir / f"{folder}__{stem}.json"
        raw_path = raw_dir / f"{folder}__{stem}.txt"
        if not plan_path.exists():
            continue
        output_folder = OUTPUT_ROOT / folder
        output_svg = output_folder / Path(svg_path).name
        meta_plans_dir = output_folder / "meta" / "plans"
        meta_raw_dir = output_folder / "meta" / "raw"
        meta_plan_path = meta_plans_dir / f"{stem}.json"
        if not force and output_svg.exists() and meta_plan_path.exists():
            continue
        tasks_for_stage3.append(
            {
                "key": task_key(folder, stem),
                "svg_path": svg_path,
                "output_svg": str(output_svg),
                "items_path": items_path or str(output_folder / "meta" / "items" / f"{stem}.json"),
                "plan_path": str(plan_path),
                "raw_path": str(raw_path),
                "meta_plans_dir": str(meta_plans_dir),
                "meta_raw_dir": str(meta_raw_dir),
                "folder_output": str(output_folder),
            }
        )

    if not tasks_for_stage3:
        if missing_items:
            print(f"Stage 3 skipped {missing_items} tasks with missing items.json")
        print("Stage 3: no SVGs need plan application")
        return 0, 0.0

    if missing_items:
        print(f"Stage 3 skipped {missing_items} tasks with missing items.json")

    total = len(tasks_for_stage3)
    processed = 0
    failures = 0
    start = time.time()
    ctx = get_context("spawn")

    folder_counts = Counter(task["folder_output"] for task in tasks_for_stage3)
    folder_remaining = dict(folder_counts)
    vis_total = len(folder_counts)
    vis_pool = None
    vis_processed = 0
    vis_start = None
    vis_lock = threading.Lock()

    def vis_callback(result: Tuple[str, Optional[str]]) -> None:
        nonlocal vis_processed
        folder_output, error = result
        with vis_lock:
            vis_processed += 1
            if error:
                print(f"Visualize error {folder_output}: {error}")
            if vis_processed % 10 == 0 or vis_processed == vis_total:
                print(f"Visualize progress: {vis_processed}/{vis_total}")

    if vis_total:
        vis_start = time.time()
        vis_pool = ctx.Pool(processes=min(workers, vis_total))

    with ctx.Pool(processes=workers) as pool:
        for key, folder_output, error in pool.imap_unordered(stage3_worker, tasks_for_stage3):
            processed += 1
            if error:
                failures += 1
                print(f"Stage 3 error {key}: {error}")
            remaining = folder_remaining.get(folder_output)
            if remaining is not None:
                remaining -= 1
                if remaining <= 0:
                    folder_remaining.pop(folder_output, None)
                    if vis_pool is not None:
                        vis_pool.apply_async(visualize_worker, (folder_output,), callback=vis_callback)
                else:
                    folder_remaining[folder_output] = remaining
            if processed % 100 == 0 or processed == total:
                print(f"Stage 3 progress: {processed}/{total}")

    if vis_pool is not None:
        vis_pool.close()
        vis_pool.join()
        if vis_start is not None:
            vis_duration = time.time() - vis_start
            print(f"Stage 3 visualize time: {vis_duration:.1f}s")

    duration = time.time() - start
    print(f"Stage 3 processed: {processed}, failed: {failures}")
    return processed, duration


def format_avg(duration: float, count: int) -> str:
    if count <= 0:
        return "n/a"
    return f"{duration / count:.3f}s/svg"


def main() -> None:
    parser = argparse.ArgumentParser(description="三阶段并行处理 SVG")
    parser.add_argument("--force", action="store_true", help="强制重新处理")
    parser.add_argument("--stage", type=int, choices=[1, 2, 3], help="只运行指定 stage")
    parser.add_argument("--stage1-workers", type=int, default=64, help="Stage 1 进程数")
    parser.add_argument("--stage2-workers", type=int, default=8, help="Stage 2 每个 key 线程数")
    parser.add_argument("--stage3-workers", type=int, default=80, help="Stage 3 进程数")
    args = parser.parse_args()

    if not INPUT_BASE.exists():
        raise SystemExit(f"input not found: {INPUT_BASE}")

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    stage_stats: Dict[str, Tuple[int, float]] = {}
    total_svg_count = 0
    overall_start = time.time()

    if args.stage in (None, 1):
        processed, total_svgs, duration = stage1_extract_items(args.force, args.stage1_workers)
        stage_stats["stage1"] = (processed, duration)
        total_svg_count = max(total_svg_count, total_svgs)

    if args.stage in (None, 2):
        processed, duration = stage2_generate_plans(args.force, args.stage2_workers)
        stage_stats["stage2"] = (processed, duration)

    if args.stage in (None, 3):
        processed, duration = stage3_apply_plans(args.force, args.stage3_workers)
        stage_stats["stage3"] = (processed, duration)

    tasks_path = OUTPUT_ROOT / "global_cache" / "tasks.jsonl"
    tasks, _ = read_tasks(tasks_path)
    if tasks:
        total_svg_count = max(total_svg_count, len(tasks))

    overall_duration = time.time() - overall_start
    if "stage1" in stage_stats:
        count, duration = stage_stats["stage1"]
        print(f"Stage 1 time: {duration:.1f}s, avg: {format_avg(duration, count)}")
    if "stage2" in stage_stats:
        count, duration = stage_stats["stage2"]
        print(f"Stage 2 time: {duration:.1f}s, avg: {format_avg(duration, count)}")
    if "stage3" in stage_stats:
        count, duration = stage_stats["stage3"]
        print(f"Stage 3 time: {duration:.1f}s, avg: {format_avg(duration, count)}")
    if total_svg_count:
        print(f"Total time: {overall_duration:.1f}s, avg: {format_avg(overall_duration, total_svg_count)}")
    else:
        print(f"Total time: {overall_duration:.1f}s")


if __name__ == "__main__":
    main()
