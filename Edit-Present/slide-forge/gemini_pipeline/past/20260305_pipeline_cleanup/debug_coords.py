#!/usr/bin/env python3
"""调试幻灯片 7 的坐标转换"""
import xml.etree.ElementTree as ET
import glob
import sys
sys.path.insert(0, '/Users/xiaoxiaobo/Downloads/sjtuwenber/gemini_pipeline')
from svg_to_pptx_pro import parse_length, parse_transform_xy

# 查找幻灯片 7
svg_files = glob.glob('/Users/xiaoxiaobo/Downloads/sjtuwenber/gemini_pipeline/output/svg/*7.svg')
if not svg_files:
    print('找不到幻灯片 7.svg')
    sys.exit(1)

svg_path = svg_files[0]
print(f'使用文件：{svg_path}\n')

# 解析 SVG
tree = ET.parse(svg_path)
root = tree.getroot()

# 查找所有 textbox（处理命名空间）
ns = {'svg': 'http://www.w3.org/2000/svg'}
textboxes = root.findall('.//svg:g[@data-type="textbox"]', ns)
if not textboxes:
    # 尝试不带命名空间
    textboxes = root.findall('.//g[@data-type="textbox"]')

print(f'找到 {len(textboxes)} 个 textbox\n')

for tb in textboxes:
    tb_id = tb.get('id', '')
    
    # 获取 bbox
    data_x = parse_length(tb.get('data-x'), 0.0)
    data_y = parse_length(tb.get('data-y'), 0.0)
    data_w = parse_length(tb.get('data-w'), 0.0)
    data_h = parse_length(tb.get('data-h'), 0.0)
    
    print(f'{tb_id:20s} Data: x={data_x:5.0f}, y={data_y:5.0f}, w={data_w:4.0f}, h={data_h:4.0f}')

# 计算 DPI 转换
dpi = 96.0
print(f'\n\nPPTX 坐标计算 (DPI={dpi}):')
print(f'tb-body-1: x={data_x/dpi:.2f}", y={data_y/dpi:.2f}", w={data_w/dpi:.2f}", h={data_h/dpi:.2f}"')
