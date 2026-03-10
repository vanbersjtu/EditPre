"""Shared constants for SVG->PPTX compiler."""

import base64

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

# EMU conversion: 1 inch = 914400 EMU
EMU_PER_INCH = 914400

# Default DPI for SVG (pixels per inch)
DEFAULT_DPI = 96.0

# Freeform precision for vector reconstruction.
FREEFORM_LOCAL_UNITS = 10000

# Path sampling controls (when approximating Bezier/Arc to polyline).
PATH_MIN_SAMPLES_PER_SEGMENT = 24
PATH_MAX_SAMPLES_PER_SEGMENT = 240
SUPPORTED_IMAGE_ASPECT_RATIOS = (
    "1:1", "1:4", "1:8", "2:3", "3:2", "3:4", "4:1",
    "4:3", "4:5", "5:4", "8:1", "9:16", "16:9", "21:9",
)

REL_IMAGE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
SVG_BLIP_NS = "http://schemas.microsoft.com/office/drawing/2016/SVG/main"
SVG_BLIP_EXT_URI = "{96DAC541-7B7A-43D3-8B79-37D633B846F1}"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

# 1x1 transparent PNG fallback for picture shape shell.
TRANSPARENT_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+X2j8AAAAASUVORK5CYII="
)
