import argparse
import re
from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt


NUM_RE = re.compile(r'[-+]?(?:\d+\.\d+|\d+|\.\d+)(?:[eE][-+]?\d+)?')
CMD_RE = re.compile(r'[MmLlHhVvCcSsQqTtAaZz]')
PATH_D_RE = re.compile(r'(<path\b[^>]*\bd=")([^"]*)(")', flags=re.I | re.S)
BASE64_HREF_RE = re.compile(r'((?:xlink:)?href)="data:image/[^"]+"', flags=re.I)
SVG_EXTS = {'.svg', '.SVG'}


@dataclass
class Metrics:
    svg_chars: int = 0
    path_chars: int = 0
    path_tokens: int = 0
    path_count: int = 0
    files: int = 0


def fmt_num(v: float, decimals: int) -> str:
    eps = 10 ** (-decimals)
    if abs(v) < eps:
        v = 0.0
    s = f'{v:.{decimals}f}'
    s = s.rstrip('0').rstrip('.')
    if s in ('', '-0'):
        return '0'
    if s.startswith('0.') and len(s) > 2:
        s = s[1:]
    if s.startswith('-0.') and len(s) > 3:
        s = '-' + s[2:]
    return s


def count_path_tokens(d: str) -> int:
    return len(NUM_RE.findall(d)) + len(CMD_RE.findall(d))


def extract_path_ds(text: str) -> list[str]:
    return re.findall(r'<path\b[^>]*\bd="([^"]*)"', text, flags=re.I | re.S)


def quantize_path_d(d: str, decimals: int) -> str:
    d2 = d.replace(',', ' ')
    d2 = re.sub(
        NUM_RE,
        lambda m: fmt_num(float(m.group(0)), decimals),
        d2,
    )
    d2 = re.sub(r'\s+', ' ', d2).strip()
    d2 = re.sub(r' (?=-)', '', d2)
    d2 = re.sub(r' (?=[A-Za-z])', '', d2)
    return d2


def quantize_svg_paths(text: str, decimals: int) -> str:
    def repl(m: re.Match) -> str:
        d = quantize_path_d(m.group(2), decimals)
        return f'{m.group(1)}{d}{m.group(3)}'

    return re.sub(PATH_D_RE, repl, text)


def strip_base64_href(text: str) -> str:
    return re.sub(BASE64_HREF_RE, r'\1="__RASTER_PLACEHOLDER__"', text)


def measure_text(text: str) -> Metrics:
    ds = extract_path_ds(text)
    return Metrics(
        svg_chars=len(text),
        path_chars=sum(len(d) for d in ds),
        path_tokens=sum(count_path_tokens(d) for d in ds),
        path_count=len(ds),
        files=1,
    )


def add_metric(a: Metrics, b: Metrics) -> Metrics:
    return Metrics(
        svg_chars=a.svg_chars + b.svg_chars,
        path_chars=a.path_chars + b.path_chars,
        path_tokens=a.path_tokens + b.path_tokens,
        path_count=a.path_count + b.path_count,
        files=a.files + b.files,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='/Users/xiaoxiaobo/random2000')
    parser.add_argument('--out_prefix', default='/Users/xiaoxiaobo/physui_random2000_compress_benchmark')
    parser.add_argument('--decimals', type=int, default=2)
    args = parser.parse_args()

    root = Path(args.root)
    out_prefix = Path(args.out_prefix)
    files = [p for p in root.rglob('*') if p.is_file() and p.suffix in SVG_EXTS]

    m_orig = Metrics()
    m_quant = Metrics()
    m_quant_b64 = Metrics()

    for fp in files:
        try:
            text = fp.read_text(errors='ignore')
        except Exception:
            continue

        quant = quantize_svg_paths(text, args.decimals)
        quant_b64 = strip_base64_href(quant)

        m_orig = add_metric(m_orig, measure_text(text))
        m_quant = add_metric(m_quant, measure_text(quant))
        m_quant_b64 = add_metric(m_quant_b64, measure_text(quant_b64))

    rows = [
        ('Original', m_orig),
        (f'Quantize(d={args.decimals})', m_quant),
        (f'Quantize+NoBase64(d={args.decimals})', m_quant_b64),
    ]

    txt = []
    txt.append('=== PhysUI random2000 Compression Benchmark ===')
    txt.append(f'root: {root}')
    txt.append(f'files: {len(files)}')
    for name, m in rows:
        txt.append(
            f'{name}: svg_chars={m.svg_chars}, path_chars={m.path_chars}, '
            f'path_tokens={m.path_tokens}, path_count={m.path_count}'
        )

    o = m_orig
    for name, m in rows[1:]:
        txt.append(
            f'{name} ratio vs original: svg={m.svg_chars / max(o.svg_chars, 1):.4f}, '
            f'path_chars={m.path_chars / max(o.path_chars, 1):.4f}, '
            f'path_tokens={m.path_tokens / max(o.path_tokens, 1):.4f}'
        )

    report_path = out_prefix.with_suffix('.txt')
    report_path.write_text('\n'.join(txt) + '\n')

    # plot
    labels = [r[0] for r in rows]
    svg_vals = [r[1].svg_chars for r in rows]
    path_char_vals = [r[1].path_chars for r in rows]
    path_tok_vals = [r[1].path_tokens for r in rows]

    x = list(range(len(labels)))
    width = 0.25

    plt.figure(figsize=(11, 5))
    plt.bar([i - width for i in x], svg_vals, width=width, label='SVG chars')
    plt.bar(x, path_char_vals, width=width, label='Path chars')
    plt.bar([i + width for i in x], path_tok_vals, width=width, label='Path tokens')
    plt.xticks(x, labels)
    plt.title('random2000 Compression Baseline (Stage-1)')
    plt.ylabel('count')
    plt.grid(axis='y', alpha=0.25)
    plt.legend()
    plt.tight_layout()
    fig_path = out_prefix.with_suffix('.png')
    plt.savefig(fig_path, dpi=180)

    print(f'Saved report: {report_path}')
    print(f'Saved figure: {fig_path}')


if __name__ == '__main__':
    main()
