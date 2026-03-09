#!/usr/bin/env python3
"""完整调试幻灯片 7 的转换流程"""
import glob
import xml.etree.ElementTree as ET
import sys
sys.path.insert(0, '/Users/xiaoxiaobo/Downloads/sjtuwenber/gemini_pipeline')
from svg_to_pptx_pro import tag_name, parse_length

# 查找幻灯片 7
svg_files = glob.glob('/Users/xiaoxiaobo/Downloads/sjtuwenber/gemini_pipeline/output/svg/*7.svg')
if not svg_files:
    print('找不到文件')
    sys.exit(1)

svg_path = svg_files[0]
print(f'使用文件：{svg_path}\n')

# 解析 SVG
tree = ET.parse(svg_path)
root = tree.getroot()

# 查找 tb-body-1
for g in root.iter():
    if tag_name(g) != "g":
        continue
    if g.get("data-type") != "textbox":
        continue
    
    tb_id = g.get("id", "")
    if tb_id != "tb-body-1":
        continue
    
    print(f'找到 tb-body-1')
    print('=' * 80)
    
    # 检查 data-* 属性
    print('\n1. Data 属性:')
    for attr in ['data-x', 'data-y', 'data-w', 'data-h', 'data-role', 'data-order']:
        val = g.get(attr)
        print(f'   {attr:12s} = {val}')
    
    # 检查 rect
    print('\n2. Rect 元素:')
    for child in g:
        if tag_name(child) == "rect" and child.get("class") == "tb-bbox":
            print(f'   x={child.get("x")}, y={child.get("y")}, width={child.get("width")}, height={child.get("height")}')
    
    # 检查 text 元素
    print('\n3. Text 元素:')
    for child in g:
        if tag_name(child) == "text":
            print(f'   x={child.get("x")}, y={child.get("y")}, font-size={child.get("font-size")}')
            tspans = [c for c in child if tag_name(c) == "tspan"]
            print(f'   Tspans: {len(tspans)}')
            for i, tspan in enumerate(tspans, 1):
                print(f'     tspan{i}: x={tspan.get("x")}, dy={tspan.get("dy")}, text="{(tspan.text or "")[:20]}"')
    
    break

print('\n\n4. 所有 textbox 的 bbox:')
for g in root.iter():
    if tag_name(g) != "g":
        continue
    if g.get("data-type") != "textbox":
        continue
    
    tb_id = g.get("id", "")
    
    # 从 rect 获取 bbox
    rect_bbox = {"x": 0, "y": 0, "w": 0, "h": 0}
    for child in g:
        if tag_name(child) == "rect" and child.get("class") == "tb-bbox":
            rect_bbox = {
                "x": parse_length(child.get("x"), 0.0),
                "y": parse_length(child.get("y"), 0.0),
                "w": parse_length(child.get("width"), 0.0),
                "h": parse_length(child.get("height"), 0.0),
            }
    
    print(f'   {tb_id:20s} x={rect_bbox["x"]:5.0f}, y={rect_bbox["y"]:5.0f}, w={rect_bbox["w"]:4.0f}, h={rect_bbox["h"]:4.0f}')
