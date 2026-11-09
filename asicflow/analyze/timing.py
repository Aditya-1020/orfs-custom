"""Timing / CDC / STA comparison analyses."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..reports.base import Report
from ..reports.path_diagnose import diagnose_paths_report
from ..reports.path_groups import collect_corner_path_groups
from ..schema import compare_snapshots, load_snapshot

_DOMAIN = re.compile(r"clock\s+(\S+)", re.I)


def check_unconstrained_paths(
    corner_dir: Path | str,
    *,
    out_dir: Path | str | None = None,
) -> Report:
    """Find endpoints missing timing constraints from unconstrained.rpt / check_setup."""
    corner_dir = Path(corner_dir)
    rpt = Report(
        name="unconstrained_paths",
        title="Unconstrained path check",
        meta={"corner_dir": str(corner_dir)},
    )
    unc = corner_dir / "unconstrained.rpt"
    setup = corner_dir / "check_setup.rpt"
    text = ""
    source = None
    for cand in (unc, setup):
        if cand.is_file():
            text = cand.read_text(errors="replace")
            source = str(cand)
            break
    rpt.meta["source"] = source or "missing"
    if not text:
        rpt.add_finding("warn", "report_missing", "no unconstrained/check_setup report")
        if out_dir:
            rpt.write(out_dir)
        return rpt

    endpoints = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("---"):
            continue
        if "unconstrained" in s.lower() or s.lower().startswith("warning"):
            endpoints.append(s)
        elif re.search(r"\b(pin|port|endpoint)\b", s, re.I):
            endpoints.append(s)
    # Dedup while preserving order.
    seen = set()
    uniq = []
    for e in endpoints:
        if e in seen:
            continue
        seen.add(e)
        uniq.append(e)

    rpt.add_table(
        "endpoints",
        ("index", "detail"),
        [[i, e] for i, e in enumerate(uniq, 1)],
        description="Potential unconstrained endpoints / setup warnings",
    )
    rpt.sections["count"] = len(uniq)
    if uniq:
        rpt.add_finding(
            "warn",
            "unconstrained_present",
            f"{len(uniq)} unconstrained-related lines",
            count=len(uniq),
        )
    else:
        rpt.add_finding("info", "fully_constrained", "no unconstrained endpoints detected")
    if out_dir:
        rpt.write(out_dir)
    return rpt


def check_clock_domains(
    corner_dir: Path | str,
    *,
    out_dir: Path | str | None = None,
) -> Report:
    """Identify clocks and inspect CDC-related constraint reports."""
    corner_dir = Path(corner_dir)
    rpt = Report(
        name="clock_domains",
        title="Clock domain / CDC constraint check",
        meta={"corner_dir": str(corner_dir)},
    )
    clk_rpt = corner_dir / "clock_summary.rpt"
    path_groups = corner_dir / "path_groups.rpt"
    recovery = corner_dir / "recovery_removal.rpt"

    clocks: list[list[Any]] = []
    if clk_rpt.is_file():
        text = clk_rpt.read_text(errors="replace")
        rpt.sections["clock_summary_excerpt"] = "\n".join(text.splitlines()[:80])
        for m in _DOMAIN.finditer(text):
            name = m.group(1)
            if name not in {r[0] for r in clocks}:
                clocks.append([name, "listed"])
        # Skew section presence.
        has_skew = "skew" in text.lower()
        rpt.meta["has_skew_report"] = has_skew
        if not has_skew:
            rpt.add_finding(
                "info",
                "ideal_clocks",
                "no skew section — clocks may still be ideal (pre-CTS)",
            )
    else:
        rpt.add_finding("warn", "clock_summary_missing", "clock_summary.rpt not found")

    rpt.add_table("clocks", ("clock", "status"), clocks or [["NA", "none_found"]])

    async_ws = None
    if path_groups.is_file():
        text = path_groups.read_text(errors="replace")
        if "asynchronous" in text.lower():
            rpt.add_finding(
                "info",
                "async_path_group",
                "asynchronous path group present — inspect CDC waivers",
            )
        m = re.search(
            r"=== path group: asynchronous \(setup\) ===.*?(-?\d+(?:\.\d+)?)\s+\((?:VIOLATED|MET)\)",
            text,
            re.S | re.I,
        )
        if m:
            async_ws = float(m.group(1))
            rpt.sections["async_setup_ws_ns"] = async_ws

    cdc_notes = []
    if recovery.is_file():
        rtext = recovery.read_text(errors="replace")
        viol = rtext.count("VIOLATED")
        cdc_notes.append(["recovery_removal_violations", viol])
        if viol:
            rpt.add_finding(
                "warn",
                "recovery_removal_violations",
                f"{viol} recovery/removal VIOLATED lines",
                count=viol,
            )
    rpt.add_table("cdc_checks", ("check", "value"), cdc_notes)
    rpt.sections["clock_count"] = len(clocks)

    groups = collect_corner_path_groups(corner_dir)
    rpt.sections["path_groups"] = {
        k: {kk: vv for kk, vv in v.items() if kk != "diagnoses"} for k, v in groups.items()
    }
    if out_dir:
        rpt.write(out_dir)
    return rpt


def compare_sta_runs(
    *,
    db: Path | str | None = None,
    platform: str,
    run_tags: list[str],
    stage: str = "6_final",
    corner: str | None = None,
    sta_roots: dict[str, Path] | None = None,
    out_dir: Path | str | None = None,
) -> Report:
    """Compare timing across configurations / stages / corners."""
    rpt = Report(
        name="compare_sta_runs",
        title="STA run comparison",
        meta={
            "platform": platform,
            "stage": stage,
            "corner": corner,
            "run_tags": list(run_tags),
        },
    )
    rows = []
    snaps = []
    if db:
        for tag in run_tags:
            snap = load_snapshot(
                db, platform=platform, run_tag=tag, stage=stage, corner=corner
            )
            snaps.append(snap)
            rows.append([
                tag,
                snap.num("timing.setup_ws"),
                snap.num("timing.setup_tns"),
                snap.num("timing.hold_ws"),
                snap.num("timing.fmax_est_mhz"),
                snap.num("timing.setup_violations"),
                snap.num("power.total.total_W"),
            ])
    if sta_roots:
        for tag, root in sta_roots.items():
            root = Path(root)
            metrics_files = sorted(root.glob("*/metrics.json"))
            for mf in metrics_files:
                data = json.loads(mf.read_text())
                rows.append([
                    f"{tag}/{mf.parent.name}",
                    data.get("setup_ws_ns"),
                    data.get("setup_tns_ns"),
                    data.get("hold_ws_ns"),
                    data.get("fmax_mhz"),
                    data.get("setup_violations"),
                    "NA",
                ])
                # Diagnose critical paths for this corner.
                crit = mf.parent / "critical_paths.rpt"
                if crit.is_file():
                    diags = diagnose_paths_report(crit, limit=3)
                    if diags:
                        rpt.sections.setdefault("diagnoses", {})[f"{tag}/{mf.parent.name}"] = [
                            d.to_dict() for d in diags
                        ]

    rpt.add_table(
        "timing",
        ("run", "setup_ws", "setup_tns", "hold_ws", "fmax", "setup_viol", "power_w"),
        rows,
    )
    if len(snaps) >= 2:
        deltas = compare_snapshots(snaps[0], snaps[1])
        rpt.sections["delta_first_vs_second"] = deltas
        rpt.add_table(
            "delta",
            ("metric", "a", "b", "abs_delta", "trend"),
            [
                [
                    d["key"],
                    d["current"],
                    d["baseline"],
                    d["abs_delta"],
                    {True: "a_better", False: "b_better", None: "n/a"}[d["improved"]],
                ]
                for d in deltas
            ],
        )
    if out_dir:
        rpt.write(out_dir)
    return rpt
