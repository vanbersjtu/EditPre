#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extract text items, ask LLM for semantic grouping, and apply grouping to SVGs.
"""

import argparse
import json
import os
import random
import re
import tempfile
import time
import threading
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - optional dependency
    sync_playwright = None

SVG_NS = "http://www.w3.org/2000/svg"
XML_DECL_RE = re.compile(r"<\\?xml[^>]*\\?>", re.I)

ROLE_SET = [
    "title",
    "subtitle",
    "body",
    "kpi",
    "kpi_unit",
    "callout",
    "bullet",
    "numbered",
    "section",
    "header",
    "footer",
    "footnote",
    "unknown",
]

FONT_SIZE_RE = re.compile(r"([0-9]+(?:\\.[0-9]+)?)")
FAILED_TASKS_NAME = "failed_tasks.json"

def tag_name(elem: ET.Element) -> str:
    return elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag


def format_num(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def load_config(path: Optional[Path]) -> Dict[str, object]:
    if not path or not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def parse_json_from_text(text: str) -> Optional[Dict[str, object]]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n", "", cleaned).strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    
    # 处理多个 JSON 对象用空格拼接的情况（LLM 有时会重复输出）
    # 尝试找到第一个完整的 JSON 对象
    # 策略：从第一个 { 开始，逐步扩展，找到第一个能成功解析的 JSON
    brace_count = 0
    start_idx = -1
    for i, char in enumerate(cleaned):
        if char == '{':
            if start_idx == -1:
                start_idx = i
            brace_count += 1
        elif char == '}':
            brace_count -= 1
            if brace_count == 0 and start_idx != -1:
                # 找到一个完整的 JSON 对象
                candidate = cleaned[start_idx:i+1]
                try:
                    parsed = json.loads(candidate)
                    # 检查是否有必需的字段
                    if isinstance(parsed, dict) and "nodes" in parsed:
                        return parsed
                except Exception:
                    # 解析失败，继续找下一个
                    start_idx = -1
                    brace_count = 0
    
    # 如果上面的方法失败，尝试修复不完整的 JSON（被截断的情况）
    # 策略：找到最后一个完整的节点，然后手动闭合 JSON
    if cleaned.startswith('{') and '"nodes"' in cleaned and not cleaned.rstrip().endswith('}'):
        # 找到最后一个完整的节点（以 } 结尾的节点对象）
        # 从后往前找，找到最后一个完整的节点
        nodes_start = cleaned.find('"nodes": [')
        if nodes_start != -1:
            # 找到最后一个完整的节点对象（以 }, 或 }] 结尾）
            # 尝试找到最后一个完整的节点：以 }, 结尾，且前面有完整的结构
            last_complete_node_pos = -1
            for i in range(len(cleaned) - 1, nodes_start, -1):
                if cleaned[i] == '}' and (i + 1 >= len(cleaned) or cleaned[i + 1] in [',', ']', ' ']):
                    # 检查这个 } 前面是否有完整的节点结构
                    # 简单检查：前面有 "item_ids" 或 "children"
                    before_brace = cleaned[max(0, i - 200):i]
                    if ('"item_ids"' in before_brace or '"children"' in before_brace) and '"id"' in before_brace:
                        last_complete_node_pos = i + 1
                        break
            
            if last_complete_node_pos > 0:
                # 构造完整的 JSON
                candidate = cleaned[:last_complete_node_pos] + ']'
                # 检查是否有 root 字段，如果没有就添加一个默认的
                if '"root"' not in candidate:
                    # 尝试从 nodes 中提取第一个节点的 id 作为 root
                    root_match = re.search(r'"id"\s*:\s*"([^"]+)"', candidate)
                    if root_match:
                        root_id = root_match.group(1)
                        candidate = candidate + f', "root": "{root_id}"'
                    else:
                        candidate = candidate + ', "root": "g-root"'
                candidate = candidate + '}'
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, dict) and "nodes" in parsed:
                        return parsed
                except Exception:
                    pass
    
    # 如果上面的方法失败，回退到原来的正则匹配
    match = re.search(r"\{.*\}", cleaned, re.S)
    if match:
        candidate = match.group(0)
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict) and "nodes" in parsed:
                return parsed
        except Exception:
            pass
    
    # 最后尝试直接解析整个文本
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict) and "nodes" in parsed:
            return parsed
    except Exception:
        pass
    
    return None


class RateLimiter:
    def __init__(self, qps: float):
        self.min_interval = 1.0 / qps if qps > 0 else 0.0
        self.lock = threading.Lock()
        self.next_time = time.monotonic()

    def acquire(self) -> None:
        if self.min_interval <= 0:
            return
        with self.lock:
            now = time.monotonic()
            if now < self.next_time:
                time.sleep(self.next_time - now)
            self.next_time = max(now, self.next_time) + self.min_interval


def call_text_llm(
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    items: Dict[str, object],
    max_tokens: int,
    temperature: float,
    timeout: int,
) -> Tuple[Optional[str], Optional[str]]:
    url = base_url.rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are an SVG text semantic annotator. Output JSON only.",
            },
            {
                "role": "user",
                "content": prompt + "\n\nINPUT_JSON:\n" + json.dumps(items, ensure_ascii=False),
            },
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        err_msg = str(exc)
        try:
            if hasattr(exc, "read"):
                err_body = exc.read().decode("utf-8", errors="ignore")
                if err_body:
                    err_msg = err_msg + "\n" + err_body
        except Exception:
            pass
        return None, err_msg
    try:
        return data["choices"][0]["message"]["content"].strip(), None
    except Exception:
        return None, "Invalid response format."


def call_text_llm_with_retries(
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    items: Dict[str, object],
    max_tokens: int,
    temperature: float,
    timeout: int,
    retries: int,
    limiter: Optional[RateLimiter],
) -> Tuple[Optional[str], Optional[str]]:
    last_error: Optional[str] = None
    for attempt in range(retries + 1):
        if limiter:
            limiter.acquire()
        resp, err = call_text_llm(
            base_url,
            api_key,
            model,
            prompt,
            items,
            max_tokens,
            temperature,
            timeout,
        )
        if resp:
            return resp, None
        if err:
            last_error = err
        if attempt < retries:
            backoff = min(2 ** attempt, 10) + random.random() * 0.3
            time.sleep(backoff)
    return None, last_error


def read_text_xml(elem: ET.Element) -> str:
    parts: List[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in list(elem.iter()):
        if child is elem:
            continue
        if tag_name(child) == "tspan" and child.text:
            parts.append(child.text)
    text = "".join(parts).replace("\u00a0", " ")
    return text.strip()


def ensure_text_ids(root: ET.Element) -> Dict[str, ET.Element]:
    mapping: Dict[str, ET.Element] = {}
    index = 1
    for elem in root.iter():
        if tag_name(elem) != "text":
            continue
        extract_id = elem.get("data-extract-id")
        if not extract_id:
            extract_id = f"t{index:04d}"
            elem.set("data-extract-id", extract_id)
            index += 1
        mapping[extract_id] = elem
    return mapping


def get_canvas_size(root: ET.Element) -> Dict[str, float]:
    width = root.get("width", "")
    height = root.get("height", "")
    view_box = root.get("viewBox", "")

    def parse_len(val: str) -> float:
        val = val.strip()
        for suf in ("px", "pt", "mm", "cm", "in"):
            if val.endswith(suf):
                val = val[: -len(suf)]
                break
        try:
            return float(val)
        except Exception:
            return 0.0

    w = parse_len(width)
    h = parse_len(height)
    if w and h:
        return {"w": w, "h": h}
    if view_box:
        parts = view_box.replace(",", " ").split()
        if len(parts) == 4:
            try:
                return {"w": float(parts[2]), "h": float(parts[3])}
            except Exception:
                pass
    return {"w": 0.0, "h": 0.0}


def extract_items_with_playwright(svg_path: Path) -> List[Dict[str, object]]:
    if sync_playwright is None:
        raise RuntimeError("playwright is not installed. Run: pip install playwright && playwright install chromium")
    svg_text = svg_path.read_text(encoding="utf-8")
    svg_text = XML_DECL_RE.sub("", svg_text).strip()
    html = (
        "<!doctype html><html><head><meta charset=\"utf-8\"></head>"
        "<body style=\"margin:0;padding:0;background:white;\">"
        f"<div id=\"wrap\">{svg_text}</div></body></html>"
    )
    js = r"""
(() => {
  const svg = document.querySelector('svg');
  const items = [];
  if (!svg) return {items};
  const texts = Array.from(svg.querySelectorAll('text[data-extract-id]')).filter(
    el => !el.closest('g[data-role="image-placeholder"]')
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
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.set_content(html, wait_until="load")
        result = page.evaluate(js)
        browser.close()
    return result.get("items", [])


def build_prompt(role_list: List[str]) -> str:
    roles = ", ".join(role_list)
    return (
        "你是 PPT 幻灯片的文本语义标注器。输入是从 SVG 中提取的 text items，"
        "每个 item 包含文本、bbox 与样式（fontSize/fontWeight/fill/fontFamily 等）。"
        "你的目标是把这些 items 分组成多层级的结构："
        "最底层是 textbox（文本框），上层可以是 group（文本块/版块），可以形成任意深度树。"
        f"角色集合仅允许这些：{roles}。"
        "必须输出严格 JSON，结构如下："
        "{\"nodes\":[...],\"root\":\"node-id\"}。"
        "其中 nodes 是节点列表，每个节点类型必须是 textbox 或 group："
        "- textbox 节点字段：id,type=\"textbox\",role,order,item_ids,confidence(可选)"
        "- group 节点字段：id,type=\"group\",role,order,children,confidence(可选)"
        "root 是根节点 id，代表整个文本组合的顶层 group。"
        "分组规则（必须遵守）："
        "1) 同一个 textbox 内的 items 必须字号相同（同一 fontSize）。"
        "2) 如果多行文本属于同一段落或同一标题块，且字号相同、上下相邻、对齐一致，"
        "   必须合并为一个 textbox（允许多行）。"
        "3) subtitle 只能在字号明显小于 title 时使用；"
        "   如果字号相同，就不要拆成 title + subtitle，必须合并为一个多行标题 textbox。"
        "4) 每个 bullet point 应该至少拆成两个 textbox（小标题 + 正文），"
        "   并将它们放进一个 group 里（bullet block）。"
        "5) 若存在主标题/副标题，应与所有 bullet groups 一起归入更高层的 group。"
        "6) 阅读顺序：先上后下，同一行从左到右。"
        "输出约束："
        "每个 item_id 只能出现 0 或 1 次；"
        "children 只能引用 nodes 中存在的 id；"
        "不要产生循环引用；"
        "只输出 nodes 与 root 字段，不要输出额外字段。"
    )


def normalize_plan(
    plan: Dict[str, object],
    item_ids: List[str],
) -> Dict[str, object]:
    items_set = set(item_ids)
    textboxes = []
    used: Dict[str, str] = {}
    tb_index = 1

    raw_textboxes = plan.get("textboxes") if isinstance(plan.get("textboxes"), list) else []
    for tb in raw_textboxes:
        if not isinstance(tb, dict):
            continue
        role = tb.get("role", "unknown")
        role = role if role in ROLE_SET else "unknown"
        order = tb.get("order")
        try:
            order_val = int(order)
        except Exception:
            order_val = tb_index
        item_list = tb.get("item_ids")
        if not isinstance(item_list, list):
            continue
        cleaned_items = []
        for iid in item_list:
            if iid in items_set and iid not in used:
                cleaned_items.append(iid)
                used[iid] = "assigned"
        if not cleaned_items:
            continue
        tb_id = tb.get("tb_id") if isinstance(tb.get("tb_id"), str) else f"tb-{tb_index:03d}"
        confidence = tb.get("confidence")
        try:
            conf_val = float(confidence)
        except Exception:
            conf_val = 0.5
        conf_val = max(0.0, min(1.0, conf_val))
        textboxes.append(
            {
                "tb_id": tb_id,
                "role": role,
                "order": order_val,
                "item_ids": cleaned_items,
                "confidence": conf_val,
                "bbox": tb.get("bbox") if isinstance(tb.get("bbox"), dict) else None,
                "notes": tb.get("notes") if isinstance(tb.get("notes"), str) else None,
            }
        )
        tb_index += 1

    unassigned = [iid for iid in item_ids if iid not in used]
    if not textboxes and item_ids:
        for iid in item_ids:
            textboxes.append(
                {
                    "tb_id": f"tb-{tb_index:03d}",
                    "role": "unknown",
                    "order": tb_index,
                    "item_ids": [iid],
                    "confidence": 0.1,
                    "bbox": None,
                    "notes": None,
                }
            )
            tb_index += 1
        unassigned = []
    textboxes.sort(key=lambda t: t.get("order", 0))
    return {"textboxes": textboxes, "unassigned": unassigned}


def normalize_tree_plan(
    plan: Dict[str, object],
    item_ids: List[str],
) -> Dict[str, object]:
    item_set = set(item_ids)
    nodes_raw = plan.get("nodes") if isinstance(plan, dict) else None
    if not isinstance(nodes_raw, list):
        legacy = normalize_plan(plan if isinstance(plan, dict) else {}, item_ids)
        nodes = []
        for tb in legacy.get("textboxes", []):
            nodes.append(
                {
                    "id": tb.get("tb_id"),
                    "type": "textbox",
                    "role": tb.get("role", "unknown"),
                    "order": tb.get("order", 0),
                    "item_ids": tb.get("item_ids", []),
                    "confidence": tb.get("confidence", 0.5),
                }
            )
        root_id = "g-root"
        nodes.append(
            {
                "id": root_id,
                "type": "group",
                "role": "unknown",
                "order": 0,
                "children": [n["id"] for n in nodes if n.get("type") == "textbox"],
                "confidence": 1.0,
            }
        )
        return {"nodes": nodes, "root": root_id, "unassigned": legacy.get("unassigned", [])}

    nodes_map: Dict[str, Dict[str, object]] = {}
    used_items = set()
    for node in nodes_raw:
        if not isinstance(node, dict):
            continue
        node_id = node.get("id") or node.get("tb_id") or f"node-{len(nodes_map) + 1}"
        if not isinstance(node_id, str):
            node_id = f"node-{len(nodes_map) + 1}"
        if node_id in nodes_map:
            continue
        node_type = node.get("type")
        if node_type not in ("textbox", "group"):
            node_type = "textbox" if "item_ids" in node else "group"
        role = node.get("role", "unknown")
        if role not in ROLE_SET:
            role = "unknown"
        order_val = node.get("order")
        try:
            order = int(order_val)
        except Exception:
            order = len(nodes_map) + 1
        confidence = node.get("confidence", 0.5)
        try:
            confidence_val = float(confidence)
        except Exception:
            confidence_val = 0.5
        confidence_val = max(0.0, min(1.0, confidence_val))

        if node_type == "textbox":
            item_list = node.get("item_ids")
            if not isinstance(item_list, list):
                item_list = []
            cleaned_items = []
            for iid in item_list:
                if iid in item_set and iid not in used_items:
                    cleaned_items.append(iid)
                    used_items.add(iid)
            if not cleaned_items:
                continue
            nodes_map[node_id] = {
                "id": node_id,
                "type": "textbox",
                "role": role,
                "order": order,
                "item_ids": cleaned_items,
                "confidence": confidence_val,
            }
        else:
            children = node.get("children")
            if not isinstance(children, list):
                children = []
            cleaned_children = [c for c in children if isinstance(c, str)]
            nodes_map[node_id] = {
                "id": node_id,
                "type": "group",
                "role": role,
                "order": order,
                "children": cleaned_children,
                "confidence": confidence_val,
            }

    # Add unassigned items as unknown textboxes.
    for iid in item_set - used_items:
        node_id = f"tb-auto-{len(nodes_map) + 1}"
        nodes_map[node_id] = {
            "id": node_id,
            "type": "textbox",
            "role": "unknown",
            "order": len(nodes_map) + 1,
            "item_ids": [iid],
            "confidence": 0.1,
        }

    node_ids = set(nodes_map.keys())
    for node in nodes_map.values():
        if node.get("type") == "group":
            node["children"] = [c for c in node.get("children", []) if c in node_ids and c != node.get("id")]

    root = plan.get("root") if isinstance(plan, dict) else None
    if not isinstance(root, str) or root not in nodes_map:
        child_ids = set()
        for node in nodes_map.values():
            if node.get("type") == "group":
                child_ids.update(node.get("children", []))
        top_level = [nid for nid in nodes_map if nid not in child_ids]
        root = "g-root"
        nodes_map[root] = {
            "id": root,
            "type": "group",
            "role": "unknown",
            "order": 0,
            "children": top_level,
            "confidence": 1.0,
        }

    return {"nodes": list(nodes_map.values()), "root": root, "unassigned": []}


def parse_font_size(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return 0.0
    match = FONT_SIZE_RE.search(value)
    if not match:
        return 0.0
    try:
        return float(match.group(1))
    except Exception:
        return 0.0


def enforce_font_size_groups(
    plan: Dict[str, object],
    items: List[Dict[str, object]],
    tolerance: float,
) -> Dict[str, object]:
    id_to_item = {it.get("id"): it for it in items if it.get("id")}
    new_textboxes: List[Dict[str, object]] = []
    group_index = 1

    def size_bucket(size: float) -> float:
        if tolerance <= 0:
            return size
        return round(size / tolerance) * tolerance

    for tb in plan.get("textboxes", []):
        item_ids = tb.get("item_ids", [])
        if not isinstance(item_ids, list) or not item_ids:
            continue
        buckets: Dict[float, List[str]] = {}
        for iid in item_ids:
            item = id_to_item.get(iid)
            if not item:
                continue
            style = item.get("style") if isinstance(item.get("style"), dict) else {}
            size = parse_font_size(style.get("fontSize"))
            bucket = size_bucket(size)
            buckets.setdefault(bucket, []).append(iid)

        if len(buckets) <= 1:
            new_textboxes.append(tb)
            continue

        base_id = tb.get("tb_id") if isinstance(tb.get("tb_id"), str) else f"tb-{group_index:03d}"
        for idx, bucket_items in enumerate(sorted(buckets.items(), key=lambda x: x[0])):
            _, ids = bucket_items
            if not ids:
                continue
            new_tb = dict(tb)
            new_tb["item_ids"] = ids
            new_tb["tb_id"] = f"{base_id}-fs{idx + 1}"
            new_textboxes.append(new_tb)
            group_index += 1

    # Re-sequence order based on original order then position.
    def order_key(tb: Dict[str, object]) -> Tuple[int, float, float]:
        order = tb.get("order")
        try:
            order_val = int(order)
        except Exception:
            order_val = 9999
        bbs = []
        for iid in tb.get("item_ids", []):
            item = id_to_item.get(iid)
            if isinstance(item, dict) and isinstance(item.get("bbox"), dict):
                bbs.append(item["bbox"])
        if not bbs:
            return (order_val, 0.0, 0.0)
        min_x = min(bb["x"] for bb in bbs)
        min_y = min(bb["y"] for bb in bbs)
        return (order_val, min_y, min_x)

    new_textboxes.sort(key=order_key)
    for idx, tb in enumerate(new_textboxes, start=1):
        tb["order"] = idx
    return {"textboxes": new_textboxes, "unassigned": plan.get("unassigned", [])}


def union_bbox(bbs: List[Dict[str, float]], pad: float = 0.0) -> Dict[str, float]:
    xs = [b["x"] for b in bbs]
    ys = [b["y"] for b in bbs]
    x2 = [b["x"] + b["w"] for b in bbs]
    y2 = [b["y"] + b["h"] for b in bbs]
    minx, miny, maxx, maxy = min(xs), min(ys), max(x2), max(y2)
    return {
        "x": minx - pad,
        "y": miny - pad,
        "w": (maxx - minx) + 2 * pad,
        "h": (maxy - miny) + 2 * pad,
    }


def apply_plan_to_svg(
    tree: ET.ElementTree,
    items: List[Dict[str, object]],
    plan: Dict[str, object],
    pad: float,
    keep_ids: bool,
) -> None:
    root = tree.getroot()
    ns_prefix = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""

    id_to_elem: Dict[str, ET.Element] = {}
    for elem in root.iter():
        if tag_name(elem) == "text":
            extract_id = elem.get("data-extract-id")
            if extract_id:
                id_to_elem[extract_id] = elem

    id_to_bbox = {it["id"]: it["bbox"] for it in items if isinstance(it.get("bbox"), dict)}
    id_to_ctm = {it["id"]: it.get("ctm") for it in items if isinstance(it.get("ctm"), dict)}

    node_list = plan.get("nodes", [])
    if not isinstance(node_list, list):
        node_list = []
    node_map = {n.get("id"): n for n in node_list if isinstance(n, dict) and isinstance(n.get("id"), str)}
    root_id = plan.get("root")
    if not isinstance(root_id, str) or root_id not in node_map:
        return

    semantic_group = ET.Element(f"{ns_prefix}g")
    semantic_group.set("id", "semantic-layer")
    semantic_group.set("data-type", "semantic-layer")

    parent_map: Dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        for child in list(parent):
            parent_map[child] = parent

    def move_element(elem: ET.Element, new_parent: ET.Element) -> None:
        parent = parent_map.get(elem)
        if parent is None:
            return
        parent.remove(elem)
        new_parent.append(elem)

    built: Dict[str, Tuple[ET.Element, Dict[str, float]]] = {}

    def build_node(node_id: str, visiting: set, depth: int) -> Optional[Tuple[ET.Element, Dict[str, float]]]:
        if node_id in built:
            return built[node_id]
        if node_id in visiting:
            return None
        node = node_map.get(node_id)
        if not node:
            return None
        visiting.add(node_id)
        node_type = node.get("type")
        if node_type == "textbox":
            item_ids = node.get("item_ids", [])
            if not isinstance(item_ids, list) or not item_ids:
                visiting.remove(node_id)
                return None
            bbs = [id_to_bbox[iid] for iid in item_ids if iid in id_to_bbox]
            if bbs:
                bbox = union_bbox(bbs, pad=pad)
            else:
                bbox = {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}

            g = ET.Element(f"{ns_prefix}g")
            g.set("id", str(node_id))
            g.set("data-type", "textbox")
            g.set("data-role", str(node.get("role", "unknown")))
            g.set("data-order", str(node.get("order", "")))
            g.set("data-confidence", format_num(float(node.get("confidence", 0.0))))
            g.set("data-x", format_num(float(bbox["x"])))
            g.set("data-y", format_num(float(bbox["y"])))
            g.set("data-w", format_num(float(bbox["w"])))
            g.set("data-h", format_num(float(bbox["h"])))
            g.set("data-source", "llm-v1")

            rect = ET.Element(f"{ns_prefix}rect")
            rect.set("class", "tb-bbox")
            rect.set("x", format_num(float(bbox["x"])))
            rect.set("y", format_num(float(bbox["y"])))
            rect.set("width", format_num(float(bbox["w"])))
            rect.set("height", format_num(float(bbox["h"])))
            rect.set("fill", "none")
            rect.set("stroke", "none")
            rect.set("opacity", "0")
            g.append(rect)

            for iid in item_ids:
                elem = id_to_elem.get(iid)
                if elem is None:
                    continue
                ctm = id_to_ctm.get(iid)
                if isinstance(ctm, dict):
                    try:
                        matrix = (
                            f"matrix({ctm['a']} {ctm['b']} {ctm['c']} "
                            f"{ctm['d']} {ctm['e']} {ctm['f']})"
                        )
                        elem.set("transform", matrix)
                    except Exception:
                        pass
                move_element(elem, g)

            built[node_id] = (g, bbox)
            visiting.remove(node_id)
            return built[node_id]

        # group node
        children = node.get("children", [])
        if not isinstance(children, list):
            children = []
        child_ids = [c for c in children if isinstance(c, str) and c in node_map]
        child_ids.sort(key=lambda cid: node_map.get(cid, {}).get("order", 0))

        g = ET.Element(f"{ns_prefix}g")
        g.set("id", str(node_id))
        g.set("data-type", "text-group")
        g.set("data-role", str(node.get("role", "unknown")))
        g.set("data-order", str(node.get("order", "")))
        g.set("data-confidence", format_num(float(node.get("confidence", 0.0))))
        g.set("data-source", "llm-v1")

        child_bbs = []
        for cid in child_ids:
            built_child = build_node(cid, visiting, depth + 1)
            if built_child is None:
                continue
            child_elem, child_bbox = built_child
            g.append(child_elem)
            if isinstance(child_bbox, dict):
                child_bbs.append(child_bbox)

        if child_bbs:
            bbox = union_bbox(child_bbs, pad=pad)
        else:
            bbox = {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}

        g.set("data-x", format_num(float(bbox["x"])))
        g.set("data-y", format_num(float(bbox["y"])))
        g.set("data-w", format_num(float(bbox["w"])))
        g.set("data-h", format_num(float(bbox["h"])))

        rect = ET.Element(f"{ns_prefix}rect")
        rect.set("class", "tb-bbox")
        rect.set("x", format_num(float(bbox["x"])))
        rect.set("y", format_num(float(bbox["y"])))
        rect.set("width", format_num(float(bbox["w"])))
        rect.set("height", format_num(float(bbox["h"])))
        rect.set("fill", "none")
        rect.set("stroke", "none")
        rect.set("opacity", "0")
        g.insert(0, rect)

        built[node_id] = (g, bbox)
        visiting.remove(node_id)
        return built[node_id]

    root_built = build_node(root_id, set(), 0)
    if root_built:
        semantic_group.append(root_built[0])
        root.append(semantic_group)

    if not keep_ids:
        for elem in root.iter():
            if tag_name(elem) == "text" and "data-extract-id" in elem.attrib:
                del elem.attrib["data-extract-id"]


def process_svg(
    svg_path: Path,
    output_path: Path,
    meta_items_dir: Path,
    meta_plans_dir: Path,
    meta_raw_dir: Path,
    base_url: str,
    api_key: str,
    model: str,
    max_tokens: int,
    temperature: float,
    timeout: int,
    retries: int,
    limiter: Optional[RateLimiter],
    pad: float,
    keep_ids: bool,
    prompt: str,
    enforce_font_size: bool,
    font_size_tol: float,
) -> Tuple[bool, Optional[str]]:
    tree = ET.parse(svg_path)
    root = tree.getroot()
    ET.register_namespace("", SVG_NS)
    id_map = ensure_text_ids(root)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".svg") as tmp:
        tmp_path = Path(tmp.name)
        tree.write(tmp_path, encoding="utf-8", xml_declaration=True)

    items = extract_items_with_playwright(tmp_path)
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

    meta_items_dir.mkdir(parents=True, exist_ok=True)
    meta_plans_dir.mkdir(parents=True, exist_ok=True)
    items_path = meta_items_dir / (svg_path.stem + ".json")
    items_path.write_text(json.dumps(items_doc, ensure_ascii=False, indent=2), encoding="utf-8")

    if not base_url or not api_key or not model:
        # 无 LLM 配置时，退化为“全部未分配”的空计划，后续可由离线脚本重新处理。
        plan = {"textboxes": [], "unassigned": [it.get("id") for it in items if it.get("id")]}
    else:
        # 第一次调用：使用用户/配置给定的 max_tokens
        response, error = call_text_llm_with_retries(
            base_url,
            api_key,
            model,
            prompt,
            items_doc,
            max_tokens,
            temperature,
            timeout,
            retries,
            limiter,
        )
        meta_raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = meta_raw_dir / (svg_path.stem + ".txt")
        if response:
            raw_path.write_text(response, encoding="utf-8")
        elif error:
            raw_path.write_text(error, encoding="utf-8")

        parsed = parse_json_from_text(response or "")

        # 如果第一次解析失败（可能是输出被截断或格式混乱），再用更高 max_tokens 重试一次。
        if not parsed and (response is not None):
            retry_max_tokens = min(max_tokens * 2, 2000)
            response_retry, error_retry = call_text_llm_with_retries(
                base_url,
                api_key,
                model,
                prompt,
                items_doc,
                retry_max_tokens,
                temperature,
                timeout,
                retries,
                limiter,
            )
            # 将重试结果单独存一份，便于后期排查
            raw_retry_path = meta_raw_dir / (svg_path.stem + "_retry.txt")
            if response_retry:
                raw_retry_path.write_text(response_retry, encoding="utf-8")
            elif error_retry:
                raw_retry_path.write_text(error_retry, encoding="utf-8")

            # 以重试结果为准再尝试解析
            parsed = parse_json_from_text(response_retry or "")
            # 如果重试也失败，则记录更详细的错误信息
            if not parsed:
                combined_error = error_retry or error or "invalid JSON after retry"
                return False, combined_error

        if not parsed:
            # 没有任何可用响应，直接视为失败，交给 failed_tasks.json 与后续修复脚本处理
            return False, error or "invalid JSON"

        plan = normalize_tree_plan(parsed or {}, [it["id"] for it in items if it.get("id")])

    plan_path = meta_plans_dir / (svg_path.stem + ".json")
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    apply_plan_to_svg(tree, items, plan, pad, keep_ids)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    return True, None


def main() -> None:
    parser = argparse.ArgumentParser(description="Semantic grouping for SVG text.")
    parser.add_argument("--input", required=True, help="Input directory with SVGs.")
    parser.add_argument("--output", required=True, help="Output directory for semantic SVGs.")
    parser.add_argument("--meta", default="", help="Metadata directory for items/plans (default: output/meta).")
    parser.add_argument("--config", default="config.json", help="Config json path.")
    parser.add_argument("--base-url", default="", help="OpenAI-compatible base URL.")
    parser.add_argument("--api-key", default="", help="API key.")
    parser.add_argument("--model", default="", help="Text model name.")
    parser.add_argument("--max-tokens", type=int, default=None, help="Max tokens for LLM response.")
    parser.add_argument("--temperature", type=float, default=None, help="LLM temperature.")
    parser.add_argument("--timeout", type=int, default=None, help="Request timeout in seconds.")
    parser.add_argument("--workers", type=int, default=None, help="Parallel workers (LLM).")
    parser.add_argument("--qps", type=float, default=None, help="Global requests per second limit.")
    parser.add_argument("--retries", type=int, default=None, help="LLM retry count.")
    parser.add_argument("--pad", type=float, default=0.0, help="BBox padding.")
    parser.add_argument("--keep-ids", action="store_true", help="Keep data-extract-id in output SVGs.")
    parser.add_argument("--prompt", default="", help="Override LLM prompt.")
    parser.add_argument("--enforce-font-size", action="store_true", help="Force same font size inside a textbox.")
    parser.add_argument("--font-size-tol", type=float, default=None, help="Font size tolerance.")
    parser.add_argument("--require-success", action="store_true", help="Exit non-zero if any failures occurred.")
    args = parser.parse_args()

    if sync_playwright is None:
        raise SystemExit("playwright not installed. Run: pip install playwright && playwright install chromium")

    input_dir = Path(args.input)
    output_dir = Path(args.output)
    meta_dir = Path(args.meta) if args.meta else output_dir / "meta"
    meta_items_dir = meta_dir / "items"
    meta_plans_dir = meta_dir / "plans"
    meta_raw_dir = meta_dir / "raw"

    config = load_config(Path(args.config) if args.config else None)
    base_url = args.base_url or str(config.get("base_url", "")) or os.getenv("OPENAI_BASE_URL", "")
    api_key = args.api_key or str(config.get("api_key", "")) or os.getenv("OPENAI_API_KEY", "")
    model = args.model or str(config.get("text_model", "")) or os.getenv("OPENAI_MODEL", "")
    max_tokens = args.max_tokens if args.max_tokens is not None else int(config.get("text_max_tokens", 1200))
    temperature = args.temperature if args.temperature is not None else float(config.get("text_temperature", 0.2))
    timeout = args.timeout if args.timeout is not None else int(config.get("text_timeout", 60))
    workers = args.workers if args.workers is not None else int(config.get("text_workers", 1))
    qps = args.qps if args.qps is not None else float(config.get("text_qps", 0.5))
    retries = args.retries if args.retries is not None else int(config.get("text_retries", 2))
    enforce_font_size = bool(config.get("text_enforce_font_size", True)) or args.enforce_font_size
    font_size_tol = (
        args.font_size_tol
        if args.font_size_tol is not None
        else float(config.get("text_font_size_tol", 0.2))
    )

    prompt = args.prompt or build_prompt(ROLE_SET)

    svg_files = [p for p in input_dir.glob("*.SVG") if not p.name.startswith("._")]
    if not svg_files:
        print("No SVG files found.")
        return
    failed_tasks_path = meta_dir / FAILED_TASKS_NAME
    failed_prev: Dict[str, str] = {}
    if failed_tasks_path.exists():
        try:
            data = json.loads(failed_tasks_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and isinstance(item.get("svg"), str):
                        failed_prev[item["svg"]] = str(item.get("error") or "")
            elif isinstance(data, dict):
                failed_prev = {str(k): str(v) for k, v in data.items()}
        except Exception:
            failed_prev = {}

    failed_names = set(failed_prev.keys())
    todo = []
    for svg_path in sorted(svg_files):
        out_svg = output_dir / svg_path.name
        if svg_path.name in failed_names or not out_svg.exists():
            todo.append(svg_path)
    if not todo:
        print("No SVG files need processing.")
        return

    limiter = RateLimiter(qps) if qps > 0 else None
    processed = 0
    total = len(todo)
    failures: Dict[str, str] = {}
    lock = threading.Lock()

    def worker(svg_path: Path) -> None:
        out_svg = output_dir / svg_path.name
        ok, err = process_svg(
            svg_path,
            out_svg,
            meta_items_dir,
            meta_plans_dir,
            meta_raw_dir,
            base_url,
            api_key,
            model,
            max_tokens,
            temperature,
            timeout,
            retries,
            limiter,
            args.pad,
            args.keep_ids,
            prompt,
            enforce_font_size,
            font_size_tol,
        )
        if not ok:
            with lock:
                failures[svg_path.name] = err or "unknown error"

    if workers <= 1:
        for svg_path in todo:
            worker(svg_path)
            processed += 1
            print(f"Semantic: {processed}/{total}")
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(worker, svg_path) for svg_path in todo]
            for future in as_completed(futures):
                future.result()
                processed += 1
                print(f"Semantic: {processed}/{total}")

    if failures:
        failed_items = [{"svg": name, "error": err} for name, err in sorted(failures.items())]
        failed_tasks_path.parent.mkdir(parents=True, exist_ok=True)
        failed_tasks_path.write_text(json.dumps(failed_items, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Semantic failures: {len(failures)} (saved to {failed_tasks_path})")
        if args.require_success:
            raise SystemExit(1)
    else:
        if failed_tasks_path.exists():
            try:
                failed_tasks_path.unlink()
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    main()
