#!/usr/bin/env python3
"""测试 SVG 解析"""
import xml.etree.ElementTree as ET
import glob
import os

# 查找所有幻灯片 7.svg 文件
svg_files = glob.glob('/Users/xiaoxiaobo/Downloads/sjtuwenber/gemini_pipeline/output/svg/*7.svg')
print(f'找到 {len(svg_files)} 个包含 7 的 SVG 文件')

for svg_path in svg_files:
    print(f'\n尝试解析：{svg_path}')
    try:
        tree = ET.parse(svg_path)
        print('SVG 解析成功！')
        root = tree.getroot()
        print(f'根元素：{root.tag}')
        
        # 查找 semantic-layer
        semantic_layer = root.find('.//{http://www.w3.org/2000/svg}g[@id="semantic-layer"]')
        if semantic_layer is None:
            semantic_layer = root.find('.//g[@id="semantic-layer"]')
        
        if semantic_layer is not None:
            print(f'找到 semantic-layer')
            # 统计 textbox 数量
            textboxes = semantic_layer.findall('.//{http://www.w3.org/2000/svg}g[@data-type="textbox"]')
            if not textboxes:
                textboxes = semantic_layer.findall('.//g[@data-type="textbox"]')
            print(f'找到 {len(textboxes)} 个 textbox')
            
            # 检查 CSS
            defs = root.find('{http://www.w3.org/2000/svg}defs')
            if defs is None:
                defs = root.find('defs')
            if defs is not None:
                style = defs.find('{http://www.w3.org/2000/svg}style')
                if style is None:
                    style = defs.find('style')
                if style is not None and style.text:
                    print(f'CSS 内容：{style.text[:100]}...')
        else:
            print('未找到 semantic-layer')
            
    except Exception as e:
        print(f'SVG 解析失败：{e}')
        import traceback
        traceback.print_exc()
