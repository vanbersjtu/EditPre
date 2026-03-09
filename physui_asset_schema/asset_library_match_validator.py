#!/usr/bin/env python3
"""Validate whether PhysUI asset library can match pattern-heavy paths on random2000.

This script is designed as a reusable acceptance test for the asset-library stage.
It reports coverage/accuracy both by sample count and by path-token share.
"""

from __future__ import annotations

import argparse
import importlib.util
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import matplotlib.pyplot as plt
import numpy as np

from asset_models import AssetLibrary


SVG_EXTS = {".svg", ".SVG"}


@dataclass
class HeavyPathFeature:
    file: str
    mode: str
    family: str
    z_count: int
    subpaths: int
    elongated_ratio: float
    angle_std: float
    angle_deg: float
    period_norm: float
    density: float
    stochasticity: float
    path_tokens: int


@dataclass
class AssetFingerprint:
    asset_id: str
    family: str
    orientation_deg: float | None
    period_norm: float | None
    anisotropy: float | None
    stochasticity: float | None
    tags: set[str]


def load_v22_module():
    spec = importlib.util.spec_from_file_location("v22", "/Users/xiaoxiaobo/physui_texture_router_v22.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load /Users/xiaoxiaobo/physui_texture_router_v22.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def classify_mode(zc: int, sub_n: int, elong_ratio: float, angle_std: float, med_area: float, bbox_fill: float) -> str:
    # Keep aligned with previous probes to make metrics comparable.
    if elong_ratio >= 0.55 and angle_std <= 30.0:
        return "diagonal_stripes_clean"
    if elong_ratio >= 0.40 and angle_std <= 55.0:
        return "diagonal_stripes_noisy"
    if elong_ratio >= 0.30 and angle_std > 55.0:
        return "hatching_multi_dir"
    if elong_ratio < 0.20 and med_area < 30.0 and sub_n >= 80:
        return "dots_or_speckles"
    if zc >= 200 and sub_n >= 120 and bbox_fill >= 0.35:
        return "dense_fragments"
    return "other_compiled"


def mode_to_family(mode: str) -> str:
    if mode in {"diagonal_stripes_clean", "diagonal_stripes_noisy"}:
        return "diagonal_stripes"
    if mode == "hatching_multi_dir":
        return "hatching"
    if mode == "dots_or_speckles":
        return "dots"
    if mode == "dense_fragments":
        return "dense_fragments"
    return "other"


def infer_asset_family(tags: set[str], asset_id: str) -> str:
    s = {t.lower() for t in tags}
    txt = (asset_id + " " + " ".join(sorted(s))).lower()
    if "stripe" in txt or "diagonal" in txt:
        return "diagonal_stripes"
    if "hatch" in txt:
        return "hatching"
    if "dot" in txt or "speckle" in txt:
        return "dots"
    if "fragment" in txt or "noise" in txt:
        return "dense_fragments"
    return "other"


def wrap180_diff(a: float, b: float) -> float:
    d = abs((a - b) % 180.0)
    return min(d, 180.0 - d)


def build_asset_fingerprints(lib: AssetLibrary) -> List[AssetFingerprint]:
    fps: List[AssetFingerprint] = []
    for a in lib.assets:
        rf = a.retrieval_features
        tags = set(a.tags)
        fam = infer_asset_family(tags, a.asset_id)
        fps.append(
            AssetFingerprint(
                asset_id=a.asset_id,
                family=fam,
                orientation_deg=rf.orientation_deg,
                period_norm=rf.period_px_norm,
                anisotropy=rf.anisotropy,
                stochasticity=rf.stochasticity,
                tags=tags,
            )
        )
    return fps


def extract_heavy_features(root: Path, z_min: int, max_paths: int) -> List[HeavyPathFeature]:
    v22 = load_v22_module()
    rows: List[HeavyPathFeature] = []

    for fp in root.rglob("*"):
        if not fp.is_file() or fp.suffix not in SVG_EXTS:
            continue
        try:
            txt = fp.read_text(errors="ignore")
        except Exception:
            continue

        d_list = v22.PATH_RE.findall(txt)
        if not d_list:
            continue

        # use viewBox scale if possible, fallback by local bbox max side.
        vbx, vby, vbw, vbh, _w, _h = v22.parse_svg_meta(txt)
        scale_ref = max(vbw, vbh, 1.0)

        for d in d_list:
            zc = d.count("Z") + d.count("z")
            if zc < z_min:
                continue
            subs = v22.parse_path_subpaths(d)
            if len(subs) < 8:
                continue

            angles = []
            elong_flags = []
            majors = []
            minors = []
            areas = []
            all_pts = []
            for s in subs:
                ang, major, minor = v22.pca_angle_and_dims(s)
                angles.append(ang)
                majors.append(major)
                minors.append(minor)
                elong_flags.append(1.0 if major / max(minor, 1e-6) >= 2.0 else 0.0)
                x = s[:, 0]
                y = s[:, 1]
                area = 0.5 * float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))
                areas.append(area)
                all_pts.append(s)

            if not angles:
                continue

            allp = np.vstack(all_pts)
            bw = float(allp[:, 0].max() - allp[:, 0].min())
            bh = float(allp[:, 1].max() - allp[:, 1].min())
            bbox_area = max(bw * bh, 1e-6)
            bbox_side = max(bw, bh, 1e-6)

            sub_n = len(subs)
            elong_ratio = float(np.mean(elong_flags))
            angle_std = float(v22.circular_std_deg(np.asarray(angles, dtype=np.float64)))
            med_area = float(np.median(areas))
            bbox_fill = float(np.sum(areas) / bbox_area)
            mode = classify_mode(zc, sub_n, elong_ratio, angle_std, med_area, bbox_fill)
            fam = mode_to_family(mode)

            centers = np.asarray([s.mean(axis=0) for s in subs], dtype=np.float64)
            angle_deg = float(np.median(np.asarray(angles, dtype=np.float64)))
            period = float(v22.estimate_period(centers, angle_deg))
            period_norm = float(np.clip(period / max(scale_ref, bbox_side), 0.0, 1.0))
            density = float(np.clip(sub_n / max(zc, 1), 0.0, 1.0))
            stochasticity = float(np.clip(angle_std / 90.0, 0.0, 1.0))
            tok = int(len(v22.NUM_RE.findall(d)) + len(v22.CMD_RE.findall(d)))

            rows.append(
                HeavyPathFeature(
                    file=str(fp),
                    mode=mode,
                    family=fam,
                    z_count=zc,
                    subpaths=sub_n,
                    elongated_ratio=elong_ratio,
                    angle_std=angle_std,
                    angle_deg=angle_deg,
                    period_norm=period_norm,
                    density=density,
                    stochasticity=stochasticity,
                    path_tokens=tok,
                )
            )
            if len(rows) >= max_paths:
                return rows
    return rows


def family_missing_penalty(target_family: str, asset_family: str) -> float:
    if target_family == asset_family:
        return 0.0
    if target_family in {"diagonal_stripes", "hatching"} and asset_family in {"diagonal_stripes", "hatching"}:
        return 0.35
    return 0.75


def score_match(f: HeavyPathFeature, a: AssetFingerprint) -> float:
    # Lower is better.
    s = 0.0
    s += family_missing_penalty(f.family, a.family)

    if a.orientation_deg is not None:
        s += 0.20 * (wrap180_diff(f.angle_deg, float(a.orientation_deg)) / 90.0)
    else:
        s += 0.08

    if a.period_norm is not None:
        s += 0.18 * min(abs(f.period_norm - float(a.period_norm)) / 0.30, 1.0)
    else:
        s += 0.06

    if a.anisotropy is not None:
        # anisotropy ~ elongated ratio proxy
        s += 0.12 * min(abs(f.elongated_ratio - float(a.anisotropy)) / 0.60, 1.0)
    else:
        s += 0.06

    if a.stochasticity is not None:
        s += 0.10 * min(abs(f.stochasticity - float(a.stochasticity)) / 0.50, 1.0)
    else:
        s += 0.04

    # A tiny preference for token-heavy paths to match deterministic families.
    if f.path_tokens > 500 and a.family == f.family:
        s -= 0.02

    return float(max(s, 0.0))


def match_best(features: List[HeavyPathFeature], assets: List[AssetFingerprint], score_thresh: float):
    rows = []
    for f in features:
        best_id = ""
        best_family = ""
        best_score = 1e9
        for a in assets:
            sc = score_match(f, a)
            if sc < best_score:
                best_score = sc
                best_id = a.asset_id
                best_family = a.family
        matched = (best_family == f.family) and (best_score <= score_thresh)
        rows.append((f, best_id, best_family, best_score, matched))
    return rows


def save_outputs(
    out_prefix: Path,
    rows,
    coverage_goal: float,
    token_goal: float,
    score_thresh: float,
    eval_families: set[str],
) -> None:
    csv_path = out_prefix.with_suffix(".csv")
    txt_path = out_prefix.with_suffix(".txt")
    fig_path = out_prefix.with_suffix(".png")

    # csv
    with csv_path.open("w", encoding="utf-8") as f:
        f.write(
            "file,mode,family,path_tokens,z_count,subpaths,elongated_ratio,angle_std,angle_deg,period_norm,"
            "best_asset,best_family,best_score,matched\n"
        )
        for item in rows:
            hf, aid, afam, score, matched = item
            f.write(
                f'"{hf.file}",{hf.mode},{hf.family},{hf.path_tokens},{hf.z_count},{hf.subpaths},'
                f"{hf.elongated_ratio:.6f},{hf.angle_std:.6f},{hf.angle_deg:.6f},{hf.period_norm:.6f},"
                f"{aid},{afam},{score:.6f},{int(matched)}\n"
            )

    # metrics
    total = len(rows)
    eval_rows = [r for r in rows if r[0].family in eval_families]
    matched_n = sum(1 for _, _, _, _, m in eval_rows if m)
    acc = matched_n / max(len(eval_rows), 1)
    tok_total = sum(hf.path_tokens for hf, *_ in eval_rows)
    tok_matched = sum(hf.path_tokens for hf, *_r, m in eval_rows if m)
    tok_acc = tok_matched / max(tok_total, 1)

    fam_count = Counter(hf.family for hf, *_ in rows)
    fam_match_count = Counter(hf.family for hf, *_r, m in rows if m)
    fam_tok = defaultdict(int)
    fam_tok_match = defaultdict(int)
    score_by_fam = defaultdict(list)

    for hf, _aid, _afam, sc, m in rows:
        fam_tok[hf.family] += hf.path_tokens
        score_by_fam[hf.family].append(sc)
        if m:
            fam_tok_match[hf.family] += hf.path_tokens

    fams = sorted(fam_count.keys(), key=lambda x: fam_count[x], reverse=True)

    # pass/fail gate: only check configured evaluation families.
    meaningful = [k for k in fams if k in eval_families and fam_count[k] >= 5]
    fam_cover_ok = True
    for k in meaningful:
        c = fam_match_count[k] / max(fam_count[k], 1)
        if c < coverage_goal:
            fam_cover_ok = False
            break

    overall_ok = acc >= coverage_goal and tok_acc >= token_goal and fam_cover_ok

    lines: List[str] = []
    lines.append("=== PhysUI Asset Library Matching Validation ===")
    lines.append(f"samples_total={total}")
    lines.append(f"samples_eval={len(eval_rows)}")
    lines.append(f"eval_families={sorted(eval_families)}")
    lines.append(f"score_threshold={score_thresh:.3f}")
    lines.append(f"match_coverage={acc:.4f} ({matched_n}/{len(eval_rows)})")
    lines.append(f"token_weighted_coverage={tok_acc:.4f} ({tok_matched}/{tok_total})")
    lines.append(f"coverage_goal={coverage_goal:.2f}, token_goal={token_goal:.2f}")
    lines.append(f"validation_result={'PASS' if overall_ok else 'FAIL'}")
    lines.append("")
    lines.append("Per-family:")
    for k in fams:
        cov = fam_match_count[k] / max(fam_count[k], 1)
        tcov = fam_tok_match[k] / max(fam_tok[k], 1)
        ms = float(np.mean(score_by_fam[k])) if score_by_fam[k] else 0.0
        eval_flag = "eval" if k in eval_families else "non-eval"
        lines.append(
            f"  {k} [{eval_flag}]: count={fam_count[k]}, coverage={cov:.4f}, token_coverage={tcov:.4f}, mean_score={ms:.4f}"
        )

    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # figure
    plt.figure(figsize=(14, 8))
    ax1 = plt.subplot(2, 2, 1)
    ax2 = plt.subplot(2, 2, 2)
    ax3 = plt.subplot(2, 1, 2)

    covs = [fam_match_count[k] / max(fam_count[k], 1) for k in fams]
    tcovs = [fam_tok_match[k] / max(fam_tok[k], 1) for k in fams]

    ax1.bar(fams, covs, color="#1f77b4")
    ax1.axhline(coverage_goal, color="red", linestyle="--", linewidth=1.3, label="coverage goal")
    ax1.set_ylim(0, 1.05)
    ax1.set_title("Family Match Coverage (count)")
    ax1.tick_params(axis="x", rotation=25)
    ax1.grid(axis="y", alpha=0.25)
    ax1.legend(fontsize=8)
    for i, v in enumerate(covs):
        ax1.text(i, v, f"{v:.2f}", ha="center", va="bottom", fontsize=8)

    ax2.bar(fams, tcovs, color="#ff7f0e")
    ax2.axhline(token_goal, color="red", linestyle="--", linewidth=1.3, label="token goal")
    ax2.set_ylim(0, 1.05)
    ax2.set_title("Family Match Coverage (token-weighted)")
    ax2.tick_params(axis="x", rotation=25)
    ax2.grid(axis="y", alpha=0.25)
    ax2.legend(fontsize=8)
    for i, v in enumerate(tcovs):
        ax2.text(i, v, f"{v:.2f}", ha="center", va="bottom", fontsize=8)

    score_vals = [sc for _hf, _aid, _afam, sc, _m in rows]
    ax3.hist(score_vals, bins=24, color="#2ca02c", alpha=0.85)
    ax3.axvline(score_thresh, color="red", linestyle="--", linewidth=1.3, label="score threshold")
    ax3.set_title(f"Best-Match Score Distribution | overall={'PASS' if overall_ok else 'FAIL'}")
    ax3.set_xlabel("best score (lower is better)")
    ax3.set_ylabel("count")
    ax3.grid(axis="y", alpha=0.25)
    ax3.legend(fontsize=8)

    plt.tight_layout()
    plt.savefig(fig_path, dpi=180)
    plt.close()

    print(f"saved csv: {csv_path}")
    print(f"saved txt: {txt_path}")
    print(f"saved fig: {fig_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/Users/xiaoxiaobo/random2000", help="dataset root")
    parser.add_argument(
        "--asset_lib",
        default="/Users/xiaoxiaobo/physui_asset_schema/example_asset_library.json",
        help="asset library json",
    )
    parser.add_argument(
        "--out_prefix",
        default="/Users/xiaoxiaobo/physui_asset_schema/random2000_asset_match_validation",
        help="output prefix (.csv/.txt/.png)",
    )
    parser.add_argument("--z_min", type=int, default=20)
    parser.add_argument("--max_paths", type=int, default=1200)
    parser.add_argument("--score_thresh", type=float, default=0.42)
    parser.add_argument("--coverage_goal", type=float, default=0.80)
    parser.add_argument("--token_goal", type=float, default=0.80)
    parser.add_argument(
        "--eval_families",
        type=str,
        default="diagonal_stripes,hatching,dots,dense_fragments",
        help="comma-separated families used for PASS/FAIL gate",
    )
    args = parser.parse_args()

    lib_data = Path(args.asset_lib).read_text(encoding="utf-8")
    lib = AssetLibrary.model_validate_json(lib_data)
    assets = build_asset_fingerprints(lib)
    if not assets:
        raise SystemExit("asset library has no assets")

    features = extract_heavy_features(Path(args.root), z_min=args.z_min, max_paths=args.max_paths)
    if not features:
        raise SystemExit("no heavy paths found under current setting")

    matches = match_best(features, assets, score_thresh=args.score_thresh)
    eval_families = {x.strip() for x in args.eval_families.split(",") if x.strip()}
    save_outputs(
        Path(args.out_prefix),
        matches,
        coverage_goal=args.coverage_goal,
        token_goal=args.token_goal,
        score_thresh=args.score_thresh,
        eval_families=eval_families,
    )


if __name__ == "__main__":
    main()
