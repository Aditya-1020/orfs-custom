"""Congestion, macro placement, and area-growth analyses."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ..physical import parse_grt_log, parse_place_gp_log
from ..reports.base import Report
from ..schema import AREA_MACRO, AREA_STD, UTIL_ALL, UTIL_STD, load_snapshot


def analyze_congestion(
    *,
    logs_dir: Path | str | None = None,
    reports_dir: Path | str | None = None,
    run_tag: str = "",
    platform: str = "",
    out_dir: Path | str | None = None,
) -> Report:
    """Extract congestion hotspots from GP / GRT logs and congestion.rpt."""
    rpt = Report(
        name="congestion",
        title="Congestion hotspots",
        meta={"run_tag": run_tag, "platform": platform},
    )
    feats: dict[str, Any] = {}
    if logs_dir:
        logs_dir = Path(logs_dir)
        feats.update(parse_place_gp_log(logs_dir / "3_3_place_gp.log"))
        feats.update(parse_grt_log(logs_dir / "5_1_grt.log"))
    hotspots = []
    if reports_dir:
        cong = Path(reports_dir) / "congestion.rpt"
        if cong.is_file():
            rpt.meta["congestion_rpt"] = str(cong)
            for ln in cong.read_text(errors="replace").splitlines():
                if ln.startswith("violation type:") or "Congestion" in ln or "overflow" in ln.lower():
                    hotspots.append([ln.strip()])
    rows = [[k, v] for k, v in sorted(feats.items())]
    rpt.add_table("features", ("metric", "value"), rows)
    rpt.add_table("hotspots", ("detail",), hotspots[:100] or [["none"]])
    rpt.sections["features"] = feats

    cong = feats.get("gp_cong_weighted_last", feats.get("gp_cong_weighted_min"))
    if cong is not None and float(cong) >= 1.1:
        rpt.add_finding("warn", "high_congestion", f"weighted congestion={cong}", congestion=cong)
    if feats.get("grt_failed"):
        rpt.add_finding(
            "error",
            "grt_failed",
            f"global route failed; overflow={feats.get('grt_total_overflow')}",
            **{k: feats[k] for k in feats if k.startswith("grt_")},
        )
    if out_dir:
        rpt.write(out_dir)
    return rpt


def analyze_macro_placement(
    *,
    db: Path | str,
    platform: str,
    run_tag: str,
    stage: str = "2_floorplan",
    halo_um: float = 10.0,
    out_dir: Path | str | None = None,
) -> Report:
    """Check macro locations, edge clearances (halo proxy), and pairwise gaps."""
    db = Path(db)
    rpt = Report(
        name="macro_placement",
        title="Macro placement check",
        meta={
            "platform": platform,
            "run_tag": run_tag,
            "stage": stage,
            "halo_um": halo_um,
        },
    )
    if not db.is_file():
        rpt.add_finding("warn", "db_missing", f"no metrics db at {db}")
        if out_dir:
            rpt.write(out_dir)
        return rpt

    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT metric, value FROM metrics "
            "WHERE platform=? AND run_tag=? AND stage=? AND category='macro' "
            "ORDER BY collected_at DESC",
            (platform, run_tag, stage),
        ).fetchall()
        dist = con.execute(
            "SELECT metric, value FROM metrics "
            "WHERE platform=? AND run_tag=? AND stage=? AND category='macro_dist' "
            "ORDER BY collected_at DESC",
            (platform, run_tag, stage),
        ).fetchall()
    finally:
        con.close()

    macros: dict[str, dict[str, Any]] = {}
    seen = set()
    for metric, value in rows:
        if metric in seen:
            continue
        seen.add(metric)
        if "." not in metric:
            continue
        name, field = metric.rsplit(".", 1)
        macros.setdefault(name, {})[field] = value

    table = []
    for name, fields in sorted(macros.items()):
        edge_min = None
        for ek in ("edge_left_um", "edge_right_um", "edge_bottom_um", "edge_top_um"):
            try:
                v = float(fields.get(ek, "nan"))
            except (TypeError, ValueError):
                continue
            edge_min = v if edge_min is None else min(edge_min, v)
        table.append([
            name,
            fields.get("master"),
            fields.get("x_um"),
            fields.get("y_um"),
            fields.get("w_um"),
            fields.get("h_um"),
            fields.get("orient"),
            edge_min,
        ])
        if edge_min is not None and edge_min < halo_um:
            rpt.add_finding(
                "warn",
                "halo_violation",
                f"{name} min edge clearance {edge_min} um < halo {halo_um} um",
                macro=name,
                edge_min_um=edge_min,
                halo_um=halo_um,
            )

    rpt.add_table(
        "macros",
        ("name", "master", "x", "y", "w", "h", "orient", "min_edge_um"),
        table,
    )
    dist_rows = []
    seen_d = set()
    for metric, value in dist:
        if metric in seen_d:
            continue
        seen_d.add(metric)
        dist_rows.append([metric, value])
        try:
            if float(value) < halo_um:
                rpt.add_finding(
                    "warn",
                    "macro_too_close",
                    f"{metric} distance {value} um < halo {halo_um}",
                    pair=metric,
                    distance_um=float(value),
                )
        except (TypeError, ValueError):
            pass
    rpt.add_table("pairwise_distance", ("pair", "manhattan_um"), dist_rows)
    rpt.sections["macro_count"] = len(macros)
    if out_dir:
        rpt.write(out_dir)
    return rpt


def analyze_area_growth(
    *,
    db: Path | str,
    platform: str,
    run_tag: str,
    stages: list[str] | None = None,
    out_dir: Path | str | None = None,
) -> Report:
    """Track area / utilization growth from synthesis through routing."""
    db = Path(db)
    stages = stages or [
        "1_synth",
        "2_floorplan",
        "3_place",
        "4_cts",
        "5_route",
        "6_final",
    ]
    rpt = Report(
        name="area_growth",
        title="Area growth through the flow",
        meta={"platform": platform, "run_tag": run_tag, "stages": stages},
    )
    rows = []
    prev_std = None
    for stage in stages:
        snap = load_snapshot(db, platform=platform, run_tag=run_tag, stage=stage)
        std = snap.num(AREA_STD)
        macro = snap.num(AREA_MACRO)
        util = snap.num(UTIL_ALL)
        util_std = snap.num(UTIL_STD)
        growth = None if prev_std is None or std is None else std - prev_std
        rows.append([stage, std, macro, util, util_std, growth])
        if std is not None:
            prev_std = std
    rpt.add_table(
        "growth",
        ("stage", "area_std_um2", "area_macro_um2", "util_all_pct", "util_std_pct", "delta_std"),
        rows,
    )
    rpt.sections["series"] = [
        {
            "stage": r[0],
            "area_std_um2": r[1],
            "area_macro_um2": r[2],
            "util_all_pct": r[3],
            "util_std_pct": r[4],
            "delta_std": r[5],
        }
        for r in rows
    ]
    if out_dir:
        rpt.write(out_dir)
    return rpt
