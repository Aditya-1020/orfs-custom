"""Compare a run against a named baseline (WNS/TNS/area/util/WL/cong/violations)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..schema import HEADLINE, REGRESSION_KEYS, compare_snapshots, load_snapshot
from .base import Report


def generate_regression_report(
    *,
    db: Path | str,
    platform: str,
    current_tag: str,
    baseline_tag: str,
    stage: str = "6_final",
    corner: str | None = None,
    out_dir: Path | str | None = None,
) -> Report:
    db = Path(db)
    current = load_snapshot(
        db, platform=platform, run_tag=current_tag, stage=stage, corner=corner
    )
    baseline = load_snapshot(
        db, platform=platform, run_tag=baseline_tag, stage=stage, corner=corner
    )
    deltas = compare_snapshots(current, baseline, HEADLINE)

    rpt = Report(
        name="regression",
        title=f"Regression — {current_tag} vs baseline {baseline_tag}",
        meta={
            "platform": platform,
            "stage": stage,
            "corner": corner or current.corner,
            "current_tag": current_tag,
            "baseline_tag": baseline_tag,
            "current_run_id": current.run_id,
            "baseline_run_id": baseline.run_id,
            "db": str(db),
        },
    )
    rows = []
    regressions = improvements = 0
    for d in deltas:
        rows.append([
            d["key"],
            d["baseline"] if d["baseline"] is not None else "NA",
            d["current"] if d["current"] is not None else "NA",
            d["abs_delta"] if d["abs_delta"] is not None else "NA",
            d["pct_delta"] if d["pct_delta"] is not None else "NA",
            {True: "better", False: "worse", None: "n/a"}[d["improved"]],
            d["unit"],
        ])
        if d["improved"] is True:
            improvements += 1
        elif d["improved"] is False:
            regressions += 1
            rpt.add_finding(
                "warn" if d["key"] not in ("timing.setup_ws", "timing.setup_tns") else "error",
                "metric_regressed",
                f"{d['key']} worsened: {d['baseline']} → {d['current']} "
                f"(Δ={d['abs_delta']})",
                **{k: d[k] for k in ("key", "baseline", "current", "abs_delta", "pct_delta")},
            )

    rpt.add_table(
        "deltas",
        ("metric", "baseline", "current", "abs_delta", "pct_delta", "trend", "unit"),
        rows,
        description="Headline metrics vs baseline",
    )
    rpt.sections["deltas"] = deltas
    rpt.sections["counts"] = {
        "improvements": improvements,
        "regressions": regressions,
        "keys": list(REGRESSION_KEYS),
    }
    if baseline.run_id is None and not baseline.values:
        rpt.add_finding(
            "warn",
            "baseline_missing",
            f"no metrics for baseline tag {baseline_tag!r}",
            baseline_tag=baseline_tag,
        )
    if out_dir is not None:
        rpt.write(out_dir, basename="regression")
    return rpt


def snapshot_baseline_payload(snap_flat: dict[str, Any]) -> dict[str, Any]:
    """Subset used when pinning a baseline JSON beside analysis outputs."""
    return {k: snap_flat.get(k) for k in REGRESSION_KEYS}
