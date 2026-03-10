"""Public API for compiler package.

Stage-1 provides stable entrypoints by delegating to legacy implementation.
"""

from pathlib import Path
from typing import Iterable, Optional

from .legacy_svg_to_pptx_pro import build_pptx_pro, natural_sort_key


def compile_svg_files_to_pptx(
    svg_paths: Iterable[Path],
    out_pptx: Path,
    dpi: float = 96.0,
    cjk_font: str = "PingFang SC",
) -> None:
    files = [Path(p) for p in svg_paths]
    build_pptx_pro(files, Path(out_pptx), dpi=dpi, cjk_font=cjk_font)


def compile_svg_dir_to_pptx(
    svg_dir: Path,
    out_pptx: Path,
    dpi: float = 96.0,
    cjk_font: str = "PingFang SC",
    pattern: Optional[str] = None,
) -> None:
    svg_root = Path(svg_dir)
    glob_pattern = pattern or "*.svg"
    svg_paths = sorted(svg_root.glob(glob_pattern), key=natural_sort_key)
    build_pptx_pro(svg_paths, Path(out_pptx), dpi=dpi, cjk_font=cjk_font)
