"""Fanout, wirelength, and buffer analyses from metrics DB + ORFS logs/reports."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from ..reports.base import Report
from ..schema import FANOUT_AVG, FANOUT_MAX, HPWL, INST_BUF, INST_INV, load_snapshot

_BUF_RE = re.compile(r"(?:^|_)(?:clk|dly)?(buf|inv)(?:_|\d)", re.I)
_CELL_USAGE = re.compile(r"^(\S+)\s+(\d+)\s*$", re.M)


def _db_rows(
    db: Path,
    *,
    platform: str,
    run_tag: str,
    stage: str,
    category: str,
    metric_prefix: str = "",
) -> list[tuple[str, str, str]]:
    if not db.is_file():
        return []
    con = sqlite3.connect(db)
    try:
        sql = (
            "SELECT metric, value, unit FROM metrics "
            "WHERE platform=? AND run_tag=? AND stage=? AND category=? "
            "ORDER BY collected_at DESC"
        )
        rows = con.execute(sql, (platform, run_tag, stage, category)).fetchall()
    finally:
        con.close()
    seen = set()
    out = []
    for metric, value, unit in rows:
        if metric_prefix and not metric.startswith(metric_prefix):
            continue
        if metric in seen:
            continue
        seen.add(metric)
        out.append((metric, value, unit))
    return out


def analyze_fanout(
    *,
    db: Path | str,
    platform: str,
    run_tag: str,
    stage: str = "6_final",
    threshold: int = 16,
    out_dir: Path | str | None = None,
    sta_corner_dir: Path | str | None = None,
) -> Report:
    """Identify high-fanout nets / max-fanout violations."""
    db = Path(db)
    snap = load_snapshot(db, platform=platform, run_tag=run_tag, stage=stage)
    rpt = Report(
        name="fanout",
        title="Fanout analysis",
        meta={"platform": platform, "run_tag": run_tag, "stage": stage, "threshold": threshold},
    )
    fo_max = snap.num(FANOUT_MAX)
    fo_avg = snap.num(FANOUT_AVG)
    rpt.sections["summary"] = {"fanout_max": fo_max, "fanout_avg": fo_avg}
    rpt.add_table(
        "summary",
        ("metric", "value"),
        [("fanout_max", fo_max), ("fanout_avg", fo_avg), ("threshold", threshold)],
    )
    if fo_max is not None and fo_max >= threshold:
        rpt.add_finding(
            "warn",
            "high_fanout",
            f"max fanout={fo_max} ≥ threshold={threshold}",
            fanout_max=fo_max,
            threshold=threshold,
        )

    viol_rows = []
    if sta_corner_dir:
        fanout_rpt = Path(sta_corner_dir) / "max_fanout.rpt"
        if fanout_rpt.is_file():
            lines = [
                ln.strip()
                for ln in fanout_rpt.read_text(errors="replace").splitlines()
                if "VIOLATED" in ln or "fanout" in ln.lower()
            ]
            viol_rows = [[i, ln] for i, ln in enumerate(lines[:50], 1)]
            rpt.meta["max_fanout_rpt"] = str(fanout_rpt)
    rpt.add_table("violators", ("index", "detail"), viol_rows or [[1, "none"]])
    if out_dir:
        rpt.write(out_dir)
    return rpt


def analyze_wirelength(
    *,
    db: Path | str,
    platform: str,
    run_tag: str,
    stages: list[str] | None = None,
    out_dir: Path | str | None = None,
) -> Report:
    """Track signal wirelength (HPWL) across stages."""
    db = Path(db)
    stages = stages or ["3_place", "4_cts", "5_route", "6_final"]
    rpt = Report(
        name="wirelength",
        title="Wirelength across stages",
        meta={"platform": platform, "run_tag": run_tag, "stages": stages},
    )
    rows = []
    prev = None
    for stage in stages:
        snap = load_snapshot(db, platform=platform, run_tag=run_tag, stage=stage)
        hpwl = snap.num(HPWL)
        delta = None if prev is None or hpwl is None else hpwl - prev
        rows.append([stage, hpwl, delta])
        if hpwl is not None:
            prev = hpwl
    rpt.add_table("hpwl", ("stage", "hpwl_um", "delta_um"), rows)
    rpt.sections["series"] = [{"stage": r[0], "hpwl_um": r[1], "delta_um": r[2]} for r in rows]
    if out_dir:
        rpt.write(out_dir)
    return rpt


def analyze_buffers(
    *,
    db: Path | str,
    platform: str,
    run_tag: str,
    stage: str = "6_final",
    reports_dir: Path | str | None = None,
    out_dir: Path | str | None = None,
) -> Report:
    """Track inserted buffers/inverters and distribution (table + example paths)."""
    db = Path(db)
    snap = load_snapshot(db, platform=platform, run_tag=run_tag, stage=stage)
    rpt = Report(
        name="buffers",
        title="Buffer / inverter distribution",
        meta={"platform": platform, "run_tag": run_tag, "stage": stage},
    )
    n_buf = snap.num(INST_BUF)
    n_inv = snap.num(INST_INV)
    rpt.add_table(
        "counts",
        ("kind", "count"),
        [("buffer", n_buf), ("inverter", n_inv)],
    )

    usage = _db_rows(db, platform=platform, run_tag=run_tag, stage=stage, category="cell_usage")
    buf_rows = []
    for master, count, unit in usage:
        if _BUF_RE.search(master):
            kind = "buffer" if "buf" in master.lower() else "inverter"
            buf_rows.append([master, kind, count])
    buf_rows.sort(key=lambda r: -int(float(r[2])) if str(r[2]).replace(".", "", 1).isdigit() else 0)
    rpt.add_table(
        "by_master",
        ("master", "kind", "count"),
        buf_rows[:40],
        description="Buffer/inverter masters by count",
    )

    # Example hierarchical paths from design_summary / cell usage reports if present.
    path_rows: list[list[Any]] = []
    if reports_dir:
        for name in ("design_summary.rpt", "synth_stat.txt", "6_report.json"):
            p = Path(reports_dir) / name
            if not p.is_file():
                # also search recursively one level
                continue
            text = p.read_text(errors="replace")
            for m in re.finditer(r"(\S*(?:buf|inv)\S*)", text, re.I):
                s = m.group(1)
                if "/" in s and _BUF_RE.search(s):
                    path_rows.append([s])
                    if len(path_rows) >= 30:
                        break
            if path_rows:
                break
    # Also pull long_net names as related high-fanout targets needing buffers.
    nets = _db_rows(
        db, platform=platform, run_tag=run_tag, stage=stage, category="net", metric_prefix="long_net"
    )
    for metric, value, _unit in nets:
        if metric.endswith(".name"):
            path_rows.append([f"net:{value}"])

    rpt.add_table(
        "paths",
        ("path_or_net",),
        path_rows or [["NA"]],
        description="Example buffer instance paths / long nets",
    )
    rpt.sections["totals"] = {"buffers": n_buf, "inverters": n_inv, "masters": len(buf_rows)}
    if out_dir:
        rpt.write(out_dir)
    return rpt
