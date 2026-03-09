import argparse
import importlib.util
import re
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


PATH_RE = re.compile(r'<path\b[^>]*\bd="([^"]*)"[^>]*>', flags=re.I | re.S)
NUM_RE = re.compile(r"[-+]?(?:\d+\.\d+|\d+|\.\d+)(?:[eE][-+]?\d+)?")
CMD_RE = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]")
SVG_EXTS = {".svg", ".SVG"}


def polygon_area(points: np.ndarray) -> float:
    if len(points) < 3:
        return 0.0
    x = points[:, 0]
    y = points[:, 1]
    return 0.5 * float(np.abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def count_tokens(d: str) -> int:
    return len(NUM_RE.findall(d)) + len(CMD_RE.findall(d))


def classify_mode(zc: int, sub_n: int, elong_ratio: float, angle_std: float, med_area: float, bbox_fill: float) -> str:
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/Users/xiaoxiaobo/random2000")
    parser.add_argument("--out_prefix", default="/Users/xiaoxiaobo/physui_random2000_texture_modes")
    parser.add_argument("--z_min", type=int, default=20)
    parser.add_argument("--max_paths", type=int, default=1200)
    args = parser.parse_args()

    # Reuse geometric utilities from v2.2 script.
    spec = importlib.util.spec_from_file_location("v22", "/Users/xiaoxiaobo/physui_texture_router_v22.py")
    v22 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(v22)

    root = Path(args.root)
    rows = []
    scanned = 0

    for fp in root.rglob("*"):
        if not fp.is_file() or fp.suffix not in SVG_EXTS:
            continue
        try:
            txt = fp.read_text(errors="ignore")
        except Exception:
            continue
        ds = PATH_RE.findall(txt)
        if not ds:
            continue
        for d in ds:
            zc = d.count("Z") + d.count("z")
            if zc < args.z_min:
                continue
            subs = v22.parse_path_subpaths(d)
            if len(subs) < 8:
                continue
            angles = []
            elong_flags = []
            areas = []
            all_pts = []
            for s in subs:
                ang, major, minor = v22.pca_angle_and_dims(s)
                angles.append(ang)
                elong_flags.append(1.0 if major / max(minor, 1e-6) >= 2.0 else 0.0)
                areas.append(polygon_area(s))
                all_pts.append(s)
            if not angles:
                continue
            allp = np.vstack(all_pts)
            bw = float(allp[:, 0].max() - allp[:, 0].min())
            bh = float(allp[:, 1].max() - allp[:, 1].min())
            bbox_area = max(bw * bh, 1e-6)

            sub_n = len(subs)
            elong_ratio = float(np.mean(elong_flags))
            angle_std = float(v22.circular_std_deg(np.asarray(angles, dtype=np.float64)))
            med_area = float(np.median(areas))
            bbox_fill = float(np.sum(areas) / bbox_area)
            tok = count_tokens(d)
            mode = classify_mode(zc, sub_n, elong_ratio, angle_std, med_area, bbox_fill)
            rows.append(
                {
                    "file": str(fp),
                    "z_count": zc,
                    "subpaths": sub_n,
                    "elong_ratio": elong_ratio,
                    "angle_std": angle_std,
                    "med_area": med_area,
                    "bbox_fill": bbox_fill,
                    "path_tokens": tok,
                    "mode": mode,
                }
            )
            scanned += 1
            if scanned >= args.max_paths:
                break
        if scanned >= args.max_paths:
            break

    if not rows:
        raise SystemExit("No heavy paths found.")

    out_prefix = Path(args.out_prefix)
    csv_path = out_prefix.with_suffix(".csv")
    txt_path = out_prefix.with_suffix(".txt")
    fig_path = out_prefix.with_suffix(".png")

    with csv_path.open("w", encoding="utf-8") as f:
        f.write("file,z_count,subpaths,elong_ratio,angle_std,med_area,bbox_fill,path_tokens,mode\n")
        for r in rows:
            f.write(
                f"\"{r['file']}\",{r['z_count']},{r['subpaths']},{r['elong_ratio']:.6f},"
                f"{r['angle_std']:.6f},{r['med_area']:.6f},{r['bbox_fill']:.6f},"
                f"{r['path_tokens']},{r['mode']}\n"
            )

    mode_count = Counter(r["mode"] for r in rows)
    mode_tokens = defaultdict(int)
    for r in rows:
        mode_tokens[r["mode"]] += int(r["path_tokens"])

    total_tok = sum(mode_tokens.values())
    sorted_modes = sorted(mode_count.keys(), key=lambda m: mode_count[m], reverse=True)

    lines = []
    lines.append("=== PhysUI random2000 Texture Mode Probe ===")
    lines.append(f"root: {root}")
    lines.append(f"z_min: {args.z_min}")
    lines.append(f"scanned_heavy_paths: {len(rows)}")
    lines.append("")
    lines.append("Mode statistics:")
    for m in sorted_modes:
        cnt = mode_count[m]
        tok = mode_tokens[m]
        lines.append(f"  {m}: count={cnt}, token={tok}, token_share={tok / max(total_tok,1):.4f}")

    # Top heavy paths by tokens.
    top = sorted(rows, key=lambda r: r["path_tokens"], reverse=True)[:20]
    lines.append("")
    lines.append("Top heavy paths:")
    for r in top:
        lines.append(
            f"  {r['file']} | mode={r['mode']} | tokens={r['path_tokens']} | z={r['z_count']} | "
            f"sub={r['subpaths']} | elong={r['elong_ratio']:.3f} | angle_std={r['angle_std']:.2f}"
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Figure: count + token share + feature scatter
    plt.figure(figsize=(13, 8))
    ax1 = plt.subplot(2, 2, 1)
    ax2 = plt.subplot(2, 2, 2)
    ax3 = plt.subplot(2, 1, 2)

    counts = [mode_count[m] for m in sorted_modes]
    ax1.bar(sorted_modes, counts, color="#1f77b4")
    ax1.set_title("Exploding Path Modes (count)")
    ax1.set_ylabel("count")
    ax1.tick_params(axis="x", rotation=25)
    ax1.grid(axis="y", alpha=0.25)
    for i, v in enumerate(counts):
        ax1.text(i, v, str(v), ha="center", va="bottom", fontsize=8)

    shares = [mode_tokens[m] / max(total_tok, 1) for m in sorted_modes]
    ax2.bar(sorted_modes, shares, color="#ff7f0e")
    ax2.set_title("Exploding Path Modes (token share)")
    ax2.set_ylabel("share")
    ax2.tick_params(axis="x", rotation=25)
    ax2.grid(axis="y", alpha=0.25)
    for i, v in enumerate(shares):
        ax2.text(i, v, f"{v:.2f}", ha="center", va="bottom", fontsize=8)

    color_map = {
        "diagonal_stripes_clean": "#2ca02c",
        "diagonal_stripes_noisy": "#17becf",
        "hatching_multi_dir": "#9467bd",
        "dots_or_speckles": "#bcbd22",
        "dense_fragments": "#d62728",
        "other_compiled": "#7f7f7f",
    }
    for m in sorted_modes:
        xs = [r["elong_ratio"] for r in rows if r["mode"] == m]
        ys = [r["angle_std"] for r in rows if r["mode"] == m]
        ax3.scatter(xs, ys, s=15, alpha=0.65, label=m, c=color_map.get(m, "#444444"))
    ax3.set_xlabel("elongated ratio")
    ax3.set_ylabel("angle std (deg)")
    ax3.set_title("Feature Distribution of Heavy Paths")
    ax3.grid(alpha=0.25)
    ax3.legend(loc="upper right", fontsize=8, ncol=2)

    plt.tight_layout()
    plt.savefig(fig_path, dpi=180)
    plt.close()

    print(f"saved csv: {csv_path}")
    print(f"saved txt: {txt_path}")
    print(f"saved fig: {fig_path}")


if __name__ == "__main__":
    main()
