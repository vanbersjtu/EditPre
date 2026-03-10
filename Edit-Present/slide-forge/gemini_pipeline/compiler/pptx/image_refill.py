"""Image refill helpers for placeholder generation/source-crop workflows."""

from __future__ import annotations

import base64
import colorsys
import hashlib
import io
import json
import math
import os
import re
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..constants import SUPPORTED_IMAGE_ASPECT_RATIOS

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

    HAS_PIL = True
except Exception:
    Image = None
    ImageChops = None
    ImageDraw = None
    ImageFilter = None
    ImageStat = None
    HAS_PIL = False

try:
    from rembg import remove as rembg_remove

    HAS_REMBG = True
except Exception:
    rembg_remove = None
    HAS_REMBG = False

try:
    from google import genai as google_genai
    from google.genai import types as google_genai_types

    HAS_GOOGLE_GENAI = True
except Exception:
    google_genai = None
    google_genai_types = None
    HAS_GOOGLE_GENAI = False


def parse_aspect_ratio(aspect_ratio: str) -> Optional[float]:
    """Parse ratio string like 16:9 into float."""
    try:
        left, right = [x.strip() for x in aspect_ratio.split(":", 1)]
        a = float(left)
        b = float(right)
        if a > 0 and b > 0:
            return a / b
    except Exception:
        return None
    return None


def pick_supported_aspect_ratio(width: float, height: float) -> str:
    """Pick nearest supported aspect ratio for Gemini image generation."""
    if width <= 0 or height <= 0:
        return "1:1"
    target = width / height
    best = "1:1"
    best_score = float("inf")
    for ratio in SUPPORTED_IMAGE_ASPECT_RATIOS:
        val = parse_aspect_ratio(ratio)
        if not val:
            continue
        score = abs(math.log(max(target, 1e-6)) - math.log(max(val, 1e-6)))
        if score < best_score:
            best_score = score
            best = ratio
    return best


def rect_intersection_area(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    """Compute intersection area of two rect dicts with x/y/w/h."""
    ax = float(a.get("x") or 0.0)
    ay = float(a.get("y") or 0.0)
    aw = float(a.get("w") or 0.0)
    ah = float(a.get("h") or 0.0)
    bx = float(b.get("x") or 0.0)
    by = float(b.get("y") or 0.0)
    bw = float(b.get("w") or 0.0)
    bh = float(b.get("h") or 0.0)
    if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return 0.0
    left = max(ax, bx)
    top = max(ay, by)
    right = min(ax + aw, bx + bw)
    bottom = min(ay + ah, by + bh)
    if right <= left or bottom <= top:
        return 0.0
    return (right - left) * (bottom - top)


def _iter_genai_parts(response: Any) -> List[Any]:
    """Collect candidate response parts from google-genai response object."""
    parts: List[Any] = []
    direct_parts = getattr(response, "parts", None)
    if direct_parts:
        parts.extend(list(direct_parts))
    candidates = getattr(response, "candidates", None) or []
    for cand in candidates:
        content = getattr(cand, "content", None)
        cand_parts = getattr(content, "parts", None) if content is not None else None
        if cand_parts:
            parts.extend(list(cand_parts))
    return parts


def _is_google_api_base(api_base: str) -> bool:
    """Return True if endpoint looks like a Google Gemini endpoint."""
    base = (api_base or "").strip().lower()
    if not base:
        return False
    return (
        "googleapis.com" in base
        or "ai.google.dev" in base
        or "generativelanguage.googleapis.com" in base
        or "aiplatform.googleapis.com" in base
    )


def _extract_data_uri_images(text: str) -> List[bytes]:
    """Extract image bytes from data:image/...;base64,... in text."""
    out: List[bytes] = []
    if not text:
        return out
    pattern = re.compile(
        r"data:image/[a-zA-Z0-9.+-]+;base64,([A-Za-z0-9+/=\r\n]+)",
        re.IGNORECASE,
    )
    for m in pattern.findall(text):
        try:
            out.append(base64.b64decode(m))
        except Exception:
            pass
    return out


def _extract_openai_compat_image_blobs(data: Dict[str, Any]) -> List[bytes]:
    """Extract image blobs from OpenAI-compatible chat completion response."""
    blobs: List[bytes] = []

    def _consume_content(content: Any) -> None:
        if isinstance(content, str):
            blobs.extend(_extract_data_uri_images(content))
            return
        if isinstance(content, list):
            for item in content:
                if isinstance(item, str):
                    blobs.extend(_extract_data_uri_images(item))
                    continue
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("text"), str):
                    blobs.extend(_extract_data_uri_images(item.get("text") or ""))
                if isinstance(item.get("content"), str):
                    blobs.extend(_extract_data_uri_images(item.get("content") or ""))
                if isinstance(item.get("b64_json"), str):
                    try:
                        blobs.append(base64.b64decode(item["b64_json"]))
                    except Exception:
                        pass
                image_url = item.get("image_url")
                if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
                    blobs.extend(_extract_data_uri_images(image_url.get("url") or ""))

    if isinstance(data.get("data"), list):
        for item in data.get("data") or []:
            if isinstance(item, dict) and isinstance(item.get("b64_json"), str):
                try:
                    blobs.append(base64.b64decode(item["b64_json"]))
                except Exception:
                    pass

    for ch in data.get("choices") or []:
        if not isinstance(ch, dict):
            continue
        msg = ch.get("message") or {}
        if isinstance(msg, dict):
            _consume_content(msg.get("content"))
        _consume_content(ch.get("text"))

    return blobs


def _call_openai_compat_image(
    *,
    api_base: str,
    api_key: str,
    model: str,
    prompt: str,
    aspect_ratio: str,
    image_size: str,
    seed_image: Optional[Path] = None,
    timeout_sec: int = 180,
) -> Tuple[Optional[bytes], Optional[str]]:
    """Generate image via OpenAI-compatible /chat/completions endpoint."""
    try:
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        if seed_image is not None and seed_image.exists():
            mime = guess_image_mime(seed_image)
            seed_b64 = base64.b64encode(seed_image.read_bytes()).decode("utf-8")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{seed_b64}"},
                }
            )

        hint = (
            "\n\nRendering constraints:\n"
            f"- target_aspect_ratio: {aspect_ratio}\n"
            f"- target_image_size: {image_size}\n"
            "- Return an image result."
        )
        content[0]["text"] = str(content[0]["text"]) + hint

        body = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 4096,
            "temperature": 0.2,
        }

        url = f"{api_base.rstrip('/')}/chat/completions"
        req = urllib.request.Request(
            url=url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as r:
            raw = r.read().decode("utf-8")
        data = json.loads(raw)
        if isinstance(data, dict) and "error" in data:
            err = data.get("error") or {}
            return None, f"OpenAI-compatible image API error: {err}"
        blobs = _extract_openai_compat_image_blobs(data if isinstance(data, dict) else {})
        if not blobs:
            return None, "OpenAI-compatible image API returned no image blob"
        return blobs[0], None
    except Exception as exc:
        return None, f"OpenAI-compatible image API call failed: {exc}"


def _normalize_to_rgba_image(obj: Any) -> Optional[Any]:
    """Best-effort normalize rembg/model output to PIL RGBA image."""
    if not HAS_PIL:
        return None
    try:
        if hasattr(obj, "convert"):
            return obj.convert("RGBA")
    except Exception:
        pass
    try:
        if isinstance(obj, (bytes, bytearray)):
            return Image.open(io.BytesIO(bytes(obj))).convert("RGBA")
    except Exception:
        pass
    return None


def _apply_rembg_with_validation(
    img_rgba: Any,
    image_refill_config: Dict[str, Any],
    *,
    min_ratio_key: str = "rembg_min_nonopaque_ratio",
    max_ratio_key: str = "rembg_max_nonopaque_ratio",
) -> Tuple[Optional[Any], Optional[str]]:
    """Run rembg and validate alpha coverage."""
    if not HAS_REMBG or rembg_remove is None:
        return None, "rembg not available"
    if not HAS_PIL:
        return None, "Pillow not available"

    try:
        out = rembg_remove(img_rgba)
    except Exception as exc:
        return None, f"rembg failed: {exc}"

    out_img = _normalize_to_rgba_image(out)
    if out_img is None:
        return None, "rembg returned unsupported image type"
    if "A" not in out_img.getbands():
        return None, "rembg output has no alpha channel"

    alpha = out_img.getchannel("A")
    hist = alpha.histogram()
    total = max(sum(hist), 1)
    opaque = hist[255]
    transparent = hist[0]
    nonopaque_ratio = float(total - opaque) / float(total)
    fully_transparent_ratio = float(transparent) / float(total)
    min_ratio = float(image_refill_config.get(min_ratio_key, 0.02) or 0.02)
    max_ratio = float(image_refill_config.get(max_ratio_key, 0.995) or 0.995)

    if nonopaque_ratio < min_ratio:
        return None, f"rembg alpha too weak ({nonopaque_ratio:.4f} < {min_ratio:.4f})"
    if fully_transparent_ratio > max_ratio:
        return None, f"rembg alpha too aggressive ({fully_transparent_ratio:.4f} > {max_ratio:.4f})"

    return out_img, None


def _alpha_stats(img_rgba: Any) -> Tuple[float, float, float, Tuple[int, int]]:
    """Return alpha stats: nonopaque, transparent, semitransparent ratios and extrema."""
    if not HAS_PIL:
        return 0.0, 0.0, 0.0, (255, 255)
    if "A" not in img_rgba.getbands():
        return 0.0, 0.0, 0.0, (255, 255)
    alpha = img_rgba.getchannel("A")
    hist = alpha.histogram()
    total = max(sum(hist), 1)
    opaque = hist[255]
    transparent = hist[0]
    semi = max(total - opaque - transparent, 0)
    return (
        float(total - opaque) / float(total),
        float(transparent) / float(total),
        float(semi) / float(total),
        alpha.getextrema(),
    )


def _normalize_remove_bg_mode(remove_bg_mode: Optional[str]) -> str:
    raw = str(remove_bg_mode or "").strip().lower()
    if raw in ("flat", "chroma", "key"):
        return "flat"
    if raw in ("photo", "rembg", "model"):
        return "photo"
    if raw in ("photo-hard", "photo_hard", "rembg-hard", "rembg_hard"):
        return "photo-hard"
    return "auto"


def _resolve_remove_bg_mode(
    caption: str,
    image_refill_config: Dict[str, Any],
    remove_bg_mode: Optional[str],
) -> str:
    mode = _normalize_remove_bg_mode(
        remove_bg_mode
        or image_refill_config.get("remove_bg_mode_default")
        or "auto"
    )
    if mode != "auto":
        return mode
    text = str(caption or "").strip().lower()
    flat_tokens = (
        "flat",
        "vector",
        "silhouette",
        "icon",
        "logo",
        "map",
        "wireframe",
        "watercolor",
        "leaf",
        "leaves",
        "sticker",
        "clipart",
        "decorative",
        "ornament",
        "corner",
        "isometric",
        "diagram",
        "isolated object",
        "transparent background",
    )
    if any(tok in text for tok in flat_tokens):
        return "flat"
    return "photo"


def _apply_flat_remove_bg_with_validation(
    img_rgba: Any,
    image_refill_config: Dict[str, Any],
) -> Tuple[Optional[Any], Optional[str]]:
    """Remove background using color-key style cutout for flat/vector-like assets."""
    if not HAS_PIL:
        return None, "Pillow not available"
    try:
        if bool(image_refill_config.get("flat_chroma_enabled", True)):
            out = _apply_green_chroma_alpha(img_rgba, image_refill_config)
        else:
            bg = estimate_border_bg_color(
                img_rgba, border=int(image_refill_config.get("flat_remove_bg_border", 2) or 2)
            )
            out = apply_background_alpha(
                img_rgba,
                bg_color=bg,
                threshold=float(image_refill_config.get("flat_remove_bg_threshold", 16.0) or 16.0),
                feather=float(image_refill_config.get("flat_remove_bg_feather", 2.0) or 2.0),
            )

        if bool(image_refill_config.get("flat_remove_small_green_islands", True)):
            out = _remove_small_green_islands(out, image_refill_config)

        if bool(image_refill_config.get("flat_despill_enabled", True)):
            out = _despill_green_edges(out, image_refill_config)

        hard_thr = int(image_refill_config.get("flat_remove_bg_hard_threshold", 242) or 242)
        hard_blur = float(image_refill_config.get("flat_remove_bg_hard_blur", 0.8) or 0.8)
        if "A" in out.getbands():
            alpha = out.getchannel("A")
            if hard_blur > 0:
                alpha = alpha.filter(ImageFilter.GaussianBlur(radius=hard_blur))
            alpha = alpha.point(lambda p: 255 if p >= hard_thr else 0, mode="L")
            out.putalpha(alpha)
        nonopaque_ratio, transparent_ratio, _, _ = _alpha_stats(out)
        min_ratio = float(image_refill_config.get("flat_remove_bg_min_nonopaque_ratio", 0.01) or 0.01)
        max_ratio = float(image_refill_config.get("flat_remove_bg_max_transparent_ratio", 0.999) or 0.999)
        if nonopaque_ratio < min_ratio:
            return None, f"flat alpha too weak ({nonopaque_ratio:.4f} < {min_ratio:.4f})"
        if transparent_ratio > max_ratio:
            return None, f"flat alpha too aggressive ({transparent_ratio:.4f} > {max_ratio:.4f})"
        return out, None
    except Exception as exc:
        return None, f"flat remove-bg failed: {exc}"


def _apply_green_chroma_alpha(
    img_rgba: Any,
    image_refill_config: Dict[str, Any],
) -> Any:
    """Apply green-screen chroma key alpha for flat/icon-like assets."""
    if not HAS_PIL:
        return img_rgba
    rgb = img_rgba.convert("RGB")
    w, h = rgb.size
    pix = list(rgb.getdata())

    hue_low = float(image_refill_config.get("flat_chroma_hue_low", 70.0) or 70.0) / 360.0
    hue_high = float(image_refill_config.get("flat_chroma_hue_high", 170.0) or 170.0) / 360.0
    sat_min = float(image_refill_config.get("flat_chroma_sat_min", 0.18) or 0.18)
    val_min = float(image_refill_config.get("flat_chroma_val_min", 0.10) or 0.10)
    g_min_weak = int(image_refill_config.get("flat_chroma_g_min_weak", 90) or 90)
    g_min_strong = int(image_refill_config.get("flat_chroma_g_min_strong", 120) or 120)
    dom_weak = int(image_refill_config.get("flat_chroma_dom_weak", 18) or 18)
    dom_strong = int(image_refill_config.get("flat_chroma_dom_strong", 42) or 42)
    smooth_blur = float(image_refill_config.get("flat_chroma_alpha_blur", 0.6) or 0.6)

    alpha_data: List[int] = []
    for r, g, b in pix:
        mx = max(r, g, b)
        mn = min(r, g, b)
        sat = 0.0 if mx == 0 else (mx - mn) / float(mx)
        val = mx / 255.0
        h_norm = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)[0]
        in_hue = (hue_low <= h_norm <= hue_high)
        dom = g - max(r, b)

        if (not in_hue) or sat < sat_min or val < val_min:
            alpha_data.append(255)
            continue

        if g >= g_min_strong and dom >= dom_strong:
            alpha_data.append(0)
            continue

        if g >= g_min_weak and dom >= dom_weak:
            lo = max(dom_weak, 1)
            hi = max(dom_strong, lo + 1)
            t = (min(max(dom, lo), hi) - lo) / float(hi - lo)
            alpha_data.append(int(round(255.0 * (1.0 - t))))
            continue

        alpha_data.append(255)

    alpha = Image.new("L", (w, h))
    alpha.putdata(alpha_data)
    if smooth_blur > 0:
        alpha = alpha.filter(ImageFilter.GaussianBlur(radius=smooth_blur))

    out = img_rgba.copy()
    out.putalpha(alpha)
    return out


def _remove_small_green_islands(
    img_rgba: Any,
    image_refill_config: Dict[str, Any],
) -> Any:
    """Remove tiny green-dominant opaque islands left after chroma cutout."""
    if not HAS_PIL:
        return img_rgba

    min_area = int(image_refill_config.get("flat_green_island_min_area", 20) or 20)
    if min_area <= 0:
        return img_rgba
    green_dom = int(image_refill_config.get("flat_green_island_dom", 18) or 18)
    green_ratio_min = float(image_refill_config.get("flat_green_island_ratio_min", 0.55) or 0.55)

    rgb = img_rgba.convert("RGB")
    alpha = img_rgba.getchannel("A")
    w, h = alpha.size
    a_data = list(alpha.getdata())
    rgb_data = list(rgb.getdata())
    n = w * h
    visited = bytearray(n)

    def idx_xy(i: int) -> Tuple[int, int]:
        return (i % w, i // w)

    for start in range(n):
        if visited[start] or a_data[start] == 0:
            visited[start] = 1
            continue
        stack = [start]
        comp: List[int] = []
        greenish = 0
        while stack:
            cur = stack.pop()
            if visited[cur]:
                continue
            visited[cur] = 1
            if a_data[cur] == 0:
                continue
            comp.append(cur)
            r, g, b = rgb_data[cur]
            if g > r + green_dom and g > b + green_dom:
                greenish += 1

            x, y = idx_xy(cur)
            if x > 0:
                stack.append(cur - 1)
            if x + 1 < w:
                stack.append(cur + 1)
            if y > 0:
                stack.append(cur - w)
            if y + 1 < h:
                stack.append(cur + w)

        area = len(comp)
        if area == 0:
            continue
        if area <= min_area and (greenish / float(area)) >= green_ratio_min:
            for p in comp:
                a_data[p] = 0

    out = img_rgba.copy()
    new_alpha = Image.new("L", (w, h))
    new_alpha.putdata(a_data)
    out.putalpha(new_alpha)
    return out


def _despill_green_edges(
    img_rgba: Any,
    image_refill_config: Dict[str, Any],
) -> Any:
    """Reduce green spill on semi-transparent edges after chroma key."""
    if not HAS_PIL:
        return img_rgba
    strength = float(image_refill_config.get("flat_despill_strength", 0.8) or 0.8)
    if strength <= 0:
        return img_rgba
    alpha_max = int(image_refill_config.get("flat_despill_alpha_max", 252) or 252)
    dom_thr = int(image_refill_config.get("flat_despill_dom_threshold", 8) or 8)

    rgba = img_rgba.convert("RGBA")
    px = list(rgba.getdata())
    out_px: List[Tuple[int, int, int, int]] = []
    for r, g, b, a in px:
        if a <= 0:
            out_px.append((r, g, b, a))
            continue
        if a <= alpha_max and g > max(r, b) + dom_thr:
            target = max(r, b)
            new_g = int(round(target + (g - target) * max(0.0, 1.0 - strength)))
            out_px.append((r, max(0, min(255, new_g)), b, a))
        else:
            out_px.append((r, g, b, a))

    out = Image.new("RGBA", rgba.size)
    out.putdata(out_px)
    return out


def _harden_alpha_edges(
    img_rgba: Any,
    threshold: int,
    blur_radius: float,
) -> Any:
    if not HAS_PIL:
        return img_rgba
    if "A" not in img_rgba.getbands():
        return img_rgba
    out = img_rgba.copy()
    alpha = out.getchannel("A")
    if blur_radius > 0:
        alpha = alpha.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    alpha = alpha.point(lambda p: 255 if p >= threshold else 0, mode="L")
    out.putalpha(alpha)
    return out


def _maybe_harden_rembg_edges(
    img_rgba: Any,
    image_refill_config: Dict[str, Any],
) -> Any:
    if not bool(image_refill_config.get("rembg_post_hard_enabled", True)):
        return img_rgba
    _, _, semi_ratio, _ = _alpha_stats(img_rgba)
    trigger = float(image_refill_config.get("rembg_post_hard_trigger_semi_ratio", 0.2) or 0.2)
    if semi_ratio <= trigger:
        return img_rgba
    hard_thr = int(image_refill_config.get("rembg_post_hard_threshold", 238) or 238)
    hard_blur = float(image_refill_config.get("rembg_post_hard_blur", 0.7) or 0.7)
    return _harden_alpha_edges(img_rgba, threshold=hard_thr, blur_radius=hard_blur)


def _apply_remove_bg_strategy(
    img_rgba: Any,
    caption: str,
    image_refill_config: Dict[str, Any],
    remove_bg_mode: Optional[str],
) -> Tuple[Optional[Any], Optional[str], str]:
    """Apply selected remove-bg strategy and return (image, error, resolved_mode)."""
    mode = _resolve_remove_bg_mode(caption, image_refill_config, remove_bg_mode)
    if mode == "flat":
        flat_img, flat_err = _apply_flat_remove_bg_with_validation(img_rgba, image_refill_config)
        if flat_img is not None:
            return flat_img, None, mode
        if bool(image_refill_config.get("flat_mode_fallback_rembg", True)):
            rembg_img, rembg_err = _apply_rembg_with_validation(img_rgba, image_refill_config)
            if rembg_img is not None:
                return _maybe_harden_rembg_edges(rembg_img, image_refill_config), None, "photo-fallback"
            return None, f"{flat_err}; rembg fallback failed: {rembg_err}", mode
        return None, flat_err, mode

    rembg_img, rembg_err = _apply_rembg_with_validation(img_rgba, image_refill_config)
    if rembg_img is None:
        return None, rembg_err, mode
    if mode == "photo-hard":
        hard_thr = int(image_refill_config.get("rembg_post_hard_threshold", 238) or 238)
        hard_blur = float(image_refill_config.get("rembg_post_hard_blur", 0.7) or 0.7)
        rembg_img = _harden_alpha_edges(rembg_img, threshold=hard_thr, blur_radius=hard_blur)
    else:
        rembg_img = _maybe_harden_rembg_edges(rembg_img, image_refill_config)
    return rembg_img, None, mode


def generate_placeholder_image(
    caption: str,
    width: float,
    height: float,
    image_refill_config: Dict[str, Any],
    remove_bg: Optional[bool] = None,
    remove_bg_mode: Optional[str] = None,
) -> Tuple[Optional[Path], bool, Optional[str]]:
    """Generate one image from placeholder caption and return local file path."""
    if not image_refill_config.get("enabled"):
        return None, False, "image refill not enabled"
    if not HAS_PIL:
        return None, False, "Pillow not available"

    api_base = str(image_refill_config.get("api_base") or "").strip()
    is_google_base = _is_google_api_base(api_base)
    api_key = str(image_refill_config.get("api_key") or "").strip()
    if not api_key:
        if is_google_base:
            api_key = str(os.environ.get("GEMINI_API_KEY", "")).strip()
        else:
            api_key = str(os.environ.get("OPENAI_API_KEY", "")).strip()
    if not api_key:
        return None, False, "missing image API key"

    model = str(image_refill_config.get("model") or "gemini-3.1-flash-image-preview").strip()
    image_size = str(image_refill_config.get("image_size") or "2K").strip()
    forced_ratio = str(image_refill_config.get("aspect_ratio") or "").strip()
    style_prompt = str(image_refill_config.get("style_prompt") or "").strip()
    cache_dir = Path(image_refill_config.get("cache_dir") or ".")
    cache_dir.mkdir(parents=True, exist_ok=True)

    aspect_ratio = forced_ratio if forced_ratio else pick_supported_aspect_ratio(width, height)
    if parse_aspect_ratio(aspect_ratio) is None:
        aspect_ratio = pick_supported_aspect_ratio(width, height)

    resolved_remove_bg_mode = (
        _resolve_remove_bg_mode(caption, image_refill_config, remove_bg_mode)
        if bool(remove_bg)
        else ""
    )
    prompt = caption.strip()
    if style_prompt:
        prompt = f"{prompt}\n\nStyle constraints: {style_prompt}"
    if bool(remove_bg):
        generic_style = str(image_refill_config.get("remove_bg_style_prompt") or "").strip()
        if generic_style:
            remove_bg_style_prompt = generic_style
        elif resolved_remove_bg_mode == "flat":
            remove_bg_style_prompt = str(
                image_refill_config.get("remove_bg_style_prompt_flat")
                or (
                    "Generate only the foreground subject on a solid chroma-key green background (#00FF00). "
                    "Background must be uniform green with no gradients, no shadows, no texture. "
                    "Do not use this chroma green color on the foreground subject. "
                    "No text labels, no logos, no watermark."
                )
            ).strip()
        else:
            remove_bg_style_prompt = str(
                image_refill_config.get("remove_bg_style_prompt_photo")
                or (
                    "Generate only the foreground subject on a pure white (#FFFFFF) background. "
                    "No cast shadows, no smoky haze, no gradients, no textures, no extra background objects, "
                    "no text labels, no logos, no watermark."
                )
            ).strip()
        if remove_bg_style_prompt:
            prompt = f"{prompt}\n\nBackground constraints: {remove_bg_style_prompt}"

    key_payload = {
        "model": model,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "image_size": image_size,
        "remove_bg": bool(remove_bg),
        "remove_bg_mode": resolved_remove_bg_mode,
    }
    cache_key = hashlib.sha256(
        json.dumps(key_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    cache_name = f"{cache_key[:24]}.png"
    out_path = cache_dir / cache_name

    memo = image_refill_config.setdefault("_memo", {})
    if cache_key in memo:
        cached = Path(memo[cache_key])
        if cached.exists():
            return cached, False, None
    if out_path.exists():
        memo[cache_key] = str(out_path)
        return out_path, False, None

    def _build_config(include_image_size: bool) -> Any:
        image_kwargs: Dict[str, Any] = {"aspect_ratio": aspect_ratio}
        if include_image_size and image_size:
            image_kwargs["image_size"] = image_size
        return google_genai_types.GenerateContentConfig(
            image_config=google_genai_types.ImageConfig(**image_kwargs)
        )

    response = None
    last_exc: Optional[Exception] = None

    def _finalize_image(out_img: Any) -> Tuple[Optional[Path], bool, Optional[str]]:
        if bool(remove_bg):
            cutout_img, cutout_err, used_mode = _apply_remove_bg_strategy(
                out_img, caption=caption, image_refill_config=image_refill_config, remove_bg_mode=remove_bg_mode
            )
            if cutout_img is not None:
                out_img = cutout_img
            elif bool(image_refill_config.get("remove_bg_require_rembg", False)):
                return (
                    None,
                    False,
                    f"remove-bg required but failed (mode={resolved_remove_bg_mode or used_mode}): {cutout_err}",
                )
        out_img.save(out_path, format="PNG")
        memo[cache_key] = str(out_path)
        return out_path, True, None

    use_openai_compat = bool(api_base) and (not is_google_base)
    if use_openai_compat:
        blob, compat_err = _call_openai_compat_image(
            api_base=api_base,
            api_key=api_key,
            model=model,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            image_size=image_size,
            seed_image=None,
        )
        if blob is None:
            return None, False, compat_err or "OpenAI-compatible image call failed"
        try:
            out_img = Image.open(io.BytesIO(blob)).convert("RGBA")
        except Exception as exc:
            return None, False, f"OpenAI-compatible image decode failed: {exc}"
        return _finalize_image(out_img)

    if not HAS_GOOGLE_GENAI or google_genai is None or google_genai_types is None:
        return None, False, "google-genai SDK not available"

    try_orders = [True, False] if image_size else [False]
    for include_image_size in try_orders:
        try:
            client = google_genai.Client(api_key=api_key)
            config = _build_config(include_image_size)
            response = client.models.generate_content(
                model=model,
                contents=[prompt],
                config=config,
            )
            break
        except Exception as exc:
            last_exc = exc
            msg = str(exc).lower()
            if include_image_size and (
                "image_size" in msg or "extra_forbidden" in msg or "validation error" in msg
            ):
                # Older google-genai versions may not support image_size in ImageConfig.
                continue
            break

    if response is None:
        return None, False, f"Gemini image API call failed: {last_exc}"

    for part in _iter_genai_parts(response):
        try:
            if getattr(part, "inline_data", None) is not None:
                inline_data = getattr(part, "inline_data")
                data = getattr(inline_data, "data", None)
                if isinstance(data, str):
                    blob = base64.b64decode(data)
                else:
                    blob = bytes(data or b"")
                if blob:
                    out_img = Image.open(io.BytesIO(blob)).convert("RGBA")
                    return _finalize_image(out_img)
        except Exception:
            pass
        try:
            as_image_fn = getattr(part, "as_image", None)
            if callable(as_image_fn):
                img = as_image_fn()
                out_img = img.convert("RGBA") if hasattr(img, "convert") else _normalize_to_rgba_image(img)
                if out_img is None:
                    out_img = Image.open(io.BytesIO(img.tobytes())).convert("RGBA")  # type: ignore[attr-defined]
                return _finalize_image(out_img)
        except Exception:
            pass

    return None, False, "no image data returned by model"


def guess_image_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".gif":
        return "image/gif"
    return "image/png"


def redraw_placeholder_crop_without_text(
    seed_image: Path,
    caption: str,
    width: float,
    height: float,
    image_refill_config: Dict[str, Any],
) -> Tuple[Optional[Path], bool, Optional[str]]:
    """Use image-to-image generation to redraw cropped placeholder image without text."""
    if not image_refill_config.get("enabled"):
        return None, False, "image refill not enabled"
    if not HAS_PIL:
        return None, False, "Pillow not available"
    if not seed_image.exists():
        return None, False, f"seed image not found: {seed_image}"

    api_base = str(image_refill_config.get("api_base") or "").strip()
    is_google_base = _is_google_api_base(api_base)
    api_key = str(image_refill_config.get("api_key") or "").strip()
    if not api_key:
        if is_google_base:
            api_key = str(os.environ.get("GEMINI_API_KEY", "")).strip()
        else:
            api_key = str(os.environ.get("OPENAI_API_KEY", "")).strip()
    if not api_key:
        return None, False, "missing image API key"

    model = str(
        image_refill_config.get("source_crop_redraw_model")
        or image_refill_config.get("model")
        or "gemini-3.1-flash-image-preview"
    ).strip()
    image_size = str(image_refill_config.get("image_size") or "2K").strip()
    forced_ratio = str(image_refill_config.get("aspect_ratio") or "").strip()
    cache_dir = Path(image_refill_config.get("cache_dir") or ".")
    cache_dir.mkdir(parents=True, exist_ok=True)

    aspect_ratio = forced_ratio if forced_ratio else pick_supported_aspect_ratio(width, height)
    if parse_aspect_ratio(aspect_ratio) is None:
        aspect_ratio = pick_supported_aspect_ratio(width, height)

    default_prompt = (
        "Redraw this image region with the same semantic content, composition, perspective and style. "
        "Remove all text, letters, numbers, symbols, watermarks and logos from the generated image. "
        "Do not add any new text. Keep it clean and natural."
    )
    prompt_template = str(image_refill_config.get("source_crop_redraw_prompt") or "").strip()
    prompt = prompt_template if prompt_template else default_prompt
    caption_clean = (caption or "").strip()
    if caption_clean:
        prompt = f"{prompt}\n\nSemantic intent: {caption_clean}"

    seed_payload = {
        "path": str(seed_image.resolve()),
        "mtime": seed_image.stat().st_mtime,
        "size": seed_image.stat().st_size,
    }
    key_payload = {
        "kind": "source-crop-redraw-no-text",
        "model": model,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "image_size": image_size,
        "seed": seed_payload,
    }
    cache_key = hashlib.sha256(
        json.dumps(key_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    cache_name = f"redraw_{cache_key[:24]}.png"
    out_path = cache_dir / cache_name

    memo = image_refill_config.setdefault("_memo", {})
    if cache_key in memo:
        cached = Path(memo[cache_key])
        if cached.exists():
            return cached, False, None
    if out_path.exists():
        memo[cache_key] = str(out_path)
        return out_path, False, None

    use_openai_compat = bool(api_base) and (not is_google_base)
    if use_openai_compat:
        blob, compat_err = _call_openai_compat_image(
            api_base=api_base,
            api_key=api_key,
            model=model,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            image_size=image_size,
            seed_image=seed_image,
        )
        if blob is None:
            return None, False, compat_err or "OpenAI-compatible redraw call failed"
        try:
            Image.open(io.BytesIO(blob)).convert("RGBA").save(out_path, format="PNG")
            memo[cache_key] = str(out_path)
            return out_path, True, None
        except Exception as exc:
            return None, False, f"OpenAI-compatible redraw decode failed: {exc}"

    if not HAS_GOOGLE_GENAI or google_genai is None or google_genai_types is None:
        return None, False, "google-genai SDK not available"

    try:
        seed_bytes = seed_image.read_bytes()
        part = google_genai_types.Part.from_bytes(
            data=seed_bytes,
            mime_type=guess_image_mime(seed_image),
        )
        client = google_genai.Client(api_key=api_key)
        config = google_genai_types.GenerateContentConfig(
            image_config=google_genai_types.ImageConfig(
                aspect_ratio=aspect_ratio,
                image_size=image_size,
            )
        )
        response = client.models.generate_content(
            model=model,
            contents=[prompt, part],
            config=config,
        )
    except Exception as exc:
        return None, False, f"Gemini image-to-image redraw failed: {exc}"

    for part in _iter_genai_parts(response):
        try:
            if getattr(part, "inline_data", None) is not None:
                inline_data = getattr(part, "inline_data")
                data = getattr(inline_data, "data", None)
                if isinstance(data, str):
                    blob = base64.b64decode(data)
                else:
                    blob = bytes(data or b"")
                if blob:
                    Image.open(io.BytesIO(blob)).convert("RGBA").save(out_path, format="PNG")
                    memo[cache_key] = str(out_path)
                    return out_path, True, None
        except Exception:
            pass
        try:
            as_image_fn = getattr(part, "as_image", None)
            if callable(as_image_fn):
                img = as_image_fn()
                img.save(out_path)
                memo[cache_key] = str(out_path)
                return out_path, True, None
        except Exception:
            pass

    return None, False, "no image data returned by model in redraw step"


def resolve_source_image_for_svg(svg_path: Path, image_refill_config: Dict[str, Any]) -> Optional[Path]:
    """Resolve source raster image path for a given SVG path."""
    memo = image_refill_config.setdefault("_source_for_svg", {})
    key = str(svg_path.resolve())
    if key in memo:
        cached = Path(memo[key])
        return cached if cached.exists() else None

    candidates: List[Path] = []
    single = str(image_refill_config.get("source_image") or "").strip()
    if single:
        candidates.append(Path(single).expanduser())

    source_dir = str(image_refill_config.get("source_image_dir") or "").strip()
    if source_dir:
        sd = Path(source_dir).expanduser()
        for ext in (".png", ".PNG", ".jpg", ".JPG", ".jpeg", ".JPEG", ".webp", ".WEBP"):
            candidates.append(sd / f"{svg_path.stem}{ext}")

    try:
        parts = list(svg_path.resolve().parts)
        for i in range(len(parts) - 1):
            if parts[i] == "output" and parts[i + 1] == "svg":
                inferred_parts = parts[:i] + ["input"] + parts[i + 2:]
                base = Path(*inferred_parts)
                for ext in (".png", ".PNG", ".jpg", ".JPG", ".jpeg", ".JPEG"):
                    candidates.append(base.with_suffix(ext))
                break
    except Exception:
        pass

    for ext in (".png", ".PNG", ".jpg", ".JPG", ".jpeg", ".JPEG"):
        candidates.append(svg_path.with_suffix(ext))

    for c in candidates:
        if c.exists() and c.is_file():
            memo[key] = str(c.resolve())
            return c.resolve()
    return None


def estimate_border_bg_color(img: Any, border: int = 2) -> Tuple[int, int, int]:
    """Estimate background color from border pixels (RGB mean)."""
    rgb = img.convert("RGB")
    w, h = rgb.size
    b = max(1, min(border, max(1, min(w, h) // 8)))
    strips = [
        rgb.crop((0, 0, w, b)),
        rgb.crop((0, max(0, h - b), w, h)),
        rgb.crop((0, 0, b, h)),
        rgb.crop((max(0, w - b), 0, w, h)),
    ]
    vals: List[Tuple[float, float, float]] = []
    for s in strips:
        st = ImageStat.Stat(s)
        vals.append((st.mean[0], st.mean[1], st.mean[2]))
    r = int(round(sum(v[0] for v in vals) / len(vals)))
    g = int(round(sum(v[1] for v in vals) / len(vals)))
    bch = int(round(sum(v[2] for v in vals) / len(vals)))
    return (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, bch)))


def apply_background_alpha(
    img_rgba: Any,
    bg_color: Tuple[int, int, int],
    threshold: float,
    feather: float,
) -> Any:
    """Turn near-background colors transparent to reduce rectangular patch look."""
    rgb = img_rgba.convert("RGB")
    bg = Image.new("RGB", rgb.size, bg_color)
    diff = ImageChops.difference(rgb, bg).convert("L")

    thr = max(0.0, float(threshold))
    fea = max(0.0, float(feather))
    if fea <= 0.0:
        alpha = diff.point(lambda p: 0 if p <= thr else 255, mode="L")
    else:
        hi = thr + fea
        scale = 255.0 / fea
        alpha = diff.point(
            lambda p: 0 if p <= thr else (255 if p >= hi else int((p - thr) * scale)),
            mode="L",
        )
        alpha = alpha.filter(ImageFilter.GaussianBlur(radius=max(0.6, fea * 0.10)))

    base_alpha = img_rgba.split()[-1]
    out_alpha = ImageChops.multiply(base_alpha, alpha)
    out = img_rgba.copy()
    out.putalpha(out_alpha)
    return out


def crop_placeholder_from_source(
    source_image: Path,
    placeholder: Dict[str, Any],
    svg_width: float,
    svg_height: float,
    image_refill_config: Dict[str, Any],
    semantic_textboxes: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Optional[Path], bool, Optional[str]]:
    """Crop source image at placeholder box and return local PNG path."""
    if not HAS_PIL:
        return None, False, "Pillow not available"
    if not source_image.exists():
        return None, False, f"source image not found: {source_image}"
    if svg_width <= 0 or svg_height <= 0:
        return None, False, "invalid svg dimensions for source-crop"

    cache_dir = Path(image_refill_config.get("cache_dir") or ".")
    cache_dir.mkdir(parents=True, exist_ok=True)

    ph_x = float(placeholder.get("x") or 0.0)
    ph_y = float(placeholder.get("y") or 0.0)
    ph_w = float(placeholder.get("w") or 0.0)
    ph_h = float(placeholder.get("h") or 0.0)
    ph_area_svg = max(ph_w * ph_h, 1e-6)
    slide_area_svg = max(float(svg_width) * float(svg_height), 1e-6)
    ph_area_ratio = ph_area_svg / slide_area_svg

    x = ph_x
    y = ph_y
    w = ph_w
    h = ph_h
    if w <= 0 or h <= 0:
        return None, False, "invalid placeholder size"

    expand = float(image_refill_config.get("source_crop_expand") or 0.0)
    x -= expand
    y -= expand
    w += 2.0 * expand
    h += 2.0 * expand

    try:
        with Image.open(source_image) as im:
            sw, sh = im.size
    except Exception as exc:
        return None, False, f"cannot open source image: {exc}"

    scale_x = sw / max(svg_width, 1e-6)
    scale_y = sh / max(svg_height, 1e-6)
    x1 = int(round(max(0.0, x) * scale_x))
    y1 = int(round(max(0.0, y) * scale_y))
    x2 = int(round(min(svg_width, x + w) * scale_x))
    y2 = int(round(min(svg_height, y + h) * scale_y))
    x1 = max(0, min(x1, sw - 1))
    y1 = max(0, min(y1, sh - 1))
    x2 = max(x1 + 1, min(x2, sw))
    y2 = max(y1 + 1, min(y2, sh))

    key_payload = {
        "src": str(source_image.resolve()),
        "mtime": source_image.stat().st_mtime,
        "bbox": [x1, y1, x2, y2],
    }
    cache_key = hashlib.sha256(
        json.dumps(key_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    out_path = cache_dir / f"crop_{cache_key[:24]}.png"
    if out_path.exists():
        return out_path, False, None

    entry = placeholder.get("entry") if isinstance(placeholder.get("entry"), dict) else {}
    remove_bg_override = placeholder.get("remove_bg")
    if remove_bg_override is None and entry:
        raw = str(entry.get("remove_bg", "")).strip().lower()
        if raw in ("1", "true", "yes", "on"):
            remove_bg_override = True
        elif raw in ("0", "false", "no", "off"):
            remove_bg_override = False

    try:
        with Image.open(source_image) as im:
            crop = im.crop((x1, y1, x2, y2)).convert("RGBA")
            erase_overlap_text = bool(
                image_refill_config.get("source_crop_erase_overlapped_text_in_image", True)
            )
            if erase_overlap_text and semantic_textboxes:
                overlap_threshold = float(
                    image_refill_config.get("source_crop_text_erase_overlap_threshold", 0.2) or 0.2
                )
                if bool(remove_bg_override):
                    overlap_threshold = min(
                        overlap_threshold,
                        float(
                            image_refill_config.get(
                                "source_crop_text_erase_overlap_threshold_remove_bg",
                                0.01,
                            )
                            or 0.01
                        ),
                    )
                pad_svg = float(image_refill_config.get("source_crop_text_erase_pad", 2.0) or 2.0)
                feather = float(image_refill_config.get("source_crop_text_erase_feather", 1.0) or 1.0)
                erase_mode = str(
                    image_refill_config.get("source_crop_text_erase_mode") or "alpha"
                ).strip().lower()
                if erase_mode not in ("alpha", "fill"):
                    erase_mode = "alpha"
                pad_x = max(0, int(round(pad_svg * max(scale_x, 1e-6))))
                pad_y = max(0, int(round(pad_svg * max(scale_y, 1e-6))))
                crop_w, crop_h = crop.size
                text_rects: List[Tuple[int, int, int, int]] = []
                ph_rect = {"x": ph_x, "y": ph_y, "w": ph_w, "h": ph_h}
                for tb in semantic_textboxes:
                    tb_w = float(tb.get("w") or 0.0)
                    tb_h = float(tb.get("h") or 0.0)
                    if tb_w <= 0 or tb_h <= 0:
                        continue
                    tb_rect = {
                        "x": float(tb.get("x") or 0.0),
                        "y": float(tb.get("y") or 0.0),
                        "w": tb_w,
                        "h": tb_h,
                    }
                    inter = rect_intersection_area(tb_rect, ph_rect)
                    if inter <= 0:
                        continue
                    # Use smaller-region normalization so partial overlap at placeholder edges
                    # (e.g., corner decorations crossing title text) still gets erased.
                    denom = max(min(tb_w * tb_h, ph_area_svg), 1e-6)
                    if inter / denom < overlap_threshold:
                        continue
                    tx1 = int(round(max(0.0, tb_rect["x"]) * scale_x))
                    ty1 = int(round(max(0.0, tb_rect["y"]) * scale_y))
                    tx2 = int(round(min(svg_width, tb_rect["x"] + tb_rect["w"]) * scale_x))
                    ty2 = int(round(min(svg_height, tb_rect["y"] + tb_rect["h"]) * scale_y))
                    rx1 = max(0, min(crop_w, tx1 - x1) - pad_x)
                    ry1 = max(0, min(crop_h, ty1 - y1) - pad_y)
                    rx2 = max(0, min(crop_w, tx2 - x1) + pad_x)
                    ry2 = max(0, min(crop_h, ty2 - y1) + pad_y)
                    if rx2 > rx1 and ry2 > ry1:
                        text_rects.append((rx1, ry1, rx2, ry2))
                if text_rects:
                    if erase_mode == "fill":
                        draw = ImageDraw.Draw(crop)
                        for rx1, ry1, rx2, ry2 in text_rects:
                            sx1 = max(0, rx1 - 6)
                            sy1 = max(0, ry1 - 6)
                            sx2 = min(crop_w, rx2 + 6)
                            sy2 = min(crop_h, ry2 + 6)
                            sample = crop.crop((sx1, sy1, sx2, sy2))
                            bg = estimate_border_bg_color(sample, border=1)
                            draw.rectangle((rx1, ry1, rx2, ry2), fill=(bg[0], bg[1], bg[2], 255))
                    else:
                        keep_mask = Image.new("L", crop.size, 255)
                        draw = ImageDraw.Draw(keep_mask)
                        for rx1, ry1, rx2, ry2 in text_rects:
                            draw.rectangle((rx1, ry1, rx2, ry2), fill=0)
                        if feather > 0:
                            keep_mask = keep_mask.filter(ImageFilter.GaussianBlur(radius=feather))
                        alpha = crop.split()[-1]
                        alpha = ImageChops.multiply(alpha, keep_mask)
                        crop.putalpha(alpha)

            if remove_bg_override is None:
                should_remove_bg = bool(image_refill_config.get("source_crop_remove_bg", True))
            else:
                should_remove_bg = bool(remove_bg_override)

            if should_remove_bg:
                max_remove_bg_area_ratio = float(
                    image_refill_config.get("source_crop_max_remove_bg_area_ratio", 0.16) or 0.16
                )
                if ph_area_ratio >= max_remove_bg_area_ratio:
                    should_remove_bg = False

            if should_remove_bg:
                rembg_done = False
                if bool(image_refill_config.get("source_crop_use_rembg", True)):
                    rembg_img, rembg_err = _apply_rembg_with_validation(
                        crop,
                        image_refill_config,
                        min_ratio_key="source_crop_rembg_min_nonopaque_ratio",
                        max_ratio_key="source_crop_rembg_max_nonopaque_ratio",
                    )
                    if rembg_img is not None:
                        crop = rembg_img
                        rembg_done = True
                    elif bool(image_refill_config.get("source_crop_require_rembg", False)):
                        return None, False, f"source-crop rembg required but failed: {rembg_err}"

                if not rembg_done:
                    bg = estimate_border_bg_color(crop, border=2)
                    crop = apply_background_alpha(
                        crop,
                        bg_color=bg,
                        threshold=float(image_refill_config.get("source_crop_bg_threshold", 14.0) or 14.0),
                        feather=float(image_refill_config.get("source_crop_feather", 18.0) or 18.0),
                    )
            crop.save(out_path, format="PNG")
        return out_path, True, None
    except Exception as exc:
        return None, False, f"source-crop failed: {exc}"
