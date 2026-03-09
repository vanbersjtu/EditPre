import importlib.util
import math
from dataclasses import dataclass

import matplotlib.pyplot as plt
import torch


def load_engine(path: str):
    spec = importlib.util.spec_from_file_location("physui_engine", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def generate_sharp_w(num_points: int = 220, noise_std: float = 0.02) -> tuple[torch.Tensor, torch.Tensor]:
    x = torch.linspace(0.0, 1.0, num_points)
    xa = torch.tensor([0.00, 0.25, 0.50, 0.75, 1.00])
    ya = torch.tensor([0.80, 0.00, -0.75, 0.00, 0.80])
    idx = torch.bucketize(x, xa, right=False).clamp(min=1, max=xa.numel() - 1)
    x0, x1 = xa[idx - 1], xa[idx]
    y0, y1 = ya[idx - 1], ya[idx]
    alpha = (x - x0) / (x1 - x0 + 1e-8)
    y_clean = y0 + alpha * (y1 - y0)
    y_noisy = y_clean + noise_std * torch.randn_like(y_clean)
    return torch.stack([x, y_clean], dim=1), torch.stack([x, y_noisy], dim=1)


@dataclass
class FitResult:
    name: str
    chain: object
    noisy_mse: float
    clean_mse: float
    curvature: float
    joint: float
    n_segments: int


def optimize_with_joint_weights(engine, chain, target_points: torch.Tensor, lambda_curv: float, lambda_joint: float, smooth_weights: torch.Tensor, steps: int, lr: float, log_prefix: str = ""):
    work = chain.clone_detached().requires_grad_(True)
    opt = torch.optim.Adam(work.parameters(), lr=lr)
    for step in range(1, steps + 1):
        opt.zero_grad()
        curve = work.sample(samples_per_segment=80)
        mse = engine.ordered_point_mse(curve, target_points)
        curv = work.curvature_energy(samples_per_segment=80)
        joint = work.joint_smoothness_loss(smooth_weights)
        loss = mse + lambda_curv * curv + lambda_joint * joint
        loss.backward()
        opt.step()
        if step == 1 or step % 100 == 0 or step == steps:
            print(
                f"{log_prefix}step={step:03d} obj={loss.item():.6f} "
                f"mse={mse.item():.6f} curv={curv.item():.6f} joint={joint.item():.6f}"
            )
    return work.clone_detached()


def smooth_weights_for_mode(n_segments: int, mode: str) -> torch.Tensor:
    if n_segments <= 1:
        return torch.empty(0)
    if mode == "all_smooth":
        return torch.ones(n_segments - 1)
    if mode == "all_corner":
        return torch.zeros(n_segments - 1)
    return torch.zeros(n_segments - 1)


def force_fixed_segments(engine, target_norm: torch.Tensor, n_segments: int, lambda_curv: float, lambda_joint: float, joint_mode: str):
    chain = engine.init_chain_one_segment(target_norm)
    w = smooth_weights_for_mode(chain.n_segments, joint_mode)
    chain = optimize_with_joint_weights(engine, chain, target_norm, lambda_curv, lambda_joint, w, steps=500, lr=1e-2, log_prefix="[force 1seg] ")
    while chain.n_segments < n_segments:
        split_idx = int(torch.argmax(chain.per_segment_curvature(samples_per_segment=80)).item())
        chain = chain.split_segment(split_idx)
        w = smooth_weights_for_mode(chain.n_segments, joint_mode)
        chain = optimize_with_joint_weights(
            engine,
            chain,
            target_norm,
            lambda_curv,
            lambda_joint,
            w,
            steps=350,
            lr=8e-3,
            log_prefix=f"[force {chain.n_segments}seg] ",
        )
    return chain


def evaluate_fit(engine, chain, noisy_raw: torch.Tensor, clean_raw: torch.Tensor, stats: dict, joint_mode: str):
    pred_raw = engine.denormalize_points(chain.sample(samples_per_segment=120), stats)
    noisy_mse = engine.ordered_point_mse(pred_raw, noisy_raw).item()
    clean_mse = engine.ordered_point_mse(pred_raw, clean_raw).item()
    curvature = chain.curvature_energy(samples_per_segment=120).item()
    w = smooth_weights_for_mode(chain.n_segments, joint_mode)
    joint = chain.joint_smoothness_loss(w).item()
    return noisy_mse, clean_mse, curvature, joint


def run_case(engine, noisy_raw: torch.Tensor, clean_raw: torch.Tensor, lambda_joint: float, joint_mode: str, n_segments: int, name: str) -> FitResult:
    noisy_norm, stats = engine.normalize_points(noisy_raw)
    chain = force_fixed_segments(
        engine,
        noisy_norm,
        n_segments=n_segments,
        lambda_curv=3e-5,
        lambda_joint=lambda_joint,
        joint_mode=joint_mode,
    )
    noisy_mse, clean_mse, curv, joint = evaluate_fit(engine, chain, noisy_raw, clean_raw, stats, joint_mode)
    return FitResult(
        name=name,
        chain=chain,
        noisy_mse=noisy_mse,
        clean_mse=clean_mse,
        curvature=curv,
        joint=joint,
        n_segments=chain.n_segments,
    )


def draw_panel(ax, engine, noisy_raw: torch.Tensor, clean_raw: torch.Tensor, result: FitResult, stats: dict, title: str):
    pred_raw = engine.denormalize_points(result.chain.sample(samples_per_segment=140), stats)
    ctrl_raw = engine.denormalize_points(result.chain.controls_polyline(), stats)
    anchors = engine.denormalize_points(result.chain.anchors, stats)
    ax.scatter(noisy_raw[:, 0].numpy(), noisy_raw[:, 1].numpy(), s=9, c="gray", alpha=0.45, label="Noisy target")
    ax.plot(clean_raw[:, 0].numpy(), clean_raw[:, 1].numpy(), lw=1.8, color="black", alpha=0.5, label="Clean target")
    ax.plot(pred_raw[:, 0].numpy(), pred_raw[:, 1].numpy(), lw=2.6, color="tab:blue", label="Fitted curve")
    ax.plot(ctrl_raw[:, 0].numpy(), ctrl_raw[:, 1].numpy(), "o--", lw=1.0, ms=3.5, alpha=0.8, color="tab:blue", label="Control poly")
    ax.scatter(
        anchors[:, 0].numpy(),
        anchors[:, 1].numpy(),
        s=42,
        c="tab:red",
        edgecolors="white",
        linewidths=0.7,
        label="Segment anchors",
        zorder=6,
    )
    ax.set_title(
        f"{title}\nseg={result.n_segments}, noisyMSE={result.noisy_mse:.4f}, cleanMSE={result.clean_mse:.4f}, joint={result.joint:.4f}",
        fontsize=10,
    )
    ax.grid(alpha=0.25)
    ax.set_xlabel("x")
    ax.set_ylabel("y")


def main():
    engine = load_engine("/Users/xiaoxiaobo/adaptive_subdivision_demo.py")

    torch.manual_seed(7)
    smooth_noisy = engine.generate_w_target(num_points=220, noise_std=0.02)
    x = smooth_noisy[:, 0]
    smooth_clean = torch.stack([x, 0.55 * torch.cos(2.0 * math.pi * x) + 0.18 * torch.cos(6.0 * math.pi * x)], dim=1)

    torch.manual_seed(11)
    sharp_clean, sharp_noisy = generate_sharp_w(num_points=220, noise_std=0.02)

    print("\n=== Smooth-W / V1 (no joint) ===")
    smooth_v1 = run_case(engine, smooth_noisy, smooth_clean, lambda_joint=0.0, joint_mode="all_corner", n_segments=4, name="Smooth-V1")
    print("\n=== Smooth-W / V1.1 (all joints smooth) ===")
    smooth_v11 = run_case(engine, smooth_noisy, smooth_clean, lambda_joint=1e-2, joint_mode="all_smooth", n_segments=4, name="Smooth-V1.1")

    print("\n=== Sharp-W / V1 (no joint) ===")
    sharp_v1 = run_case(engine, sharp_noisy, sharp_clean, lambda_joint=0.0, joint_mode="all_corner", n_segments=4, name="Sharp-V1")
    print("\n=== Sharp-W / V1.1 (all joints are corners; no smoothing) ===")
    sharp_v11 = run_case(engine, sharp_noisy, sharp_clean, lambda_joint=1e-2, joint_mode="all_corner", n_segments=4, name="Sharp-V1.1")

    smooth_stats = engine.normalize_points(smooth_noisy)[1]
    sharp_stats = engine.normalize_points(sharp_noisy)[1]

    fig, axes = plt.subplots(2, 2, figsize=(14, 10), dpi=140)
    draw_panel(axes[0, 0], engine, smooth_noisy, smooth_clean, smooth_v1, smooth_stats, "Smooth-W | V1")
    draw_panel(axes[0, 1], engine, smooth_noisy, smooth_clean, smooth_v11, smooth_stats, "Smooth-W | V1.1")
    draw_panel(axes[1, 0], engine, sharp_noisy, sharp_clean, sharp_v1, sharp_stats, "Sharp-W | V1")
    draw_panel(axes[1, 1], engine, sharp_noisy, sharp_clean, sharp_v11, sharp_stats, "Sharp-W | V1.1")

    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=5, fontsize=9)
    fig.suptitle("PhysUI V1 vs V1.1 (Controlled): Smooth-Joint vs Corner-Preserving", fontsize=13)
    fig.tight_layout(rect=[0, 0.05, 1, 0.97])
    out_main = "/Users/xiaoxiaobo/physui_v11_ablation.png"
    fig.savefig(out_main)
    plt.close(fig)

    labels_case = ["Smooth-V1", "Smooth-V1.1", "Sharp-V1", "Sharp-V1.1"]
    results = [smooth_v1, smooth_v11, sharp_v1, sharp_v11]
    noisy_mse = [r.noisy_mse for r in results]
    clean_mse = [r.clean_mse for r in results]
    joints = [r.joint for r in results]
    segs = [r.n_segments for r in results]

    x_id = torch.arange(len(labels_case)).numpy()
    width = 0.2
    fig, ax = plt.subplots(1, 1, figsize=(11, 4.8), dpi=140)
    ax.bar(x_id - 1.5 * width, noisy_mse, width=width, label="Noisy MSE")
    ax.bar(x_id - 0.5 * width, clean_mse, width=width, label="Clean MSE")
    ax.bar(x_id + 0.5 * width, joints, width=width, label="Joint kink")
    ax.bar(x_id + 1.5 * width, segs, width=width, label="#Segments")
    ax.set_xticks(x_id)
    ax.set_xticklabels(labels_case)
    ax.set_title("Metric Comparison: Fit Error vs Topology Complexity")
    ax.grid(alpha=0.25, axis="y")
    ax.legend()
    fig.tight_layout()
    out_metric = "/Users/xiaoxiaobo/physui_v11_metrics.png"
    fig.savefig(out_metric)
    plt.close(fig)

    print("\n=== Summary ===")
    for r in results:
        print(
            f"{r.name}: seg={r.n_segments}, noisy_mse={r.noisy_mse:.6f}, "
            f"clean_mse={r.clean_mse:.6f}, curv={r.curvature:.6f}, joint={r.joint:.6f}"
        )
    print(f"\nSaved: {out_main}")
    print(f"Saved: {out_metric}")


if __name__ == "__main__":
    main()
