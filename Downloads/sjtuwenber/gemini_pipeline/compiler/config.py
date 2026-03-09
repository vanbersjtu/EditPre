"""Compiler config models (stage-1 minimal skeleton)."""

from dataclasses import dataclass


@dataclass
class CompilerConfig:
    dpi: float = 96.0
    cjk_font: str = "PingFang SC"
