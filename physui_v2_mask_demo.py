import math
from dataclasses import dataclass

import matplotlib.pyplot as plt
import torch


def set_seed(seed: int = 7) -> None:
    torch.manual_seed(seed)


def generate_noisy_w_edge(num_points: int = 260, noise_std: float = 0.008) -> torch.Tensor:
    """
    Synthetic jagged W-like edge in [0,1]^2.
    """
    x = torch.linspace(0.0, 1.0, num_points)
    y_clean = 0.5 + 0.23 * torch.cos(2.0 * math.pi * x) + 0.08 * torch.cos(6.0 * math.pi * x)
    high_freq = 0.020 * torch.sin(28.0 * math.pi * x + 0.35)
    y_noisy = y_clean + high_freq + noise_std * torch.randn_like(x)
    y_noisy = y_noisy.clamp(0.08, 0.92)
    return torch.stack([x, y_noisy], dim=1)


def cubic_bezier(ctrl: torch.Tensor, t: torch.Tensor) -> torch.Tensor:
    t = t.unsqueeze(1)
    omt = 1.0 - t
    p0, p1, p2, p3 = ctrl
    return (
        (omt**3) * p0
        + 3.0 * (omt**2) * t * p1
        + 3.0 * omt * (t**2) * p2
        + (t**3) * p3
    )


def cubic_second_derivative(ctrl: torch.Tensor, t: torch.Tensor) -> torch.Tensor:
    t = t.unsqueeze(1)
    omt = 1.0 - t
    p0, p1, p2, p3 = ctrl
    a = p2 - 2.0 * p1 + p0
    b = p3 - 2.0 * p2 + p1
    return 6.0 * omt * a + 6.0 * t * b


@dataclass
class StageMetric:
    n_segments: int
    mask_loss: float
    curvature: float
    score: float
    split_accepted: bool


class BezierChain:
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

    def segment_ctrl(self, i: int) -> torch.Tensor:
        return torch.stack([self.anchors[i], self.h1[i], self.h2[i], self.anchors[i + 1]], dim=0)

    def sample(self, samples_per_segment: int = 100) -> torch.Tensor:
        t = torch.linspace(0.0, 1.0, samples_per_segment)
        pieces = [cubic_bezier(self.segment_ctrl(i), t) for i in range(self.n_segments)]
        return torch.cat(pieces, dim=0)

    def curvature_energy(self, samples_per_segment: int = 80) -> torch.Tensor:
        t = torch.linspace(0.0, 1.0, samples_per_segment)
        vals = []
        for i in range(self.n_segments):
            c2 = cubic_second_derivative(self.segment_ctrl(i), t)
            vals.append((c2.pow(2).sum(dim=1)).mean())
        return torch.stack(vals).mean()

    def per_segment_curvature(self, samples_per_segment: int = 80) -> torch.Tensor:
        t = torch.linspace(0.0, 1.0, samples_per_segment)
        vals = []
        for i in range(self.n_segments):
            c2 = cubic_second_derivative(self.segment_ctrl(i), t)
            vals.append((c2.pow(2).sum(dim=1)).mean())
        return torch.stack(vals)

    def split_segment(self, idx: int) -> "BezierChain":
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

        new_anchors = torch.cat([self.anchors[: idx + 1], s.unsqueeze(0), self.anchors[idx + 1 :]], dim=0)
        new_h1 = torch.cat([self.h1[:idx], q0.unsqueeze(0), r1.unsqueeze(0), self.h1[idx + 1 :]], dim=0)
        new_h2 = torch.cat([self.h2[:idx], r0.unsqueeze(0), q2.unsqueeze(0), self.h2[idx + 1 :]], dim=0)
        return BezierChain(new_anchors, new_h1, new_h2)


def init_chain_one_segment(edge_points: torch.Tensor) -> BezierChain:
    p0 = edge_points[0]
    p3 = edge_points[-1]
    dx = p3[0] - p0[0]
    p1 = p0 + torch.tensor([0.33 * dx, 0.0])
    p2 = p0 + torch.tensor([0.66 * dx, 0.0])
    anchors = torch.stack([p0, p3], dim=0)
    return BezierChain(anchors=anchors, h1=p1.unsqueeze(0), h2=p2.unsqueeze(0))


class SoftMaskRenderer:
    def __init__(self, height: int = 128, width: int = 128, radius: float = 0.012, tau: float = 0.006):
        self.h = height
        self.w = width
        self.radius = radius
        self.tau = tau
        ys = torch.linspace(0.0, 1.0, height)
        xs = torch.linspace(0.0, 1.0, width)
        gy, gx = torch.meshgrid(ys, xs, indexing="ij")
        self.grid = torch.stack([gx, gy], dim=-1).reshape(-1, 2)  # [H*W,2]

    def render(self, points: torch.Tensor) -> torch.Tensor:
        d = torch.cdist(self.grid, points, p=2)
        dmin = d.min(dim=1).values
        m = torch.sigmoid((self.radius - dmin) / self.tau)
        return m.view(self.h, self.w)


def optimize_chain(
    chain: BezierChain,
    target_mask: torch.Tensor,
    renderer: SoftMaskRenderer,
    lambda_curv: float,
    steps: int = 350,
    lr: float = 8e-3,
    log_prefix: str = "",
) -> BezierChain:
    work = chain.clone_detached().requires_grad_(True)
    opt = torch.optim.Adam(work.parameters(), lr=lr)

    for step in range(1, steps + 1):
        opt.zero_grad()
        curve = work.sample(samples_per_segment=110)
        pred_mask = renderer.render(curve)
        l_mask = ((pred_mask - target_mask) ** 2).mean()
        l_curv = work.curvature_energy(samples_per_segment=80)
        loss = l_mask + lambda_curv * l_curv
        loss.backward()
        opt.step()

        with torch.no_grad():
            work.anchors.clamp_(0.0, 1.0)
            work.h1.clamp_(0.0, 1.0)
            work.h2.clamp_(0.0, 1.0)

        if step == 1 or step % 100 == 0 or step == steps:
            print(
                f"{log_prefix}step={step:03d} "
                f"obj={loss.item():.6f} mask={l_mask.item():.6f} curv={l_curv.item():.6f}"
            )
    return work.clone_detached()


def evaluate_chain(chain: BezierChain, target_mask: torch.Tensor, renderer: SoftMaskRenderer, lambda_curv: float, lambda_seg: float) -> tuple[float, float, float]:
    with torch.no_grad():
        curve = chain.sample(samples_per_segment=120)
        pred_mask = renderer.render(curve)
        l_mask = ((pred_mask - target_mask) ** 2).mean().item()
        l_curv = chain.curvature_energy(samples_per_segment=100).item()
        score = l_mask + lambda_curv * l_curv + lambda_seg * chain.n_segments
    return l_mask, l_curv, score


def run_physui_v2(
    target_mask: torch.Tensor,
    edge_points: torch.Tensor,
    renderer: SoftMaskRenderer,
    lambda_curv: float = 5e-4,
    lambda_seg: float = 0.0012,
    max_segments: int = 5,
) -> tuple[BezierChain, list[StageMetric]]:
    chain = init_chain_one_segment(edge_points)
    stages: list[StageMetric] = []

    while True:
        print(f"\n=== PhysUI-V2 optimize: {chain.n_segments} segment(s) ===")
        chain = optimize_chain(
            chain,
            target_mask=target_mask,
            renderer=renderer,
            lambda_curv=lambda_curv,
            steps=380,
            lr=8e-3,
            log_prefix=f"[V2 {chain.n_segments}seg] ",
        )
        l_mask, l_curv, score = evaluate_chain(chain, target_mask, renderer, lambda_curv, lambda_seg)
        print(
            f"[V2 {chain.n_segments}seg] final mask={l_mask:.6f} curv={l_curv:.6f} "
            f"score={score:.6f}"
        )

        base_metric = StageMetric(
            n_segments=chain.n_segments,
            mask_loss=l_mask,
            curvature=l_curv,
            score=score,
            split_accepted=False,
        )

        if chain.n_segments >= max_segments:
            stages.append(base_metric)
            print("Stop: reach max segments")
            break

        split_idx = int(torch.argmax(chain.per_segment_curvature(samples_per_segment=80)).item())
        candidate = chain.split_segment(split_idx)
        candidate = optimize_chain(
            candidate,
            target_mask=target_mask,
            renderer=renderer,
            lambda_curv=lambda_curv,
            steps=260,
            lr=7e-3,
            log_prefix=f"[V2 cand {candidate.n_segments}seg] ",
        )
        c_mask, c_curv, c_score = evaluate_chain(candidate, target_mask, renderer, lambda_curv, lambda_seg)
        print(
            f"[V2 cand {candidate.n_segments}seg] mask={c_mask:.6f} curv={c_curv:.6f} "
            f"score={c_score:.6f}"
        )

        if c_score + 1e-6 < score:
            base_metric.split_accepted = True
            stages.append(base_metric)
            chain = candidate
            print(f"Accept split -> {chain.n_segments} segments")
        else:
            stages.append(base_metric)
            print("Reject split (Occam penalty wins)")
            break

    return chain, stages


def run_baseline_many_segments(
    target_mask: torch.Tensor,
    edge_points: torch.Tensor,
    renderer: SoftMaskRenderer,
    forced_segments: int = 5,
) -> BezierChain:
    chain = init_chain_one_segment(edge_points)
    chain = optimize_chain(
        chain,
        target_mask=target_mask,
        renderer=renderer,
        lambda_curv=0.0,
        steps=380,
        lr=8e-3,
        log_prefix="[Base 1seg] ",
    )
    while chain.n_segments < forced_segments:
        split_idx = int(torch.argmax(chain.per_segment_curvature(samples_per_segment=80)).item())
        chain = chain.split_segment(split_idx)
        chain = optimize_chain(
            chain,
            target_mask=target_mask,
            renderer=renderer,
            lambda_curv=0.0,
            steps=260,
            lr=7e-3,
            log_prefix=f"[Base {chain.n_segments}seg] ",
        )
    return chain


def draw_overlay(ax, title: str, mask: torch.Tensor, chain: BezierChain, renderer: SoftMaskRenderer, target_edge: torch.Tensor, metric_text: str) -> None:
    pred_points = chain.sample(samples_per_segment=150).detach()
    anchors = chain.anchors.detach()
    ax.imshow(mask.numpy(), cmap="gray", origin="lower", extent=[0, 1, 0, 1], alpha=0.9)
    ax.plot(target_edge[:, 0].numpy(), target_edge[:, 1].numpy(), color="gray", lw=1.0, alpha=0.4, label="Target edge")
    ax.plot(pred_points[:, 0].numpy(), pred_points[:, 1].numpy(), color="tab:blue", lw=2.5, label="Bezier chain")
    ax.scatter(anchors[:, 0].numpy(), anchors[:, 1].numpy(), c="tab:red", s=42, edgecolors="white", linewidths=0.7, zorder=6, label="Anchors")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_title(f"{title}\n{metric_text}", fontsize=10)
    ax.set_xlabel("x")
    ax.set_ylabel("y")
    ax.grid(alpha=0.15)


def main() -> None:
    set_seed(7)
    renderer = SoftMaskRenderer(height=128, width=128, radius=0.012, tau=0.006)

    target_edge = generate_noisy_w_edge(num_points=260, noise_std=0.008)
    target_mask = renderer.render(target_edge).detach()

    print("=== Baseline (mask-only, many segments) ===")
    baseline = run_baseline_many_segments(
        target_mask=target_mask,
        edge_points=target_edge,
        renderer=renderer,
        forced_segments=5,
    )
    b_mask, b_curv, b_score = evaluate_chain(baseline, target_mask, renderer, lambda_curv=0.0, lambda_seg=0.0)

    print("\n=== PhysUI-V2 (mask + curvature + segment penalty) ===")
    physui, stages = run_physui_v2(
        target_mask=target_mask,
        edge_points=target_edge,
        renderer=renderer,
        lambda_curv=5e-4,
        lambda_seg=0.0012,
        max_segments=5,
    )
    p_mask, p_curv, p_score = evaluate_chain(physui, target_mask, renderer, lambda_curv=5e-4, lambda_seg=0.0012)

    print("\n=== V2 Summary ===")
    print(f"Baseline: seg={baseline.n_segments}, mask={b_mask:.6f}, curv={b_curv:.6f}")
    print(f"PhysUI-V2: seg={physui.n_segments}, mask={p_mask:.6f}, curv={p_curv:.6f}, score={p_score:.6f}")

    fig, axes = plt.subplots(1, 3, figsize=(15, 4.8), dpi=140)
    axes[0].imshow(target_mask.numpy(), cmap="gray", origin="lower", extent=[0, 1, 0, 1])
    axes[0].plot(target_edge[:, 0].numpy(), target_edge[:, 1].numpy(), color="tab:orange", lw=1.4)
    axes[0].set_title("Target Mask (V2 input)")
    axes[0].set_xlabel("x")
    axes[0].set_ylabel("y")
    axes[0].grid(alpha=0.15)

    draw_overlay(
        axes[1],
        "Baseline (No Physics)",
        target_mask,
        baseline,
        renderer,
        target_edge,
        metric_text=f"seg={baseline.n_segments}, mask={b_mask:.4f}, curv={b_curv:.2f}",
    )
    draw_overlay(
        axes[2],
        "PhysUI-V2",
        target_mask,
        physui,
        renderer,
        target_edge,
        metric_text=f"seg={physui.n_segments}, mask={p_mask:.4f}, curv={p_curv:.2f}",
    )

    handles, labels = axes[2].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=4, fontsize=8)
    fig.suptitle("PhysUI V2: Differentiable Mask Rendering + Adaptive Subdivision", fontsize=12)
    fig.tight_layout(rect=[0, 0.06, 1, 0.96])
    out_path = "physui_v2_mask_ablation.png"
    fig.savefig(out_path)
    plt.close(fig)
    print(f"\nSaved figure: {out_path}")

    # Stage curve
    fig, ax = plt.subplots(1, 1, figsize=(7.2, 4.2), dpi=140)
    ns = [s.n_segments for s in stages]
    ml = [s.mask_loss for s in stages]
    cv = [s.curvature for s in stages]
    sc = [s.score for s in stages]
    ax.plot(ns, ml, "o-", lw=2, label="Mask loss")
    ax.plot(ns, cv, "o-", lw=2, label="Curvature")
    ax.plot(ns, sc, "o-", lw=2, label="Score")
    ax.set_xticks(sorted(set(ns)))
    ax.set_title("PhysUI-V2 Stage Metrics")
    ax.set_xlabel("num segments")
    ax.grid(alpha=0.2)
    ax.legend()
    stage_path = "physui_v2_stage_metrics.png"
    fig.tight_layout()
    fig.savefig(stage_path)
    plt.close(fig)
    print(f"Saved figure: {stage_path}")


if __name__ == "__main__":
    main()
