"""Per-corner path-group reports: reg2reg / in2reg / reg2out."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .base import Report
from .path_diagnose import diagnose_paths_report, diagnosis_table

NUM = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
PATH_GROUPS = ("reg2reg", "in2reg", "reg2out")

_WS = re.compile(rf"worst slack\s+(?:max\s+)?({NUM})", re.I)
_TNS = re.compile(rf"tns\s+(?:max\s+)?({NUM})", re.I)
_SLACK_END = re.compile(rf"({NUM})\s+\((?:VIOLATED|MET)\)")
_VIOL = re.compile(r"VIOLATED")


def _f(rx: re.Pattern[str], text: str) -> float | None:
    m = rx.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def summarize_path_group_rpt(path: Path) -> dict[str, Any]:
    text = path.read_text(errors="replace") if path.is_file() else ""
    slacks = [float(m.group(1)) for m in _SLACK_END.finditer(text)]
    ws = _f(_WS, text)
    if ws is None and slacks:
        ws = min(slacks)
    return {
        "file": str(path),
        "exists": path.is_file(),
        "setup_ws_ns": ws,
        "setup_tns_ns": _f(_TNS, text),
        "violations": len(_VIOL.findall(text)),
        "path_count": len(slacks),
        "bytes": path.stat().st_size if path.is_file() else 0,
    }


def collect_corner_path_groups(corner_dir: Path) -> dict[str, dict[str, Any]]:
    """Read reg2reg.rpt / in2reg.rpt / reg2out.rpt (or aliases) under one corner."""
    aliases = {
        "reg2reg": ("reg2reg.rpt", "paths_reg2reg.rpt"),
        "in2reg": ("in2reg.rpt", "paths_in2reg.rpt"),
        "reg2out": ("reg2out.rpt", "paths_reg2out.rpt"),
    }
    out: dict[str, dict[str, Any]] = {}
    for group, names in aliases.items():
        path = None
        for name in names:
            cand = corner_dir / name
            if cand.is_file():
                path = cand
                break
        if path is None:
            path = corner_dir / names[0]
        out[group] = summarize_path_group_rpt(path)
        # Attach root-cause for the group report when present.
        if path.is_file():
            diags = diagnose_paths_report(path, limit=5)
            if diags:
                out[group]["dominant"] = diags[0].dominant
                out[group]["top_endpoint"] = diags[0].endpoint
                out[group]["diagnoses"] = [d.to_dict() for d in diags]
    return out


def path_group_report(
    sta_root: Path,
    *,
    corner: str | None = None,
    run_meta: dict[str, Any] | None = None,
) -> Report:
    """Build a dual-consumable report for path groups across corners."""
    sta_root = Path(sta_root)
    corners = [corner] if corner else sorted(
        p.name for p in sta_root.iterdir() if p.is_dir() and (p / "metrics.json").is_file()
    )
    rpt = Report(
        name="path_groups",
        title="Path group timing (reg2reg / in2reg / reg2out)",
        meta={"sta_root": str(sta_root), **(run_meta or {})},
    )
    rows = []
    for cname in corners:
        cdir = sta_root / cname
        groups = collect_corner_path_groups(cdir)
        rpt.sections[cname] = groups
        for gname, summary in groups.items():
            rows.append([
                cname,
                gname,
                summary.get("setup_ws_ns", "NA"),
                summary.get("setup_tns_ns", "NA"),
                summary.get("violations", "NA"),
                summary.get("dominant", "NA"),
                summary.get("top_endpoint", "NA"),
            ])
            if not summary.get("exists"):
                rpt.add_finding(
                    "warn",
                    "missing_path_group_rpt",
                    f"{cname}/{gname}.rpt missing — re-run sta-pvt",
                    corner=cname,
                    group=gname,
                )
            elif (summary.get("setup_ws_ns") is not None) and summary["setup_ws_ns"] < 0:
                rpt.add_finding(
                    "error" if gname == "reg2reg" else "warn",
                    "path_group_negative_ws",
                    f"{cname} {gname} WNS={summary['setup_ws_ns']} ns "
                    f"(dominant={summary.get('dominant', '?')})",
                    corner=cname,
                    group=gname,
                    **{k: summary[k] for k in ("setup_ws_ns", "dominant", "top_endpoint") if k in summary},
                )
    rpt.add_table(
        "summary",
        ("corner", "group", "ws_ns", "tns_ns", "violations", "dominant", "endpoint"),
        rows,
        description="Per-corner path-group WNS with root-cause hint",
    )

    # Detailed diagnosis from worst corner's critical_paths / reg2reg.
    for cname in corners:
        crit = sta_root / cname / "critical_paths.rpt"
        if not crit.is_file():
            crit = sta_root / cname / "reg2reg.rpt"
        if not crit.is_file():
            continue
        diags = diagnose_paths_report(crit, limit=10)
        if not diags:
            continue
        headers, drows = diagnosis_table(diags)
        rpt.add_table(
            f"diagnose_{cname}",
            headers,
            drows,
            description=f"Root-cause classification for {cname}",
        )
        break
    return rpt
