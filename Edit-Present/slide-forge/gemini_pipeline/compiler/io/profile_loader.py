"""Profile loading and overrides for multi-task pipeline/compiler behavior."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_PROFILE = "slide"
SUPPORTED_PROFILES = ("slide", "figure", "poster")
DEFAULT_PROFILE_DIR = Path(__file__).resolve().parents[2] / "profiles"


def _deep_merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_profile_spec(profile: str, profile_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Load profile spec JSON. Returns {} if missing/invalid."""
    name = str(profile or DEFAULT_PROFILE).strip().lower() or DEFAULT_PROFILE
    if name not in SUPPORTED_PROFILES:
        raise ValueError(
            f"Unsupported profile '{name}', expected one of {', '.join(SUPPORTED_PROFILES)}"
        )

    root = Path(profile_dir).expanduser() if profile_dir else DEFAULT_PROFILE_DIR
    cfg_path = root / f"{name}.json"
    if not cfg_path.exists():
        return {}

    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    data["_profile_dir"] = str(root.resolve())
    data["_profile_path"] = str(cfg_path.resolve())
    return data


def apply_profile_overrides(
    base_config: Dict[str, Any],
    profile_spec: Dict[str, Any],
    section: str,
) -> Dict[str, Any]:
    """Merge profile section dict into base config."""
    if not isinstance(base_config, dict):
        base_config = {}
    if not isinstance(profile_spec, dict):
        return dict(base_config)
    overrides = profile_spec.get(section)
    if not isinstance(overrides, dict):
        return dict(base_config)
    return _deep_merge_dict(dict(base_config), overrides)


def resolve_profile_prompt_file(
    profile_spec: Dict[str, Any],
    base_dir: Path,
) -> Optional[Path]:
    """Resolve pipeline.prompt_file in profile spec to an absolute path."""
    if not isinstance(profile_spec, dict):
        return None
    pipeline = profile_spec.get("pipeline")
    if not isinstance(pipeline, dict):
        return None
    raw = str(pipeline.get("prompt_file") or "").strip()
    if not raw:
        return None

    p = Path(raw).expanduser()
    if p.is_absolute():
        return p if p.exists() else None

    candidate = (base_dir / p).resolve()
    if candidate.exists():
        return candidate

    profile_root = Path(profile_spec.get("_profile_dir") or "").expanduser()
    if str(profile_root):
        candidate2 = (profile_root / p).resolve()
        if candidate2.exists():
            return candidate2
    return None
