#!/usr/bin/env python3
"""PhysUI asset schema Pydantic models and validator CLI."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

AssetType = Literal["pattern", "texture", "gradient", "stroke"]
SourceStandard = Literal["OOXML", "VBA_MSO", "CUSTOM"]
SlotType = Literal["color", "float", "int", "bool", "string"]
FallbackPolicy = Literal["physics_fit", "keep_original", "manual_review"]

FIELD_BILINGUAL_MAP: Dict[str, str] = {
    "schema_version": "模式版本",
    "library_id": "资产库 ID",
    "asset_id": "资产唯一 ID",
    "asset_type": "资产类型",
    "canonical_name_en": "英文标准名",
    "canonical_name_zh": "中文标准名",
    "source_standard": "来源标准",
    "source_enum": "来源枚举名",
    "source_value": "来源枚举值",
    "tags": "检索标签",
    "retrieval_features": "检索特征",
    "param_slots": "参数槽位",
    "svg_template": "SVG 模板",
    "routing_policy": "路由策略",
    "quality_gate": "质量门控",
    "type_specific": "类型扩展字段",
    "provenance": "溯源信息",
}


class ParamSlot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: SlotType
    required: bool
    default: Optional[Union[str, float, int, bool]] = None
    description_en: Optional[str] = None
    description_zh: Optional[str] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not v:
            raise ValueError("name cannot be empty")
        if not (v[0].isalpha() or v[0] == "_"):
            raise ValueError("name must start with letter or underscore")
        for ch in v:
            if not (ch.isalnum() or ch == "_"):
                raise ValueError("name must contain only letters, digits, underscore")
        return v


class RetrievalFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid")

    orientation_deg: Optional[float] = None
    period_px_norm: Optional[float] = Field(default=None, ge=0)
    duty_cycle: Optional[float] = Field(default=None, ge=0, le=1)
    anisotropy: Optional[float] = Field(default=None, ge=0, le=1)
    frequency_peaks: List[float] = Field(default_factory=list)
    granularity: Optional[float] = Field(default=None, ge=0)
    stochasticity: Optional[float] = Field(default=None, ge=0, le=1)

    @field_validator("frequency_peaks")
    @classmethod
    def validate_peaks(cls, values: List[float]) -> List[float]:
        if any(v < 0 for v in values):
            raise ValueError("frequency_peaks must be non-negative")
        return values


class RoutingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_confidence: float = Field(ge=0, le=1)
    fallback: FallbackPolicy


class QualityGate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_iou: float = Field(ge=0, le=1)
    max_token_ratio: float = Field(gt=0)
    min_soft_iou: Optional[float] = Field(default=None, ge=0, le=1)


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    created_from: str
    license: str
    created_at: date
    notes: Optional[str] = None


class PatternSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    orientation_deg: float
    tile_ratio: float = Field(gt=0)


class TextureSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    texture_family: str
    frequency_band: Optional[List[float]] = None

    @field_validator("frequency_band")
    @classmethod
    def validate_freq_band(cls, v: Optional[List[float]]) -> Optional[List[float]]:
        if v is None:
            return v
        if len(v) != 2:
            raise ValueError("frequency_band must be [low, high]")
        if v[0] < 0 or v[1] < 0 or v[0] > v[1]:
            raise ValueError("frequency_band must satisfy 0 <= low <= high")
        return v


class GradientStop(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offset: float = Field(ge=0, le=1)
    color: str


class GradientSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gradient_kind: Literal["linear", "radial"]
    angle_deg: Optional[float] = None
    stops: List[GradientStop] = Field(min_length=2)


class StrokeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dash_array: Optional[List[float]] = None
    linecap: Optional[Literal["butt", "round", "square"]] = None
    linejoin: Optional[Literal["miter", "round", "bevel"]] = None

    @field_validator("dash_array")
    @classmethod
    def validate_dash_array(cls, v: Optional[List[float]]) -> Optional[List[float]]:
        if v is None:
            return v
        if any(x <= 0 for x in v):
            raise ValueError("dash_array must contain positive values")
        return v


class TypeSpecific(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pattern: Optional[PatternSpec] = None
    texture: Optional[TextureSpec] = None
    gradient: Optional[GradientSpec] = None
    stroke: Optional[StrokeSpec] = None


class Asset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    asset_type: AssetType
    canonical_name_en: str
    canonical_name_zh: str
    source_standard: SourceStandard
    source_enum: str
    source_value: Union[str, int]
    tags: List[str] = Field(default_factory=list)
    retrieval_features: RetrievalFeatures
    param_slots: List[ParamSlot] = Field(min_length=1)
    svg_template: str
    routing_policy: RoutingPolicy
    quality_gate: QualityGate
    type_specific: Optional[TypeSpecific] = None
    provenance: Provenance

    @field_validator("asset_id")
    @classmethod
    def validate_asset_id(cls, v: str) -> str:
        allowed = set("abcdefghijklmnopqrstuvwxyz0123456789._-")
        if not v:
            raise ValueError("asset_id cannot be empty")
        if any(ch not in allowed for ch in v):
            raise ValueError("asset_id must use lowercase letters/digits/._-")
        if v[0] in "._-" or v[-1] in "._-":
            raise ValueError("asset_id cannot start/end with separator")
        return v

    @model_validator(mode="after")
    def validate_slots_and_type_specific(self) -> "Asset":
        slot_names = [s.name for s in self.param_slots]
        if len(slot_names) != len(set(slot_names)):
            raise ValueError("param_slots.name must be unique within one asset")

        if self.type_specific is None:
            return self

        expected = self.asset_type
        options = {
            "pattern": self.type_specific.pattern,
            "texture": self.type_specific.texture,
            "gradient": self.type_specific.gradient,
            "stroke": self.type_specific.stroke,
        }

        for key, value in options.items():
            if key == expected and value is None:
                raise ValueError(f"type_specific.{key} is required for asset_type={expected}")
            if key != expected and value is not None:
                raise ValueError(f"type_specific.{key} must be null for asset_type={expected}")

        return self


class AssetLibrary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    library_id: str
    created_at: Optional[date] = None
    assets: List[Asset] = Field(min_length=1)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, v: str) -> str:
        parts = v.split(".")
        if len(parts) != 3 or any((not p.isdigit()) for p in parts):
            raise ValueError("schema_version must follow semver: major.minor.patch")
        return v

    @model_validator(mode="after")
    def ensure_unique_asset_id(self) -> "AssetLibrary":
        ids = [a.asset_id for a in self.assets]
        if len(ids) != len(set(ids)):
            raise ValueError("asset_id must be globally unique in one library")
        return self


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def cmd_validate(path: Path) -> int:
    data = load_json(path)
    try:
        if "assets" in data:
            lib = AssetLibrary.model_validate(data)
            print(f"OK: library_id={lib.library_id}, assets={len(lib.assets)}")
        else:
            asset = Asset.model_validate(data)
            print(f"OK: asset_id={asset.asset_id}, type={asset.asset_type}")
        return 0
    except ValidationError as e:
        print("Validation failed:")
        print(e)
        return 1


def cmd_export_json_schema(path: Path) -> int:
    schema = AssetLibrary.model_json_schema()
    with path.open("w", encoding="utf-8") as f:
        json.dump(schema, f, ensure_ascii=False, indent=2)
    print(f"Exported Pydantic JSON schema to {path}")
    return 0


def cmd_print_bilingual_fields() -> int:
    print("PhysUI Asset Fields (EN -> ZH)")
    for k, v in FIELD_BILINGUAL_MAP.items():
        print(f"- {k}: {v}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="PhysUI asset schema helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="Validate JSON asset/library file")
    validate_parser.add_argument("path", type=Path)

    export_parser = subparsers.add_parser("export-schema", help="Export JSON schema from Pydantic")
    export_parser.add_argument("path", type=Path)

    subparsers.add_parser("fields", help="Print EN/ZH field mapping")

    args = parser.parse_args()

    if args.command == "validate":
        return cmd_validate(args.path)
    if args.command == "export-schema":
        return cmd_export_json_schema(args.path)
    if args.command == "fields":
        return cmd_print_bilingual_fields()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
