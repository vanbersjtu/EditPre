#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility wrapper for SVG->PPTX compiler CLI/module.

This file keeps the historical entrypoint path stable:
`gemini_pipeline/svg_to_pptx_pro.py`

Implementation now lives in `gemini_pipeline/compiler/`.
"""

from pathlib import Path
import sys

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from compiler import legacy_svg_to_pptx_pro as _legacy

# Re-export legacy module symbols for backward compatibility with
# existing local tooling importing `svg_to_pptx_pro`.
for _name in dir(_legacy):
    if _name.startswith("_"):
        continue
    globals()[_name] = getattr(_legacy, _name)

main = _legacy.main


if __name__ == "__main__":
    main()
