"""Shared compiler types (stage-1 minimal skeleton)."""

from dataclasses import dataclass


@dataclass
class CompileStats:
    slide_count: int = 0
    shape_count: int = 0
