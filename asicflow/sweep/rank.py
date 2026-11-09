"""Compare implementations and rank configurations."""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from ..reports.base import Report
from ..schema import (
    AREA_STD,
    FMAX_MHZ,
    HEADLINE,
    POWER_TOTAL,
    SETUP_WNS,
    UTIL_ALL,
    compare_snapshots,
    load_snapshot,
)


def normalize_metric(
    values: Sequence[float | None],
    *,
    minimize: bool = False,
) -> list[float]:
    """Normalize finite metric values to [0, 1], preserving missing values as 0.

    A constant series receives zero utility because it provides no ranking
    information. Set minimize=True when lower raw values are preferable.
    """
    present = [value for value in values if value is not None]
    if not present:
        return [0.0] * len(values)

    low, high = min(present), max(present)
    span = high - low
    if span == 0:
        return [0.0 for _ in values]

    normalized = []
    for value in values:
        if value is None:
            normalized.append(0.0)
            continue
        utility = (value - low) / span
        normalized.append(1.0 - utility if minimize else utility)
    return normalized


def compare_implementations(
    *,
    db: Path | str,
    platform: str,
    run_tags: Sequence[str],
    stage: str = "6_final",
    corner: str | None = None,
    out_dir: Path | str | None = None,
) -> Report:
    """Compare different RTL / configuration variants side by side."""
    db = Path(db)
    rpt = Report(
        name="compare_implementations",
        title="Implementation comparison",
        meta={
            "platform": platform,
            "stage": stage,
            "corner": corner,
            "run_tags": list(run_tags),
        },
    )
    snaps = [
        load_snapshot(db, platform=platform, run_tag=t, stage=stage, corner=corner)
        for t in run_tags
    ]
    headers = ["metric"] + list(run_tags)
    rows = []
    for mk in HEADLINE:
        rows.append([mk.key] + [s.num(mk) for s in snaps])
    rpt.add_table("metrics", headers, rows)
    rpt.sections["snapshots"] = [s.as_flat() for s in snaps]
    if len(snaps) >= 2:
        rpt.sections["delta_0_vs_1"] = compare_snapshots(snaps[0], snaps[1])
    if out_dir:
        rpt.write(out_dir)
    return rpt


def find_best_configuration(
    *,
    db: Path | str,
    platform: str,
    run_tags: Sequence[str],
    stage: str = "6_final",
    corner: str | None = None,
    weights: dict[str, float] | None = None,
    out_dir: Path | str | None = None,
) -> Report:
    """Rank configurations using weighted, direction-aware normalized metrics.

    Positive weights maximize a metric; negative weights minimize it. Scores
    are higher-is-better and missing metrics contribute zero utility.
    """
    db = Path(db)
    weights = weights if weights is not None else {
        SETUP_WNS.key: 0.45,
        FMAX_MHZ.key: 0.25,
        AREA_STD.key: -0.15,
        POWER_TOTAL.key: -0.10,
        UTIL_ALL.key: -0.05,
    }
    rpt = Report(
        name="best_configuration",
        title="Configuration ranking",
        meta={"platform": platform, "stage": stage, "weights": weights, "run_tags": list(run_tags)},
    )
    snaps = [
        load_snapshot(db, platform=platform, run_tag=t, stage=stage, corner=corner)
        for t in run_tags
    ]
    # Collect raw values per weighted metric.
    series: dict[str, list[float | None]] = {k: [] for k in weights}
    for s in snaps:
        for k in weights:
            series[k].append(s.num(k))

    norms = {
        key: normalize_metric(values, minimize=weight < 0)
        for key, values in series.items()
        for weight in [weights[key]]
    }

    ranked = []
    for i, s in enumerate(snaps):
        score = 0.0
        detail = {}
        for k, w in weights.items():
            term = abs(w) * norms[k][i]
            score += term
            detail[k] = {
                "raw": series[k][i],
                "utility": norms[k][i],
                "weight": w,
                "term": term,
            }
        ranked.append({
            "run_tag": s.run_tag,
            "score": score,
            "setup_ws": s.num(SETUP_WNS),
            "fmax_mhz": s.num(FMAX_MHZ),
            "area_std_um2": s.num(AREA_STD),
            "power_w": s.num(POWER_TOTAL),
            "util_pct": s.num(UTIL_ALL),
            "detail": detail,
        })
    ranked.sort(key=lambda r: r["score"], reverse=True)
    rpt.add_table(
        "ranking",
        ("rank", "run_tag", "score", "setup_ws", "fmax", "area", "power", "util"),
        [
            [i, r["run_tag"], round(r["score"], 4), r["setup_ws"], r["fmax_mhz"],
             r["area_std_um2"], r["power_w"], r["util_pct"]]
            for i, r in enumerate(ranked, 1)
        ],
    )
    rpt.sections["ranking"] = ranked
    if ranked:
        rpt.add_finding(
            "info",
            "best",
            f"best configuration: {ranked[0]['run_tag']} (score={ranked[0]['score']:.4f})",
            run_tag=ranked[0]["run_tag"],
            score=ranked[0]["score"],
        )
    if out_dir:
        rpt.write(out_dir)
    return rpt
