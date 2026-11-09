"""Area-vs-frequency tradeoff plots (congestion + power as extra dimensions)."""
from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any, Sequence

from ..reports.base import Report
from .store import ImplRecord, RunDataset


def _finite(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def plot_area_vs_frequency(
    records: Sequence[ImplRecord],
    out_dir: Path | str,
    *,
    basename: str = "area_vs_frequency",
    title: str = "Area vs frequency tradeoffs",
) -> Report:
    """Write CSV always; PNG via matplotlib when available; SVG fallback otherwise."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rpt = Report(name=basename, title=title, meta={"n_points": len(records)})

    points = []
    for r in records:
        area = _finite(r.area_um2)
        freq = _finite(r.freq_mhz)
        if area is None or freq is None:
            continue
        points.append({
            "run_tag": r.run_tag,
            "stage": r.stage,
            "corner": r.corner,
            "area_um2": area,
            "freq_mhz": freq,
            "power_w": _finite(r.power_w),
            "congestion": _finite(r.congestion),
            "setup_ws_ns": _finite(r.setup_ws_ns),
            "util_pct": _finite(r.util_pct),
        })

    csv_path = out_dir / f"{basename}.csv"
    with csv_path.open("w", newline="") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "run_tag", "stage", "corner", "area_um2", "freq_mhz",
                "power_w", "congestion", "setup_ws_ns", "util_pct",
            ],
        )
        w.writeheader()
        w.writerows(points)

    rpt.add_table(
        "points",
        ("run_tag", "freq_mhz", "area_um2", "power_w", "congestion", "setup_ws"),
        [
            [p["run_tag"], p["freq_mhz"], p["area_um2"], p["power_w"], p["congestion"], p["setup_ws_ns"]]
            for p in points
        ],
        description="Pareto-relevant points (area vs frequency)",
    )
    rpt.sections["points"] = points
    rpt.meta["csv"] = str(csv_path)

    png = out_dir / f"{basename}.png"
    svg = out_dir / f"{basename}.svg"
    if points and _try_matplotlib(points, png, title=title):
        rpt.meta["png"] = str(png)
        rpt.add_finding("info", "plot_png", f"wrote {png}")
    elif points:
        _write_svg(points, svg, title=title)
        rpt.meta["svg"] = str(svg)
        rpt.add_finding("info", "plot_svg", f"wrote {svg} (matplotlib unavailable)")
    else:
        rpt.add_finding("warn", "no_points", "need area_um2 and freq_mhz on records")

    rpt.write(out_dir, basename=basename)
    return rpt


def plot_dataset(
    dataset: RunDataset,
    out_dir: Path | str,
    *,
    platform: str | None = None,
    stage: str | None = "6_final",
) -> Report:
    recs = dataset.list_records(platform=platform, stage=stage)
    return plot_area_vs_frequency(recs, out_dir)


def _try_matplotlib(points: list[dict[str, Any]], png: Path, *, title: str) -> bool:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return False

    xs = [p["freq_mhz"] for p in points]
    ys = [p["area_um2"] for p in points]
    powers = [p["power_w"] if p["power_w"] is not None else 0.0 for p in points]
    congs = [p["congestion"] if p["congestion"] is not None else 1.0 for p in points]
    # Marker size ~ power; color ~ congestion.
    pmax = max(powers) or 1.0
    sizes = [40 + 200 * (p / pmax) for p in powers]

    fig, ax = plt.subplots(figsize=(8, 5.5))
    sc = ax.scatter(xs, ys, c=congs, s=sizes, cmap="viridis", alpha=0.85, edgecolors="k", linewidths=0.4)
    for p in points:
        ax.annotate(p["run_tag"], (p["freq_mhz"], p["area_um2"]), fontsize=7, alpha=0.8)
    ax.set_xlabel("Frequency (MHz)")
    ax.set_ylabel("Stdcell area (um²)")
    ax.set_title(title)
    cbar = fig.colorbar(sc, ax=ax)
    cbar.set_label("Congestion")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(png, dpi=140)
    plt.close(fig)
    return True


def _write_svg(points: list[dict[str, Any]], path: Path, *, title: str) -> None:
    """Minimal SVG scatter: x=freq, y=area, radius~power, color~congestion."""
    w, h, pad = 720, 480, 60
    xs = [p["freq_mhz"] for p in points]
    ys = [p["area_um2"] for p in points]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    if xmax == xmin:
        xmax = xmin + 1
    if ymax == ymin:
        ymax = ymin + 1
    powers = [p["power_w"] or 0 for p in points]
    congs = [p["congestion"] or 1 for p in points]
    pmax = max(powers) or 1
    cmin, cmax = min(congs), max(congs)
    if cmax == cmin:
        cmax = cmin + 1

    def sx(x: float) -> float:
        return pad + (x - xmin) / (xmax - xmin) * (w - 2 * pad)

    def sy(y: float) -> float:
        return h - pad - (y - ymin) / (ymax - ymin) * (h - 2 * pad)

    def color(c: float) -> str:
        t = (c - cmin) / (cmax - cmin)
        # yellow → purple-ish without relying on external CSS
        r = int(255 * (1 - t))
        g = int(200 * (1 - t))
        b = int(80 + 175 * t)
        return f"rgb({r},{g},{b})"

    circles = []
    for p, pw, cg in zip(points, powers, congs):
        r = 4 + 12 * (pw / pmax)
        circles.append(
            f'<circle cx="{sx(p["freq_mhz"]):.1f}" cy="{sy(p["area_um2"]):.1f}" '
            f'r="{r:.1f}" fill="{color(cg)}" stroke="#222" stroke-width="0.8">'
            f'<title>{p["run_tag"]} f={p["freq_mhz"]} area={p["area_um2"]} '
            f'P={p["power_w"]} cong={p["congestion"]}</title></circle>'
            f'<text x="{sx(p["freq_mhz"])+6:.1f}" y="{sy(p["area_um2"])-6:.1f}" '
            f'font-size="10" fill="#333">{p["run_tag"]}</text>'
        )
    svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">
  <rect width="100%" height="100%" fill="#fafafa"/>
  <text x="{w/2}" y="24" text-anchor="middle" font-size="16" font-family="sans-serif">{title}</text>
  <text x="{w/2}" y="{h-12}" text-anchor="middle" font-size="12">Frequency (MHz)</text>
  <text x="16" y="{h/2}" text-anchor="middle" font-size="12" transform="rotate(-90 16 {h/2})">Area (um^2)</text>
  <line x1="{pad}" y1="{h-pad}" x2="{w-pad}" y2="{h-pad}" stroke="#444"/>
  <line x1="{pad}" y1="{pad}" x2="{pad}" y2="{h-pad}" stroke="#444"/>
  {''.join(circles)}
</svg>
"""
    path.write_text(svg)
