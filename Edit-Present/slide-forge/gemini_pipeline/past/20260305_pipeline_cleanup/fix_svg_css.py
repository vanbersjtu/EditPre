#!/usr/bin/env python3
"""
修复SVG文件中的CSS问题，移除可能导致XML解析错误的@import和@font-face语句。
"""
import os
import re

svg_dir = "/Users/xiaoxiaobo/Downloads/sjtuwenber/gemini_pipeline/output/svg"

def fix_svg_css(svg_path):
    """修复单个SVG文件的CSS问题"""
    try:
        with open(svg_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 移除CSS @import和@font-face语句
        content = re.sub(r'@import.*?;', '', content, flags=re.DOTALL)
        content = re.sub(r'@font-face.*?}', '', content, flags=re.DOTALL)
        
        # 保存修复后的文件
        with open(svg_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        return True
    except Exception as e:
        print(f"修复文件 {svg_path} 时出错: {e}")
        return False

def main():
    """主函数"""
    fixed_count = 0
    total_count = 0
    
    for root, dirs, files in os.walk(svg_dir):
        for file in files:
            if file.endswith('.svg'):
                total_count += 1
                svg_path = os.path.join(root, file)
                if fix_svg_css(svg_path):
                    fixed_count += 1
                    print(f"修复了文件: {svg_path}")
    
    print(f"\n修复完成: 共处理 {total_count} 个SVG文件，成功修复 {fixed_count} 个")

if __name__ == "__main__":
    main()