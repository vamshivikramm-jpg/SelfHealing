"""Minimal SVG figures for the report (Module 9). No plotting library needed.

- ``bar_chart``: one bar per condition with 95 % CI whiskers
- ``line_chart``: one or more series over a numeric x axis (optionally log x)

Figures use a light background so they print well in a report.
"""

from __future__ import annotations

import math
from html import escape
from typing import Dict, List, Optional, Sequence, Tuple

PALETTE = ("#2f6fbf", "#d1495b", "#3a9d5d", "#e8a33d", "#7b5ea7", "#3aa1a8", "#8d6e63", "#5c6b73")


def _nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    exp = 10 ** math.floor(math.log10(v))
    for step in (1, 2, 2.5, 5, 10):
        if v <= step * exp:
            return step * exp
    return 10 * exp


def _fmt(v: float) -> str:
    return f"{v:.0f}" if abs(v) >= 10 or v == 0 else f"{v:.2g}"


def bar_chart(title: str, labels: Sequence[str], means: Sequence[Optional[float]],
              lows: Sequence[Optional[float]] = (), highs: Sequence[Optional[float]] = (),
              ylabel: str = "", width: int = 760, height: int = 360, note: str = "") -> str:
    lows = list(lows) or [None] * len(means)
    highs = list(highs) or [None] * len(means)
    top = _nice_max(max([h for h in highs if h is not None] + [m for m in means if m is not None] + [0]) * 1.05)
    L, R, T, B = 64, 16, 40, 90
    pw, ph = width - L - R, height - T - B
    bw = pw / max(1, len(labels))
    y = lambda v: T + ph - (v / top) * ph
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" font-family="Helvetica, Arial, sans-serif">',
             f'<rect width="{width}" height="{height}" fill="#fff"/>',
             f'<text x="{L}" y="24" font-size="15" font-weight="bold" fill="#222">{escape(title)}</text>']
    for i in range(6):
        v = top * i / 5
        parts.append(f'<line x1="{L}" x2="{width - R}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="#e5e5e5"/>')
        parts.append(f'<text x="{L - 6}" y="{y(v) + 4:.1f}" font-size="11" text-anchor="end" fill="#555">{_fmt(v)}</text>')
    parts.append(f'<text x="14" y="{T + ph / 2}" font-size="12" fill="#333" transform="rotate(-90 14 {T + ph / 2})" '
                 f'text-anchor="middle">{escape(ylabel)}</text>')
    for i, (label, m) in enumerate(zip(labels, means)):
        cx = L + bw * i + bw / 2
        color = PALETTE[i % len(PALETTE)]
        if m is not None:
            parts.append(f'<rect x="{cx - bw * 0.32:.1f}" y="{y(m):.1f}" width="{bw * 0.64:.1f}" '
                         f'height="{max(0.0, T + ph - y(m)):.1f}" fill="{color}" opacity="0.85"/>')
            parts.append(f'<text x="{cx:.1f}" y="{y(m) - 6:.1f}" font-size="11" text-anchor="middle" fill="#222">{_fmt(m)}</text>')
        lo, hi = lows[i], highs[i]
        if lo is not None and hi is not None:
            parts.append(f'<line x1="{cx:.1f}" x2="{cx:.1f}" y1="{y(max(lo, 0)):.1f}" y2="{y(hi):.1f}" stroke="#222"/>')
            for v in (max(lo, 0), hi):
                parts.append(f'<line x1="{cx - 6:.1f}" x2="{cx + 6:.1f}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="#222"/>')
        parts.append(f'<text x="{cx:.1f}" y="{T + ph + 16}" font-size="11" text-anchor="end" fill="#333" '
                     f'transform="rotate(-30 {cx:.1f} {T + ph + 16})">{escape(label)}</text>')
    if note:
        parts.append(f'<text x="{L}" y="{height - 8}" font-size="10" fill="#777">{escape(note)}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def line_chart(title: str, series: Dict[str, List[Tuple[float, float]]], xlabel: str = "", ylabel: str = "",
               log_x: bool = False, width: int = 760, height: int = 360, note: str = "") -> str:
    pts = [p for s in series.values() for p in s if p[1] is not None]
    if not pts:
        return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}"><text x="20" y="30">{escape(title)}: no data</text></svg>'
    fx = (lambda v: math.log10(v)) if log_x else (lambda v: v)
    xs = [fx(p[0]) for p in pts]
    x0, x1 = min(xs), max(xs)
    if x1 == x0:
        x1 = x0 + 1
    top = _nice_max(max(p[1] for p in pts) * 1.05)
    L, R, T, B = 64, 150, 40, 56
    pw, ph = width - L - R, height - T - B
    X = lambda v: L + (fx(v) - x0) / (x1 - x0) * pw
    Y = lambda v: T + ph - (v / top) * ph
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" font-family="Helvetica, Arial, sans-serif">',
             f'<rect width="{width}" height="{height}" fill="#fff"/>',
             f'<text x="{L}" y="24" font-size="15" font-weight="bold" fill="#222">{escape(title)}</text>']
    for i in range(6):
        v = top * i / 5
        parts.append(f'<line x1="{L}" x2="{L + pw}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="#e5e5e5"/>')
        parts.append(f'<text x="{L - 6}" y="{Y(v) + 4:.1f}" font-size="11" text-anchor="end" fill="#555">{_fmt(v)}</text>')
    for xv in sorted({p[0] for p in pts}):
        parts.append(f'<text x="{X(xv):.1f}" y="{T + ph + 16}" font-size="11" text-anchor="middle" fill="#555">{_fmt(xv)}</text>')
    parts.append(f'<text x="{L + pw / 2}" y="{height - 14}" font-size="12" text-anchor="middle" fill="#333">{escape(xlabel)}</text>')
    parts.append(f'<text x="14" y="{T + ph / 2}" font-size="12" fill="#333" transform="rotate(-90 14 {T + ph / 2})" '
                 f'text-anchor="middle">{escape(ylabel)}</text>')
    for i, (name, s) in enumerate(series.items()):
        color = PALETTE[i % len(PALETTE)]
        s = sorted((p for p in s if p[1] is not None), key=lambda p: p[0])
        if not s:
            continue
        d = " ".join(f"{'M' if j == 0 else 'L'}{X(x):.1f},{Y(v):.1f}" for j, (x, v) in enumerate(s))
        parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2"/>')
        parts.extend(f'<circle cx="{X(x):.1f}" cy="{Y(v):.1f}" r="3" fill="{color}"/>' for x, v in s)
        ly = T + 14 + i * 18
        parts.append(f'<line x1="{L + pw + 14}" x2="{L + pw + 34}" y1="{ly - 4}" y2="{ly - 4}" stroke="{color}" stroke-width="3"/>')
        parts.append(f'<text x="{L + pw + 40}" y="{ly}" font-size="11" fill="#333">{escape(name)}</text>')
    if note:
        parts.append(f'<text x="{L}" y="{height - 2}" font-size="10" fill="#777">{escape(note)}</text>')
    parts.append("</svg>")
    return "\n".join(parts)
