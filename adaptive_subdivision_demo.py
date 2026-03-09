import math
from dataclasses import dataclass

import matplotlib.pyplot as plt
import torch
import torch.nn.functional as F


def set_seed(seed: int = 7) -> None:
    torch.manual_seed(seed)


def generate_w_target(num_points: int = 180, noise_std: float = 0.02) -> torch.Tensor:
    """
    W-like noisy target edge.
    """
    x = torch.linspace(0.0, 1.0, num_points)
    y_clean = 0.55 * torch.cos(2.0 * math.pi * x) + 0.18 * torch.cos(6.0 * math.pi * x)
    y_noisy = y_clean + noise_std * torch.randn_like(y_clean)
    return torch.stack([x, y_noisy], dim=1)


def normalize_points(points: torch.Tensor) -> tuple[torch.Tensor, dict]:
    min_xy = points.min(dim=0).values
    max_xy = points.max(dim=0).values
    center = 0.5 * (min_xy + max_xy)
    scale = (max_xy - min_xy).clamp_min(1e-6)
    points_norm = (points - center) / scale
    return points_norm, {"center": center, "scale": scale}


def denormalize_points(points: torch.Tensor, stats: dict) -> torch.Tensor:
    return points * stats["scale"] + stats["center"]


def cubic_bezier(controls: torch.Tensor, t: torch.Tensor) -> torch.Tensor:
    """
    controls: [4,2], t: [N], return [N,2]
    """
    t = t.unsqueeze(1)
    omt = 1.0 - t
    p0, p1, p2, p3 = controls
    return (
        (omt**3) * p0
        + 3.0 * (omt**2) * t * p1
        + 3.0 * omt * (t**2) * p2
        + (t**3) * p3
    )


def cubic_second_derivative(controls: torch.Tensor, t: torch.Tensor) -> torch.Tensor:
    t = t.unsqueeze(1)
    omt = 1.0 - t
    p0, p1, p2, p3 = controls
    a = p2 - 2.0 * p1 + p0
    b = p3 - 2.0 * p2 + p1
    return 6.0 * omt * a + 6.0 * t * b


def cubic_first_derivative(controls: torch.Tensor, t: torch.Tensor) -> torch.Tensor:
    t = t.unsqueeze(1)
    omt = 1.0 - t
    p0, p1, p2, p3 = controls
    return (
        3.0 * (omt**2) * (p1 - p0)
        + 6.0 * omt * t * (p2 - p1)
        + 3.0 * (t**2) * (p3 - p2)
    )


def resample_sequence(points: torch.Tensor, n_out: int) -> torch.Tensor:
    """
    Differentiably resample [N,2] -> [n_out,2] along sequence order.
    """
    x = points.t().unsqueeze(0)  # [1,2,N]
    y = F.interpolate(x, size=n_out, mode="linear", align_corners=True)
    return y.squeeze(0).t()


def smooth_polyline(points: torch.Tensor, kernel_size: int = 9) -> torch.Tensor:
    """
    Light 1D smoothing on polyline coordinates to make corner detection robust to noise.
    """
    if kernel_size <= 1 or points.shape[0] < kernel_size:
        return points
    if kernel_size % 2 == 0:
        kernel_size += 1

    x = points.t().unsqueeze(0)  # [1,2,N]
    pad = kernel_size // 2
    x_pad = F.pad(x, (pad, pad), mode="replicate")
    w = torch.ones(2, 1, kernel_size, dtype=points.dtype, device=points.device) / float(kernel_size)
    y = F.conv1d(x_pad, w, groups=2)
    return y.squeeze(0).t()


def ordered_point_mse(curve_pts: torch.Tensor, target_pts: torch.Tensor) -> torch.Tensor:
    curve_rs = resample_sequence(curve_pts, target_pts.shape[0])
    return ((curve_rs - target_pts) ** 2).sum(dim=1).mean()


@dataclass
class StageMetrics:
    n_segments: int
    mse: float
    curvature: float
    joint: float
    score: float
    accepted_split: bool


class BezierChain:
    """
    Segment i controls = [A_i, H1_i, H2_i, A_{i+1}]
    C0 continuity is guaranteed by shared anchors A_i.
    """

    def __init__(self, anchors: torch.Tensor, h1: torch.Tensor, h2: torch.Tensor):
        self.anchors = anchors
        self.h1 = h1
        self.h2 = h2

    @property
    def n_segments(self) -> int:
        return self.h1.shape[0]

    def clone_detached(self) -> "BezierChain":
        return BezierChain(
            self.anchors.detach().clone(),
            self.h1.detach().clone(),
            self.h2.detach().clone(),
        )

    def requires_grad_(self, mode: bool = True) -> "BezierChain":
        self.anchors.requires_grad_(mode)
        self.h1.requires_grad_(mode)
        self.h2.requires_grad_(mode)
        return self

    def parameters(self) -> list[torch.Tensor]:
        return [self.anchors, self.h1, self.h2]

    def segment_controls(self, i: int) -> torch.Tensor:
        return torch.stack(
            [self.anchors[i], self.h1[i], self.h2[i], self.anchors[i + 1]], dim=0
        )

    def sample(self, samples_per_segment: int = 80) -> torch.Tensor:
        t = torch.linspace(0.0, 1.0, samples_per_segment)
        seg_pts = [cubic_bezier(self.segment_controls(i), t) for i in range(self.n_segments)]
        return torch.cat(seg_pts, dim=0)

    def curvature_energy(self, samples_per_segment: int = 80) -> torch.Tensor:
        t = torch.linspace(0.0, 1.0, samples_per_segment)
        terms = []
        for i in range(self.n_segments):
            c2 = cubic_second_derivative(self.segment_controls(i), t)
            terms.append((c2.pow(2).sum(dim=1)).mean())
        return torch.stack(terms).mean()

    def per_segment_curvature(self, samples_per_segment: int = 80) -> torch.Tensor:
        t = torch.linspace(0.0, 1.0, samples_per_segment)
        vals = []
        for i in range(self.n_segments):
            c2 = cubic_second_derivative(self.segment_controls(i), t)
            vals.append((c2.pow(2).sum(dim=1)).mean())
        return torch.stack(vals)

    def split_segment(self, idx: int) -> "BezierChain":
        """
        De Casteljau split at t=0.5 for segment idx.
        """
        p0 = self.anchors[idx]
        p1 = self.h1[idx]
        p2 = self.h2[idx]
        p3 = self.anchors[idx + 1]

        q0 = 0.5 * (p0 + p1)
        q1 = 0.5 * (p1 + p2)
        q2 = 0.5 * (p2 + p3)
        r0 = 0.5 * (q0 + q1)
        r1 = 0.5 * (q1 + q2)
        s = 0.5 * (r0 + r1)

        anchors_left = self.anchors[: idx + 1]
        anchors_right = self.anchors[idx + 1 :]
        new_anchors = torch.cat([anchors_left, s.unsqueeze(0), anchors_right], dim=0)

        h1_left = self.h1[:idx]
        h1_right = self.h1[idx + 1 :]
        new_h1 = torch.cat([h1_left, q0.unsqueeze(0), r1.unsqueeze(0), h1_right], dim=0)

        h2_left = self.h2[:idx]
        h2_right = self.h2[idx + 1 :]
        new_h2 = torch.cat([h2_left, r0.unsqueeze(0), q2.unsqueeze(0), h2_right], dim=0)

        return BezierChain(new_anchors, new_h1, new_h2)

    def controls_polyline(self) -> torch.Tensor:
        """
        Return a polyline of all controls for visualization.
        """
        pts = []
        for i in range(self.n_segments):
            seg = self.segment_controls(i)
            if i == 0:
                pts.append(seg)
            else:
                pts.append(seg[1:, :])
        return torch.cat(pts, dim=0)

    def joint_smoothness_loss(self, smooth_weights: torch.Tensor) -> torch.Tensor:
        """
        Penalize artificial kinks only at smooth joints.
        smooth_weights: [n_segments-1], 1=strong smoothness, 0=allow corner.
        """
        if self.n_segments <= 1:
            return torch.tensor(0.0, dtype=self.anchors.dtype, device=self.anchors.device)

        eps = 1e-6
        losses = []
        for i in range(1, self.n_segments):
            a = self.anchors[i]
            left_h2 = self.h2[i - 1]
            left_h1 = self.h1[i - 1]
            right_h1 = self.h1[i]
            right_h2 = self.h2[i]

            # Tangent continuity (G1-like), bounded in [0,2].
            t_left = 3.0 * (a - left_h2)
            t_right = 3.0 * (right_h1 - a)
            t_left_u = t_left / (t_left.norm() + eps)
            t_right_u = t_right / (t_right.norm() + eps)
            cosv = torch.clamp((t_left_u * t_right_u).sum(), -1.0, 1.0)
            l_tan = 1.0 - cosv
            losses.append(l_tan)

        joint_losses = torch.stack(losses)
        w = smooth_weights.to(joint_losses.device)
        return (w * joint_losses).mean()


def target_turning_angles(target_points: torch.Tensor, half_window: int = 3) -> torch.Tensor:
    n = target_points.shape[0]
    angles = torch.zeros(n, dtype=target_points.dtype)
    if n < 2 * half_window + 1:
        return angles

    eps = 1e-6
    for i in range(half_window, n - half_window):
        v1 = target_points[i] - target_points[i - half_window]
        v2 = target_points[i + half_window] - target_points[i]
        v1n = v1 / (v1.norm() + eps)
        v2n = v2 / (v2.norm() + eps)
        cos_val = torch.clamp((v1n * v2n).sum(), -1.0 + 1e-6, 1.0 - 1e-6)
        angles[i] = torch.acos(cos_val)
    return angles


def joint_smooth_weights_from_target(
    target_points: torch.Tensor,
    n_segments: int,
    corner_angle_threshold: float = 0.95,
    sharpness: float = 10.0,
) -> torch.Tensor:
    """
    Build per-joint smoothness weights from target local turning angle.
    Large angle -> likely real corner -> small smoothness weight.
    Small angle -> likely smooth region -> large smoothness weight.
    """
    if n_segments <= 1:
        return torch.empty(0, dtype=target_points.dtype)

    # Combine raw and lightly smoothed angles: keep true corners while resisting noise.
    angles_raw = target_turning_angles(target_points, half_window=3)
    angles_smooth = target_turning_angles(smooth_polyline(target_points, kernel_size=7), half_window=4)
    angles = torch.maximum(angles_raw, angles_smooth)
    n = target_points.shape[0]
    idx = torch.linspace(0, n - 1, n_segments + 1).round().long()[1:-1]
    joint_angles = angles[idx]
    # sigma(corner_thr - angle): smooth area -> near 1.
    weights = torch.sigmoid(sharpness * (corner_angle_threshold - joint_angles))
    # Hard gate for true corners: do not penalize them.
    weights = torch.where(
        joint_angles > corner_angle_threshold,
        torch.zeros_like(weights),
        weights,
    )
    return weights


def init_chain_one_segment(target_points: torch.Tensor) -> BezierChain:
    x_min, x_max = target_points[:, 0].min(), target_points[:, 0].max()
    y_left, y_right = target_points[0, 1], target_points[-1, 1]
    a0 = torch.tensor([x_min, y_left])
    a1 = torch.tensor([x_max, y_right])
    width = x_max - x_min
    h1 = a0 + torch.tensor([0.33 * width, 0.0])
    h2 = a0 + torch.tensor([0.66 * width, 0.0])
    anchors = torch.stack([a0, a1], dim=0)
    return BezierChain(anchors=anchors, h1=h1.unsqueeze(0), h2=h2.unsqueeze(0))


def evaluate_chain(
    chain: BezierChain,
    target_points: torch.Tensor,
    lambda_curvature: float,
    lambda_joint: float,
    lambda_segments: float,
) -> tuple[float, float, float, float]:
    with torch.no_grad():
        curve = chain.sample(samples_per_segment=80)
        mse = ordered_point_mse(curve, target_points).item()
        curv = chain.curvature_energy(samples_per_segment=80).item()
        smooth_w = joint_smooth_weights_from_target(target_points, chain.n_segments)
        joint = chain.joint_smoothness_loss(smooth_w).item()
        score = (
            mse
            + lambda_curvature * curv
            + lambda_joint * joint
            + lambda_segments * chain.n_segments
        )
    return mse, curv, joint, score


def optimize_chain(
    chain: BezierChain,
    target_points: torch.Tensor,
    lambda_curvature: float,
    lambda_joint: float,
    steps: int = 500,
    lr: float = 1e-2,
    log_prefix: str = "",
) -> BezierChain:
    work = chain.clone_detached().requires_grad_(True)
    opt = torch.optim.Adam(work.parameters(), lr=lr)
    smooth_w = joint_smooth_weights_from_target(target_points, work.n_segments)

    for step in range(1, steps + 1):
        opt.zero_grad()
        curve = work.sample(samples_per_segment=80)
        mse = ordered_point_mse(curve, target_points)
        curv = work.curvature_energy(samples_per_segment=80)
        joint = work.joint_smoothness_loss(smooth_w)
        loss = mse + lambda_curvature * curv + lambda_joint * joint
        loss.backward()
        opt.step()

        if step == 1 or step % 100 == 0 or step == steps:
            print(
                f"{log_prefix}step={step:03d} "
                f"obj={loss.item():.6f} mse={mse.item():.6f} "
                f"curv={curv.item():.6f} joint={joint.item():.6f}"
            )

    return work.clone_detached()


def run_adaptive_subdivision(
    target_points: torch.Tensor,
    lambda_curvature: float = 3e-5,
    lambda_joint: float = 1e-3,
    lambda_segments: float = 6e-4,
    max_segments: int = 4,
    mse_threshold: float = 2.5e-4,
) -> tuple[BezierChain, list[StageMetrics], list[BezierChain]]:
    chain = init_chain_one_segment(target_points)
    stages: list[StageMetrics] = []
    snapshots: list[BezierChain] = []

    while True:
        print(f"\n=== Optimize with {chain.n_segments} segment(s) ===")
        chain = optimize_chain(
            chain=chain,
            target_points=target_points,
            lambda_curvature=lambda_curvature,
            lambda_joint=lambda_joint,
            steps=500,
            lr=1e-2,
            log_prefix=f"[{chain.n_segments}seg] ",
        )
        snapshots.append(chain.clone_detached())

        mse, curv, joint, score = evaluate_chain(
            chain, target_points, lambda_curvature, lambda_joint, lambda_segments
        )
        print(
            f"[{chain.n_segments}seg] final mse={mse:.6f} curv={curv:.6f} joint={joint:.6f} "
            f"score={score:.6f} "
            f"(score=mse+{lambda_curvature}*curv+{lambda_joint}*joint+{lambda_segments}*n_seg)"
        )

        accepted_split = False
        base_metrics = StageMetrics(
            n_segments=chain.n_segments,
            mse=mse,
            curvature=curv,
            joint=joint,
            score=score,
            accepted_split=False,
        )

        if chain.n_segments >= max_segments or mse <= mse_threshold:
            stages.append(base_metrics)
            print(f"Stop: n_segments={chain.n_segments}, mse={mse:.6f}")
            break

        per_seg_curv = chain.per_segment_curvature(samples_per_segment=80)
        split_idx = int(torch.argmax(per_seg_curv).item())
        print(f"Try split at segment {split_idx} (highest curvature={per_seg_curv[split_idx].item():.6f})")

        candidate = chain.split_segment(split_idx)
        candidate = optimize_chain(
            chain=candidate,
            target_points=target_points,
            lambda_curvature=lambda_curvature,
            lambda_joint=lambda_joint,
            steps=300,
            lr=8e-3,
            log_prefix=f"[cand {candidate.n_segments}seg] ",
        )
        cand_mse, cand_curv, cand_joint, cand_score = evaluate_chain(
            candidate, target_points, lambda_curvature, lambda_joint, lambda_segments
        )
        print(
            f"[cand {candidate.n_segments}seg] mse={cand_mse:.6f} curv={cand_curv:.6f} "
            f"joint={cand_joint:.6f} "
            f"score={cand_score:.6f}"
        )

        if cand_score + 1e-6 < score:
            accepted_split = True
            base_metrics.accepted_split = True
            stages.append(base_metrics)
            chain = candidate
            print(f"Accept split: {chain.n_segments} segments")
        else:
            stages.append(base_metrics)
            print("Reject split: segment penalty outweighs gain")
            break

    return chain, stages, snapshots


def plot_results(
    target_raw: torch.Tensor,
    stats: dict,
    final_chain: BezierChain,
    stages: list[StageMetrics],
    snapshots: list[BezierChain],
    out_path: str = "adaptive_subdivision_v1.png",
) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(17, 5), dpi=130)

    # Panel 1: final result
    ax = axes[0]
    ax.scatter(
        target_raw[:, 0].numpy(),
        target_raw[:, 1].numpy(),
        s=12,
        c="gray",
        alpha=0.55,
        label="Noisy W target",
    )
    final_curve = denormalize_points(final_chain.sample(samples_per_segment=100), stats)
    final_ctrl_poly = denormalize_points(final_chain.controls_polyline(), stats)
    ax.plot(final_curve[:, 0].numpy(), final_curve[:, 1].numpy(), lw=2.5, color="tab:blue", label="Final chain")
    ax.plot(
        final_ctrl_poly[:, 0].numpy(),
        final_ctrl_poly[:, 1].numpy(),
        "o--",
        color="tab:blue",
        alpha=0.8,
        lw=1.2,
        ms=4,
        label="Control polygon",
    )
    ax.set_title(f"Final Fit ({final_chain.n_segments} segments)")
    ax.set_xlabel("x")
    ax.set_ylabel("y")
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8, loc="best")

    # Panel 2: stage overlays
    ax = axes[1]
    ax.scatter(target_raw[:, 0].numpy(), target_raw[:, 1].numpy(), s=10, c="gray", alpha=0.3)
    colors = ["tab:red", "tab:orange", "tab:green", "tab:blue", "tab:purple"]
    for i, chain in enumerate(snapshots):
        curve = denormalize_points(chain.sample(samples_per_segment=100), stats)
        n = chain.n_segments
        ax.plot(curve[:, 0].numpy(), curve[:, 1].numpy(), lw=2.0, color=colors[i % len(colors)], label=f"{n} seg")
    ax.set_title("Adaptive Subdivision Trajectory")
    ax.set_xlabel("x")
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8, loc="best")

    # Panel 3: metrics vs segments
    ax = axes[2]
    n_seg = [s.n_segments for s in stages]
    mse = [s.mse for s in stages]
    curv = [s.curvature for s in stages]
    joint = [s.joint for s in stages]
    score = [s.score for s in stages]
    ax.plot(n_seg, mse, "o-", lw=2, label="MSE")
    ax.plot(n_seg, curv, "o-", lw=2, label="Curvature")
    ax.plot(n_seg, joint, "o-", lw=2, label="Joint kink")
    ax.plot(n_seg, score, "o-", lw=2, label="Score(+seg penalty)")
    ax.set_xticks(sorted(set(n_seg)))
    ax.set_title("Model Selection by Segment Penalty")
    ax.set_xlabel("num segments")
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8, loc="best")

    fig.suptitle(
        "PhysUI V1.1: Adaptive Subdivision + Corner-Aware Joint Smoothness",
        fontsize=12,
    )
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def main() -> None:
    set_seed(7)
    target_raw = generate_w_target(num_points=220, noise_std=0.02)
    target_norm, stats = normalize_points(target_raw)

    final_chain, stages, snapshots = run_adaptive_subdivision(
        target_points=target_norm,
        lambda_curvature=3e-5,
        lambda_joint=1e-3,
        lambda_segments=6e-4,
        max_segments=4,
        mse_threshold=2.5e-4,
    )

    print("\n=== Stage Summary ===")
    for s in stages:
        flag = "split accepted" if s.accepted_split else "no split/stop"
        print(
            f"{s.n_segments} seg -> mse={s.mse:.6f}, curv={s.curvature:.6f}, joint={s.joint:.6f}, "
            f"score={s.score:.6f}, {flag}"
        )

    out_path = "adaptive_subdivision_v1.png"
    plot_results(
        target_raw=target_raw,
        stats=stats,
        final_chain=final_chain,
        stages=stages,
        snapshots=snapshots,
        out_path=out_path,
    )
    print(f"\nSaved figure: {out_path}")
    print(f"Final segments: {final_chain.n_segments}")


if __name__ == "__main__":
    main()
