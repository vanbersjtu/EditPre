import argparse
import importlib.util
import math
from typing import List

import matplotlib.patches as patches
import matplotlib.pyplot as plt
import torch
import torch.nn.functional as F


def load_v2_engine():
    spec = importlib.util.spec_from_file_location("physui_v2", "/Users/xiaoxiaobo/physui_v2_mask_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_synthetic_reference_image(height: int = 320, width: int = 512):
    """
    Build a grayscale reference image and a bbox containing a noisy wave edge.
    """
    y = torch.linspace(0.0, 1.0, height).unsqueeze(1).repeat(1, width)
    x = torch.linspace(0.0, 1.0, width).unsqueeze(0).repeat(height, 1)
    bg = 0.92 - 0.10 * y + 0.03 * torch.sin(6.0 * math.pi * x)

    # BBox region
    x0, y0, x1, y1 = 64, 70, 448, 260
    bw = x1 - x0
    bh = y1 - y0

    xs = torch.linspace(0.0, 1.0, bw)
    y_edge = 0.62 - 0.28 * torch.sin(math.pi * xs) - 0.08 * torch.sin(8.0 * math.pi * xs + 0.4)
    y_edge += 0.008 * torch.randn_like(y_edge)
    y_edge = y_edge.clamp(0.12, 0.88)
    ys_pix = (y0 + (bh - 1) * y_edge).round().long()

    img = bg.clone()
    # Draw bright wave stroke in bbox
    for i in range(bw):
        cx = x0 + i
        cy = ys_pix[i].item()
        for d in range(-2, 3):
            yy = cy + d
            if 0 <= yy < height:
                img[yy, cx] = 0.08 + 0.02 * abs(d)
    # Add mild noise
    img = (img + 0.01 * torch.randn_like(img)).clamp(0.0, 1.0)
    return img, (x0, y0, x1, y1)


def parse_bbox(bbox_text: str):
    vals = [int(v.strip()) for v in bbox_text.split(",")]
    if len(vals) != 4:
        raise ValueError("bbox 必须是 x0,y0,x1,y1")
    return tuple(vals)


def crop_and_binarize(gray_img: torch.Tensor, bbox, threshold: float = None):
    x0, y0, x1, y1 = bbox
    crop = gray_img[y0:y1, x0:x1]
    if threshold is None:
        # Robust threshold for dark stroke on bright background.
        threshold = torch.quantile(crop, 0.25).item()
    binary = (crop < threshold).float()
    # Convert to y-up coordinate for renderer/plot with origin='lower'.
    binary = torch.flip(binary, dims=[0])
    return crop, binary, threshold


def extract_ordered_edge_points(binary_mask: torch.Tensor, smooth_window: int = 7) -> torch.Tensor:
    """
    Extract one ordered centerline point per column from a binary edge/stroke mask.
    Coordinates are in [0,1]^2, y-up.
    """
    h, w = binary_mask.shape
    xs = []
    ys = []
    for col in range(w):
        rows = torch.where(binary_mask[:, col] > 0.5)[0]
        if rows.numel() == 0:
            continue
        ys.append(rows.float().mean() / max(1, h - 1))
        xs.append(torch.tensor(col / max(1, w - 1)))
    if len(xs) < 4:
        raise RuntimeError("提取到的边缘点过少，无法拟合。")
    points = torch.stack([torch.stack(xs), torch.stack(ys)], dim=1)

    # 1D smoothing along order to suppress tiny pixel jaggies.
    if smooth_window > 1 and points.shape[0] > smooth_window:
        if smooth_window % 2 == 0:
            smooth_window += 1
        pad = smooth_window // 2
        yv = points[:, 1]
        ypad = torch.nn.functional.pad(yv.unsqueeze(0).unsqueeze(0), (pad, pad), mode="replicate")
        kernel = torch.ones(1, 1, smooth_window) / float(smooth_window)
        ys_smooth = torch.nn.functional.conv1d(ypad, kernel).squeeze(0).squeeze(0)
        points = torch.stack([points[:, 0], ys_smooth], dim=1)
    return points


def extract_edge_points_from_gray(crop_gray: torch.Tensor, smooth_window: int = 11) -> torch.Tensor:
    """
    Robust 1D tracing for dark stroke on bright background:
    choose darkest row per column, then smooth along x.
    Return y-up coordinates in [0,1]^2.
    """
    h, w = crop_gray.shape
    xs = torch.linspace(0.0, 1.0, w)
    y_idx_top = torch.argmin(crop_gray, dim=0).float()  # 0 at top
    y_up = 1.0 - y_idx_top / max(1, h - 1)
    points = torch.stack([xs, y_up], dim=1)

    if smooth_window > 1 and points.shape[0] > smooth_window:
        if smooth_window % 2 == 0:
            smooth_window += 1
        pad = smooth_window // 2
        yv = points[:, 1]
        ypad = F.pad(yv.unsqueeze(0).unsqueeze(0), (pad, pad), mode="replicate")
        kernel = torch.ones(1, 1, smooth_window) / float(smooth_window)
        ys_smooth = F.conv1d(ypad, kernel).squeeze(0).squeeze(0)
        points = torch.stack([points[:, 0], ys_smooth], dim=1)
    return points


def downsample_binary_mask(binary_mask: torch.Tensor, max_side: int = 140) -> torch.Tensor:
    h, w = binary_mask.shape
    cur_max = max(h, w)
    if cur_max <= max_side:
        return binary_mask
    scale = max_side / float(cur_max)
    new_h = max(24, int(round(h * scale)))
    new_w = max(24, int(round(w * scale)))
    x = binary_mask.unsqueeze(0).unsqueeze(0)  # [1,1,H,W]
    y = F.interpolate(x, size=(new_h, new_w), mode="bilinear", align_corners=False)
    return (y.squeeze(0).squeeze(0) > 0.5).float()


def downsample_gray(gray: torch.Tensor, max_side: int = 140) -> torch.Tensor:
    h, w = gray.shape
    cur_max = max(h, w)
    if cur_max <= max_side:
        return gray
    scale = max_side / float(cur_max)
    new_h = max(24, int(round(h * scale)))
    new_w = max(24, int(round(w * scale)))
    x = gray.unsqueeze(0).unsqueeze(0)
    y = F.interpolate(x, size=(new_h, new_w), mode="bilinear", align_corners=False)
    return y.squeeze(0).squeeze(0)


def draw_overlay(ax, title: str, mask: torch.Tensor, chain, edge_points: torch.Tensor):
    curve = chain.sample(samples_per_segment=160).detach()
    anchors = chain.anchors.detach()
    ax.imshow(mask.numpy(), cmap="gray", origin="lower", extent=[0, 1, 0, 1], alpha=0.95)
    ax.plot(edge_points[:, 0].numpy(), edge_points[:, 1].numpy(), color="orange", lw=1.3, alpha=0.8, label="Extracted edge")
    ax.plot(curve[:, 0].numpy(), curve[:, 1].numpy(), color="tab:blue", lw=2.5, label="Bezier chain")
    ax.scatter(anchors[:, 0].numpy(), anchors[:, 1].numpy(), c="tab:red", s=40, edgecolors="white", linewidths=0.7, zorder=6, label="Anchors")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_title(title, fontsize=10)
    ax.set_xlabel("x")
    ax.set_ylabel("y")
    ax.grid(alpha=0.15)


def _point_line_distance(p: torch.Tensor, a: torch.Tensor, b: torch.Tensor) -> float:
    ab = b - a
    denom = float(torch.dot(ab, ab).item())
    if denom < 1e-12:
        return float(torch.norm(p - a).item())
    t = float(torch.dot(p - a, ab).item()) / denom
    t = max(0.0, min(1.0, t))
    proj = a + t * ab
    return float(torch.norm(p - proj).item())


def rdp(points: torch.Tensor, epsilon: float) -> torch.Tensor:
    """
    Ramer-Douglas-Peucker simplification for ordered 2D points.
    """
    n = points.shape[0]
    if n <= 2:
        return points
    a = points[0]
    b = points[-1]
    max_d = -1.0
    idx = -1
    for i in range(1, n - 1):
        d = _point_line_distance(points[i], a, b)
        if d > max_d:
            max_d = d
            idx = i
    if max_d <= epsilon:
        return torch.stack([a, b], dim=0)
    left = rdp(points[: idx + 1], epsilon)
    right = rdp(points[idx:], epsilon)
    return torch.cat([left[:-1], right], dim=0)


def build_bezier_chain_from_polyline(v2, poly: torch.Tensor):
    anchors = poly.clone()
    nseg = anchors.shape[0] - 1
    h1 = []
    h2 = []
    for i in range(nseg):
        a = anchors[i]
        b = anchors[i + 1]
        h1.append(a + (b - a) / 3.0)
        h2.append(a + 2.0 * (b - a) / 3.0)
    h1 = torch.stack(h1, dim=0)
    h2 = torch.stack(h2, dim=0)
    return v2.BezierChain(anchors=anchors, h1=h1, h2=h2)


def run_diffvg_like_adaptive(v2, target_mask: torch.Tensor, edge_points: torch.Tensor, renderer, max_segments: int = 8):
    """
    DiffVG-like baseline: differentiable rendering with pure mask loss.
    Adaptive split by mask loss only (no curvature/segment penalty).
    """
    chain = v2.init_chain_one_segment(edge_points)
    best_mask = 1e9
    while True:
        chain = v2.optimize_chain(
            chain,
            target_mask=target_mask,
            renderer=renderer,
            lambda_curv=0.0,
            steps=280,
            lr=8e-3,
            log_prefix=f"[DiffVG {chain.n_segments}seg] ",
        )
        cur_mask, cur_curv, _ = v2.evaluate_chain(chain, target_mask, renderer, lambda_curv=0.0, lambda_seg=0.0)
        print(f"[DiffVG {chain.n_segments}seg] mask={cur_mask:.6f}, curv={cur_curv:.6f}")
        if chain.n_segments >= max_segments:
            break
        split_idx = int(torch.argmax(chain.per_segment_curvature(samples_per_segment=80)).item())
        cand = chain.split_segment(split_idx)
        cand = v2.optimize_chain(
            cand,
            target_mask=target_mask,
            renderer=renderer,
            lambda_curv=0.0,
            steps=180,
            lr=7e-3,
            log_prefix=f"[DiffVG cand {cand.n_segments}seg] ",
        )
        c_mask, c_curv, _ = v2.evaluate_chain(cand, target_mask, renderer, lambda_curv=0.0, lambda_seg=0.0)
        print(f"[DiffVG cand {cand.n_segments}seg] mask={c_mask:.6f}, curv={c_curv:.6f}")
        # Split if mask improves by at least tiny margin.
        if c_mask + 1e-5 < cur_mask:
            chain = cand
            best_mask = c_mask
        else:
            break
        if best_mask < 5e-5:
            break
    return chain


def main():
    parser = argparse.ArgumentParser(description="PhysUI V2.1: image+bbox preprocessing + differentiable mask fitting")
    parser.add_argument("--bbox", type=str, default="", help="x0,y0,x1,y1")
    parser.add_argument("--threshold", type=float, default=None, help="binary threshold for dark stroke")
    args = parser.parse_args()

    v2 = load_v2_engine()
    v2.set_seed(7)

    # V2.1 demo: use synthetic reference image, but pipeline is exactly bbox->mask->engine.
    gray_img, default_bbox = make_synthetic_reference_image()
    bbox = parse_bbox(args.bbox) if args.bbox else default_bbox

    crop_gray, target_binary, used_thr = crop_and_binarize(gray_img, bbox, threshold=args.threshold)
    target_binary = downsample_binary_mask(target_binary, max_side=140)
    crop_gray_ds = downsample_gray(crop_gray, max_side=140)
    # Use robust grayscale tracing for edge extraction; binary is kept for visualization.
    edge_points = extract_edge_points_from_gray(crop_gray_ds, smooth_window=11)

    h, w = target_binary.shape
    radius = 1.6 / max(h, w)
    tau = 0.9 * radius
    renderer = v2.SoftMaskRenderer(height=h, width=w, radius=radius, tau=tau)
    # Clean target mask from traced edge.
    target_mask = renderer.render(edge_points).detach()

    print(f"bbox={bbox}, threshold={used_thr:.4f}, crop_ds=({h},{w})")
    print("=== DiffVG-like Baseline (adaptive, mask-only) ===")
    diffvg_chain = run_diffvg_like_adaptive(
        v2=v2,
        target_mask=target_mask,
        edge_points=edge_points,
        renderer=renderer,
        max_segments=8,  # safety cap only
    )
    d_mask, d_curv, _ = v2.evaluate_chain(diffvg_chain, target_mask, renderer, lambda_curv=0.0, lambda_seg=0.0)

    print("\n=== Img2Vec-like Baseline (RDP polyline simplify) ===")
    poly = rdp(edge_points, epsilon=0.025)
    img2vec_chain = build_bezier_chain_from_polyline(v2, poly)
    i_mask, i_curv, _ = v2.evaluate_chain(img2vec_chain, target_mask, renderer, lambda_curv=0.0, lambda_seg=0.0)

    print("\n=== PhysUI V2.1 (mask + curvature + segment penalty) ===")
    physui, stages = v2.run_physui_v2(
        target_mask=target_mask,
        edge_points=edge_points,
        renderer=renderer,
        lambda_curv=2e-4,
        lambda_seg=0.0014,
        max_segments=8,  # safety cap only, stop by score reject
    )
    p_mask, p_curv, p_score = v2.evaluate_chain(physui, target_mask, renderer, lambda_curv=2e-4, lambda_seg=0.0014)

    print("\n=== V2.1 Summary ===")
    print(f"DiffVG-like: seg={diffvg_chain.n_segments}, mask={d_mask:.6f}, curv={d_curv:.6f}")
    print(f"Img2Vec-like: seg={img2vec_chain.n_segments}, mask={i_mask:.6f}, curv={i_curv:.6f}")
    print(f"PhysUI: seg={physui.n_segments}, mask={p_mask:.6f}, curv={p_curv:.6f}, score={p_score:.6f}")

    fig, axes = plt.subplots(2, 3, figsize=(15, 9), dpi=140)

    # Panel A: full reference + bbox
    axes[0, 0].imshow(gray_img.numpy(), cmap="gray", origin="upper")
    x0, y0, x1, y1 = bbox
    rect = patches.Rectangle((x0, y0), x1 - x0, y1 - y0, linewidth=2.0, edgecolor="tab:red", facecolor="none")
    axes[0, 0].add_patch(rect)
    axes[0, 0].set_title("Reference Image + BBox")
    axes[0, 0].axis("off")

    # Panel B: cropped binary mask + extracted edge
    axes[0, 1].imshow(target_binary.numpy(), cmap="gray", origin="lower", extent=[0, 1, 0, 1])
    axes[0, 1].plot(edge_points[:, 0].numpy(), edge_points[:, 1].numpy(), color="orange", lw=1.4)
    axes[0, 1].set_title("Cropped Binary Mask + Extracted Edge")
    axes[0, 1].set_xlabel("x")
    axes[0, 1].set_ylabel("y")
    axes[0, 1].grid(alpha=0.15)

    # Panel C: summary bars
    labels = ["DiffVG", "Img2Vec", "PhysUI"]
    segs = [diffvg_chain.n_segments, img2vec_chain.n_segments, physui.n_segments]
    masks = [d_mask, i_mask, p_mask]
    curvs = [d_curv, i_curv, p_curv]
    x_id = torch.arange(3).numpy()
    wbar = 0.25
    axes[0, 2].bar(x_id - wbar, masks, width=wbar, label="Mask loss")
    axes[0, 2].bar(x_id, curvs, width=wbar, label="Curvature")
    axes[0, 2].bar(x_id + wbar, segs, width=wbar, label="#Segments")
    axes[0, 2].set_xticks(x_id)
    axes[0, 2].set_xticklabels(labels)
    axes[0, 2].set_title("Method Comparison")
    axes[0, 2].grid(alpha=0.2, axis="y")
    axes[0, 2].legend(fontsize=8)

    # Panel D/E/F: fits
    draw_overlay(
        axes[1, 0],
        f"DiffVG-like\nseg={diffvg_chain.n_segments}, mask={d_mask:.4f}, curv={d_curv:.2f}",
        target_mask,
        diffvg_chain,
        edge_points,
    )
    draw_overlay(
        axes[1, 1],
        f"Img2Vec-like\nseg={img2vec_chain.n_segments}, mask={i_mask:.4f}, curv={i_curv:.2f}",
        target_mask,
        img2vec_chain,
        edge_points,
    )
    draw_overlay(
        axes[1, 2],
        f"PhysUI V2.1 (Adaptive)\nseg={physui.n_segments}, mask={p_mask:.4f}, curv={p_curv:.2f}",
        target_mask,
        physui,
        edge_points,
    )

    handles, labels = axes[1, 2].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=4, fontsize=8)
    fig.suptitle("PhysUI V2.1: DiffVG-like / Img2Vec-like Baselines vs PhysUI Adaptive", fontsize=12)
    fig.tight_layout(rect=[0, 0.06, 1, 0.96])
    out_path = "/Users/xiaoxiaobo/physui_v2_bbox_pipeline.png"
    fig.savefig(out_path)
    plt.close(fig)
    print(f"\nSaved figure: {out_path}")

    # Stage metrics
    fig, ax = plt.subplots(1, 1, figsize=(7, 4), dpi=140)
    ns = [s.n_segments for s in stages]
    ml = [s.mask_loss for s in stages]
    cv = [s.curvature for s in stages]
    sc = [s.score for s in stages]
    ax.plot(ns, ml, "o-", lw=2, label="Mask loss")
    ax.plot(ns, cv, "o-", lw=2, label="Curvature")
    ax.plot(ns, sc, "o-", lw=2, label="Score")
    ax.set_xticks(sorted(set(ns)))
    ax.set_title("PhysUI V2.1 Stage Metrics")
    ax.set_xlabel("num segments")
    ax.grid(alpha=0.2)
    ax.legend()
    fig.tight_layout()
    stage_path = "/Users/xiaoxiaobo/physui_v2_bbox_stage_metrics.png"
    fig.savefig(stage_path)
    plt.close(fig)
    print(f"Saved figure: {stage_path}")


if __name__ == "__main__":
    main()
