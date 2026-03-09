#!/usr/bin/env python3
"""详细检查幻灯片 7 的 tspan 坐标"""
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
    
    for i, tb in enumerate(textboxes, 1):
        tb_id = tb.get('id', 'unknown')
        
        # 获取 text 元素
        text_elem = tb.find('{http://www.w3.org/2000/svg}text')
        if text_elem is None:
            text_elem = tb.find('text')
        
        if text_elem is not None:
            text_x = text_elem.get('x', 'N/A')
            text_y = text_elem.get('y', 'N/A')
            
            # 检查 tspan
            tspans = text_elem.findall('{http://www.w3.org/2000/svg}tspan')
            if not tspans:
                tspans = text_elem.findall('tspan')
            
            if tspans:
                print(f'\n{tb_id}: text(x={text_x}, y={text_y})')
                for j, tspan in enumerate(tspans, 1):
                    tspan_x = tspan.get('x', 'N/A')
                    tspan_dy = tspan.get('dy', 'N/A')
                    tspan_y = tspan.get('y', 'N/A')
                    tspan_text = (tspan.text or "")[:30]
                    print(f'  tspan{j}: x={tspan_x:5s}, y={tspan_y:5s}, dy={tspan_dy:5s}  text="{tspan_text}"')
            else:
                text_content = (text_elem.text or "")[:50]
                print(f'\n{tb_id}: text(x={text_x}, y={text_y})  text="{text_content}"')
