"""Overall design summary across stages and corners."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..schema import (
    AREA_STD,
    FMAX_MHZ,
    HEADLINE,
    HPWL,
    POWER_TOTAL,
    SETUP_TNS,
    SETUP_WNS,
    UTIL_ALL,
    load_snapshot,
)
from .base import Report
from .path_groups import path_group_report
from .regression import generate_regression_report


def generate_summary(
    *,
    db: Path | str,
    platform: str,
    run_tag: str,
    stage: str = "6_final",
    corner: str | None = None,
    baseline_tag: str | None = None,
    sta_root: Path | str | None = None,
    out_dir: Path | str | None = None,
) -> Report:
    """Assemble a human+script summary for one implementation run."""
    db = Path(db)
    snap = load_snapshot(db, platform=platform, run_tag=run_tag, stage=stage, corner=corner)
    rpt = Report(
        name="summary",
        title=f"Design summary — {platform}/{run_tag}/{stage}",
        meta={
            "platform": platform,
            "run_tag": run_tag,
            "stage": stage,
            "corner": snap.corner,
            "run_id": snap.run_id,
            "db": str(db),
        },
    )
    flat = snap.as_flat()
    rows = []
    for mk in HEADLINE:
        mv = snap.get(mk)
        rows.append([
            mk.key,
            None if mv is None else mv.value,
            mk.unit,
            mk.description,
        ])
    rpt.add_table("headline", ("metric", "value", "unit", "description"), rows)
    rpt.sections["headline"] = snap.headline()

    # Quick health findings.
    ws = snap.num(SETUP_WNS)
    if ws is not None and ws < 0:
        rpt.add_finding("error", "setup_failing", f"setup WNS={ws} ns", setup_ws_ns=ws)
    elif ws is not None:
        rpt.add_finding("info", "setup_met", f"setup WNS={ws} ns", setup_ws_ns=ws)
    util = snap.num(UTIL_ALL)
    if util is not None and util > 80:
        rpt.add_finding("warn", "high_utilization", f"utilization={util}%", util_pct=util)

    for label, key in (
        ("area_std_um2", AREA_STD),
        ("fmax_mhz", FMAX_MHZ),
        ("hpwl_um", HPWL),
        ("power_w", POWER_TOTAL),
        ("setup_tns_ns", SETUP_TNS),
    ):
        rpt.sections.setdefault("key_values", {})[label] = snap.num(key)

    if baseline_tag:
        reg = generate_regression_report(
            db=db,
            platform=platform,
            current_tag=run_tag,
            baseline_tag=baseline_tag,
            stage=stage,
            corner=corner,
            out_dir=None,
        )
        rpt.sections["regression"] = reg.to_dict()
        for f in reg.findings:
            rpt.findings.append(f)
        if reg.tables:
            rpt.tables.append(reg.tables[0])

    if sta_root and Path(sta_root).is_dir():
        pg = path_group_report(Path(sta_root), corner=corner, run_meta=rpt.meta)
        rpt.sections["path_groups"] = pg.sections
        for f in pg.findings[:10]:
            rpt.findings.append(f)

    if out_dir is not None:
        rpt.write(out_dir, basename="summary")
    return rpt
