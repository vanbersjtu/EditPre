#!/usr/bin/env python3
"""测试幻灯片 7 的 SVG 到 PPTX 转换"""
import sys
import glob
import os

sys.path.insert(0, '/Users/xiaoxiaobo/Downloads/sjtuwenber/gemini_pipeline')
from svg_to_pptx_pro import convert_svg_to_slide

# 查找幻灯片 7
svg_files = glob.glob('/Users/xiaoxiaobo/Downloads/sjtuwenber/gemini_pipeline/output/svg/*7.svg')

for svg_path in svg_files:
    print(f'\n处理文件：{svg_path}')
    pptx_path = '/Users/xiaoxiaobo/Downloads/sjtuwenber/gemini_pipeline/output/pptx/test_slide7.pptx'
    
    # 创建输出目录
    os.makedirs(os.path.dirname(pptx_path), exist_ok=True)
    
    try:
        convert_svg_to_slide(svg_path, pptx_path)
        print(f'✓ 转换成功：{pptx_path}')
    except Exception as e:
        print(f'✗ 转换失败：{e}')
        import traceback
        traceback.print_exc()
