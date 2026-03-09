#!/usr/bin/env python3
"""检查幻灯片 7 的文本位置"""
import xml.etree.ElementTree as ET
import glob

# 查找幻灯片 7
svg_files = glob.glob('/Users/xiaoxiaobo/Downloads/sjtuwenber/gemini_pipeline/output/svg/*7.svg')

for svg_path in svg_files:
    print(f'\n文件：{svg_path}')
    print('=' * 80)
    
    tree = ET.parse(svg_path)
    root = tree.getroot()
    
    # 查找所有 textbox
    textboxes = root.findall('.//{http://www.w3.org/2000/svg}g[@data-type="textbox"]')
    if not textboxes:
        textboxes = root.findall('.//g[@data-type="textbox"]')
    
    print(f'找到 {len(textboxes)} 个 textbox\n')
    
    for i, tb in enumerate(textboxes, 1):
        tb_id = tb.get('id', 'unknown')
        role = tb.get('data-role', 'unknown')
        order = tb.get('data-order', 'unknown')
        
        # 获取 bbox
        rect = tb.find('{http://www.w3.org/2000/svg}rect')
        if rect is None:
            rect = tb.find('rect')
        
        if rect is not None:
            x = rect.get('x', '0')
            y = rect.get('y', '0')
            w = rect.get('width', '0')
            h = rect.get('height', '0')
        else:
            x = tb.get('data-x', '0')
            y = tb.get('data-y', '0')
            w = tb.get('data-w', '0')
            h = tb.get('data-h', '0')
        
        # 获取文本内容
        text_elem = tb.find('{http://www.w3.org/2000/svg}text')
        if text_elem is None:
            text_elem = tb.find('text')
        
        text_content = ""
        if text_elem is not None:
            text_content = (text_elem.text or "").strip()[:50]
            # 检查是否有 tspan
            tspans = text_elem.findall('{http://www.w3.org/2000/svg}tspan')
            if not tspans:
                tspans = text_elem.findall('tspan')
            if tspans:
                text_content = f"[{len(tspans)} 个 tspan]"
        
        print(f'{i:2d}. {tb_id:20s} role={role:10s} order={order:3s}  pos=({x:5s}, {y:5s})  size=({w:4s}, {h:4s})  text="{text_content}"')
