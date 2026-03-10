"""Affine transform utilities for SVG parsing."""

import math
import re
from typing import Optional, Tuple

MATRIX_RE = re.compile(r"matrix\s*\(\s*([^)]+)\s*\)")
TRANSLATE_RE = re.compile(r"translate\s*\(\s*([^)]+)\s*\)")
SCALE_RE = re.compile(r"scale\s*\(\s*([^)]+)\s*\)")
ROTATE_RE = re.compile(r"rotate\s*\(\s*([^)]+)\s*\)")


class TransformMatrix:
    """2D affine transform matrix [a, b, c, d, e, f]."""

    def __init__(
        self,
        a: float = 1.0,
        b: float = 0.0,
        c: float = 0.0,
        d: float = 1.0,
        e: float = 0.0,
        f: float = 0.0,
    ):
        self.a = a
        self.b = b
        self.c = c
        self.d = d
        self.e = e
        self.f = f

    @classmethod
    def identity(cls) -> "TransformMatrix":
        return cls()

    @classmethod
    def translate(cls, tx: float, ty: float = 0.0) -> "TransformMatrix":
        return cls(1.0, 0.0, 0.0, 1.0, tx, ty)

    @classmethod
    def scale(cls, sx: float, sy: Optional[float] = None) -> "TransformMatrix":
        if sy is None:
            sy = sx
        return cls(sx, 0.0, 0.0, sy, 0.0, 0.0)

    @classmethod
    def rotate(cls, angle_deg: float, cx: float = 0.0, cy: float = 0.0) -> "TransformMatrix":
        rad = math.radians(angle_deg)
        cos_a = math.cos(rad)
        sin_a = math.sin(rad)
        return cls(
            cos_a,
            sin_a,
            -sin_a,
            cos_a,
            cx - cos_a * cx + sin_a * cy,
            cy - sin_a * cx - cos_a * cy,
        )

    def multiply(self, other: "TransformMatrix") -> "TransformMatrix":
        """Return self * other (apply other first, then self)."""
        return TransformMatrix(
            a=self.a * other.a + self.c * other.b,
            b=self.b * other.a + self.d * other.b,
            c=self.a * other.c + self.c * other.d,
            d=self.b * other.c + self.d * other.d,
            e=self.a * other.e + self.c * other.f + self.e,
            f=self.b * other.e + self.d * other.f + self.f,
        )

    def transform_point(self, x: float, y: float) -> Tuple[float, float]:
        return (self.a * x + self.c * y + self.e, self.b * x + self.d * y + self.f)

    def transform_vector(self, dx: float, dy: float) -> Tuple[float, float]:
        return (self.a * dx + self.c * dy, self.b * dx + self.d * dy)

    def get_scale(self) -> Tuple[float, float]:
        sx = math.sqrt(self.a * self.a + self.b * self.b)
        sy = math.sqrt(self.c * self.c + self.d * self.d)
        return (sx, sy)

    def get_rotation_degrees(self) -> float:
        return math.degrees(math.atan2(self.b, self.a))

    def get_translation(self) -> Tuple[float, float]:
        return (self.e, self.f)


def parse_transform(transform_str: Optional[str]) -> TransformMatrix:
    """Parse SVG transform attribute into a TransformMatrix."""
    if not transform_str:
        return TransformMatrix.identity()

    result = TransformMatrix.identity()

    for match in MATRIX_RE.finditer(transform_str):
        parts = [p.strip() for p in re.split(r"[\s,]+", match.group(1).strip()) if p]
        if len(parts) == 6:
            try:
                a, b, c, d, e, f = [float(p) for p in parts]
                result = result.multiply(TransformMatrix(a, b, c, d, e, f))
            except ValueError:
                pass

    for match in TRANSLATE_RE.finditer(transform_str):
        if "matrix" in transform_str[: match.start()]:
            continue
        parts = [p.strip() for p in re.split(r"[\s,]+", match.group(1).strip()) if p]
        if parts:
            try:
                tx = float(parts[0])
                ty = float(parts[1]) if len(parts) > 1 else 0.0
                result = result.multiply(TransformMatrix.translate(tx, ty))
            except ValueError:
                pass

    for match in SCALE_RE.finditer(transform_str):
        parts = [p.strip() for p in re.split(r"[\s,]+", match.group(1).strip()) if p]
        if parts:
            try:
                sx = float(parts[0])
                sy = float(parts[1]) if len(parts) > 1 else sx
                result = result.multiply(TransformMatrix.scale(sx, sy))
            except ValueError:
                pass

    for match in ROTATE_RE.finditer(transform_str):
        parts = [p.strip() for p in re.split(r"[\s,]+", match.group(1).strip()) if p]
        if parts:
            try:
                angle = float(parts[0])
                cx = float(parts[1]) if len(parts) > 1 else 0.0
                cy = float(parts[2]) if len(parts) > 2 else 0.0
                result = result.multiply(TransformMatrix.rotate(angle, cx, cy))
            except ValueError:
                pass

    return result
