# PhysUI Asset Schema

This folder provides a bilingual-ready asset schema for PhysUI retrieval routing.

## Files

- `assets_schema.json`: JSON Schema (draft 2020-12)
- `asset_models.py`: Pydantic models + CLI validator
- `example_asset_library.json`: Valid sample library

## Quick Start

```bash
python3 /Users/xiaoxiaobo/physui_asset_schema/asset_models.py validate /Users/xiaoxiaobo/physui_asset_schema/example_asset_library.json
python3 /Users/xiaoxiaobo/physui_asset_schema/asset_models.py fields
python3 /Users/xiaoxiaobo/physui_asset_schema/asset_models.py export-schema /Users/xiaoxiaobo/physui_asset_schema/assets_schema_from_pydantic.json
```

## Notes (EN/ZH)

- Canonical keys stay in English for tooling interoperability.
- Chinese names and descriptions are stored in `canonical_name_zh` and `description_zh`.
- Use `routing_policy` + `quality_gate` to enforce safe rewrite acceptance.

## Asset Matching Validation (Dataset-level)

Run this to validate whether current asset library can match exploding-pattern paths:

```bash
python3 /Users/xiaoxiaobo/physui_asset_schema/asset_library_match_validator.py \
  --root /Users/xiaoxiaobo/random2000 \
  --asset_lib /Users/xiaoxiaobo/physui_asset_schema/example_asset_library.json \
  --out_prefix /Users/xiaoxiaobo/physui_asset_schema/random2000_asset_match_validation \
  --max_paths 1200
```

Outputs:

- `*.txt`: PASS/FAIL + overall and per-family coverage
- `*.csv`: sample-level best match records
- `*.png`: coverage bars + score distribution

## Build Office Pattern Library (v1)

Generate a richer pattern asset library (diagonal stripes + hatching + dots + dense fragments):

```bash
python3 /Users/xiaoxiaobo/physui_asset_schema/build_office_asset_library.py
python3 /Users/xiaoxiaobo/physui_asset_schema/asset_models.py validate /Users/xiaoxiaobo/physui_asset_schema/office_pattern_assets_v1.json
```

Validate the new library on random2000:

```bash
python3 /Users/xiaoxiaobo/physui_asset_schema/asset_library_match_validator.py \
  --root /Users/xiaoxiaobo/random2000 \
  --asset_lib /Users/xiaoxiaobo/physui_asset_schema/office_pattern_assets_v1.json \
  --out_prefix /Users/xiaoxiaobo/physui_asset_schema/random2000_asset_match_validation_v1 \
  --max_paths 1200
```
