from dataclasses import dataclass
from pathlib import Path
from collections import Counter

import matplotlib.pyplot as plt
import numpy as np
from PIL import Image

import physui_stripe_fit_compress_v2 as m


INPUT_SVG = Path('/Users/xiaoxiaobo/user_path.svg')
ORIG_PNG = Path('/Users/xiaoxiaobo/user_path_render.png')
OUT_DIR = Path('/Users/xiaoxiaobo')


@dataclass
class Profile:
    key: str
    title: str
    seg_min: int
    seg_max: int
    lam_curv: float
    lam_kink: float
    lam_seg: float
    steps: int
    lr: float
    optimize_anchors: bool
    use_corner_hint: bool


def fit_stripe_adaptive_cfg(boundary_norm: np.ndarray, profile: Profile):
    corner_indices = m.detect_corner_indices(boundary_norm, pick=2, neigh=4) if profile.use_corner_hint else []

    best = None
    records = []
    for seg_n in range(profile.seg_min, profile.seg_max + 1):
        init_segs, corner_mask, starts = m.fit_chain_from_boundary(
            boundary_norm,
            seg_n=seg_n,
            alpha=0.96,
            corner_indices=corner_indices,
        )

        ls_mse, ls_curv, ls_kink = m.eval_chain_metrics(init_segs, boundary_norm, starts)
        ls_score = ls_mse + profile.lam_curv * ls_curv + profile.lam_kink * ls_kink + profile.lam_seg * seg_n

        segs_bp, mse_bp, curv_bp, kink_bp = m.nonlinear_refine_chain(
            init_segs,
            boundary_norm,
            start_indices=starts,
            corner_mask=corner_mask,
            steps=profile.steps,
            lr=profile.lr,
            optimize_anchors=profile.optimize_anchors,
        )
        bp_score = mse_bp + profile.lam_curv * curv_bp + profile.lam_kink * kink_bp + profile.lam_seg * seg_n

        if bp_score <= ls_score:
            segs, mse, curv, kink, score = segs_bp, mse_bp, curv_bp, kink_bp, bp_score
        else:
            segs, mse, curv, kink, score = init_segs, ls_mse, ls_curv, ls_kink, ls_score

        rec = m.StripeFitAdaptive(seg_n=seg_n, segments=segs, mse=mse, curv=curv, kink=kink, score=score)
        records.append(rec)
        if best is None or rec.score < best.score:
            best = rec

    return best, records


def run_profile(profile: Profile, viewbox, canvas_w, canvas_h, orig_mask, w, h):
    comps = m.split_components(orig_mask, area_min=120)
    stripe_fits = []

    for _, comp, area, cx, cy in comps:
        boundary_px = m.extract_ordered_boundary(comp)
        boundary_norm = m.normalize_points(boundary_px, px_w=w, px_h=h)
        best, _ = fit_stripe_adaptive_cfg(boundary_norm, profile)
        stripe_fits.append((best, area, cx, cy))

    subpaths = [m.stripe_to_path_d(best, viewbox, w, h) for best, _, _, _ in stripe_fits]
    new_d = ''.join(subpaths)

    out_svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{canvas_w}" height="{canvas_h}" '
        f'viewBox="{viewbox[0]} {viewbox[1]} {viewbox[2]} {viewbox[3]}">\n'
        '  <rect x="0" y="0" width="100%" height="100%" fill="#1e1e1e"/>\n'
        f'  <path d="{new_d}" fill="#FFFFFF"/>\n'
        '</svg>\n'
    )

    svg_path = OUT_DIR / f'physui_profile_{profile.key}.svg'
    png_path = OUT_DIR / f'physui_profile_{profile.key}.png'
    svg_path.write_text(out_svg)
    m.os_run(f'rsvg-convert {svg_path} -o {png_path}')

    fit_img = np.array(Image.open(png_path).convert('RGB'))
    fit_mask = fit_img.mean(axis=2) > 180

    seg_hist = Counter([best.seg_n for best, _, _, _ in stripe_fits])
    total_segments = int(sum(best.seg_n for best, _, _, _ in stripe_fits))

    metrics = {
        'profile': profile.key,
        'title': profile.title,
        'svg_path': str(svg_path),
        'png_path': str(png_path),
        'iou': m.iou(orig_mask, fit_mask),
        'path_chars': len(new_d),
        'path_tokens': m.approx_tokens_path(new_d),
        'svg_chars': len(out_svg),
        'segments_total': total_segments,
        'segment_hist': dict(seg_hist),
    }
    return metrics, fit_img, fit_mask


def main():
    svg_text = INPUT_SVG.read_text()
    viewbox, canvas_w, canvas_h = m.parse_svg_meta(svg_text)
    orig_d = m.extract_path_d(svg_text)

    m.os_run(f'rsvg-convert {INPUT_SVG} -o {ORIG_PNG}')
    orig_img = np.array(Image.open(ORIG_PNG).convert('RGB'))
    h, w = orig_img.shape[:2]
    orig_mask = orig_img.mean(axis=2) > 180

    orig_metrics = {
        'path_chars': len(orig_d),
        'path_tokens': m.approx_tokens_path(orig_d),
        'svg_chars': len(svg_text),
    }

    hi_fi = Profile(
        key='hifi',
        title='High Fidelity',
        seg_min=2,
        seg_max=4,
        lam_curv=4e-4,
        lam_kink=6e-4,
        lam_seg=2e-4,
        steps=120,
        lr=0.008,
        optimize_anchors=False,
        use_corner_hint=False,
    )

    hi_comp = Profile(
        key='compress',
        title='High Compression',
        seg_min=2,
        seg_max=3,
        lam_curv=6e-4,
        lam_kink=8e-4,
        lam_seg=1.2e-3,
        steps=90,
        lr=0.006,
        optimize_anchors=False,
        use_corner_hint=False,
    )

    m1, img1, mask1 = run_profile(hi_fi, viewbox, canvas_w, canvas_h, orig_mask, w, h)
    m2, img2, mask2 = run_profile(hi_comp, viewbox, canvas_w, canvas_h, orig_mask, w, h)

    # text report
    report = []
    report.append('=== PhysUI Dual-Profile Ablation ===')
    report.append(f"Original: path_chars={orig_metrics['path_chars']}, path_tokens={orig_metrics['path_tokens']}, svg_chars={orig_metrics['svg_chars']}")
    for mm in [m1, m2]:
        report.append(
            f"{mm['title']} | IoU={mm['iou']:.6f}, seg_total={mm['segments_total']}, seg_hist={mm['segment_hist']}, "
            f"path_chars={mm['path_chars']}, path_tokens={mm['path_tokens']}, svg_chars={mm['svg_chars']}"
        )
        report.append(
            f"{mm['title']} ratios | chars={mm['path_chars']/orig_metrics['path_chars']:.3f}x, "
            f"tokens={mm['path_tokens']/orig_metrics['path_tokens']:.3f}x"
        )

    report_path = OUT_DIR / 'physui_profile_metrics.txt'
    report_path.write_text('\n'.join(report) + '\n')

    # figure
    fig = plt.figure(figsize=(18, 11))
    gs = fig.add_gridspec(2, 3)

    ax0 = fig.add_subplot(gs[0, 0])
    ax0.imshow(orig_img)
    ax0.set_title('Original Render')
    ax0.axis('off')

    ax1 = fig.add_subplot(gs[0, 1])
    ax1.imshow(img1)
    ax1.set_title(f"{m1['title']}\nIoU={m1['iou']:.4f}, tokens={m1['path_tokens']}")
    ax1.axis('off')

    ax2 = fig.add_subplot(gs[0, 2])
    ax2.imshow(img2)
    ax2.set_title(f"{m2['title']}\nIoU={m2['iou']:.4f}, tokens={m2['path_tokens']}")
    ax2.axis('off')

    ax3 = fig.add_subplot(gs[1, 0])
    diff1 = np.abs(orig_mask.astype(np.float32) - mask1.astype(np.float32))
    ax3.imshow(diff1, cmap='magma')
    ax3.set_title(f"Diff Map: {m1['title']}")
    ax3.axis('off')

    ax4 = fig.add_subplot(gs[1, 1])
    diff2 = np.abs(orig_mask.astype(np.float32) - mask2.astype(np.float32))
    ax4.imshow(diff2, cmap='magma')
    ax4.set_title(f"Diff Map: {m2['title']}")
    ax4.axis('off')

    ax5 = fig.add_subplot(gs[1, 2])
    labels = ['Path chars', 'Path tokens', 'Total segments']
    x = np.arange(len(labels))
    bw = 0.26
    orig_vals = [orig_metrics['path_chars'], orig_metrics['path_tokens'], 0]
    hifi_vals = [m1['path_chars'], m1['path_tokens'], m1['segments_total']]
    comp_vals = [m2['path_chars'], m2['path_tokens'], m2['segments_total']]
    ax5.bar(x - bw, orig_vals, width=bw, label='Original')
    ax5.bar(x, hifi_vals, width=bw, label='High Fidelity')
    ax5.bar(x + bw, comp_vals, width=bw, label='High Compression')
    ax5.set_xticks(x)
    ax5.set_xticklabels(labels)
    ax5.set_title('Metric Comparison')
    ax5.grid(alpha=0.2, axis='y')
    ax5.legend()

    fig.suptitle('PhysUI Profile Ablation: Fidelity vs Compression', fontsize=18)
    fig.tight_layout(rect=[0, 0, 1, 0.96])

    fig_path = OUT_DIR / 'physui_profile_compare.png'
    fig.savefig(fig_path, dpi=180)

    print(f'Saved: {fig_path}')
    print(f'Saved: {report_path}')


if __name__ == '__main__':
    main()
