"""Compiler package for SVG -> PPTX conversion."""

from .api import compile_svg_dir_to_pptx, compile_svg_files_to_pptx

__all__ = [
    "compile_svg_dir_to_pptx",
    "compile_svg_files_to_pptx",
]
