#!/usr/bin/env python3
"""
测试图表生成 - 使用 Gemini API 生成 python-pptx 图表代码
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from svg_to_pptx_pro import generate_chart_code

# 测试用的图表描述
test_captions = [
    "柱状图：显示 2023 年四个季度的销售额，Q1: 120 万，Q2: 150 万，Q3: 180 万，Q4: 200 万",
    "饼图：市场份额分布，公司 A 35%，公司 B 25%，公司 C 20%，公司 D 15%，其他 5%",
    "折线图：用户增长趋势，1 月 1000 人，2 月 1500 人，3 月 2200 人，4 月 3000 人，5 月 4100 人",
]

def main():
    print("=" * 80)
    print("测试图表代码生成 (Gemini API)")
    print("=" * 80)
    
    for i, caption in enumerate(test_captions, 1):
        print(f"\n{'='*80}")
        print(f"测试 {i}/{len(test_captions)}: {caption}")
        print(f"{'='*80}")
        
        code = generate_chart_code(
            caption=caption,
            api_key=os.environ.get("GEMINI_API_KEY", ""),
            base_url="https://generativelanguage.googleapis.com/v1beta",  # 使用相同的 URL
            model="gemini-3.1-pro-preview",
            max_tokens=2000,
            temperature=0.2,
        )
        
        if code:
            print("\n✓ 生成的代码:")
            print("-" * 80)
            print(code[:500] + "..." if len(code) > 500 else code)
            print("-" * 80)
        else:
            print("\n✗ 代码生成失败")

if __name__ == "__main__":
    main()
