import argparse
import json
import math
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from PIL import Image
from scipy import ndimage as ndi


SVG_EXTS = {".svg", ".SVG"}
PATH_RE = re.compile(r'<path\b[^>]*\bd="([^"]*)"[^>]*>', flags=re.I | re.S)
NUM_RE = re.compile(r"[-+]?(?:\d+\.\d+|\d+|\.\d+)(?:[eE][-+]?\d+)?")
CMD_RE = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]")
TOKEN_RE = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d+\.\d+|\d+|\.\d+)(?:[eE][-+]?\d+)?")
FILL_RE = re.compile(r'fill="([^"]+)"', flags=re.I)
VIEWBOX_RE = re.compile(r'viewBox="([^"]+)"', flags=re.I)
WIDTH_RE = re.compile(r'width="([^"]+)"', flags=re.I)
HEIGHT_RE = re.compile(r'height="([^"]+)"', flags=re.I)


@dataclass
class StripeCandidate:
    idx: int
    span_start: int
    span_end: int
    tag: str
    d: str
    z_count: int
    subpath_count: int
    elongated_ratio: float
    angle_deg: float
    angle_std_deg: float
    period_svg: float
    stroke_svg: float
    center_svg: tuple[float, float]
    bbox_svg: tuple[float, float, float, float]
    score: float
    fill_color: str


@dataclass
class RewriteResult:
    accepted: bool
    in_svg: Path
    out_svg: Path
    fig_path: Path
    texture_label: str
    texture_conf: float
    iou_local: float
    iou_soft: float
    edge_iou: float
    rgb_mse: float
    token_before: int
    token_after: int
    token_ratio: float
    candidate_info: str
    reason: str


@dataclass
class AssetFingerprint:
    asset_id: str
    family: str
    orientation_deg: float | None
    period_norm: float | None
    anisotropy: float | None
    stochasticity: float | None


@dataclass
class AssetRouteDecision:
    asset: AssetFingerprint | None
    accepted: bool
    best_score: float
    second_score: float
    margin: float
    confidence: float
    reason: str


def parse_numeric_attr(value: str | None, default: float) -> float:
    if value is None:
        return default
    m = re.search(r"[-+]?\d*\.?\d+", value)
    if not m:
        return default
    try:
        return float(m.group(0))
    except Exception:
        return default


def parse_svg_meta(svg_text: str) -> tuple[float, float, float, float, int, int]:
    width_attr = WIDTH_RE.search(svg_text)
    height_attr = HEIGHT_RE.search(svg_text)
    parsed_w = parse_numeric_attr(width_attr.group(1), 1000.0) if width_attr else 1000.0
    parsed_h = parse_numeric_attr(height_attr.group(1), 1000.0) if height_attr else 1000.0

    vb = VIEWBOX_RE.search(svg_text)
    if vb:
        vals = [float(x) for x in vb.group(1).replace(",", " ").split()]
        if len(vals) == 4:
            vbx, vby, vbw, vbh = vals
        else:
            vbx, vby, vbw, vbh = 0.0, 0.0, parsed_w, parsed_h
    else:
        # Important: if viewBox is absent, use width/height as user space.
        vbx, vby, vbw, vbh = 0.0, 0.0, parsed_w, parsed_h

    width = int(round(parsed_w))
    height = int(round(parsed_h))
    width = max(width, 16)
    height = max(height, 16)
    vbw = max(vbw, 1e-6)
    vbh = max(vbh, 1e-6)
    return vbx, vby, vbw, vbh, width, height


def count_path_tokens_in_svg(svg_text: str) -> int:
    total = 0
    for d in PATH_RE.findall(svg_text):
        total += len(NUM_RE.findall(d)) + len(CMD_RE.findall(d))
    return total


def render_svg_to_rgba(svg_text: str, render_w: int, render_h: int) -> np.ndarray:
    with tempfile.TemporaryDirectory(prefix="physui_v22_") as td:
        tdp = Path(td)
        svg_p = tdp / "in.svg"
        png_p = tdp / "out.png"
        svg_p.write_text(svg_text, encoding="utf-8")
        cmd = [
            "rsvg-convert",
            "-w",
            str(render_w),
            "-h",
            str(render_h),
            str(svg_p),
            "-o",
            str(png_p),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        arr = np.array(Image.open(png_p).convert("RGBA"))
    return arr


def build_svg_shell(
    vbx: float,
    vby: float,
    vbw: float,
    vbh: float,
    width: int,
    height: int,
    content: str,
) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}" viewBox="{vbx} {vby} {vbw} {vbh}">'
        f"{content}</svg>"
    )


def tokenize_path(d: str) -> list[str]:
    return TOKEN_RE.findall(d)


def parse_path_subpaths(d: str) -> list[np.ndarray]:
    toks = tokenize_path(d)
    i = 0
    cmd = ""
    cx, cy = 0.0, 0.0
    sx, sy = 0.0, 0.0
    cur: list[tuple[float, float]] = []
    subs: list[np.ndarray] = []

    def read_num() -> float:
        nonlocal i
        v = float(toks[i])
        i += 1
        return v

    while i < len(toks):
        tk = toks[i]
        if re.fullmatch(r"[A-Za-z]", tk):
            cmd = tk
            i += 1
        elif not cmd:
            i += 1
            continue

        up = cmd.upper()
        rel = cmd.islower()

        if up == "M":
            first = True
            while i + 1 < len(toks) and not re.fullmatch(r"[A-Za-z]", toks[i]):
                x = read_num()
                y = read_num()
                if rel:
                    x += cx
                    y += cy
                cx, cy = x, y
                if first:
                    if cur:
                        subs.append(np.asarray(cur, dtype=np.float64))
                        cur = []
                    sx, sy = cx, cy
                    first = False
                cur.append((cx, cy))
            continue

        if up == "L":
            while i + 1 < len(toks) and not re.fullmatch(r"[A-Za-z]", toks[i]):
                x = read_num()
                y = read_num()
                if rel:
                    x += cx
                    y += cy
                cx, cy = x, y
                cur.append((cx, cy))
            continue

        if up == "H":
            while i < len(toks) and not re.fullmatch(r"[A-Za-z]", toks[i]):
                x = read_num()
                if rel:
                    x += cx
                cx = x
                cur.append((cx, cy))
            continue

        if up == "V":
            while i < len(toks) and not re.fullmatch(r"[A-Za-z]", toks[i]):
                y = read_num()
                if rel:
                    y += cy
                cy = y
                cur.append((cx, cy))
            continue

        if up == "C":
            while i + 5 < len(toks) and not re.fullmatch(r"[A-Za-z]", toks[i]):
                x1, y1, x2, y2, x, y = [read_num() for _ in range(6)]
                if rel:
                    x1 += cx
                    y1 += cy
                    x2 += cx
                    y2 += cy
                    x += cx
                    y += cy
                cur.extend([(x1, y1), (x2, y2), (x, y)])
                cx, cy = x, y
            continue

        if up == "S":
            while i + 3 < len(toks) and not re.fullmatch(r"[A-Za-z]", toks[i]):
                x2, y2, x, y = [read_num() for _ in range(4)]
                if rel:
                    x2 += cx
                    y2 += cy
                    x += cx
                    y += cy
                cur.extend([(x2, y2), (x, y)])
                cx, cy = x, y
            continue

        if up == "Q":
            while i + 3 < len(toks) and not re.fullmatch(r"[A-Za-z]", toks[i]):
                x1, y1, x, y = [read_num() for _ in range(4)]
                if rel:
                    x1 += cx
                    y1 += cy
                    x += cx
                    y += cy
                cur.extend([(x1, y1), (x, y)])
                cx, cy = x, y
            continue

        if up == "T":
            while i + 1 < len(toks) and not re.fullmatch(r"[A-Za-z]", toks[i]):
                x, y = [read_num() for _ in range(2)]
                if rel:
                    x += cx
                    y += cy
                cur.append((x, y))
                cx, cy = x, y
            continue

        if up == "A":
            while i + 6 < len(toks) and not re.fullmatch(r"[A-Za-z]", toks[i]):
                _rx, _ry, _rot, _laf, _sf, x, y = [read_num() for _ in range(7)]
                if rel:
                    x += cx
                    y += cy
                cur.append((x, y))
                cx, cy = x, y
            continue

        if up == "Z":
            cx, cy = sx, sy
            if cur:
                cur.append((cx, cy))
            if cur:
                subs.append(np.asarray(cur, dtype=np.float64))
                cur = []
            continue

        # Unknown command fallback.
        i += 1

    if cur:
        subs.append(np.asarray(cur, dtype=np.float64))
    return [s for s in subs if len(s) >= 3]


def wrap_deg180(angle_deg: float) -> float:
    v = angle_deg % 180.0
    if v < 0:
        v += 180.0
    return v


def circular_std_deg(angles_deg: np.ndarray) -> float:
    if len(angles_deg) <= 1:
        return 0.0
    th = np.deg2rad(angles_deg * 2.0)
    c = np.mean(np.cos(th))
    s = np.mean(np.sin(th))
    r = max(math.sqrt(c * c + s * s), 1e-9)
    return float(np.rad2deg(math.sqrt(-2.0 * np.log(r)) / 2.0))


def pca_angle_and_dims(points: np.ndarray) -> tuple[float, float, float]:
    ctr = points.mean(axis=0)
    q = points - ctr
    cov = np.cov(q[:, 0], q[:, 1]) + 1e-6 * np.eye(2)
    vals, vecs = np.linalg.eigh(cov)
    idx = np.argsort(vals)[::-1]
    vals = vals[idx]
    vecs = vecs[:, idx]
    major = max(math.sqrt(max(vals[0], 1e-12)), 1e-6)
    minor = max(math.sqrt(max(vals[1], 1e-12)), 1e-6)
    v = vecs[:, 0]
    ang = wrap_deg180(math.degrees(math.atan2(v[1], v[0])))
    return ang, major, minor


def estimate_period(centers: np.ndarray, stripe_angle_deg: float) -> float:
    th = math.radians(stripe_angle_deg)
    n = np.array([-math.sin(th), math.cos(th)], dtype=np.float64)
    proj = np.sort(centers @ n)
    if len(proj) < 2:
        return 8.0
    diff = np.diff(proj)
    diff = diff[diff > 1e-5]
    if len(diff) == 0:
        return 8.0
    med = float(np.median(diff))
    p10 = float(np.percentile(diff, 10))
    p90 = float(np.percentile(diff, 90))
    # Robust clamp avoids degenerate period from outliers.
    return float(np.clip(med, max(1e-3, p10 * 0.5), max(med, p90 * 1.5)))


def parse_fill(tag: str) -> str:
    m = FILL_RE.search(tag)
    if not m:
        return "#FFFFFF"
    fill = m.group(1).strip()
    if fill.lower() in {"none", "transparent"}:
        return "#FFFFFF"
    return fill


def infer_asset_family(asset_id: str, tags: list[str]) -> str:
    txt = (asset_id + " " + " ".join(tags)).lower()
    if "stripe" in txt or "diagonal" in txt:
        return "diagonal_stripes"
    if "hatch" in txt:
        return "hatching"
    if "dot" in txt or "speckle" in txt:
        return "dots"
    if "fragment" in txt or "noise" in txt:
        return "dense_fragments"
    return "other"


def load_asset_fingerprints(asset_lib_path: str) -> list[AssetFingerprint]:
    p = Path(asset_lib_path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    assets = data.get("assets", [])
    out: list[AssetFingerprint] = []
    for a in assets:
        rf = a.get("retrieval_features", {})
        asset_id = str(a.get("asset_id", "")).strip()
        if not asset_id:
            continue
        tags = [str(x) for x in a.get("tags", [])]
        out.append(
            AssetFingerprint(
                asset_id=asset_id,
                family=infer_asset_family(asset_id, tags),
                orientation_deg=float(rf["orientation_deg"]) if rf.get("orientation_deg") is not None else None,
                period_norm=float(rf["period_px_norm"]) if rf.get("period_px_norm") is not None else None,
                anisotropy=float(rf["anisotropy"]) if rf.get("anisotropy") is not None else None,
                stochasticity=float(rf["stochasticity"]) if rf.get("stochasticity") is not None else None,
            )
        )
    return out


def wrap180_diff(a: float, b: float) -> float:
    d = abs((a - b) % 180.0)
    return min(d, 180.0 - d)


def score_asset_match(
    family: str,
    angle_deg: float,
    period_norm: float,
    elongated_ratio: float,
    stochasticity: float,
    asset: AssetFingerprint,
) -> float:
    s = 0.0
    if asset.family != family:
        if family in {"diagonal_stripes", "hatching"} and asset.family in {"diagonal_stripes", "hatching"}:
            s += 0.35
        else:
            s += 0.75
    if asset.orientation_deg is not None:
        s += 0.20 * (wrap180_diff(angle_deg, asset.orientation_deg) / 90.0)
    else:
        s += 0.08
    if asset.period_norm is not None:
        s += 0.18 * min(abs(period_norm - asset.period_norm) / 0.30, 1.0)
    else:
        s += 0.06
    if asset.anisotropy is not None:
        s += 0.12 * min(abs(elongated_ratio - asset.anisotropy) / 0.60, 1.0)
    else:
        s += 0.06
    if asset.stochasticity is not None:
        s += 0.10 * min(abs(stochasticity - asset.stochasticity) / 0.50, 1.0)
    else:
        s += 0.04
    return float(max(0.0, s))


def best_asset_for_candidate(
    assets: list[AssetFingerprint],
    family: str,
    angle_deg: float,
    period_norm: float,
    elongated_ratio: float,
    stochasticity: float,
) -> tuple[AssetFingerprint | None, float]:
    if not assets:
        return None, 1e9
    best = None
    best_score = 1e9
    for a in assets:
        sc = score_asset_match(family, angle_deg, period_norm, elongated_ratio, stochasticity, a)
        if sc < best_score:
            best_score = sc
            best = a
    return best, best_score


def _score_to_confidence(score: float) -> float:
    # score is lower-is-better; convert to [0,1] confidence.
    return float(1.0 / (1.0 + max(score, 0.0) * 2.0))


def route_asset_for_candidate(
    assets: list[AssetFingerprint],
    family: str,
    angle_deg: float,
    period_norm: float,
    elongated_ratio: float,
    stochasticity: float,
    score_thresh: float,
    min_confidence: float,
    min_margin: float,
    max_angle_diff: float,
) -> AssetRouteDecision:
    if not assets:
        return AssetRouteDecision(
            asset=None,
            accepted=False,
            best_score=1e9,
            second_score=1e9,
            margin=0.0,
            confidence=0.0,
            reason="no_asset_library",
        )

    scored: list[tuple[float, AssetFingerprint]] = []
    for a in assets:
        sc = score_asset_match(family, angle_deg, period_norm, elongated_ratio, stochasticity, a)
        scored.append((sc, a))
    scored.sort(key=lambda x: x[0])
    best_score, best_asset = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 1e9
    margin = float(second_score - best_score)
    conf = _score_to_confidence(best_score)

    if best_asset.family != family:
        return AssetRouteDecision(
            asset=best_asset,
            accepted=False,
            best_score=best_score,
            second_score=second_score,
            margin=margin,
            confidence=conf,
            reason="family_mismatch",
        )
    if best_score > score_thresh:
        return AssetRouteDecision(
            asset=best_asset,
            accepted=False,
            best_score=best_score,
            second_score=second_score,
            margin=margin,
            confidence=conf,
            reason=f"score>{score_thresh:.3f}",
        )
    if conf < min_confidence:
        return AssetRouteDecision(
            asset=best_asset,
            accepted=False,
            best_score=best_score,
            second_score=second_score,
            margin=margin,
            confidence=conf,
            reason=f"conf<{min_confidence:.3f}",
        )
    if margin < min_margin:
        return AssetRouteDecision(
            asset=best_asset,
            accepted=False,
            best_score=best_score,
            second_score=second_score,
            margin=margin,
            confidence=conf,
            reason=f"ambiguous_margin<{min_margin:.3f}",
        )

    if best_asset.orientation_deg is not None:
        ad = wrap180_diff(angle_deg, best_asset.orientation_deg)
        if ad > max_angle_diff:
            return AssetRouteDecision(
                asset=best_asset,
                accepted=False,
                best_score=best_score,
                second_score=second_score,
                margin=margin,
                confidence=conf,
                reason=f"angle_diff>{max_angle_diff:.1f}",
            )

    return AssetRouteDecision(
        asset=best_asset,
        accepted=True,
        best_score=best_score,
        second_score=second_score,
        margin=margin,
        confidence=conf,
        reason="ok",
    )


def classify_texture_mask(mask: np.ndarray) -> tuple[str, float, dict]:
    # Object-level texture recognizer for routing.
    lab, num = ndi.label(mask)
    if num <= 0:
        return "non_stripe", 0.0, {"n_comp": 0}

    feats = []
    for cid in range(1, num + 1):
        ys, xs = np.where(lab == cid)
        if len(xs) < 6:
            continue
        pts = np.stack([xs, ys], axis=1).astype(np.float64)
        ang, major, minor = pca_angle_and_dims(pts)
        feats.append(
            (
                ang,
                major / max(minor, 1e-6),
                float(xs.mean()),
                float(ys.mean()),
                float(len(xs)),
            )
        )

    if len(feats) < 8:
        return "non_stripe", 0.05, {"n_comp": len(feats)}

    arr = np.asarray(feats, dtype=np.float64)
    angles = arr[:, 0]
    elong = arr[:, 1]
    centers = arr[:, 2:4]
    areas = arr[:, 4]

    angle_std = circular_std_deg(angles)
    elongated_ratio = float(np.mean(elong >= 2.0))
    n_comp = int(len(feats))
    area_ratio = float(mask.mean())

    main_ang = float(np.median(angles))
    period = estimate_period(centers, main_ang)
    th = math.radians(main_ang)
    nvec = np.array([-math.sin(th), math.cos(th)], dtype=np.float64)
    proj = np.sort(centers @ nvec)
    d = np.diff(proj)
    d = d[d > 1e-5]
    if len(d) >= 4:
        period_cv = float(np.std(d) / max(np.mean(d), 1e-6))
    else:
        period_cv = 2.0

    comp_score = min(n_comp / 30.0, 1.0)
    elong_score = min(elongated_ratio / 0.70, 1.0)
    angle_score = max(0.0, 1.0 - angle_std / 60.0)
    period_score = max(0.0, 1.0 - min(period_cv, 2.0) / 1.2)
    area_score = 1.0 if 0.01 <= area_ratio <= 0.85 else 0.4

    conf = (
        0.20 * comp_score
        + 0.35 * elong_score
        + 0.25 * angle_score
        + 0.15 * period_score
        + 0.05 * area_score
    )
    conf = float(np.clip(conf, 0.0, 1.0))

    is_stripe = (
        n_comp >= 10
        and elongated_ratio >= 0.25
        and angle_std <= 70.0
        and conf >= 0.45
    )
    label = "diagonal_stripes" if is_stripe else "non_stripe"
    info = {
        "n_comp": n_comp,
        "elongated_ratio": elongated_ratio,
        "angle_std": angle_std,
        "period": float(period),
        "period_cv": period_cv,
        "area_ratio": area_ratio,
    }
    return label, conf, info


def _cv(arr: np.ndarray) -> float:
    m = float(np.mean(arr))
    if m <= 1e-9:
        return 999.0
    return float(np.std(arr) / m)


def candidate_pattern_purity_metrics(cand: StripeCandidate) -> dict[str, float]:
    subs = parse_path_subpaths(cand.d)
    if len(subs) < 6:
        return {
            "ar5_ratio": 0.0,
            "ar3_ratio": 0.0,
            "perim_cv": 999.0,
            "npts_cv": 999.0,
        }
    major = []
    minor = []
    npts = []
    perims = []
    for s in subs:
        _, ma, mi = pca_angle_and_dims(s)
        major.append(ma)
        minor.append(max(mi, 1e-6))
        npts.append(float(len(s)))
        d = np.diff(s, axis=0)
        perims.append(float(np.sqrt((d * d).sum(axis=1)).sum()))
    major_a = np.asarray(major, dtype=np.float64)
    minor_a = np.asarray(minor, dtype=np.float64)
    ar = major_a / np.maximum(minor_a, 1e-6)
    return {
        "ar5_ratio": float(np.mean(ar >= 5.0)),
        "ar3_ratio": float(np.mean(ar >= 3.0)),
        "perim_cv": _cv(np.asarray(perims, dtype=np.float64)),
        "npts_cv": _cv(np.asarray(npts, dtype=np.float64)),
    }


def detect_stripe_candidates(svg_text: str) -> list[StripeCandidate]:
    out: list[StripeCandidate] = []
    for idx, m in enumerate(PATH_RE.finditer(svg_text)):
        tag = m.group(0)
        d = m.group(1)
        zc = d.count("Z") + d.count("z")
        if zc < 24:
            continue
        subs = parse_path_subpaths(d)
        if len(subs) < 16:
            continue

        centers = []
        angles = []
        majors = []
        minors = []
        elongated = 0
        all_pts = []
        for s in subs:
            ang, major, minor = pca_angle_and_dims(s)
            centers.append(s.mean(axis=0))
            angles.append(ang)
            majors.append(major)
            minors.append(minor)
            all_pts.append(s)
            if major / max(minor, 1e-6) >= 2.4 and major >= 0.75:
                elongated += 1

        centers_a = np.asarray(centers, dtype=np.float64)
        angles_a = np.asarray(angles, dtype=np.float64)
        majors_a = np.asarray(majors, dtype=np.float64)
        minors_a = np.asarray(minors, dtype=np.float64)

        elongated_ratio = float(elongated / max(len(subs), 1))
        if elongated_ratio < 0.20:
            continue
        angle_std = circular_std_deg(angles_a)
        mean_ang = float(np.median(angles_a))
        period = estimate_period(centers_a, mean_ang)
        stroke = float(np.median(minors_a) * 2.2)
        center_svg = tuple(np.median(centers_a, axis=0).tolist())
        allp = np.vstack(all_pts)
        bbox_svg = (float(allp[:, 0].min()), float(allp[:, 1].min()), float(allp[:, 0].max()), float(allp[:, 1].max()))

        score = zc * elongated_ratio / (1.0 + angle_std / 60.0)
        out.append(
            StripeCandidate(
                idx=idx,
                span_start=m.start(),
                span_end=m.end(),
                tag=tag,
                d=d,
                z_count=zc,
                subpath_count=len(subs),
                elongated_ratio=elongated_ratio,
                angle_deg=mean_ang,
                angle_std_deg=angle_std,
                period_svg=period,
                stroke_svg=stroke,
                center_svg=center_svg,
                bbox_svg=bbox_svg,
                score=score,
                fill_color=parse_fill(tag),
            )
        )
    out.sort(key=lambda x: x.score, reverse=True)
    return out


def svg_to_px(x: float, y: float, vbx: float, vby: float, vbw: float, vbh: float, w: int, h: int) -> tuple[int, int]:
    px = int(round((x - vbx) / max(vbw, 1e-9) * (w - 1)))
    py = int(round((y - vby) / max(vbh, 1e-9) * (h - 1)))
    px = int(np.clip(px, 0, w - 1))
    py = int(np.clip(py, 0, h - 1))
    return px, py


def px_to_svg(x: np.ndarray, y: np.ndarray, vbx: float, vby: float, vbw: float, vbh: float, w: int, h: int) -> tuple[np.ndarray, np.ndarray]:
    sx = vbx + (x / max(w - 1, 1)) * vbw
    sy = vby + (y / max(h - 1, 1)) * vbh
    return sx, sy


def extract_component(mask: np.ndarray, seed_xy: tuple[int, int]) -> np.ndarray | None:
    lab, num = ndi.label(mask)
    if num == 0:
        return None
    x, y = seed_xy
    cid = int(lab[y, x])
    if cid <= 0:
        # fallback: nearest component center
        ys, xs = np.where(lab > 0)
        if len(xs) == 0:
            return None
        d2 = (xs - x) ** 2 + (ys - y) ** 2
        k = int(np.argmin(d2))
        cid = int(lab[ys[k], xs[k]])
        if cid <= 0:
            return None
    return lab == cid


def recover_silhouette(component_mask: np.ndarray, kernel: int = 7) -> np.ndarray:
    k = max(3, int(kernel))
    if k % 2 == 0:
        k += 1
    struct = np.ones((k, k), dtype=bool)
    closed = ndi.binary_closing(component_mask, structure=struct, iterations=1)
    filled = ndi.binary_fill_holes(closed)
    lab, num = ndi.label(filled)
    if num == 0:
        return component_mask
    area = np.bincount(lab.ravel())
    area[0] = 0
    cid = int(np.argmax(area))
    return lab == cid


def rdp(points: np.ndarray, eps: float) -> np.ndarray:
    if len(points) < 3:
        return points
    p0 = points[0]
    p1 = points[-1]
    v = p1 - p0
    denom = float(np.dot(v, v))
    if denom < 1e-12:
        dist = np.linalg.norm(points - p0, axis=1)
    else:
        t = np.clip(((points - p0) @ v) / denom, 0.0, 1.0)
        proj = p0 + t[:, None] * v
        dist = np.linalg.norm(points - proj, axis=1)
    idx = int(np.argmax(dist))
    dmax = float(dist[idx])
    if dmax <= eps:
        return np.vstack([p0, p1])
    left = rdp(points[: idx + 1], eps)
    right = rdp(points[idx:], eps)
    return np.vstack([left[:-1], right])


def mask_to_boundary_path(
    mask: np.ndarray,
    vbx: float,
    vby: float,
    vbw: float,
    vbh: float,
    render_w: int,
    render_h: int,
    max_points: int = 120,
) -> tuple[str, np.ndarray]:
    # Use contour extraction to keep geometric order; avoids "angle sort self-crossing".
    fig = plt.figure(figsize=(1, 1))
    cs = plt.contour(mask.astype(np.float32), levels=[0.5])
    segs = cs.allsegs[0] if cs.allsegs else []
    plt.close(fig)
    if not segs:
        raise RuntimeError("contour extraction failed")
    seg = max(segs, key=lambda s: len(s))
    if len(seg) < 12:
        raise RuntimeError("contour too small")

    # contour gives [x,y] in pixel space.
    poly = np.asarray(seg, dtype=np.float64)
    if np.linalg.norm(poly[0] - poly[-1]) > 1e-6:
        poly = np.vstack([poly, poly[0]])

    simp = rdp(poly, eps=1.8)
    if len(simp) > max_points:
        sel = np.linspace(0, len(simp) - 1, max_points).astype(int)
        simp = simp[sel]
    if np.linalg.norm(simp[0] - simp[-1]) > 1e-6:
        simp = np.vstack([simp, simp[0]])

    sx, sy = px_to_svg(simp[:, 0], simp[:, 1], vbx, vby, vbw, vbh, render_w, render_h)
    coords = np.stack([sx, sy], axis=1)

    parts = [f"M{coords[0,0]:.3f} {coords[0,1]:.3f}"]
    for p in coords[1:]:
        parts.append(f"L{p[0]:.3f} {p[1]:.3f}")
    parts.append("Z")
    d = "".join(parts)
    return d, coords


def estimate_component_color(rgba: np.ndarray, comp_mask: np.ndarray, default_fill: str) -> str:
    pix = rgba[comp_mask]
    if len(pix) == 0:
        return default_fill
    rgb = pix[:, :3].astype(np.float64)
    med = np.median(rgb, axis=0).astype(int)
    return "#{:02x}{:02x}{:02x}".format(int(med[0]), int(med[1]), int(med[2]))


def build_replacement_snippet(
    cand: StripeCandidate,
    clip_d: str,
    bbox_svg: tuple[float, float, float, float],
    fg_color: str,
    unique_id: str,
    fill_ratio: float,
    angle_override: float | None = None,
    period_override: float | None = None,
) -> tuple[str, str]:
    x0, y0, x1, y1 = bbox_svg
    w = max(x1 - x0, 1e-3)
    h = max(y1 - y0, 1e-3)

    pat = f"physui_pat_{unique_id}"
    clip = f"physui_clip_{unique_id}"
    # librsvg can drop ultra-dense patterns (period ~= 1px); keep a safe lower bound.
    period = max(period_override if period_override is not None else cand.period_svg, 2.0)
    fill_ratio = float(np.clip(fill_ratio, 0.12, 0.88))
    stroke = max(min(period * fill_ratio, period * 0.95), 0.5)
    angle = angle_override if angle_override is not None else cand.angle_deg

    defs = (
        f'<defs id="physui_defs_{unique_id}">'
        f'<pattern id="{pat}" patternUnits="userSpaceOnUse" width="{period:.3f}" height="{period:.3f}" '
        f'patternContentUnits="userSpaceOnUse" patternTransform="rotate({angle:.3f})">'
        f'<rect x="0" y="0" width="{period:.3f}" height="{period:.3f}" fill="none"/>'
        f'<rect x="0" y="0" width="{stroke:.3f}" height="{period:.3f}" fill="{fg_color}" />'
        f"</pattern>"
        f'<clipPath id="{clip}" clipPathUnits="userSpaceOnUse"><path d="{clip_d}" /></clipPath>'
        f"</defs>"
    )
    use = (
        f'<g id="physui_rewrite_{unique_id}" clip-path="url(#{clip})">'
        f'<rect x="{x0:.3f}" y="{y0:.3f}" width="{w:.3f}" height="{h:.3f}" fill="url(#{pat})"/>'
        f"</g>"
    )
    return defs, use


def inject_defs(svg_text: str, defs_text: str) -> str:
    p = svg_text.rfind("</svg>")
    if p < 0:
        return svg_text + defs_text
    return svg_text[:p] + defs_text + svg_text[p:]


def local_iou(mask_a: np.ndarray, mask_b: np.ndarray, bbox: tuple[int, int, int, int]) -> float:
    x0, y0, x1, y1 = bbox
    a = mask_a[y0:y1, x0:x1]
    b = mask_b[y0:y1, x0:x1]
    inter = np.logical_and(a, b).sum()
    union = np.logical_or(a, b).sum()
    if union == 0:
        return 1.0
    return float(inter / union)


def local_iou_soft(mask_a: np.ndarray, mask_b: np.ndarray, bbox: tuple[int, int, int, int], radius: int = 2) -> float:
    x0, y0, x1, y1 = bbox
    a = mask_a[y0:y1, x0:x1]
    b = mask_b[y0:y1, x0:x1]
    if radius > 0:
        k = np.ones((2 * radius + 1, 2 * radius + 1), dtype=bool)
        a = ndi.binary_dilation(a, structure=k, iterations=1)
        b = ndi.binary_dilation(b, structure=k, iterations=1)
    inter = np.logical_and(a, b).sum()
    union = np.logical_or(a, b).sum()
    if union == 0:
        return 1.0
    return float(inter / union)


def local_rgb_mse(
    rgba_a: np.ndarray,
    rgba_b: np.ndarray,
    bbox: tuple[int, int, int, int],
    mask_a: np.ndarray | None = None,
    mask_b: np.ndarray | None = None,
) -> float:
    x0, y0, x1, y1 = bbox
    a = rgba_a[y0:y1, x0:x1, :3].astype(np.float32) / 255.0
    b = rgba_b[y0:y1, x0:x1, :3].astype(np.float32) / 255.0
    if mask_a is not None and mask_b is not None:
        ma = mask_a[y0:y1, x0:x1]
        mb = mask_b[y0:y1, x0:x1]
        m = np.logical_or(ma, mb)
        if m.sum() == 0:
            return 0.0
        d = a[m] - b[m]
        return float(np.mean(d * d))
    d = a - b
    return float(np.mean(d * d))


def local_edge_iou(
    rgba_a: np.ndarray,
    rgba_b: np.ndarray,
    bbox: tuple[int, int, int, int],
    mask_a: np.ndarray | None = None,
    mask_b: np.ndarray | None = None,
) -> float:
    x0, y0, x1, y1 = bbox
    a = rgba_a[y0:y1, x0:x1, :3].astype(np.float32)
    b = rgba_b[y0:y1, x0:x1, :3].astype(np.float32)
    ga = 0.299 * a[:, :, 0] + 0.587 * a[:, :, 1] + 0.114 * a[:, :, 2]
    gb = 0.299 * b[:, :, 0] + 0.587 * b[:, :, 1] + 0.114 * b[:, :, 2]
    ax = ndi.sobel(ga, axis=1)
    ay = ndi.sobel(ga, axis=0)
    bx = ndi.sobel(gb, axis=1)
    by = ndi.sobel(gb, axis=0)
    amag = np.hypot(ax, ay)
    bmag = np.hypot(bx, by)
    ta = np.percentile(amag, 82) if amag.size else 0.0
    tb = np.percentile(bmag, 82) if bmag.size else 0.0
    ea = amag > ta
    eb = bmag > tb
    if mask_a is not None and mask_b is not None:
        ma = mask_a[y0:y1, x0:x1]
        mb = mask_b[y0:y1, x0:x1]
        m = np.logical_or(ma, mb)
        ea = np.logical_and(ea, m)
        eb = np.logical_and(eb, m)
    inter = np.logical_and(ea, eb).sum()
    union = np.logical_or(ea, eb).sum()
    if union == 0:
        return 1.0
    return float(inter / union)


def make_debug_figure(
    rgba_before: np.ndarray,
    rgba_after: np.ndarray,
    local_iou_val: float,
    local_iou_soft_val: float,
    edge_iou_val: float,
    rgb_mse_val: float,
    token_before: int,
    token_after: int,
    fig_path: Path,
) -> None:
    alpha_a = rgba_before[:, :, 3] > 10
    alpha_b = rgba_after[:, :, 3] > 10
    diff = np.logical_xor(alpha_a, alpha_b)

    plt.figure(figsize=(12, 8))
    ax1 = plt.subplot(2, 2, 1)
    ax2 = plt.subplot(2, 2, 2)
    ax3 = plt.subplot(2, 2, 3)
    ax4 = plt.subplot(2, 2, 4)

    ax1.imshow(rgba_before)
    ax1.set_title("Original Render")
    ax1.axis("off")

    ax2.imshow(rgba_after)
    ax2.set_title("PhysUI v2.2 Rewrite Render")
    ax2.axis("off")

    overlay = np.zeros((*diff.shape, 3), dtype=np.uint8)
    overlay[..., :] = np.array([255, 250, 180], dtype=np.uint8)
    bg = np.zeros((*diff.shape, 3), dtype=np.uint8)
    bg[alpha_a] = np.array([25, 25, 25], dtype=np.uint8)
    mix = bg.copy()
    mix[diff] = overlay[diff]
    ax3.imshow(mix)
    ax3.set_title(
        f"Diff Map (IoU={local_iou_val:.4f}, soft={local_iou_soft_val:.4f}, "
        f"edge={edge_iou_val:.4f}, rgbMSE={rgb_mse_val:.5f})"
    )
    ax3.axis("off")

    labels = ["Path tokens"]
    before = [token_before]
    after = [token_after]
    x = np.arange(len(labels))
    width = 0.35
    ax4.bar(x - width / 2, before, width=width, label="Original")
    ax4.bar(x + width / 2, after, width=width, label="PhysUI v2.2")
    ax4.set_xticks(x)
    ax4.set_xticklabels(labels)
    ax4.set_title("Compression")
    ax4.legend()
    ax4.grid(axis="y", alpha=0.25)

    plt.tight_layout()
    fig_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(fig_path, dpi=180)
    plt.close()


def rewrite_one_svg(
    in_svg: Path,
    out_svg: Path,
    fig_path: Path,
    iou_thresh: float,
    render_max_side: int,
    min_texture_conf: float,
    min_obj_iou: float,
    min_components_gate: int,
    min_elongated_ratio_gate: float,
    min_ar5_ratio_gate: float,
    max_perim_cv_gate: float,
    min_period_svg_gate: float,
    max_angle_std_gate: float,
    max_period_cv_gate: float,
    min_area_ratio_gate: float,
    max_area_ratio_gate: float,
    assets: list[AssetFingerprint],
    asset_score_thresh: float,
    asset_min_confidence: float,
    asset_min_margin: float,
    asset_max_angle_diff: float,
    min_edge_iou: float,
    max_rgb_mse: float,
) -> RewriteResult:
    svg_text = in_svg.read_text(errors="ignore")
    vbx, vby, vbw, vbh, width, height = parse_svg_meta(svg_text)
    scale = min(1.0, render_max_side / max(width, height))
    rw = max(64, int(round(width * scale)))
    rh = max(64, int(round(height * scale)))

    cands = detect_stripe_candidates(svg_text)
    if not cands:
        return RewriteResult(
            accepted=False,
            in_svg=in_svg,
            out_svg=out_svg,
            fig_path=fig_path,
            texture_label="none",
            texture_conf=0.0,
            iou_local=0.0,
            iou_soft=0.0,
            edge_iou=0.0,
            rgb_mse=1.0,
            token_before=count_path_tokens_in_svg(svg_text),
            token_after=count_path_tokens_in_svg(svg_text),
            token_ratio=1.0,
            candidate_info="none",
            reason="no stripe candidate",
        )

    rgba_before = render_svg_to_rgba(svg_text, rw, rh)
    token_before = count_path_tokens_in_svg(svg_text)

    best: RewriteResult | None = None
    best_svg_text: str | None = None
    best_rgba_after: np.ndarray | None = None

    for rank, cand in enumerate(cands[:3]):
        obj_before_svg = build_svg_shell(vbx, vby, vbw, vbh, width, height, cand.tag)
        try:
            rgba_obj_before = render_svg_to_rgba(obj_before_svg, rw, rh)
        except Exception:
            continue
        mask_obj_before = rgba_obj_before[:, :, 3] > 10
        texture_label, texture_conf, texture_stats = classify_texture_mask(mask_obj_before)
        if texture_label != "diagonal_stripes" or texture_conf < min_texture_conf:
            continue
        n_comp = int(texture_stats.get("n_comp", 0))
        angle_std_obj = float(texture_stats.get("angle_std", 999.0))
        period_cv = float(texture_stats.get("period_cv", 999.0))
        area_ratio = float(texture_stats.get("area_ratio", 0.0))
        purity = candidate_pattern_purity_metrics(cand)
        if n_comp < min_components_gate:
            continue
        if cand.elongated_ratio < min_elongated_ratio_gate:
            continue
        if purity["ar5_ratio"] < min_ar5_ratio_gate:
            continue
        if purity["perim_cv"] > max_perim_cv_gate:
            continue
        if cand.period_svg < min_period_svg_gate:
            continue
        if angle_std_obj > max_angle_std_gate:
            continue
        if period_cv > max_period_cv_gate:
            continue
        if area_ratio < min_area_ratio_gate or area_ratio > max_area_ratio_gate:
            continue

        period_norm = float(np.clip(cand.period_svg / max(vbw, vbh, 1e-6), 0.0, 1.0))
        stochasticity = float(np.clip(angle_std_obj / 90.0, 0.0, 1.0))
        asset_route = route_asset_for_candidate(
            assets=assets,
            family="diagonal_stripes",
            angle_deg=cand.angle_deg,
            period_norm=period_norm,
            elongated_ratio=cand.elongated_ratio,
            stochasticity=stochasticity,
            score_thresh=asset_score_thresh,
            min_confidence=asset_min_confidence,
            min_margin=asset_min_margin,
            max_angle_diff=asset_max_angle_diff,
        )
        if not asset_route.accepted:
            continue
        matched_asset = asset_route.asset
        asset_score = asset_route.best_score

        ys0, xs0 = np.where(mask_obj_before)
        if len(xs0) < 20:
            continue
        x0, x1 = int(xs0.min()), int(xs0.max())
        y0, y1 = int(ys0.min()), int(ys0.max())
        pad = max(6, int(round(0.02 * max(rw, rh))))
        x0 = max(0, x0 - pad)
        y0 = max(0, y0 - pad)
        x1 = min(rw, x1 + pad + 1)
        y1 = min(rh, y1 + pad + 1)
        crop = mask_obj_before[y0:y1, x0:x1]
        if crop.sum() < 20:
            continue

        px_per_svg = 0.5 * ((rw - 1) / max(vbw, 1e-6) + (rh - 1) / max(vbh, 1e-6))
        kernel = int(round(max(3.0, cand.period_svg * px_per_svg * 0.65)))
        sil_local = recover_silhouette(crop, kernel=kernel)
        sil = np.zeros_like(mask_obj_before, dtype=bool)
        sil[y0:y1, x0:x1] = sil_local

        ys, xs = np.where(sil)
        if len(xs) < 20:
            continue
        x0, x1 = int(xs.min()), int(xs.max())
        y0, y1 = int(ys.min()), int(ys.max())
        margin = 8
        x0 = max(0, x0 - margin)
        y0 = max(0, y0 - margin)
        x1 = min(rw, x1 + margin + 1)
        y1 = min(rh, y1 + margin + 1)
        bbox_px = (x0, y0, x1, y1)

        clip_d, clip_pts = mask_to_boundary_path(sil, vbx, vby, vbw, vbh, rw, rh, max_points=96)
        cx = clip_pts[:, 0]
        cy = clip_pts[:, 1]
        bbox_svg = (float(cx.min()), float(cy.min()), float(cx.max()), float(cy.max()))
        fg = estimate_component_color(rgba_obj_before, sil, cand.fill_color)

        uid = f"{cand.idx}_{rank}"
        fill_ratio = float(crop.sum() / max(sil_local.sum(), 1))
        angle_override = matched_asset.orientation_deg if matched_asset is not None else None
        period_override = None
        if matched_asset is not None and matched_asset.period_norm is not None:
            period_override = max(matched_asset.period_norm * max(vbw, vbh), 2.0)
        defs, use = build_replacement_snippet(
            cand,
            clip_d,
            bbox_svg,
            fg,
            uid,
            fill_ratio=fill_ratio,
            angle_override=angle_override,
            period_override=period_override,
        )
        new_svg = svg_text[: cand.span_start] + use + svg_text[cand.span_end :]
        new_svg = inject_defs(new_svg, defs)

        obj_after_svg = build_svg_shell(vbx, vby, vbw, vbh, width, height, defs + use)
        try:
            rgba_after = render_svg_to_rgba(new_svg, rw, rh)
            rgba_obj_after = render_svg_to_rgba(obj_after_svg, rw, rh)
        except Exception:
            continue
        mask_obj_after = rgba_obj_after[:, :, 3] > 10
        iou = local_iou(mask_obj_before, mask_obj_after, bbox_px)
        iou_soft = local_iou_soft(mask_obj_before, mask_obj_after, bbox_px, radius=2)
        edge_iou = local_edge_iou(
            rgba_before,
            rgba_after,
            bbox_px,
            mask_obj_before,
            mask_obj_after,
        )
        rgb_mse = local_rgb_mse(
            rgba_before,
            rgba_after,
            bbox_px,
            mask_obj_before,
            mask_obj_after,
        )

        tok_after = count_path_tokens_in_svg(new_svg)
        ratio = tok_after / max(token_before, 1)
        accepted = (
            iou_soft >= iou_thresh
            and iou >= min_obj_iou
            and edge_iou >= min_edge_iou
            and rgb_mse <= max_rgb_mse
            and tok_after < token_before
        )
        reason = (
            "ok"
            if accepted
            else (
                f"reject(iou={iou:.4f},soft={iou_soft:.4f},edge={edge_iou:.4f},"
                f"mse={rgb_mse:.5f},ratio={ratio:.4f},"
                f"asset={matched_asset.asset_id if matched_asset else 'none'}:{asset_score:.3f},"
                f"conf={asset_route.confidence:.3f},mrg={asset_route.margin:.3f})"
            )
        )

        result = RewriteResult(
            accepted=accepted,
            in_svg=in_svg,
            out_svg=out_svg,
            fig_path=fig_path,
            texture_label=texture_label,
            texture_conf=texture_conf,
            iou_local=iou,
            iou_soft=iou_soft,
            edge_iou=edge_iou,
            rgb_mse=rgb_mse,
            token_before=token_before,
            token_after=tok_after,
            token_ratio=ratio,
            candidate_info=(
                f"idx={cand.idx},z={cand.z_count},sub={cand.subpath_count},"
                f"elong={cand.elongated_ratio:.3f},ang={cand.angle_deg:.2f}±{cand.angle_std_deg:.2f},"
                f"ar5={purity['ar5_ratio']:.3f},pcv2={purity['perim_cv']:.2f},"
                f"cls={texture_label}@{texture_conf:.3f},"
                f"n={n_comp},std={angle_std_obj:.2f},pcv={period_cv:.2f},ar={area_ratio:.3f},"
                f"asset={(matched_asset.asset_id if matched_asset else 'none')}:{asset_score:.3f},"
                f"conf={asset_route.confidence:.3f},mrg={asset_route.margin:.3f}"
            ),
            reason=reason,
        )

        if best is None or (result.accepted and result.token_after < best.token_after) or (
            not best.accepted and result.iou_local + result.edge_iou > best.iou_local + best.edge_iou
        ):
            best = result
            best_svg_text = new_svg
            best_rgba_after = rgba_after

    if best is None:
        return RewriteResult(
            accepted=False,
            in_svg=in_svg,
            out_svg=out_svg,
            fig_path=fig_path,
            texture_label="none",
            texture_conf=0.0,
            iou_local=0.0,
            iou_soft=0.0,
            edge_iou=0.0,
            rgb_mse=1.0,
            token_before=token_before,
            token_after=token_before,
            token_ratio=1.0,
            candidate_info="none",
            reason="no valid candidate trial",
        )

    if best_svg_text is not None and best_rgba_after is not None:
        make_debug_figure(
            rgba_before,
            best_rgba_after,
            best.iou_local,
            best.iou_soft,
            best.edge_iou,
            best.rgb_mse,
            best.token_before,
            best.token_after,
            fig_path,
        )
    if best.accepted and best_svg_text is not None:
        out_svg.parent.mkdir(parents=True, exist_ok=True)
        out_svg.write_text(best_svg_text, encoding="utf-8")
    return best


def rank_dataset_candidates(root: Path, top_k: int = 50) -> list[Path]:
    rows: list[tuple[float, Path]] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix not in SVG_EXTS:
            continue
        try:
            txt = p.read_text(errors="ignore")
        except Exception:
            continue
        cands = detect_stripe_candidates(txt)
        if not cands:
            continue
        score = cands[0].score
        rows.append((score, p))
    rows.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in rows[:top_k]]


def run_batch(args: argparse.Namespace) -> None:
    root = Path(args.dataset_root)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    assets = load_asset_fingerprints(args.asset_lib)

    cands = rank_dataset_candidates(root, top_k=max(args.max_files * 4, args.max_files))
    cands = cands[: args.max_files]
    print(f"[v2.2] ranked candidates: {len(cands)}")

    results: list[RewriteResult] = []
    for i, fp in enumerate(cands):
        stem = fp.stem + f"_v22_{i}"
        out_svg = out_dir / f"{stem}.svg"
        fig = out_dir / f"{stem}.png"
        try:
            r = rewrite_one_svg(
                fp,
                out_svg,
                fig,
                iou_thresh=args.iou_thresh,
                render_max_side=args.render_max_side,
                min_texture_conf=args.min_texture_conf,
                min_obj_iou=args.min_obj_iou,
                min_components_gate=args.min_components_gate,
                min_elongated_ratio_gate=args.min_elongated_ratio_gate,
                min_ar5_ratio_gate=args.min_ar5_ratio_gate,
                max_perim_cv_gate=args.max_perim_cv_gate,
                min_period_svg_gate=args.min_period_svg_gate,
                max_angle_std_gate=args.max_angle_std_gate,
                max_period_cv_gate=args.max_period_cv_gate,
                min_area_ratio_gate=args.min_area_ratio_gate,
                max_area_ratio_gate=args.max_area_ratio_gate,
                assets=assets,
                asset_score_thresh=args.asset_score_thresh,
                asset_min_confidence=args.asset_min_confidence,
                asset_min_margin=args.asset_min_margin,
                asset_max_angle_diff=args.asset_max_angle_diff,
                min_edge_iou=args.min_edge_iou,
                max_rgb_mse=args.max_rgb_mse,
            )
        except Exception as e:
            r = RewriteResult(
                accepted=False,
                in_svg=fp,
                out_svg=out_svg,
                fig_path=fig,
                texture_label="error",
                texture_conf=0.0,
                iou_local=0.0,
                iou_soft=0.0,
                edge_iou=0.0,
                rgb_mse=1.0,
                token_before=0,
                token_after=0,
                token_ratio=1.0,
                candidate_info="exception",
                reason=str(e),
            )
        results.append(r)
        print(
            f"[{i+1}/{len(cands)}] {fp.name} accepted={r.accepted} "
            f"cls={r.texture_label}@{r.texture_conf:.2f} "
            f"iou={r.iou_local:.4f}/{r.iou_soft:.4f} edge={r.edge_iou:.4f} mse={r.rgb_mse:.5f} "
            f"tok={r.token_before}->{r.token_after} reason={r.reason}"
        )

    accepted = [r for r in results if r.accepted]
    summary_txt = out_dir / "v22_batch_report.txt"
    lines = []
    lines.append("=== PhysUI v2.2 Texture Routing Batch Report ===")
    lines.append(f"dataset_root: {root}")
    lines.append(f"processed: {len(results)}")
    lines.append(f"accepted: {len(accepted)}")
    if accepted:
        tb = sum(r.token_before for r in accepted)
        ta = sum(r.token_after for r in accepted)
        iou = float(np.mean([r.iou_local for r in accepted]))
        iou_soft = float(np.mean([r.iou_soft for r in accepted]))
        edge_iou = float(np.mean([r.edge_iou for r in accepted]))
        rgb_mse = float(np.mean([r.rgb_mse for r in accepted]))
        lines.append(f"accepted_token_before: {tb}")
        lines.append(f"accepted_token_after: {ta}")
        lines.append(f"accepted_token_ratio: {ta/max(tb,1):.4f}")
        lines.append(f"accepted_mean_local_iou: {iou:.4f}")
        lines.append(f"accepted_mean_soft_iou: {iou_soft:.4f}")
        lines.append(f"accepted_mean_edge_iou: {edge_iou:.4f}")
        lines.append(f"accepted_mean_rgb_mse: {rgb_mse:.5f}")
    lines.append("")
    for r in results:
        lines.append(
            f"{r.in_svg} | accepted={r.accepted} | cls={r.texture_label}@{r.texture_conf:.3f} | iou={r.iou_local:.4f} | "
            f"soft_iou={r.iou_soft:.4f} | edge_iou={r.edge_iou:.4f} | rgb_mse={r.rgb_mse:.5f} | "
            f"tok={r.token_before}->{r.token_after} ({r.token_ratio:.4f}) | "
            f"cand={r.candidate_info} | reason={r.reason}"
        )
    summary_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Summary figure.
    fig_sum = out_dir / "v22_batch_metrics.png"
    plt.figure(figsize=(10, 5))
    labels = ["Accepted count", "Rejected count"]
    vals = [len(accepted), len(results) - len(accepted)]
    plt.subplot(1, 2, 1)
    bars = plt.bar(labels, vals, color=["#2ca02c", "#d62728"])
    plt.title("PhysUI v2.2 Acceptance")
    plt.grid(axis="y", alpha=0.25)
    for b, v in zip(bars, vals):
        plt.text(b.get_x() + b.get_width() / 2, v, str(v), ha="center", va="bottom", fontsize=9)

    plt.subplot(1, 2, 2)
    if accepted:
        idx = np.arange(len(accepted))
        before = [r.token_before for r in accepted]
        after = [r.token_after for r in accepted]
        w = 0.35
        plt.bar(idx - w / 2, before, width=w, label="before")
        plt.bar(idx + w / 2, after, width=w, label="after")
        plt.xticks(idx, [str(i + 1) for i in idx], fontsize=8)
        plt.title("Accepted Samples: Path Tokens")
        plt.xlabel("sample id")
        plt.legend()
    else:
        plt.text(0.5, 0.5, "No accepted rewrites", ha="center", va="center")
        plt.xlim(0, 1)
        plt.ylim(0, 1)
    plt.grid(axis="y", alpha=0.25)
    plt.tight_layout()
    plt.savefig(fig_sum, dpi=180)
    plt.close()
    print(f"[v2.2] saved: {summary_txt}")
    print(f"[v2.2] saved: {fig_sum}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_svg", type=str, default="")
    parser.add_argument("--out_svg", type=str, default="/Users/xiaoxiaobo/physui_v22_single.svg")
    parser.add_argument("--fig_path", type=str, default="/Users/xiaoxiaobo/physui_v22_single.png")
    parser.add_argument("--dataset_root", type=str, default="")
    parser.add_argument("--out_dir", type=str, default="/Users/xiaoxiaobo/physui_v22_outputs")
    parser.add_argument("--max_files", type=int, default=8)
    parser.add_argument("--iou_thresh", type=float, default=0.94)
    parser.add_argument("--render_max_side", type=int, default=1024)
    parser.add_argument("--min_texture_conf", type=float, default=0.65)
    parser.add_argument("--min_obj_iou", type=float, default=0.60)
    parser.add_argument("--min_components_gate", type=int, default=16)
    parser.add_argument("--min_elongated_ratio_gate", type=float, default=0.70)
    parser.add_argument("--min_ar5_ratio_gate", type=float, default=0.70)
    parser.add_argument("--max_perim_cv_gate", type=float, default=0.90)
    parser.add_argument("--min_period_svg_gate", type=float, default=1.60)
    parser.add_argument("--max_angle_std_gate", type=float, default=40.0)
    parser.add_argument("--max_period_cv_gate", type=float, default=0.95)
    parser.add_argument("--min_area_ratio_gate", type=float, default=0.002)
    parser.add_argument("--max_area_ratio_gate", type=float, default=0.35)
    parser.add_argument("--asset_lib", type=str, default="/Users/xiaoxiaobo/physui_asset_schema/office_pattern_assets_v1.json")
    parser.add_argument("--asset_score_thresh", type=float, default=0.42)
    parser.add_argument("--asset_min_confidence", type=float, default=0.62)
    parser.add_argument("--asset_min_margin", type=float, default=0.004)
    parser.add_argument("--asset_max_angle_diff", type=float, default=45.0)
    parser.add_argument("--min_edge_iou", type=float, default=0.85)
    parser.add_argument("--max_rgb_mse", type=float, default=0.008)
    args = parser.parse_args()

    if args.input_svg:
        in_svg = Path(args.input_svg)
        out_svg = Path(args.out_svg)
        fig = Path(args.fig_path)
        assets = load_asset_fingerprints(args.asset_lib)
        r = rewrite_one_svg(
            in_svg,
            out_svg,
            fig,
            iou_thresh=args.iou_thresh,
            render_max_side=args.render_max_side,
            min_texture_conf=args.min_texture_conf,
            min_obj_iou=args.min_obj_iou,
            min_components_gate=args.min_components_gate,
            min_elongated_ratio_gate=args.min_elongated_ratio_gate,
            min_ar5_ratio_gate=args.min_ar5_ratio_gate,
            max_perim_cv_gate=args.max_perim_cv_gate,
            min_period_svg_gate=args.min_period_svg_gate,
            max_angle_std_gate=args.max_angle_std_gate,
            max_period_cv_gate=args.max_period_cv_gate,
            min_area_ratio_gate=args.min_area_ratio_gate,
            max_area_ratio_gate=args.max_area_ratio_gate,
            assets=assets,
            asset_score_thresh=args.asset_score_thresh,
            asset_min_confidence=args.asset_min_confidence,
            asset_min_margin=args.asset_min_margin,
            asset_max_angle_diff=args.asset_max_angle_diff,
            min_edge_iou=args.min_edge_iou,
            max_rgb_mse=args.max_rgb_mse,
        )
        print("=== single result ===")
        print(f"in: {in_svg}")
        print(f"accepted: {r.accepted}")
        print(f"texture: {r.texture_label}@{r.texture_conf:.3f}")
        print(f"local_iou: {r.iou_local:.4f}")
        print(f"soft_iou: {r.iou_soft:.4f}")
        print(f"edge_iou: {r.edge_iou:.4f}")
        print(f"rgb_mse: {r.rgb_mse:.5f}")
        print(f"path_tokens: {r.token_before} -> {r.token_after} ({r.token_ratio:.4f})")
        print(f"candidate: {r.candidate_info}")
        print(f"reason: {r.reason}")
        if r.accepted:
            print(f"out_svg: {out_svg}")
            print(f"fig: {fig}")
        return

    if args.dataset_root:
        run_batch(args)
        return

    raise SystemExit("Provide either --input_svg or --dataset_root")


if __name__ == "__main__":
    main()
