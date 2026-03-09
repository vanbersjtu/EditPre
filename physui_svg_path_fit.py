from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import torch
from PIL import Image
from scipy import ndimage as ndi


SVG_PATH = Path('/Users/xiaoxiaobo/user_path.svg')
PNG_PATH = Path('/Users/xiaoxiaobo/user_path_render.png')
RESULT_PATH = Path('/Users/xiaoxiaobo/user_path_fit_result.png')
METRIC_PATH = Path('/Users/xiaoxiaobo/user_path_fit_metrics.png')


def render_svg(svg_path: Path, png_path: Path) -> None:
    cmd = f"rsvg-convert {svg_path} -o {png_path}"
    if os_system(cmd) != 0:
        raise RuntimeError('rsvg-convert failed')


def os_system(cmd: str) -> int:
    import os

    return os.system(cmd)


def extract_ordered_boundary(binary_mask: np.ndarray) -> np.ndarray:
    eroded = ndi.binary_erosion(binary_mask, structure=np.ones((3, 3), dtype=bool))
    boundary = binary_mask & (~eroded)
    ys, xs = np.where(boundary)
    if len(xs) < 10:
        raise RuntimeError('Boundary extraction failed.')
    cx = float(xs.mean())
    cy = float(ys.mean())
    angles = np.arctan2(ys - cy, xs - cx)
    order = np.argsort(angles)
    pts = np.stack([xs[order], ys[order]], axis=1).astype(np.float32)
    return pts


def build_silhouette_mask(stripe_mask: np.ndarray) -> np.ndarray:
    # The provided path is stripe-filled. Close gaps to recover the intended outer silhouette.
    closed = ndi.binary_closing(stripe_mask, structure=np.ones((11, 11), dtype=bool), iterations=2)
    labeled, num = ndi.label(closed)
    if num == 0:
        raise RuntimeError('No connected components after closing.')
    areas = np.bincount(labeled.ravel())
    areas[0] = 0
    keep = int(areas.argmax())
    return (labeled == keep)


def resample_closed_polyline(points: np.ndarray, n: int) -> np.ndarray:
    pts = np.asarray(points, dtype=np.float32)
    if not np.allclose(pts[0], pts[-1]):
        pts = np.vstack([pts, pts[0]])
    seg = pts[1:] - pts[:-1]
    seg_len = np.linalg.norm(seg, axis=1)
    total = float(seg_len.sum())
    if total < 1e-9:
        raise RuntimeError('Contour length too small.')
    cum = np.concatenate([[0.0], np.cumsum(seg_len)])
    targets = np.linspace(0.0, total, n + 1)[:-1]
    out = []
    j = 0
    for t in targets:
        while j < len(seg_len) - 1 and cum[j + 1] < t:
            j += 1
        local = 0.0 if seg_len[j] < 1e-12 else (t - cum[j]) / seg_len[j]
        out.append(pts[j] * (1.0 - local) + pts[j + 1] * local)
    return np.asarray(out, dtype=np.float32)


def normalize_points_xy(points: np.ndarray, width: int, height: int) -> np.ndarray:
    pts = points.copy().astype(np.float32)
    pts[:, 0] = pts[:, 0] / max(width - 1, 1)
    pts[:, 1] = 1.0 - pts[:, 1] / max(height - 1, 1)
    return pts


def init_closed_chain_from_target(target: np.ndarray, num_segments: int, handle_scale: float = 0.25):
    n = len(target)
    idx = np.linspace(0, n, num_segments + 1)[:-1]
    idx = np.round(idx).astype(int) % n
    anchors = target[idx].copy()

    prev_a = np.roll(anchors, 1, axis=0)
    next_a = np.roll(anchors, -1, axis=0)
    tangent = next_a - prev_a
    h1 = anchors + handle_scale * tangent
    h2 = np.roll(anchors, -1, axis=0) - handle_scale * tangent

    return (
        torch.tensor(anchors, dtype=torch.float32, requires_grad=True),
        torch.tensor(h1, dtype=torch.float32, requires_grad=True),
        torch.tensor(h2, dtype=torch.float32, requires_grad=True),
    )


def sample_closed_chain(anchors: torch.Tensor, h1: torch.Tensor, h2: torch.Tensor, samples_per_seg: int):
    device = anchors.device
    t = torch.linspace(0.0, 1.0, samples_per_seg, device=device)
    omt = 1.0 - t

    a0 = anchors
    a1 = torch.roll(anchors, shifts=-1, dims=0)

    c = (
        (omt**3)[None, :, None] * a0[:, None, :]
        + 3.0 * (omt**2 * t)[None, :, None] * h1[:, None, :]
        + 3.0 * (omt * t**2)[None, :, None] * h2[:, None, :]
        + (t**3)[None, :, None] * a1[:, None, :]
    )
    return c.reshape(-1, 2)


def chain_curvature_loss(anchors: torch.Tensor, h1: torch.Tensor, h2: torch.Tensor, samples_per_seg: int = 30):
    device = anchors.device
    t = torch.linspace(0.0, 1.0, samples_per_seg, device=device)

    a0 = anchors
    a1 = torch.roll(anchors, shifts=-1, dims=0)

    term_a = h2 - 2.0 * h1 + a0
    term_b = a1 - 2.0 * h2 + h1

    c2 = 6.0 * (1.0 - t)[None, :, None] * term_a[:, None, :] + 6.0 * t[None, :, None] * term_b[:, None, :]
    return (c2.pow(2).sum(dim=-1)).mean()


def joint_kink_loss(anchors: torch.Tensor, h1: torch.Tensor, h2: torch.Tensor):
    # At anchor i, compare outgoing tangent of seg(i-1) and incoming tangent of seg(i)
    out_tan = 3.0 * (anchors - torch.roll(h2, shifts=1, dims=0))
    in_tan = 3.0 * (h1 - anchors)

    out_n = out_tan / (out_tan.norm(dim=1, keepdim=True) + 1e-8)
    in_n = in_tan / (in_tan.norm(dim=1, keepdim=True) + 1e-8)
    return ((out_n - in_n).pow(2).sum(dim=1)).mean()


def symmetric_chamfer(curve_pts: torch.Tensor, target_pts: torch.Tensor):
    d = torch.cdist(curve_pts, target_pts, p=2)
    a = d.min(dim=1).values.pow(2).mean()
    b = d.min(dim=0).values.pow(2).mean()
    return 0.5 * (a + b)


@dataclass
class FitResult:
    segments: int
    anchors: np.ndarray
    h1: np.ndarray
    h2: np.ndarray
    curve: np.ndarray
    mask_loss: float
    curvature: float
    joint_kink: float
    score: float


def fit_fixed_segments(
    target_np: np.ndarray,
    segments: int,
    steps: int,
    lr: float,
    lambda_curv: float,
    lambda_joint: float,
    device: str,
):
    target = torch.tensor(target_np, dtype=torch.float32, device=device)
    anchors, h1, h2 = init_closed_chain_from_target(target_np, segments)
    anchors = anchors.to(device)
    h1 = h1.to(device)
    h2 = h2.to(device)

    opt = torch.optim.Adam([anchors, h1, h2], lr=lr)
    for _ in range(steps):
        opt.zero_grad()
        curve = sample_closed_chain(anchors, h1, h2, samples_per_seg=36)
        mask_loss = symmetric_chamfer(curve, target)
        curv = chain_curvature_loss(anchors, h1, h2, samples_per_seg=24)
        kink = joint_kink_loss(anchors, h1, h2)
        loss = mask_loss + lambda_curv * curv + lambda_joint * kink
        loss.backward()
        opt.step()

    with torch.no_grad():
        curve = sample_closed_chain(anchors, h1, h2, samples_per_seg=60)
        mask_loss = symmetric_chamfer(curve, target).item()
        curv = chain_curvature_loss(anchors, h1, h2, samples_per_seg=40).item()
        kink = joint_kink_loss(anchors, h1, h2).item()

    return FitResult(
        segments=segments,
        anchors=anchors.detach().cpu().numpy(),
        h1=h1.detach().cpu().numpy(),
        h2=h2.detach().cpu().numpy(),
        curve=curve.detach().cpu().numpy(),
        mask_loss=float(mask_loss),
        curvature=float(curv),
        joint_kink=float(kink),
        score=0.0,
    )


def select_physui_adaptive(target_np: np.ndarray, seg_min: int, seg_max: int, device: str):
    records = []
    best = None

    lambda_curv = 7e-4
    lambda_joint = 2e-3
    lambda_seg = 3e-4

    for seg in range(seg_min, seg_max + 1):
        r = fit_fixed_segments(
            target_np=target_np,
            segments=seg,
            steps=900,
            lr=0.02,
            lambda_curv=lambda_curv,
            lambda_joint=lambda_joint,
            device=device,
        )
        score = r.mask_loss + lambda_curv * r.curvature + lambda_joint * r.joint_kink + lambda_seg * seg
        r.score = float(score)
        records.append(r)
        if best is None or r.score < best.score:
            best = r
        print(
            f"[PhysUI] seg={seg} mask={r.mask_loss:.6f} curv={r.curvature:.4f} "
            f"joint={r.joint_kink:.4f} score={r.score:.6f}"
        )
    return best, records


def fit_baseline_diffvg_like(target_np: np.ndarray, device: str):
    # Baseline: no physics, higher segment count to chase fidelity.
    seg = 12
    r = fit_fixed_segments(
        target_np=target_np,
        segments=seg,
        steps=1200,
        lr=0.02,
        lambda_curv=0.0,
        lambda_joint=0.0,
        device=device,
    )
    r.score = r.mask_loss
    print(f"[Baseline] seg={r.segments} mask={r.mask_loss:.6f} curv={r.curvature:.4f} joint={r.joint_kink:.4f}")
    return r


def draw_chain(ax, fit: FitResult, title: str):
    c = fit.curve
    a = fit.anchors
    ax.plot(c[:, 0], c[:, 1], color='#2c7fb8', lw=2.2, label='Bezier chain')
    ax.scatter(a[:, 0], a[:, 1], s=36, c='#e63946', zorder=5, edgecolors='white', linewidths=0.8, label='Anchors')
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_aspect('equal')
    ax.grid(alpha=0.2)
    ax.set_xlabel('x')
    ax.set_ylabel('y')
    ax.set_title(title)


def main():
    import os

    if not SVG_PATH.exists():
        raise FileNotFoundError(f'Missing SVG: {SVG_PATH}')

    os.system(f"rsvg-convert {SVG_PATH} -o {PNG_PATH}")

    img = np.array(Image.open(PNG_PATH).convert('RGB'))
    gray = img.mean(axis=2)

    # foreground from white stripe fill
    stripe_mask = gray > 180
    silhouette_mask = build_silhouette_mask(stripe_mask)
    h, w = silhouette_mask.shape

    contour_px = extract_ordered_boundary(silhouette_mask)
    contour_px = resample_closed_polyline(contour_px, n=1200)
    target = normalize_points_xy(contour_px, width=w, height=h)

    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    print('device:', device)

    baseline = fit_baseline_diffvg_like(target, device=device)
    physui_best, physui_records = select_physui_adaptive(target, seg_min=3, seg_max=10, device=device)

    fig = plt.figure(figsize=(18, 10))
    gs = fig.add_gridspec(2, 3)

    ax0 = fig.add_subplot(gs[0, 0])
    ax0.imshow(img)
    ax0.set_title('Rendered SVG')
    ax0.axis('off')

    ax1 = fig.add_subplot(gs[0, 1])
    ax1.imshow(silhouette_mask, cmap='gray')
    ax1.plot(contour_px[:, 0], contour_px[:, 1], color='#ffb703', lw=1.0)
    ax1.set_title('Recovered Silhouette + Extracted Contour')
    ax1.set_xlabel('px')
    ax1.set_ylabel('px')

    ax2 = fig.add_subplot(gs[0, 2])
    rec_segs = [r.segments for r in physui_records]
    rec_mask = [r.mask_loss for r in physui_records]
    rec_curv = [r.curvature for r in physui_records]
    rec_score = [r.score for r in physui_records]
    ax2.plot(rec_segs, rec_mask, marker='o', label='Mask loss')
    ax2.plot(rec_segs, rec_curv, marker='o', label='Curvature')
    ax2.plot(rec_segs, rec_score, marker='o', label='PhysUI score')
    ax2.set_title('PhysUI Adaptive Model Selection')
    ax2.set_xlabel('num segments')
    ax2.grid(alpha=0.2)
    ax2.legend()

    ax3 = fig.add_subplot(gs[1, 0])
    ax3.scatter(target[:, 0], target[:, 1], s=1.0, c='lightgray', alpha=0.7, label='Target contour')
    draw_chain(
        ax3,
        baseline,
        title=f'Baseline (DiffVG-like)\nseg={baseline.segments}, mask={baseline.mask_loss:.4g}, curv={baseline.curvature:.3f}',
    )

    ax4 = fig.add_subplot(gs[1, 1])
    ax4.scatter(target[:, 0], target[:, 1], s=1.0, c='lightgray', alpha=0.7, label='Target contour')
    draw_chain(
        ax4,
        physui_best,
        title=(
            f'PhysUI Adaptive\nseg={physui_best.segments}, '
            f'mask={physui_best.mask_loss:.4g}, curv={physui_best.curvature:.3f}'
        ),
    )

    ax5 = fig.add_subplot(gs[1, 2])
    labels = ['Baseline', 'PhysUI']
    x = np.arange(2)
    width = 0.25
    vals_mask = [baseline.mask_loss, physui_best.mask_loss]
    vals_curv = [baseline.curvature, physui_best.curvature]
    vals_seg = [baseline.segments, physui_best.segments]
    ax5.bar(x - width, vals_mask, width=width, label='Mask loss')
    ax5.bar(x, vals_curv, width=width, label='Curvature')
    ax5.bar(x + width, vals_seg, width=width, label='#Segments')
    ax5.set_xticks(x)
    ax5.set_xticklabels(labels)
    ax5.set_title('Comparison')
    ax5.legend()
    ax5.grid(alpha=0.2, axis='y')

    fig.suptitle('PhysUI Test on User SVG Path: Render -> Contour -> Bezier Chain Fitting', fontsize=18, y=0.98)
    fig.tight_layout(rect=[0, 0.02, 1, 0.96])
    fig.savefig(RESULT_PATH, dpi=180)

    plt.figure(figsize=(8, 5))
    plt.plot(rec_segs, rec_mask, marker='o', label='Mask loss')
    plt.plot(rec_segs, rec_curv, marker='o', label='Curvature')
    plt.plot(rec_segs, rec_score, marker='o', label='PhysUI score')
    plt.xlabel('num segments')
    plt.title('PhysUI Stage Metrics on User SVG')
    plt.grid(alpha=0.2)
    plt.legend()
    plt.tight_layout()
    plt.savefig(METRIC_PATH, dpi=180)

    print(f'Saved main figure: {RESULT_PATH}')
    print(f'Saved metric figure: {METRIC_PATH}')


if __name__ == '__main__':
    main()
