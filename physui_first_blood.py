import math
from dataclasses import dataclass

import matplotlib.pyplot as plt
import torch


def set_seed(seed: int = 42) -> None:
    torch.manual_seed(seed)


def generate_noisy_parabola(num_points: int = 120, noise_std: float = 0.05) -> torch.Tensor:
    """
    生成带噪声的抛物线边界点（模拟粗糙目标轮廓）。
    使用单拱形，便于单段 cubic Bezier 拟合。
    """
    x = torch.linspace(0.0, 1.0, num_points)
    y_clean = 1.0 - 4.0 * (x - 0.5) ** 2
    y_noisy = y_clean + noise_std * torch.randn_like(y_clean)
    return torch.stack([x, y_noisy], dim=1)


def normalize_points(points: torch.Tensor) -> tuple[torch.Tensor, dict]:
    min_xy = points.min(dim=0).values
    max_xy = points.max(dim=0).values
    center = 0.5 * (min_xy + max_xy)
    scale = (max_xy - min_xy).clamp_min(1e-6)
    points_norm = (points - center) / scale
    stats = {"center": center, "scale": scale}
    return points_norm, stats


def denormalize_points(points: torch.Tensor, stats: dict) -> torch.Tensor:
    return points * stats["scale"] + stats["center"]


class CubicBezier:
    @staticmethod
    def curve(control_points: torch.Tensor, t: torch.Tensor) -> torch.Tensor:
        t = t.unsqueeze(1)  # [N,1]
        omt = 1.0 - t
        p0, p1, p2, p3 = control_points
        return (
            (omt**3) * p0
            + 3.0 * (omt**2) * t * p1
            + 3.0 * omt * (t**2) * p2
            + (t**3) * p3
        )

    @staticmethod
    def second_derivative(control_points: torch.Tensor, t: torch.Tensor) -> torch.Tensor:
        """
        C''(t) = 6(1-t)(P2-2P1+P0) + 6t(P3-2P2+P1)
        """
        t = t.unsqueeze(1)
        omt = 1.0 - t
        p0, p1, p2, p3 = control_points
        a = p2 - 2.0 * p1 + p0
        b = p3 - 2.0 * p2 + p1
        return 6.0 * omt * a + 6.0 * t * b


def chamfer_like_mse(curve_pts: torch.Tensor, target_pts: torch.Tensor) -> torch.Tensor:
    d2 = torch.cdist(curve_pts, target_pts, p=2) ** 2
    c2t = d2.min(dim=1).values.mean()
    t2c = d2.min(dim=0).values.mean()
    return 0.5 * (c2t + t2c)


def curvature_energy(control_points: torch.Tensor, t_samples: torch.Tensor) -> torch.Tensor:
    c2 = CubicBezier.second_derivative(control_points, t_samples)
    return (c2.pow(2).sum(dim=1)).mean()


@dataclass
class TrainLog:
    total: list
    mse: list
    curvature: list
    controls: list


def init_control_points(target_points: torch.Tensor) -> torch.Tensor:
    x_min, x_max = target_points[:, 0].min(), target_points[:, 0].max()
    y_left, y_right = target_points[0, 1], target_points[-1, 1]
    p0 = torch.tensor([x_min, y_left])
    p3 = torch.tensor([x_max, y_right])
    width = x_max - x_min
    p1 = p0 + torch.tensor([0.33 * width, 0.15])
    p2 = p0 + torch.tensor([0.66 * width, 0.15])
    return torch.stack([p0, p1, p2, p3], dim=0)


def train_single_bezier(
    target_points: torch.Tensor,
    lambda_curvature: float = 1e-4,
    steps: int = 500,
    lr: float = 1e-2,
    n_curve_samples: int = 180,
    log_every: int = 50,
) -> tuple[torch.Tensor, torch.Tensor, TrainLog]:
    t = torch.linspace(0.0, 1.0, n_curve_samples)
    controls = init_control_points(target_points).clone().detach().requires_grad_(True)
    optimizer = torch.optim.Adam([controls], lr=lr)

    log = TrainLog(total=[], mse=[], curvature=[], controls=[])
    log.controls.append(controls.detach().clone())

    for step in range(1, steps + 1):
        optimizer.zero_grad()
        curve_pts = CubicBezier.curve(controls, t)
        l_mse = chamfer_like_mse(curve_pts, target_points)
        l_curv = curvature_energy(controls, t)
        total = l_mse + lambda_curvature * l_curv

        total.backward()
        optimizer.step()

        log.total.append(total.item())
        log.mse.append(l_mse.item())
        log.curvature.append(l_curv.item())
        log.controls.append(controls.detach().clone())

        if step == 1 or step % log_every == 0 or step == steps:
            print(
                f"step={step:03d} total={total.item():.6f} "
                f"mse={l_mse.item():.6f} curv={l_curv.item():.6f}"
            )

    with torch.no_grad():
        final_curve = CubicBezier.curve(controls, t)
    return controls.detach().clone(), final_curve.detach().clone(), log


def plot_first_blood(
    target_raw: torch.Tensor,
    target_norm: torch.Tensor,
    stats: dict,
    final_controls_norm: torch.Tensor,
    final_curve_norm: torch.Tensor,
    log: TrainLog,
    out_path: str = "first_blood_demo.png",
) -> None:
    t = torch.linspace(0.0, 1.0, 180)
    init_controls_norm = log.controls[0]
    init_curve_norm = CubicBezier.curve(init_controls_norm, t).detach()

    final_controls = denormalize_points(final_controls_norm, stats)
    final_curve = denormalize_points(final_curve_norm, stats)
    init_controls = denormalize_points(init_controls_norm, stats)
    init_curve = denormalize_points(init_curve_norm, stats)

    fig, axes = plt.subplots(1, 3, figsize=(16, 4.8), dpi=130)

    # 1) Loss 收敛
    ax = axes[0]
    ax.plot(log.mse, label="L_MSE", color="tab:orange", lw=2)
    ax.plot(log.curvature, label="L_curvature", color="tab:green", lw=2, alpha=0.9)
    ax.plot(log.total, label="L_total", color="tab:blue", lw=2)
    ax.set_title("Training Curves (500 steps)")
    ax.set_xlabel("step")
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8)

    # 2) 初始 vs 最终拟合
    ax = axes[1]
    ax.scatter(target_raw[:, 0].numpy(), target_raw[:, 1].numpy(), s=12, c="gray", alpha=0.55, label="Noisy target")
    ax.plot(init_curve[:, 0].numpy(), init_curve[:, 1].numpy(), "--", lw=2.0, color="black", alpha=0.6, label="Initial Bezier")
    ax.plot(final_curve[:, 0].numpy(), final_curve[:, 1].numpy(), lw=2.6, color="tab:blue", label="Final Bezier")
    ax.plot(init_controls[:, 0].numpy(), init_controls[:, 1].numpy(), "o--", color="black", alpha=0.6, lw=1.2, ms=5)
    ax.plot(final_controls[:, 0].numpy(), final_controls[:, 1].numpy(), "o--", color="tab:blue", lw=1.4, ms=6)
    ax.set_title("Geometry Fit (Initial -> Final)")
    ax.set_xlabel("x")
    ax.set_ylabel("y")
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8, loc="best")

    # 3) 控制点轨迹（显示“弹性钢条”被拉动）
    ax = axes[2]
    history = torch.stack(log.controls, dim=0)  # [T,4,2] in normalized space
    colors = ["tab:red", "tab:orange", "tab:green", "tab:purple"]
    labels = ["P0", "P1", "P2", "P3"]
    for i in range(4):
        traj_denorm = denormalize_points(history[:, i, :], stats)
        ax.plot(traj_denorm[:, 0].numpy(), traj_denorm[:, 1].numpy(), color=colors[i], lw=1.8, label=labels[i])
        ax.scatter(traj_denorm[0, 0].item(), traj_denorm[0, 1].item(), color=colors[i], marker="x", s=35)
        ax.scatter(traj_denorm[-1, 0].item(), traj_denorm[-1, 1].item(), color=colors[i], marker="o", s=30)
    ax.set_title("Control-Point Trajectories")
    ax.set_xlabel("x")
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8, loc="best")

    fig.suptitle("PhysUI First Blood: Single Cubic Bezier + Curvature Prior", fontsize=12)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def main() -> None:
    set_seed(7)
    target_raw = generate_noisy_parabola(num_points=120, noise_std=0.05)
    target_norm, stats = normalize_points(target_raw)

    print("=== Train Single Bezier Physics Engine ===")
    final_controls_norm, final_curve_norm, log = train_single_bezier(
        target_norm,
        lambda_curvature=1e-4,
        steps=500,
        lr=1e-2,
        n_curve_samples=180,
        log_every=50,
    )

    print("\n=== Final Metrics ===")
    print(f"L_total: {log.total[-1]:.6f}")
    print(f"L_MSE:   {log.mse[-1]:.6f}")
    print(f"L_curv:  {log.curvature[-1]:.6f}")

    out_path = "first_blood_demo.png"
    plot_first_blood(
        target_raw=target_raw,
        target_norm=target_norm,
        stats=stats,
        final_controls_norm=final_controls_norm,
        final_curve_norm=final_curve_norm,
        log=log,
        out_path=out_path,
    )
    print(f"\nSaved figure: {out_path}")


if __name__ == "__main__":
    main()
