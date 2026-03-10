"""Config and placeholder JSON loading helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def load_config(config_path: Optional[Path]) -> Dict[str, Any]:
    """Load config JSON for API and runtime settings."""
    if not config_path or not config_path.exists():
        return {}
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Backward-compatible key normalization for chart codegen config.
    # Accept both compiler keys and pipeline-style env-style keys.
    if not isinstance(data, dict):
        return {}

    normalized = dict(data)
    if "base_url" not in normalized and "DEFAULT_API_BASE" in normalized:
        normalized["base_url"] = normalized.get("DEFAULT_API_BASE")
    if "api_key" not in normalized and "OPENAI_API_KEY" in normalized:
        normalized["api_key"] = normalized.get("OPENAI_API_KEY")
    if "chart_model" not in normalized and "DEFAULT_MODEL" in normalized:
        normalized["chart_model"] = normalized.get("DEFAULT_MODEL")
    if "image_api_base" not in normalized and "IMAGE_API_BASE" in normalized:
        normalized["image_api_base"] = normalized.get("IMAGE_API_BASE")
    if "image_model" not in normalized and "IMAGE_MODEL" in normalized:
        normalized["image_model"] = normalized.get("IMAGE_MODEL")
    if "image_api_key" not in normalized and "OPENAI_API_KEY" in normalized:
        normalized["image_api_key"] = normalized.get("OPENAI_API_KEY")
    if "image_api_key" not in normalized and "GEMINI_API_KEY" in normalized:
        normalized["image_api_key"] = normalized.get("GEMINI_API_KEY")
    return normalized


def load_placeholders(json_path: Optional[Path]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Load image placeholder mapping keyed by (svg_id, placeholder_id)."""
    if not json_path or not json_path.exists():
        return {}
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    mapping: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for entry in data:
        placeholder_id = entry.get("placeholder_id", "")
        if not placeholder_id:
            continue
        candidates = [
            entry.get("svg_path", ""),
            entry.get("svg_relpath", ""),
            entry.get("svg_rel_path", ""),
            entry.get("svg_file", ""),
        ]
        for svg_id in candidates:
            if svg_id:
                mapping[(svg_id, placeholder_id)] = entry
    return mapping
