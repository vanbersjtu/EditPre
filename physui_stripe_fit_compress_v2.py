import re
from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import torch
from PIL import Image
from scipy import ndimage as ndi


INPUT_SVG = Path('/Users/xiaoxiaobo/user_path.svg')
ORIG_PNG = Path('/Users/xiaoxiaobo/user_path_render.png')
OUT_SVG = Path('/Users/xiaoxiaobo/user_path_physui_fit_v2.svg')
OUT_PNG = Path('/Users/xiaoxiaobo/user_path_physui_fit_v2.png')
FIG_PATH = Path('/Users/xiaoxiaobo/user_path_physui_fit_v2_comparison.png')
REPORT_PATH = Path('/Users/xiaoxiaobo/user_path_physui_fit_v2_report.txt')


def os_run(cmd: str) -> None:
    import os

    code = os.system(cmd)
    if code != 0:
        raise RuntimeError(f'Command failed: {cmd}')


def parse_svg_meta(svg_text: str):
    vb = re.search(r'viewBox="([^"]+)"', svg_text)
    if vb is None:
        raise RuntimeError('viewBox not found')
    viewbox = [float(x) for x in vb.group(1).split()]
    w = re.search(r'width="([0-9.]+)"', svg_text)
    h = re.search(r'height="([0-9.]+)"', svg_text)
    width = int(float(w.group(1))) if w else 800
    height = int(float(h.group(1))) if h else 800
    return viewbox, width, height


def extract_path_d(svg_text: str) -> str:
    m = re.search(r'<path[^>]*d="([^"]+)"', svg_text, flags=re.S)
    if m is None:
        raise RuntimeError('No path d found')
    return m.group(1)


def split_components(mask: np.ndarray, area_min: int = 120):
    lab, num = ndi.label(mask)
    if num == 0:
        return []
    areas = np.bincount(lab.ravel())
    areas[0] = 0
    ids = [i for i in range(1, num + 1) if areas[i] >= area_min]
    items = []
    for cid in ids:
        comp = (lab == cid)
        ys, xs = np.where(comp)
        items.append((cid, comp, int(areas[cid]), float(xs.mean()), float(ys.mean())))
    items.sort(key=lambda x: (x[3], x[4]))
    return items


def extract_ordered_boundary(comp: np.ndarray) -> np.ndarray:
    eroded = ndi.binary_erosion(comp, structure=np.ones((3, 3), dtype=bool))
    boundary = comp & (~eroded)
    ys, xs = np.where(boundary)
    pts = np.stack([xs, ys], axis=1).astype(np.float64)
    ctr = pts.mean(axis=0)
    ang = np.arctan2(pts[:, 1] - ctr[1], pts[:, 0] - ctr[0])
    return pts[np.argsort(ang)]


def farthest_pair_indices(pts: np.ndarray):
    d2 = ((pts[:, None, :] - pts[None, :, :]) ** 2).sum(axis=2)
    i, j = np.unravel_index(np.argmax(d2), d2.shape)
    return (i, j) if i < j else (j, i)


def circular_index_distance(i: int, j: int, n: int) -> int:
    d = abs(i - j)
    return min(d, n - d)


def detect_corner_indices(boundary: np.ndarray, pick: int = 2, neigh: int = 4):
    n = len(boundary)
    if n < max(16, 2 * neigh + 1):
        i, j = farthest_pair_indices(boundary)
        return [i, j]

    scores = np.zeros(n, dtype=np.float64)
    for i in range(n):
        p_prev = boundary[(i - neigh) % n]
        p = boundary[i]
        p_next = boundary[(i + neigh) % n]
        v1 = p - p_prev
        v2 = p_next - p
        n1 = np.linalg.norm(v1)
        n2 = np.linalg.norm(v2)
        if n1 < 1e-10 or n2 < 1e-10:
            continue
        cosang = np.clip(np.dot(v1, v2) / (n1 * n2), -1.0, 1.0)
        angle = np.arccos(cosang)
        # sharper corner => larger score
        scores[i] = np.pi - angle

    order = np.argsort(-scores)
    min_sep = max(6, int(0.18 * n))
    chosen = []
    for idx in order:
        if all(circular_index_distance(int(idx), int(c), n) >= min_sep for c in chosen):
            chosen.append(int(idx))
        if len(chosen) >= pick:
            break

    if len(chosen) < 2:
        i, j = farthest_pair_indices(boundary)
        return [i, j]
    return chosen[:pick]


def normalize_points(points_px: np.ndarray, px_w: int, px_h: int) -> np.ndarray:
    pts = points_px.copy().astype(np.float64)
    pts[:, 0] = pts[:, 0] / max(px_w - 1, 1)
    pts[:, 1] = pts[:, 1] / max(px_h - 1, 1)
    return pts


def resample_open_polyline(points: np.ndarray, n: int) -> np.ndarray:
    pts = np.asarray(points, dtype=np.float64)
    seg = pts[1:] - pts[:-1]
    seg_len = np.linalg.norm(seg, axis=1)
    total = float(seg_len.sum())
    if total < 1e-10:
        return np.repeat(pts[:1], n, axis=0)

    cum = np.concatenate([[0.0], np.cumsum(seg_len)])
    ts = np.linspace(0.0, total, n)
    out = []
    j = 0
    for t in ts:
        while j < len(seg_len) - 1 and cum[j + 1] < t:
            j += 1
        u = 0.0 if seg_len[j] < 1e-10 else (t - cum[j]) / seg_len[j]
        out.append(pts[j] * (1.0 - u) + pts[j + 1] * u)
    return np.asarray(out, dtype=np.float64)


def resample_closed_polyline(points: np.ndarray, n: int) -> np.ndarray:
    pts = np.asarray(points, dtype=np.float64)
    if not np.allclose(pts[0], pts[-1]):
        pts = np.vstack([pts, pts[0]])
    seg = pts[1:] - pts[:-1]
    seg_len = np.linalg.norm(seg, axis=1)
    total = float(seg_len.sum())
    if total < 1e-10:
        return np.repeat(pts[:1], n, axis=0)

    cum = np.concatenate([[0.0], np.cumsum(seg_len)])
    ts = np.linspace(0.0, total, n + 1)[:-1]
    out = []
    j = 0
    for t in ts:
        while j < len(seg_len) - 1 and cum[j + 1] < t:
            j += 1
        u = 0.0 if seg_len[j] < 1e-10 else (t - cum[j]) / seg_len[j]
        out.append(pts[j] * (1.0 - u) + pts[j + 1] * u)
    return np.asarray(out, dtype=np.float64)


def fit_cubic_handles_fixed(points: np.ndarray, p0: np.ndarray, p3: np.ndarray, alpha: float):
    pts = np.asarray(points, dtype=np.float64)
    d = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    s = np.concatenate([[0.0], np.cumsum(d)])
    t = s / max(s[-1], 1e-10)

    a1 = 3.0 * (1.0 - t) ** 2 * t
    a2 = 3.0 * (1.0 - t) * t**2
    rhs = pts - ((1.0 - t) ** 3)[:, None] * p0 - (t**3)[:, None] * p3
    A = np.stack([a1, a2], axis=1)

    x = np.linalg.lstsq(A, rhs[:, 0], rcond=None)[0]
    y = np.linalg.lstsq(A, rhs[:, 1], rcond=None)[0]

    p1 = np.array([x[0], y[0]], dtype=np.float64)
    p2 = np.array([x[1], y[1]], dtype=np.float64)

    # beam prior: keep handles from exploding
    p1 = p0 + alpha * (p1 - p0)
    p2 = p3 + alpha * (p2 - p3)
    return p1, p2


@dataclass
class CubicSeg:
    p0: np.ndarray
    p1: np.ndarray
    p2: np.ndarray
    p3: np.ndarray


@dataclass
class StripeFitAdaptive:
    seg_n: int
    segments: list
    mse: float
    curv: float
    kink: float
    score: float


def build_arcs_from_starts(boundary: np.ndarray, starts: np.ndarray):
    starts = np.array(sorted(int(x) for x in starts), dtype=np.int64)
    n = len(boundary)
    arcs = []
    for k in range(len(starts)):
        s = starts[k]
        e = starts[(k + 1) % len(starts)]
        if s < e:
            arc = boundary[s : e + 1]
        else:
            arc = np.vstack([boundary[s:], boundary[: e + 1]])
        arcs.append(arc)
    return arcs


def choose_start_indices(boundary: np.ndarray, seg_n: int, corner_indices: list[int] | None = None):
    n = len(boundary)
    corner_indices = corner_indices or []
    if seg_n == 2:
        if len(corner_indices) >= 2:
            i, j = corner_indices[:2]
            if i == j:
                i, j = farthest_pair_indices(boundary)
        else:
            i, j = farthest_pair_indices(boundary)
        return np.array(sorted([i, j]), dtype=np.int64)

    if seg_n == 3:
        if len(corner_indices) >= 2:
            i, j = corner_indices[:2]
        else:
            i, j = farthest_pair_indices(boundary)
        i, j = sorted([int(i), int(j)])
        span_ij = j - i
        span_ji = n - span_ij
        if span_ij >= span_ji:
            m = (i + span_ij // 2) % n
        else:
            m = (j + span_ji // 2) % n
        starts = sorted({i, j, int(m)})
        if len(starts) < 3:
            starts = sorted({i, j, (j + 1) % n})
        return np.array(starts[:3], dtype=np.int64)

    idx = np.linspace(0, n, seg_n + 1)
    idx = np.round(idx).astype(int)
    starts = (idx[:-1] % n).astype(np.int64)
    return starts


def fit_chain_from_boundary(boundary: np.ndarray, seg_n: int, alpha: float = 0.96, corner_indices: list[int] | None = None):
    starts = choose_start_indices(boundary, seg_n=seg_n, corner_indices=corner_indices)
    arcs = build_arcs_from_starts(boundary, starts)
    corner_set = set(corner_indices or [])
    corner_mask = np.array([int(s) in corner_set for s in starts], dtype=bool)

    segs = []
    for arc in arcs:
        p0 = arc[0]
        p3 = arc[-1]
        sampled = resample_open_polyline(arc, n=120)
        p1, p2 = fit_cubic_handles_fixed(sampled, p0=p0, p3=p3, alpha=alpha)
        segs.append(CubicSeg(p0=p0, p1=p1, p2=p2, p3=p3))
    return segs, corner_mask, starts


def segs_to_param_arrays(segs: list):
    anchors = np.stack([s.p0 for s in segs], axis=0).astype(np.float32)
    h1 = np.stack([s.p1 for s in segs], axis=0).astype(np.float32)
    h2 = np.stack([s.p2 for s in segs], axis=0).astype(np.float32)
    return anchors, h1, h2


def params_to_segs(anchors: np.ndarray, h1: np.ndarray, h2: np.ndarray):
    n = len(anchors)
    segs = []
    for i in range(n):
        segs.append(
            CubicSeg(
                p0=anchors[i].astype(np.float64),
                p1=h1[i].astype(np.float64),
                p2=h2[i].astype(np.float64),
                p3=anchors[(i + 1) % n].astype(np.float64),
            )
        )
    return segs


def sample_chain_torch(anchors: torch.Tensor, h1: torch.Tensor, h2: torch.Tensor, samples_per_seg: int):
    t = torch.linspace(0.0, 1.0, samples_per_seg + 1, device=anchors.device)[:-1]
    omt = 1.0 - t
    a0 = anchors
    a1 = torch.roll(anchors, shifts=-1, dims=0)
    c = (
        (omt**3)[None, :, None] * a0[:, None, :]
        + 3.0 * (omt**2 * t)[None, :, None] * h1[:, None, :]
        + 3.0 * (omt * t**2)[None, :, None] * h2[:, None, :]
        + (t**3)[None, :, None] * a1[:, None, :]
    )
    return c


def chain_curvature_torch(anchors: torch.Tensor, h1: torch.Tensor, h2: torch.Tensor, samples_per_seg: int = 24):
    t = torch.linspace(0.0, 1.0, samples_per_seg, device=anchors.device)
    a0 = anchors
    a1 = torch.roll(anchors, shifts=-1, dims=0)
    term_a = h2 - 2.0 * h1 + a0
    term_b = a1 - 2.0 * h2 + h1
    c2 = 6.0 * (1.0 - t)[None, :, None] * term_a[:, None, :] + 6.0 * t[None, :, None] * term_b[:, None, :]
    return c2.pow(2).sum(dim=-1).mean()


def chain_joint_kink_torch(anchors: torch.Tensor, h1: torch.Tensor, h2: torch.Tensor, joint_weights: torch.Tensor | None = None):
    out_t = 3.0 * (anchors - torch.roll(h2, shifts=1, dims=0))
    in_t = 3.0 * (h1 - anchors)
    out_n = out_t / (out_t.norm(dim=1, keepdim=True) + 1e-8)
    in_n = in_t / (in_t.norm(dim=1, keepdim=True) + 1e-8)
    vals = ((out_n - in_n).pow(2).sum(dim=1))
    if joint_weights is None:
        return vals.mean()
    return (vals * joint_weights).sum() / (joint_weights.sum() + 1e-8)


def ordered_mse_torch(curve: torch.Tensor, target: torch.Tensor):
    return (curve - target).pow(2).sum(dim=1).mean()


def nonlinear_refine_chain(
    init_segs: list,
    boundary_norm: np.ndarray,
    start_indices: np.ndarray,
    corner_mask: np.ndarray,
    steps: int = 90,
    lr: float = 0.006,
    optimize_anchors: bool = False,
):
    seg_n = len(init_segs)
    samples_per_seg = 64
    arcs = build_arcs_from_starts(boundary_norm, start_indices)
    target_np = np.stack([resample_open_polyline(arc, n=samples_per_seg) for arc in arcs], axis=0).astype(np.float32)

    a0_np, h1_np, h2_np = segs_to_param_arrays(init_segs)
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    anchors = torch.tensor(a0_np, dtype=torch.float32, device=device, requires_grad=optimize_anchors)
    h1 = torch.tensor(h1_np, dtype=torch.float32, device=device, requires_grad=True)
    h2 = torch.tensor(h2_np, dtype=torch.float32, device=device, requires_grad=True)
    target = torch.tensor(target_np, dtype=torch.float32, device=device)
    anchors_init = torch.tensor(a0_np, dtype=torch.float32, device=device)
    corner_mask_t = torch.tensor(corner_mask, dtype=torch.bool, device=device)
    # True corners are allowed to keep kink; smooth joints are penalized more.
    joint_weights = torch.where(corner_mask_t, torch.full_like(corner_mask_t, 0.12, dtype=torch.float32), torch.ones_like(corner_mask_t, dtype=torch.float32))

    params = [h1, h2] if not optimize_anchors else [anchors, h1, h2]
    opt = torch.optim.Adam(params, lr=lr)

    lam_curv = 1e-5
    lam_kink = 1e-5
    lam_anchor = 2e-2
    lam_handle = 8e-4

    for _ in range(steps):
        opt.zero_grad()
        curve = sample_chain_torch(anchors, h1, h2, samples_per_seg=samples_per_seg)
        l_mse = (curve - target).pow(2).sum(dim=-1).mean()
        l_curv = chain_curvature_torch(anchors, h1, h2, samples_per_seg=24)
        l_kink = chain_joint_kink_torch(anchors, h1, h2, joint_weights=joint_weights)

        a_next = torch.roll(anchors, shifts=-1, dims=0)
        chord = (a_next - anchors).norm(dim=1) + 1e-8
        handle_ratio = ((h1 - anchors).norm(dim=1) + (a_next - h2).norm(dim=1)) / chord
        l_handle = torch.relu(handle_ratio - 2.2).pow(2).mean()
        l_anchor = (anchors - anchors_init).pow(2).sum(dim=1).mean() if optimize_anchors else torch.tensor(0.0, device=device)

        loss = l_mse + lam_curv * l_curv + lam_kink * l_kink + lam_anchor * l_anchor + lam_handle * l_handle
        loss.backward()
        opt.step()

        with torch.no_grad():
            if optimize_anchors:
                anchors.clamp_(0.0, 1.0)
            h1.clamp_(0.0, 1.0)
            h2.clamp_(0.0, 1.0)

    with torch.no_grad():
        curve = sample_chain_torch(anchors, h1, h2, samples_per_seg=samples_per_seg)
        mse = float((curve - target).pow(2).sum(dim=-1).mean().item())
        curv = float(chain_curvature_torch(anchors, h1, h2, samples_per_seg=32).item())
        kink = float(chain_joint_kink_torch(anchors, h1, h2, joint_weights=joint_weights).item())

    segs = params_to_segs(
        anchors.detach().cpu().numpy(),
        h1.detach().cpu().numpy(),
        h2.detach().cpu().numpy(),
    )
    return segs, mse, curv, kink


def sample_cubic(seg: CubicSeg, samples: int):
    t = np.linspace(0.0, 1.0, samples + 1)[:-1]
    omt = 1.0 - t
    c = (
        (omt**3)[:, None] * seg.p0[None, :]
        + 3.0 * (omt**2 * t)[:, None] * seg.p1[None, :]
        + 3.0 * (omt * t**2)[:, None] * seg.p2[None, :]
        + (t**3)[:, None] * seg.p3[None, :]
    )
    return c


def chain_samples(segs: list, samples_per_seg: int = 60) -> np.ndarray:
    parts = [sample_cubic(s, samples=samples_per_seg) for s in segs]
    return np.vstack(parts)


def cubic_second_derivative(seg: CubicSeg, t: np.ndarray) -> np.ndarray:
    ta = seg.p2 - 2.0 * seg.p1 + seg.p0
    tb = seg.p3 - 2.0 * seg.p2 + seg.p1
    return 6.0 * (1.0 - t)[:, None] * ta[None, :] + 6.0 * t[:, None] * tb[None, :]


def chain_curvature(segs: list) -> float:
    t = np.linspace(0.0, 1.0, 32)
    vals = []
    for s in segs:
        c2 = cubic_second_derivative(s, t)
        vals.append(np.sum(c2 * c2, axis=1))
    arr = np.concatenate(vals)
    return float(arr.mean())


def chain_joint_kink(segs: list) -> float:
    vals = []
    n = len(segs)
    for i in range(n):
        a = segs[i]
        b = segs[(i + 1) % n]
        out_t = 3.0 * (a.p3 - a.p2)
        in_t = 3.0 * (b.p1 - b.p0)
        out_n = out_t / (np.linalg.norm(out_t) + 1e-10)
        in_n = in_t / (np.linalg.norm(in_t) + 1e-10)
        vals.append(np.sum((out_n - in_n) ** 2))
    return float(np.mean(vals))


def ordered_mse(curve: np.ndarray, target: np.ndarray) -> float:
    return float(np.mean(np.sum((curve - target) ** 2, axis=1)))


def eval_chain_metrics(segs: list, boundary_norm: np.ndarray, starts: np.ndarray):
    arcs = build_arcs_from_starts(boundary_norm, starts)
    target = np.stack([resample_open_polyline(arc, n=64) for arc in arcs], axis=0).reshape(-1, 2)
    curve = chain_samples(segs, samples_per_seg=64)
    mse = ordered_mse(curve, target)
    curv = chain_curvature(segs)
    kink = chain_joint_kink(segs)
    return mse, curv, kink


def fit_stripe_adaptive(boundary_norm: np.ndarray, seg_min: int = 2, seg_max: int = 3):
    lam_curv = 6e-4
    lam_kink = 8e-4
    lam_seg = 9.0e-4

    best = None
    records = []
    corner_indices = []
    for seg_n in range(seg_min, seg_max + 1):
        init_segs, corner_mask, starts = fit_chain_from_boundary(
            boundary_norm, seg_n=seg_n, alpha=0.96, corner_indices=corner_indices
        )
        ls_mse, ls_curv, ls_kink = eval_chain_metrics(init_segs, boundary_norm, starts)
        ls_score = ls_mse + lam_curv * ls_curv + lam_kink * ls_kink + lam_seg * seg_n

        segs_bp, mse_bp, curv_bp, kink_bp = nonlinear_refine_chain(
            init_segs,
            boundary_norm,
            start_indices=starts,
            corner_mask=corner_mask,
            steps=90,
            lr=0.006,
            optimize_anchors=False,
        )
        bp_score = mse_bp + lam_curv * curv_bp + lam_kink * kink_bp + lam_seg * seg_n

        if bp_score <= ls_score:
            segs, mse, curv, kink, score = segs_bp, mse_bp, curv_bp, kink_bp, bp_score
        else:
            segs, mse, curv, kink, score = init_segs, ls_mse, ls_curv, ls_kink, ls_score

        rec = StripeFitAdaptive(seg_n=seg_n, segments=segs, mse=mse, curv=curv, kink=kink, score=score)
        records.append(rec)
        if best is None or rec.score < best.score:
            best = rec
    return best, records


def norm_to_view(pt_norm: np.ndarray, viewbox, px_w: int, px_h: int):
    vx, vy, vw, vh = viewbox
    scale = min(px_w / vw, px_h / vh)
    rw = vw * scale
    rh = vh * scale
    off_x = (px_w - rw) * 0.5
    off_y = (px_h - rh) * 0.5

    px = float(pt_norm[0]) * (px_w - 1)
    py = float(pt_norm[1]) * (px_h - 1)

    x = vx + (px - off_x) / max(scale, 1e-8)
    y = vy + (py - off_y) / max(scale, 1e-8)
    return x, y


def fmt(v: float) -> str:
    s = f'{v:.2f}'.rstrip('0').rstrip('.')
    if s in ('', '-0'):
        return '0'
    return s


def stripe_to_path_d(fit: StripeFitAdaptive, viewbox, px_w: int, px_h: int):
    segs = fit.segments
    x0, y0 = norm_to_view(segs[0].p0, viewbox, px_w, px_h)
    parts = [f'M{fmt(x0)} {fmt(y0)}']
    for s in segs:
        x1, y1 = norm_to_view(s.p1, viewbox, px_w, px_h)
        x2, y2 = norm_to_view(s.p2, viewbox, px_w, px_h)
        x3, y3 = norm_to_view(s.p3, viewbox, px_w, px_h)
        parts.append(f'C{fmt(x1)} {fmt(y1)} {fmt(x2)} {fmt(y2)} {fmt(x3)} {fmt(y3)}')
    parts.append('Z')
    return ''.join(parts)


def approx_tokens_path(d: str) -> int:
    nums = re.findall(r'[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?', d)
    cmds = re.findall(r'[MmLlHhVvCcSsQqTtAaZz]', d)
    return len(nums) + len(cmds)


def iou(a: np.ndarray, b: np.ndarray) -> float:
    inter = np.logical_and(a, b).sum()
    union = np.logical_or(a, b).sum()
    return float(inter / max(union, 1))


def main():
    svg_text = INPUT_SVG.read_text()
    viewbox, canvas_w, canvas_h = parse_svg_meta(svg_text)
    orig_d = extract_path_d(svg_text)

    os_run(f'rsvg-convert {INPUT_SVG} -o {ORIG_PNG}')
    orig_img = np.array(Image.open(ORIG_PNG).convert('RGB'))
    h, w = orig_img.shape[:2]
    orig_mask = orig_img.mean(axis=2) > 180

    comps = split_components(orig_mask, area_min=120)
    print(f'num_components={len(comps)}')

    stripe_fits = []
    for idx, (_, comp, area, cx, cy) in enumerate(comps):
        boundary_px = extract_ordered_boundary(comp)
        boundary_norm = normalize_points(boundary_px, px_w=w, px_h=h)
        best, recs = fit_stripe_adaptive(boundary_norm, seg_min=2, seg_max=3)
        stripe_fits.append((best, recs, area, cx, cy))
        print(
            f'[{idx + 1:02d}/{len(comps)}] area={area:4d} center=({cx:.1f},{cy:.1f}) '
            f'best_seg={best.seg_n} mse={best.mse:.6g} curv={best.curv:.4f} score={best.score:.6g}'
        )

    subpaths = [stripe_to_path_d(best, viewbox, w, h) for best, _, _, _, _ in stripe_fits]
    new_d = ''.join(subpaths)

    out_svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{canvas_w}" height="{canvas_h}" '
        f'viewBox="{viewbox[0]} {viewbox[1]} {viewbox[2]} {viewbox[3]}">\n'
        '  <rect x="0" y="0" width="100%" height="100%" fill="#1e1e1e"/>\n'
        f'  <path d="{new_d}" fill="#FFFFFF"/>\n'
        '</svg>\n'
    )
    OUT_SVG.write_text(out_svg)

    os_run(f'rsvg-convert {OUT_SVG} -o {OUT_PNG}')
    fit_img = np.array(Image.open(OUT_PNG).convert('RGB'))
    fit_mask = fit_img.mean(axis=2) > 180

    m_iou = iou(orig_mask, fit_mask)

    orig_svg_chars = len(svg_text)
    new_svg_chars = len(out_svg)
    orig_d_chars = len(orig_d)
    new_d_chars = len(new_d)

    orig_tokens = approx_tokens_path(orig_d)
    new_tokens = approx_tokens_path(new_d)

    total_segments = int(sum(best.seg_n for best, _, _, _, _ in stripe_fits))
    seg_hist = {}
    for best, _, _, _, _ in stripe_fits:
        seg_hist[best.seg_n] = seg_hist.get(best.seg_n, 0) + 1

    report_lines = [
        '=== PhysUI Stripe Fitting v2 Adaptive Report ===',
        f'num_stripes: {len(stripe_fits)}',
        f'total_segments: {total_segments}',
        f'segment_hist: {seg_hist}',
        f'IoU(rendered masks): {m_iou:.6f}',
        '--- length ---',
        f'original_svg_chars: {orig_svg_chars}',
        f'physui_svg_chars:   {new_svg_chars}',
        f'compression(svg):   {new_svg_chars / orig_svg_chars:.3f}x',
        f'original_d_chars:   {orig_d_chars}',
        f'physui_d_chars:     {new_d_chars}',
        f'compression(d):     {new_d_chars / orig_d_chars:.3f}x',
        '--- token-like count (commands + numbers) ---',
        f'original_d_tokens:  {orig_tokens}',
        f'physui_d_tokens:    {new_tokens}',
        f'compression(tokens): {new_tokens / orig_tokens:.3f}x',
    ]
    REPORT_PATH.write_text('\n'.join(report_lines) + '\n')
    print('\n'.join(report_lines))

    fig = plt.figure(figsize=(16, 10))
    gs = fig.add_gridspec(2, 2)

    ax0 = fig.add_subplot(gs[0, 0])
    ax0.imshow(orig_img)
    ax0.set_title('Original Render')
    ax0.axis('off')

    ax1 = fig.add_subplot(gs[0, 1])
    ax1.imshow(fit_img)
    ax1.set_title('PhysUI Adaptive Stripe Fit Render')
    ax1.axis('off')

    ax2 = fig.add_subplot(gs[1, 0])
    diff = np.abs(orig_mask.astype(np.float32) - fit_mask.astype(np.float32))
    ax2.imshow(diff, cmap='magma')
    ax2.set_title(f'Binary Difference (IoU={m_iou:.4f})')
    ax2.axis('off')

    ax3 = fig.add_subplot(gs[1, 1])
    labels = ['SVG chars', 'Path chars', 'Path tokens']
    orig_vals = [orig_svg_chars, orig_d_chars, orig_tokens]
    new_vals = [new_svg_chars, new_d_chars, new_tokens]
    x = np.arange(len(labels))
    bw = 0.36
    ax3.bar(x - bw / 2, orig_vals, width=bw, label='Original')
    ax3.bar(x + bw / 2, new_vals, width=bw, label='PhysUI adaptive')
    ax3.set_xticks(x)
    ax3.set_xticklabels(labels)
    ax3.set_title('Compression Comparison')
    ax3.grid(alpha=0.2, axis='y')
    ax3.legend()

    fig.suptitle('PhysUI v2: Adaptive Stripe Fitting + Token Compression', fontsize=18)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(FIG_PATH, dpi=180)

    print(f'Saved figure: {FIG_PATH}')
    print(f'Saved svg: {OUT_SVG}')
    print(f'Saved report: {REPORT_PATH}')


if __name__ == '__main__':
    main()
