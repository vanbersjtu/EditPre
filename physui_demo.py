import math
from dataclasses import dataclass

import torch

# Work around environments where dateutil is a namespace package without __version__.
import dateutil  # type: ignore

if not hasattr(dateutil, "__version__"):
    dateutil.__version__ = "2.8.2"

import matplotlib.pyplot as plt


def set_seed(seed: int = 42) -> None:
    torch.manual_seed(seed)


def generate_noisy_target(num_points: int = 100, noise_std: float = 0.12) -> torch.Tensor:
    """Generate noisy 2D points sampled from a single-arch edge with jagged details."""
    # A single arch is representable by one cubic Bezier; high-frequency term simulates pixel noise.
    x = torch.linspace(0.0, math.pi, num_points)
    y_clean = 0.9 * torch.sin(x) + 0.08 * torch.sin(14.0 * x)
    y_noisy = y_clean + noise_std * torch.randn_like(y_clean)
    points = torch.stack([x, y_noisy], dim=1)
    return points


def normalize_points(points: torch.Tensor) -> tuple[torch.Tensor, dict]:
    """Normalize to roughly [-1, 1] in each axis for stable optimization."""
    min_xy = points.min(dim=0).values
    max_xy = points.max(dim=0).values
    center = 0.5 * (min_xy + max_xy)
    scale = (max_xy - min_xy).clamp_min(1e-6)
    norm_points = (points - center) / scale
    stats = {"center": center, "scale": scale}
    return norm_points, stats


def denormalize_points(points: torch.Tensor, stats: dict) -> torch.Tensor:
    return points * stats["scale"] + stats["center"]


class CubicBezier:
    @staticmethod
    def curve(control_points: torch.Tensor, t: torch.Tensor) -> torch.Tensor:
        """
        control_points: [4,2]
        t: [N]
        returns: [N,2]
        """
        t = t.unsqueeze(1)
        one_minus_t = 1.0 - t
        p0, p1, p2, p3 = control_points
        return (
            (one_minus_t**3) * p0
            + 3.0 * (one_minus_t**2) * t * p1
            + 3.0 * one_minus_t * (t**2) * p2
            + (t**3) * p3
        )

    @staticmethod
    def second_derivative(control_points: torch.Tensor, t: torch.Tensor) -> torch.Tensor:
        """Analytical C''(t) for cubic Bezier. Returns [N,2]."""
        t = t.unsqueeze(1)
        one_minus_t = 1.0 - t
        p0, p1, p2, p3 = control_points
        a = p2 - 2.0 * p1 + p0
        b = p3 - 2.0 * p2 + p1
        return 6.0 * one_minus_t * a + 6.0 * t * b


@dataclass
class FitResult:
    control_points: torch.Tensor
    curve_points: torch.Tensor
    final_mse: float
    final_curvature: float


def chamfer_like_mse(curve_pts: torch.Tensor, target_pts: torch.Tensor) -> torch.Tensor:
    """
    Symmetric nearest-neighbor squared distance between point sets.
    """
    dists = torch.cdist(curve_pts, target_pts, p=2) ** 2
    loss_curve_to_target = dists.min(dim=1).values.mean()
    loss_target_to_curve = dists.min(dim=0).values.mean()
    return 0.5 * (loss_curve_to_target + loss_target_to_curve)


def curvature_energy(control_points: torch.Tensor, t_samples: torch.Tensor) -> torch.Tensor:
    c2 = CubicBezier.second_derivative(control_points, t_samples)
    # Integral approximation: mean(||C''(t)||^2) * dt sum -> mean is stable scale-wise.
    return (c2.pow(2).sum(dim=1)).mean()


def init_control_points(target_points: torch.Tensor) -> torch.Tensor:
    """Initialize as a gentle polyline across x-range and y trend."""
    x_min, x_max = target_points[:, 0].min(), target_points[:, 0].max()
    y_start = target_points[0, 1]
    y_end = target_points[-1, 1]
    p0 = torch.tensor([x_min, y_start])
    p3 = torch.tensor([x_max, y_end])
    p1 = p0 + torch.tensor([0.33 * (x_max - x_min), 0.0])
    p2 = p0 + torch.tensor([0.66 * (x_max - x_min), 0.0])
    return torch.stack([p0, p1, p2, p3], dim=0)


def fit_bezier(
    target_points: torch.Tensor,
    lambda_curv: float,
    iterations: int = 2000,
    lr: float = 0.01,
    n_curve_samples: int = 160,
    log_every: int = 200,
) -> FitResult:
    t = torch.linspace(0.0, 1.0, n_curve_samples)

    control = init_control_points(target_points).clone().detach().requires_grad_(True)
    optimizer = torch.optim.Adam([control], lr=lr)

    for step in range(1, iterations + 1):
        optimizer.zero_grad()

        curve_pts = CubicBezier.curve(control, t)
        mse = chamfer_like_mse(curve_pts, target_points)
        curv = curvature_energy(control, t)
        total = mse + lambda_curv * curv

        total.backward()
        optimizer.step()

        if step == 1 or step % log_every == 0 or step == iterations:
            print(
                f"[lambda={lambda_curv:.2e}] step={step:4d} "
                f"total={total.item():.6f} mse={mse.item():.6f} "
                f"curv={curv.item():.6f}"
            )

    with torch.no_grad():
        final_curve = CubicBezier.curve(control, t)
        final_mse = chamfer_like_mse(final_curve, target_points).item()
        final_curv = curvature_energy(control, t).item()

    return FitResult(
        control_points=control.detach().clone(),
        curve_points=final_curve.detach().clone(),
        final_mse=final_mse,
        final_curvature=final_curv,
    )


def plot_results(
    target_raw: torch.Tensor,
    baseline: FitResult,
    physui: FitResult,
    stats: dict,
    out_path: str = "ablation_study.png",
) -> None:
    target = target_raw
    baseline_curve = denormalize_points(baseline.curve_points, stats)
    baseline_ctrl = denormalize_points(baseline.control_points, stats)
    physui_curve = denormalize_points(physui.curve_points, stats)
    physui_ctrl = denormalize_points(physui.control_points, stats)

    fig, axes = plt.subplots(1, 2, figsize=(13, 5), dpi=130)

    panels = [
        (
            axes[0],
            baseline_curve,
            baseline_ctrl,
            "Baseline (No Physics)",
            "tab:red",
            baseline,
        ),
        (
            axes[1],
            physui_curve,
            physui_ctrl,
            "PhysUI (Curvature Penalty)",
            "tab:blue",
            physui,
        ),
    ]

    for ax, curve, ctrl, title, color, result in panels:
        ax.scatter(target[:, 0].numpy(), target[:, 1].numpy(), s=14, c="gray", alpha=0.55, label="Noisy target")
        ax.plot(curve[:, 0].numpy(), curve[:, 1].numpy(), color=color, lw=2.8, label="Bezier fit")
        ax.plot(ctrl[:, 0].numpy(), ctrl[:, 1].numpy(), "o--", color=color, lw=1.3, ms=6, label="Control points")
        ax.set_title(
            f"{title}\nMSE={result.final_mse:.4f}, Curv(raw)={result.final_curvature:.2f}",
            fontsize=10,
        )
        ax.set_xlabel("x")
        ax.set_ylabel("y")
        ax.grid(alpha=0.25)
        ax.legend(loc="best", fontsize=8)

    fig.suptitle("PhysUI Toy Demo: Elastic Curvature Prior Improves Bezier Smoothness", fontsize=12)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def main() -> None:
    set_seed(7)

    target_raw = generate_noisy_target(num_points=100, noise_std=0.15)
    target_norm, stats = normalize_points(target_raw)

    print("=== Running Baseline (lambda=0.0) ===")
    baseline = fit_bezier(target_norm, lambda_curv=0.0, iterations=2200, lr=0.01)

    print("\n=== Running PhysUI (lambda=3e-5) ===")
    physui = fit_bezier(target_norm, lambda_curv=3e-5, iterations=2200, lr=0.01)

    print("\n=== Final Metrics (Normalized Space) ===")
    print(f"Baseline: mse={baseline.final_mse:.6f}, curvature={baseline.final_curvature:.6f}")
    print(f"PhysUI  : mse={physui.final_mse:.6f}, curvature={physui.final_curvature:.6f}")

    output_path = "ablation_study.png"
    plot_results(target_raw, baseline, physui, stats, out_path=output_path)
    print(f"\nSaved figure to: {output_path}")


if __name__ == "__main__":
    main()
