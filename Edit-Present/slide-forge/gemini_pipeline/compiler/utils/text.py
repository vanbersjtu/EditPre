"""Text-related constants/helpers."""

import re

CJK_RE = re.compile(r"[\u3400-\u9fff\u3000-\u303f\u3040-\u30ff\u31f0-\u31ff\uac00-\ud7af]")

GENERIC_FONTS = {
    "sans-serif", "serif", "monospace", "system-ui",
    "ui-sans-serif", "ui-serif", "ui-monospace",
}


def has_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text))
