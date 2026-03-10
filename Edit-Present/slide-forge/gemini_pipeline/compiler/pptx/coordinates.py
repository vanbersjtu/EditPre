"""Coordinate conversion utilities for SVG px -> PPTX EMU."""

from ..constants import EMU_PER_INCH


class CoordinateConverter:
    """Convert SVG coordinates to PPTX EMU."""

    def __init__(
        self,
        svg_width: float,
        svg_height: float,
        slide_width_emu: int,
        slide_height_emu: int,
        dpi: float = 96.0,
    ):
        self.svg_width = svg_width
        self.svg_height = svg_height
        self.slide_width_emu = slide_width_emu
        self.slide_height_emu = slide_height_emu
        self.dpi = dpi

        self.scale_x = slide_width_emu / svg_width if svg_width else 1.0
        self.scale_y = slide_height_emu / svg_height if svg_height else 1.0

    def to_emu_x(self, svg_x: float) -> int:
        return int(round(svg_x * self.scale_x))

    def to_emu_y(self, svg_y: float) -> int:
        return int(round(svg_y * self.scale_y))

    def to_emu_width(self, svg_width: float) -> int:
        return int(round(svg_width * self.scale_x))

    def to_emu_height(self, svg_height: float) -> int:
        return int(round(svg_height * self.scale_y))

    def to_emu_length(self, svg_length: float) -> int:
        avg_scale = (self.scale_x + self.scale_y) / 2
        return int(round(svg_length * avg_scale))

    def font_scale_factor(self) -> float:
        px_to_emu_base = EMU_PER_INCH / max(self.dpi, 1e-6)
        avg_scale = (self.scale_x + self.scale_y) / 2.0
        if px_to_emu_base <= 0:
            return 1.0
        return max(0.1, avg_scale / px_to_emu_base)
