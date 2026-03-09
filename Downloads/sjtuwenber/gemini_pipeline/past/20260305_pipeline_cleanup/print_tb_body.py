#!/usr/bin/env python3
"""打印 tb-body-1 的原始 XML"""
import glob

svg_files = glob.glob('/Users/xiaoxiaobo/Downloads/sjtuwenber/gemini_pipeline/output/svg/*7.svg')
if not svg_files:
    print('找不到文件')
    exit(1)

svg_path = svg_files[0]
with open(svg_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# 查找 tb-body-1
in_tb_body = False
for i, line in enumerate(lines, 1):
    if 'tb-body-1' in line:
        in_tb_body = True
    if in_tb_body:
        print(f'{i:4d}: {line}', end='')
        if '</g>' in line and i > 1:
            break
