import re
from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import torch
from PIL import Image
from scipy import ndimage as ndi
from scipy.spatial import ConvexHull


INPUT_SVG = Path('/Users/xiaoxiaobo/user_path.svg')
ORIG_PNG = Path('/Users/xiaoxiaobo/user_path_render.png')
OUT_SVG = Path('/Users/xiaoxiaobo/user_path_physui_fit.svg')
OUT_PNG = Path('/Users/xiaoxiaobo/user_path_physui_fit.png')
FIG_PATH = Path('/Users/xiaoxiaobo/user_path_physui_fit_comparison.png')
REPORT_PATH = Path('/Users/xiaoxiaobo/user_path_physui_fit_report.txt')


def os_run(cmd: str) -> None:
    import os

    code = os.system(cmd)
    if code != 0:
        raise RuntimeError(f'Command failed: {cmd}')


def parse_svg_meta(svg_text: str):
    vb = re.search(r'viewBox="([^"]+)"', svg_text)
    if vb is None:
        raise RuntimeError('viewBox not found in input svg')
    vals = [float(x) for x in vb.group(1).split()]
    if len(vals) != 4:
        raise RuntimeError('Invalid viewBox')
    w = re.search(r'width="([0-9.]+)"', svg_text)
    h = re.search(r'height="([0-9.]+)"', svg_text)
    width = int(float(w.group(1))) if w else 800
    height = int(float(h.group(1))) if h else 800
    return vals, width, height


def extract_path_d(svg_text: str) -> str:
    m = re.search(r'<path[^>]*d="([^"]+)"', svg_text, flags=re.S)
    if m is None:
        raise RuntimeError('path d not found')
    return m.group(1)


def split_components(mask: np.ndarray, area_min: int = 120):
    lab, num = ndi.label(mask)
    if num == 0:
        return []
    areas = np.bincount(lab.ravel())
    areas[0] = 0
    keep_ids = [i for i in range(1, num + 1) if areas[i] >= area_min]
    keep_ids = sorted(keep_ids, key=lambda i: areas[i], reverse=True)
    comps = []
    for cid in keep_ids:
        comp = (lab == cid)
        ys, xs = np.where(comp)
        cx = float(xs.mean())
        cy = float(ys.mean())
        comps.append((cid, comp, int(areas[cid]), cx, cy))
    # Left->right ordering for stable output
    comps.sort(key=lambda x: (x[3], x[4]))
    return comps


def convex_boundary_points(comp_mask: np.ndarray) -> np.ndarray:
    ys, xs = np.where(comp_mask)
    pts = np.stack([xs, ys], axis=1).astype(np.float32)
    if len(pts) < 8:
        return pts
    hull = ConvexHull(pts)
    hp = pts[hull.vertices]
    return hp


def resample_closed_polyline(points: np.ndarray, n: int) -> np.ndarray:
    pts = np.asarray(points, dtype=np.float32)
    if len(pts) < 3:
        return pts
    if not np.allclose(pts[0], pts[-1]):
        pts = np.vstack([pts, pts[0]])
    seg = pts[1:] - pts[:-1]
    seg_len = np.linalg.norm(seg, axis=1)
    total = float(seg_len.sum())
    if total < 1e-8:
        return pts[:-1]
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


def init_chain(target: np.ndarray, seg_n: int, handle_scale: float = 0.28):
    n = len(target)
    idx = np.linspace(0, n, seg_n + 1)[:-1]
    idx = np.round(idx).astype(int) % n
    a = target[idx].copy()
    prev_a = np.roll(a, 1, axis=0)
    next_a = np.roll(a, -1, axis=0)
    tan = next_a - prev_a
    h1 = a + handle_scale * tan
    h2 = np.roll(a, -1, axis=0) - handle_scale * tan
    return (
        torch.tensor(a, dtype=torch.float32, requires_grad=True),
        torch.tensor(h1, dtype=torch.float32, requires_grad=True),
        torch.tensor(h2, dtype=torch.float32, requires_grad=True),
    )


def sample_chain(a: torch.Tensor, h1: torch.Tensor, h2: torch.Tensor, k: int):
    # Exclude t=1 for each segment to avoid duplicated seam samples.
    t = torch.linspace(0.0, 1.0, k + 1, device=a.device)[:-1]
    omt = 1.0 - t
    a_next = torch.roll(a, shifts=-1, dims=0)
    c = (
        (omt**3)[None, :, None] * a[:, None, :]
        + 3.0 * (omt**2 * t)[None, :, None] * h1[:, None, :]
        + 3.0 * (omt * t**2)[None, :, None] * h2[:, None, :]
        + (t**3)[None, :, None] * a_next[:, None, :]
    )
    return c.reshape(-1, 2)


def chamfer_symmetric(curve: torch.Tensor, target: torch.Tensor):
    d = torch.cdist(curve, target, p=2)
    return 0.5 * (d.min(1).values.pow(2).mean() + d.min(0).values.pow(2).mean())


def ordered_mse(curve: torch.Tensor, target: torch.Tensor):
    return (curve - target).pow(2).sum(dim=1).mean()


def curvature_loss(a: torch.Tensor, h1: torch.Tensor, h2: torch.Tensor, k: int = 20):
    t = torch.linspace(0.0, 1.0, k, device=a.device)
    a_next = torch.roll(a, shifts=-1, dims=0)
    ta = h2 - 2.0 * h1 + a
    tb = a_next - 2.0 * h2 + h1
    c2 = 6.0 * (1.0 - t)[None, :, None] * ta[:, None, :] + 6.0 * t[None, :, None] * tb[:, None, :]
    return c2.pow(2).sum(dim=-1).mean()


def joint_loss(a: torch.Tensor, h1: torch.Tensor, h2: torch.Tensor):
    out_t = 3.0 * (a - torch.roll(h2, shifts=1, dims=0))
    in_t = 3.0 * (h1 - a)
    out_n = out_t / (out_t.norm(dim=1, keepdim=True) + 1e-8)
    in_n = in_t / (in_t.norm(dim=1, keepdim=True) + 1e-8)
    return ((out_n - in_n).pow(2).sum(dim=1)).mean()


@dataclass
class StripeFit:
    seg_n: int
    anchors: np.ndarray
    h1: np.ndarray
    h2: np.ndarray
    mask_loss: float
    curv: float
    joint: float
    score: float


def fit_one_stripe(target_norm: np.ndarray, device: str) -> StripeFit:
    lam_curv = 9e-4
    lam_joint = 4e-3
    lam_handle = 1.2e-2
    lam_seg = 6e-4

    best = None
    for seg_n in range(2, 5):
        k = 42
        tgt_np = resample_closed_polyline(target_norm, n=seg_n * k)
        tgt = torch.tensor(tgt_np, dtype=torch.float32, device=device)

        a, h1, h2 = init_chain(target_norm, seg_n)
        a, h1, h2 = a.to(device), h1.to(device), h2.to(device)
        opt = torch.optim.Adam([a, h1, h2], lr=0.03)

        for _ in range(420):
            opt.zero_grad()
            curve = sample_chain(a, h1, h2, k=k)
            l_mask = ordered_mse(curve, tgt)
            l_curv = curvature_loss(a, h1, h2, k=18)
            l_joint = joint_loss(a, h1, h2)
            a_next = torch.roll(a, shifts=-1, dims=0)
            l_handle = ((h1 - a).pow(2).sum(dim=1).mean() + (h2 - a_next).pow(2).sum(dim=1).mean())
            loss = l_mask + lam_curv * l_curv + lam_joint * l_joint + lam_handle * l_handle
            loss.backward()
            opt.step()
            with torch.no_grad():
                a.clamp_(0.0, 1.0)
                h1.clamp_(0.0, 1.0)
                h2.clamp_(0.0, 1.0)

        with torch.no_grad():
            curve = sample_chain(a, h1, h2, k=k)
            l_mask = float(ordered_mse(curve, tgt).item())
            l_curv = float(curvature_loss(a, h1, h2, k=26).item())
            l_joint = float(joint_loss(a, h1, h2).item())
            a_next = torch.roll(a, shifts=-1, dims=0)
            l_handle = float(
                ((h1 - a).pow(2).sum(dim=1).mean() + (h2 - a_next).pow(2).sum(dim=1).mean()).item()
            )
            score = l_mask + lam_curv * l_curv + lam_joint * l_joint + lam_handle * l_handle + lam_seg * seg_n

        cand = StripeFit(
            seg_n=seg_n,
            anchors=a.detach().cpu().numpy(),
            h1=h1.detach().cpu().numpy(),
            h2=h2.detach().cpu().numpy(),
            mask_loss=l_mask,
            curv=l_curv,
            joint=l_joint,
            score=float(score),
        )
        if best is None or cand.score < best.score:
            best = cand
    return best


def fmt_float(v: float) -> str:
    s = f'{v:.2f}'.rstrip('0').rstrip('.')
    if s == '-0':
        s = '0'
    return s


def chain_to_svg_subpath(fit: StripeFit, px_w: int, px_h: int, viewbox):
    vx, vy, vw, vh = viewbox
    scale = min(px_w / vw, px_h / vh)
    render_w = vw * scale
    render_h = vh * scale
    off_x = (px_w - render_w) * 0.5
    off_y = (px_h - render_h) * 0.5

    def px_to_view_xy(xn, yn):
        # Normalize -> pixel -> viewBox coordinates with preserveAspectRatio=xMidYMid meet.
        px = float(xn) * (px_w - 1)
        py = float(yn) * (px_h - 1)
        x = vx + (px - off_x) / max(scale, 1e-8)
        y = vy + (py - off_y) / max(scale, 1e-8)
        return x, y

    a = fit.anchors
    h1 = fit.h1
    h2 = fit.h2
    n = len(a)
    x0, y0 = px_to_view_xy(a[0, 0], a[0, 1])
    parts = [f'M{fmt_float(x0)} {fmt_float(y0)}']
    for i in range(n):
        j = (i + 1) % n
        x1, y1 = px_to_view_xy(h1[i, 0], h1[i, 1])
        x2, y2 = px_to_view_xy(h2[i, 0], h2[i, 1])
        x3, y3 = px_to_view_xy(a[j, 0], a[j, 1])
        parts.append(
            'C'
            + f'{fmt_float(x1)} {fmt_float(y1)} '
            + f'{fmt_float(x2)} {fmt_float(y2)} '
            + f'{fmt_float(x3)} {fmt_float(y3)}'
        )
    parts.append('Z')
    return ''.join(parts)


def approx_tokens_path(d: str) -> int:
    nums = re.findall(r'[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?', d)
    cmds = re.findall(r'[MmLlHhVvCcSsQqTtAaZz]', d)
    return len(nums) + len(cmds)


def iou_mask(a: np.ndarray, b: np.ndarray) -> float:
    inter = np.logical_and(a, b).sum()
    union = np.logical_or(a, b).sum()
    return float(inter / max(union, 1))


def run():
    svg_text = INPUT_SVG.read_text()
    viewbox, canvas_w, canvas_h = parse_svg_meta(svg_text)
    orig_d = extract_path_d(svg_text)

    os_run(f'rsvg-convert {INPUT_SVG} -o {ORIG_PNG}')
    orig_img = np.array(Image.open(ORIG_PNG).convert('RGB'))
    h, w = orig_img.shape[:2]
    orig_mask = orig_img.mean(axis=2) > 180

    comps = split_components(orig_mask, area_min=120)
    print(f'components={len(comps)}')

    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    print('device:', device)

    stripe_fits = []
    for idx, (_, comp, area, cx, cy) in enumerate(comps):
        hull = convex_boundary_points(comp)
        n_target = int(np.clip(max(len(hull) * 4, 120), 120, 260))
        tgt_px = resample_closed_polyline(hull, n=n_target)
        # normalize to [0,1] in image coordinate (y down), consistent with SVG
        tgt_norm = tgt_px.copy()
        tgt_norm[:, 0] = tgt_norm[:, 0] / max(w - 1, 1)
        tgt_norm[:, 1] = tgt_norm[:, 1] / max(h - 1, 1)
        fit = fit_one_stripe(tgt_norm, device=device)
        stripe_fits.append((fit, area, cx, cy))
        print(
            f'[{idx + 1:02d}/{len(comps)}] area={area:4d} center=({cx:.1f},{cy:.1f}) '
            f'seg={fit.seg_n} mask={fit.mask_loss:.6g} curv={fit.curv:.4f} score={fit.score:.6g}'
        )

    subpaths = [chain_to_svg_subpath(fit, w, h, viewbox) for fit, _, _, _ in stripe_fits]
    new_d = ''.join(subpaths)

    out_svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{canvas_w}" height="{canvas_h}" '
        f'viewBox="{viewbox[0]} {viewbox[1]} {viewbox[2]} {viewbox[3]}">\n'
        f'  <rect x="0" y="0" width="100%" height="100%" fill="#1e1e1e"/>\n'
        f'  <path d="{new_d}" fill="#FFFFFF" fill-rule="evenodd"/>\n'
        '</svg>\n'
    )
    OUT_SVG.write_text(out_svg)

    os_run(f'rsvg-convert {OUT_SVG} -o {OUT_PNG}')
    fit_img = np.array(Image.open(OUT_PNG).convert('RGB'))
    fit_mask = fit_img.mean(axis=2) > 180

    iou = iou_mask(orig_mask, fit_mask)

    orig_svg_chars = len(svg_text)
    new_svg_chars = len(out_svg)
    orig_d_chars = len(orig_d)
    new_d_chars = len(new_d)

    orig_tokens = approx_tokens_path(orig_d)
    new_tokens = approx_tokens_path(new_d)

    total_segments = sum(fit.seg_n for fit, _, _, _ in stripe_fits)
    avg_seg = total_segments / max(len(stripe_fits), 1)

    report = []
    report.append('=== PhysUI Stripe Fitting Compression Report ===')
    report.append(f'num_stripes: {len(stripe_fits)}')
    report.append(f'total_segments(PhysUI): {total_segments} (avg {avg_seg:.2f}/stripe)')
    report.append(f'IoU(rendered masks): {iou:.6f}')
    report.append('--- length ---')
    report.append(f'original_svg_chars: {orig_svg_chars}')
    report.append(f'physui_svg_chars:   {new_svg_chars}')
    report.append(f'compression(svg):   {new_svg_chars / orig_svg_chars:.3f}x')
    report.append(f'original_d_chars:   {orig_d_chars}')
    report.append(f'physui_d_chars:     {new_d_chars}')
    report.append(f'compression(d):     {new_d_chars / orig_d_chars:.3f}x')
    report.append('--- token-like count (commands + numbers) ---')
    report.append(f'original_d_tokens:  {orig_tokens}')
    report.append(f'physui_d_tokens:    {new_tokens}')
    report.append(f'compression(tokens): {new_tokens / orig_tokens:.3f}x')

    REPORT_PATH.write_text('\n'.join(report) + '\n')
    print('\n'.join(report))

    # visualization
    fig = plt.figure(figsize=(16, 10))
    gs = fig.add_gridspec(2, 2)

    ax0 = fig.add_subplot(gs[0, 0])
    ax0.imshow(orig_img)
    ax0.set_title('Original Render')
    ax0.axis('off')

    ax1 = fig.add_subplot(gs[0, 1])
    ax1.imshow(fit_img)
    ax1.set_title('PhysUI Stripe Fit Render')
    ax1.axis('off')

    ax2 = fig.add_subplot(gs[1, 0])
    diff = np.abs(orig_mask.astype(np.float32) - fit_mask.astype(np.float32))
    ax2.imshow(diff, cmap='magma')
    ax2.set_title(f'Binary Difference Map (IoU={iou:.4f})')
    ax2.axis('off')

    ax3 = fig.add_subplot(gs[1, 1])
    labels = ['SVG chars', 'Path chars', 'Path tokens']
    orig_vals = [orig_svg_chars, orig_d_chars, orig_tokens]
    new_vals = [new_svg_chars, new_d_chars, new_tokens]
    x = np.arange(len(labels))
    bw = 0.36
    ax3.bar(x - bw / 2, orig_vals, width=bw, label='Original')
    ax3.bar(x + bw / 2, new_vals, width=bw, label='PhysUI')
    ax3.set_xticks(x)
    ax3.set_xticklabels(labels)
    ax3.set_title('Compression Comparison')
    ax3.grid(alpha=0.2, axis='y')
    ax3.legend()

    fig.suptitle('PhysUI: Stripe-Level Fitting and SVG Compression', fontsize=18)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(FIG_PATH, dpi=180)
    print(f'Saved figure: {FIG_PATH}')
    print(f'Saved fitted svg: {OUT_SVG}')


if __name__ == '__main__':
    run()
