import numpy as np
import matplotlib.pyplot as plt
import torch
from PIL import Image

import physui_stripe_fit_compress_v2 as m


def run_debug():
    svg_text = m.INPUT_SVG.read_text()
    viewbox, cw, ch = m.parse_svg_meta(svg_text)
    m.os_run(f'rsvg-convert {m.INPUT_SVG} -o {m.ORIG_PNG}')
    img = np.array(Image.open(m.ORIG_PNG).convert('RGB'))
    h, w = img.shape[:2]
    mask = img.mean(axis=2) > 180

    comps = m.split_components(mask, area_min=120)
    # Pick a representative center stripe (large + middle region)
    cid, comp, area, cx, cy = comps[len(comps) // 2]

    boundary_px = m.extract_ordered_boundary(comp)
    boundary_norm = m.normalize_points(boundary_px, px_w=w, px_h=h)

    seg_n = 3
    samples_per_seg = 64
    init_segs = m.fit_chain_from_boundary(boundary_norm, seg_n=seg_n, alpha=0.96)

    arcs = m.split_boundary_arcs(boundary_norm, seg_n=seg_n)
    target_np = np.stack([m.resample_open_polyline(arc, n=samples_per_seg) for arc in arcs], axis=0).astype(np.float32)

    a0_np, h1_np, h2_np = m.segs_to_param_arrays(init_segs)

    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    anchors = torch.tensor(a0_np, dtype=torch.float32, device=device, requires_grad=True)
    h1 = torch.tensor(h1_np, dtype=torch.float32, device=device, requires_grad=True)
    h2 = torch.tensor(h2_np, dtype=torch.float32, device=device, requires_grad=True)
    target = torch.tensor(target_np, dtype=torch.float32, device=device)
    anchors_init = torch.tensor(a0_np, dtype=torch.float32, device=device)

    opt = torch.optim.Adam([anchors, h1, h2], lr=0.01)

    lam_curv = 2e-5
    lam_kink = 2e-5
    lam_anchor = 2e-2
    lam_handle = 8e-4

    steps = 200
    history = {
        'total': [],
        'mse': [],
        'curv': [],
        'kink': [],
        'anchor': [],
        'handle': [],
    }
    snapshots = {}
    snap_steps = [0, 20, 60, 120, 199]

    for step in range(steps):
        opt.zero_grad()
        curve = m.sample_chain_torch(anchors, h1, h2, samples_per_seg=samples_per_seg)
        l_mse = (curve - target).pow(2).sum(dim=-1).mean()
        l_curv = m.chain_curvature_torch(anchors, h1, h2, samples_per_seg=24)
        l_kink = m.chain_joint_kink_torch(anchors, h1, h2)

        a_next = torch.roll(anchors, shifts=-1, dims=0)
        chord = (a_next - anchors).norm(dim=1) + 1e-8
        handle_ratio = ((h1 - anchors).norm(dim=1) + (a_next - h2).norm(dim=1)) / chord
        l_handle = torch.relu(handle_ratio - 2.2).pow(2).mean()
        l_anchor = (anchors - anchors_init).pow(2).sum(dim=1).mean()

        loss = l_mse + lam_curv * l_curv + lam_kink * l_kink + lam_anchor * l_anchor + lam_handle * l_handle
        loss.backward()
        opt.step()

        with torch.no_grad():
            anchors.clamp_(0.0, 1.0)
            h1.clamp_(0.0, 1.0)
            h2.clamp_(0.0, 1.0)

        history['total'].append(float(loss.item()))
        history['mse'].append(float(l_mse.item()))
        history['curv'].append(float(l_curv.item()))
        history['kink'].append(float(l_kink.item()))
        history['anchor'].append(float(l_anchor.item()))
        history['handle'].append(float(l_handle.item()))

        if step in snap_steps:
            snapshots[step] = curve.detach().cpu().numpy().copy()

    # Build figure
    fig = plt.figure(figsize=(15, 10))
    gs = fig.add_gridspec(2, 2)

    ax0 = fig.add_subplot(gs[0, 0])
    ax0.plot(history['total'], label='total')
    ax0.plot(history['mse'], label='mse')
    ax0.set_title('Optimization Curves (linear scale)')
    ax0.set_xlabel('step')
    ax0.grid(alpha=0.25)
    ax0.legend()

    ax1 = fig.add_subplot(gs[0, 1])
    for k in ['curv', 'kink', 'anchor', 'handle']:
        ax1.plot(history[k], label=k)
    ax1.set_yscale('log')
    ax1.set_title('Regularizers (log scale)')
    ax1.set_xlabel('step')
    ax1.grid(alpha=0.25)
    ax1.legend()

    ax2 = fig.add_subplot(gs[1, 0])
    target_flat = target_np.reshape(-1, 2)
    ax2.scatter(target_flat[:, 0], target_flat[:, 1], s=6, c='lightgray', alpha=0.8, label='target points')
    colors = ['#d62828', '#f77f00', '#fcbf49', '#2a9d8f', '#264653']
    for c, st in zip(colors, snap_steps):
        cur = snapshots[st].reshape(-1, 2)
        ax2.plot(cur[:, 0], cur[:, 1], color=c, lw=1.6, label=f'step {st}')
    ax2.set_aspect('equal')
    ax2.set_xlim(0, 1)
    ax2.set_ylim(0, 1)
    ax2.set_title('Geometry Evolution on One Stripe')
    ax2.grid(alpha=0.25)
    ax2.legend(fontsize=8)

    ax3 = fig.add_subplot(gs[1, 1])
    init_curve = snapshots[snap_steps[0]].reshape(-1, 2)
    fin_curve = snapshots[snap_steps[-1]].reshape(-1, 2)
    init_err = np.sum((init_curve - target_flat) ** 2, axis=1)
    fin_err = np.sum((fin_curve - target_flat) ** 2, axis=1)
    ax3.plot(init_err, label='init per-point sq error', alpha=0.8)
    ax3.plot(fin_err, label='final per-point sq error', alpha=0.8)
    ax3.set_title('Pointwise Squared Error Drop (same sample index)')
    ax3.set_xlabel('sample index')
    ax3.set_ylabel('squared error')
    ax3.grid(alpha=0.25)
    ax3.legend()

    fig.suptitle(
        f'PhysUI Backprop Debug (stripe area={area}, center=({cx:.1f},{cy:.1f}), seg={seg_n}, device={device})',
        fontsize=16,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.96])

    out = '/Users/xiaoxiaobo/physui_bp_optimization_process.png'
    fig.savefig(out, dpi=180)
    print('saved', out)


if __name__ == '__main__':
    run_debug()
