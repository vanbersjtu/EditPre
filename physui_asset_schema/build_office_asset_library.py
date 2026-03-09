#!/usr/bin/env python3
"""Build initial Office-like pattern asset library for PhysUI routing.

Focus: diagonal stripes + hatching + dots + dense fragments.
This is a retrieval library, not a full visual template bank yet.
"""

from __future__ import annotations

import json
from pathlib import Path


OUT_PATH = Path("/Users/xiaoxiaobo/physui_asset_schema/office_pattern_assets_v1.json")


def make_pattern_asset(
    asset_id: str,
    name_en: str,
    name_zh: str,
    source_value: str,
    tags: list[str],
    orientation_deg: float | None,
    period_norm: float,
    anisotropy: float,
    stochasticity: float,
    tile_ratio: float,
    min_conf: float = 0.65,
    min_iou: float = 0.80,
    max_token_ratio: float = 0.65,
) -> dict:
    type_specific = {
        "pattern": {
            "orientation_deg": float(orientation_deg if orientation_deg is not None else 0.0),
            "tile_ratio": float(tile_ratio),
        }
    }
    rf = {
        "period_px_norm": float(period_norm),
        "anisotropy": float(anisotropy),
        "stochasticity": float(stochasticity),
        "frequency_peaks": [float(max(period_norm, 1e-6)), float(max(period_norm * 2.0, 1e-6))],
    }
    if orientation_deg is not None:
        rf["orientation_deg"] = float(orientation_deg)

    return {
        "asset_id": asset_id,
        "asset_type": "pattern",
        "canonical_name_en": name_en,
        "canonical_name_zh": name_zh,
        "source_standard": "OOXML",
        "source_enum": "ST_PresetPatternVal",
        "source_value": source_value,
        "tags": tags,
        "retrieval_features": rf,
        "param_slots": [
            {"name": "fg_color", "type": "color", "required": True},
            {"name": "bg_color", "type": "color", "required": True},
            {"name": "stroke_width", "type": "float", "required": False, "default": 1.0},
            {"name": "tile", "type": "float", "required": False, "default": 8.0},
            {"name": "angle", "type": "float", "required": False, "default": float(orientation_deg or 0.0)},
        ],
        "svg_template": (
            "<defs><pattern id='{{id}}' width='{{tile}}' height='{{tile}}' patternUnits='userSpaceOnUse' "
            "patternTransform='rotate({{angle}})'><rect width='100%' height='100%' fill='{{bg_color}}'/>"
            "<line x1='0' y1='0' x2='0' y2='{{tile}}' stroke='{{fg_color}}' stroke-width='{{stroke_width}}'/></pattern></defs>"
        ),
        "routing_policy": {"min_confidence": min_conf, "fallback": "physics_fit"},
        "quality_gate": {"min_iou": min_iou, "max_token_ratio": max_token_ratio, "min_soft_iou": 0.88},
        "type_specific": type_specific,
        "provenance": {
            "created_from": "random2000_mode_probe + ooxml_mapping",
            "license": "internal_research",
            "created_at": "2026-03-04",
            "notes": "Bootstrap v1; retrieval-first template mapping.",
        },
    }


def main() -> None:
    assets: list[dict] = []

    # 1) diagonal stripes
    stripe_angles = [30.0, 45.0, 60.0, 120.0, 135.0, 150.0]
    stripe_vals = ["pct5", "pct10", "pct20", "pct30", "pct40", "pct50"]
    for i, (ang, sval) in enumerate(zip(stripe_angles, stripe_vals), start=1):
        assets.append(
            make_pattern_asset(
                asset_id=f"pattern.diagonal.stripe_v1_{i}",
                name_en=f"Diagonal Stripe v1-{i}",
                name_zh=f"对角线条纹 v1-{i}",
                source_value=sval,
                tags=["stripe", "diagonal", "office", "pattern"],
                orientation_deg=ang,
                period_norm=0.0010,
                anisotropy=0.70,
                stochasticity=0.34,
                tile_ratio=0.0010,
                min_conf=0.60,
                min_iou=0.78,
                max_token_ratio=0.70,
            )
        )

    # 2) hatching (multi-direction line textures)
    hatch_specs = [
        ("ltDnDiag", 90.0, 0.0020, 0.65, 0.62),
        ("dkDnDiag", 105.0, 0.0020, 0.68, 0.66),
        ("ltUpDiag", 75.0, 0.0016, 0.62, 0.70),
        ("dkUpDiag", 120.0, 0.0018, 0.70, 0.64),
        ("smCheck", 45.0, 0.0014, 0.58, 0.78),
        ("diagCross", 135.0, 0.0015, 0.60, 0.82),
    ]
    for i, (sval, ang, per, aniso, stoc) in enumerate(hatch_specs, start=1):
        assets.append(
            make_pattern_asset(
                asset_id=f"pattern.hatching.multi_v1_{i}",
                name_en=f"Hatching Multi-dir v1-{i}",
                name_zh=f"多方向网纹 v1-{i}",
                source_value=sval,
                tags=["hatching", "multi_dir", "office", "pattern"],
                orientation_deg=ang,
                period_norm=per,
                anisotropy=aniso,
                stochasticity=stoc,
                tile_ratio=per,
                min_conf=0.58,
                min_iou=0.74,
                max_token_ratio=0.72,
            )
        )

    # 3) dots/speckles
    dot_specs = [
        ("smGrid", 0.0011, 0.12, 0.72),
        ("lgGrid", 0.0018, 0.15, 0.68),
        ("dotGrid", 0.0013, 0.10, 0.75),
        ("solidDmnd", 0.0009, 0.18, 0.62),
    ]
    for i, (sval, per, aniso, stoc) in enumerate(dot_specs, start=1):
        assets.append(
            make_pattern_asset(
                asset_id=f"pattern.dots.speckle_v1_{i}",
                name_en=f"Dots Speckle v1-{i}",
                name_zh=f"点状纹理 v1-{i}",
                source_value=sval,
                tags=["dots", "speckle", "office", "pattern"],
                orientation_deg=None,
                period_norm=per,
                anisotropy=aniso,
                stochasticity=stoc,
                tile_ratio=per,
                min_conf=0.55,
                min_iou=0.72,
                max_token_ratio=0.75,
            )
        )

    # 4) dense fragments (fallback compiled-heavy textures)
    dense_specs = [
        ("wdDnDiag", 45.0, 0.0030, 0.35, 0.85),
        ("wdUpDiag", 135.0, 0.0030, 0.35, 0.85),
        ("plaid", 90.0, 0.0026, 0.42, 0.88),
    ]
    for i, (sval, ang, per, aniso, stoc) in enumerate(dense_specs, start=1):
        assets.append(
            make_pattern_asset(
                asset_id=f"pattern.dense.fragment_v1_{i}",
                name_en=f"Dense Fragment v1-{i}",
                name_zh=f"稠密碎片纹理 v1-{i}",
                source_value=sval,
                tags=["dense_fragments", "compiled", "office", "pattern"],
                orientation_deg=ang,
                period_norm=per,
                anisotropy=aniso,
                stochasticity=stoc,
                tile_ratio=per,
                min_conf=0.50,
                min_iou=0.70,
                max_token_ratio=0.80,
            )
        )

    data = {
        "schema_version": "1.0.0",
        "library_id": "physui.office.patterns.v1",
        "created_at": "2026-03-04",
        "assets": assets,
    }

    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved: {OUT_PATH}")
    print(f"assets: {len(assets)}")


if __name__ == "__main__":
    main()
