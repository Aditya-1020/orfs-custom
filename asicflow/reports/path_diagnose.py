"""Classify critical-path bottleneck: cell/net/fanout/slew/cap/uncertainty."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NUM = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"

_PATH_SPLIT = re.compile(r"(?=^Startpoint:\s)", re.M)
_SLACK = re.compile(rf"({NUM})\s+slack\b")
_ARRIVAL = re.compile(rf"({NUM})\s+data arrival time")
_REQUIRED = re.compile(rf"({NUM})\s+data required time")
_GROUP = re.compile(r"Path Group:\s+(\S+)")
_START = re.compile(r"Startpoint:\s+(\S+)")
_END = re.compile(r"Endpoint:\s+(\S+)")
_UNCERT = re.compile(rf"clock uncertainty\s+({NUM})", re.I)
# OpenSTA point lines often look like:
#   u1/Z (BUF_X1)  0.12  0.45  ^  ...
_POINT = re.compile(
    rf"^(\S+)\s+\(([^)]+)\)\s+({NUM})(?:\s+({NUM}))?(?:\s+({NUM}))?",
    re.M,
)
_SLEW_FIELD = re.compile(rf"\bslew\s+({NUM})", re.I)
_CAP_FIELD = re.compile(rf"\bcap(?:acitance)?\s+({NUM})", re.I)
_FANOUT_FIELD = re.compile(rf"\bfanout\s+(\d+)", re.I)


@dataclass
class PathPoint:
    pin: str
    cell: str
    incr_ns: float | None = None
    arrival_ns: float | None = None
    slew_ns: float | None = None
    cap_pf: float | None = None
    fanout: int | None = None
    kind: str = ""  # cell | net | other


@dataclass
class PathDiagnosis:
    startpoint: str | None = None
    endpoint: str | None = None
    group: str | None = None
    slack_ns: float | None = None
    arrival_ns: float | None = None
    required_ns: float | None = None
    cell_delay_ns: float = 0.0
    net_delay_ns: float = 0.0
    logic_depth: int = 0
    max_fanout: int = 0
    max_slew_ns: float | None = None
    max_cap: float | None = None
    clock_uncertainty_ns: float | None = None
    dominant: str = "unknown"
    dominant_share: float | None = None
    hot_cells: list[str] = field(default_factory=list)
    hot_nets: list[str] = field(default_factory=list)
    points: list[PathPoint] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "startpoint": self.startpoint,
            "endpoint": self.endpoint,
            "group": self.group,
            "slack_ns": self.slack_ns,
            "arrival_ns": self.arrival_ns,
            "required_ns": self.required_ns,
            "cell_delay_ns": self.cell_delay_ns,
            "net_delay_ns": self.net_delay_ns,
            "logic_depth": self.logic_depth,
            "max_fanout": self.max_fanout,
            "max_slew_ns": self.max_slew_ns,
            "max_cap": self.max_cap,
            "clock_uncertainty_ns": self.clock_uncertainty_ns,
            "dominant": self.dominant,
            "dominant_share": self.dominant_share,
            "hot_cells": self.hot_cells,
            "hot_nets": self.hot_nets,
            "notes": self.notes,
        }


def _f(m: re.Match[str] | None, group: int = 1) -> float | None:
    if not m:
        return None
    try:
        return float(m.group(group))
    except (TypeError, ValueError):
        return None


def diagnose_path_text(text: str) -> PathDiagnosis:
    """Parse one OpenSTA path report chunk into a root-cause diagnosis."""
    d = PathDiagnosis()
    sm = _START.search(text)
    em = _END.search(text)
    gm = _GROUP.search(text)
    d.startpoint = sm.group(1) if sm else None
    d.endpoint = em.group(1) if em else None
    d.group = gm.group(1) if gm else None
    d.slack_ns = _f(_SLACK.search(text))
    d.arrival_ns = _f(_ARRIVAL.search(text))
    d.required_ns = _f(_REQUIRED.search(text))
    d.clock_uncertainty_ns = _f(_UNCERT.search(text))

    cell_contrib: dict[str, float] = {}
    net_contrib: dict[str, float] = {}
    slews: list[float] = []
    caps: list[float] = []
    fanouts: list[int] = []

    # Prefer structured critical_paths.rpt fields when present.
    cell_m = re.search(rf"Cell delay:\s+({NUM})\s+ns", text, re.I)
    net_m = re.search(rf"Net delay:\s+({NUM})\s+ns", text, re.I)
    depth_m = re.search(r"Logic depth:\s+(\d+)", text, re.I)
    fo_m = re.search(r"Fanout:\s+(\d+)", text, re.I)
    slew_m = re.search(rf"Max slew:\s+({NUM})\s+ns", text, re.I)
    cap_m = re.search(rf"Max cap:\s+({NUM})", text, re.I)
    unc_m = re.search(rf"Uncertainty:\s+({NUM})\s+ns", text, re.I)
    dom_m = re.search(rf"Dominant:\s+(\w+)\s+\(({NUM})\)", text, re.I)
    hot_cell_m = re.search(r"Hot cell:\s+(\S+)", text, re.I)
    hot_net_m = re.search(r"Hot net:\s+(\S+)", text, re.I)
    if cell_m:
        d.cell_delay_ns = float(cell_m.group(1))
    if net_m:
        d.net_delay_ns = float(net_m.group(1))
    if depth_m:
        d.logic_depth = int(depth_m.group(1))
    if fo_m:
        d.max_fanout = int(fo_m.group(1))
    if slew_m:
        d.max_slew_ns = float(slew_m.group(1))
    if cap_m:
        d.max_cap = float(cap_m.group(1))
    if unc_m:
        d.clock_uncertainty_ns = float(unc_m.group(1))
    if dom_m:
        d.dominant = dom_m.group(1)
        d.dominant_share = float(dom_m.group(2))
    if hot_cell_m and hot_cell_m.group(1) != "NA":
        d.hot_cells = [hot_cell_m.group(1)]
    if hot_net_m and hot_net_m.group(1) != "NA":
        d.hot_nets = [hot_net_m.group(1)]

    for line in text.splitlines():
        for smatch in _SLEW_FIELD.finditer(line):
            slews.append(float(smatch.group(1)))
        for cmatch in _CAP_FIELD.finditer(line):
            caps.append(float(cmatch.group(1)))
        for fmatch in _FANOUT_FIELD.finditer(line):
            fanouts.append(int(fmatch.group(1)))

        pm = _POINT.match(line.strip())
        if not pm:
            continue
        pin, cell, incr_s = pm.group(1), pm.group(2), pm.group(3)
        try:
            incr = float(incr_s)
        except ValueError:
            incr = None
        is_net = cell.lower() in ("net", "wire") or "/net" in cell.lower()
        kind = "net" if is_net else "cell"
        pt = PathPoint(pin=pin, cell=cell, incr_ns=incr, kind=kind)
        d.points.append(pt)
        if incr is None:
            continue
        if kind == "net":
            d.net_delay_ns += incr if not net_m else 0.0
            net_contrib[pin] = net_contrib.get(pin, 0.0) + incr
        else:
            if not cell_m:
                d.cell_delay_ns += incr
            cell_contrib[f"{cell}:{pin}"] = cell_contrib.get(f"{cell}:{pin}", 0.0) + incr
            if "dfxtp" not in cell.lower() and "dff" not in cell.lower():
                d.logic_depth += 0 if cell_m else 1

    if slews:
        d.max_slew_ns = max(slews)
    if caps:
        d.max_cap = max(caps)
    if fanouts:
        d.max_fanout = max(d.max_fanout, max(fanouts))

    if cell_contrib and not d.hot_cells:
        d.hot_cells = [k for k, _ in sorted(cell_contrib.items(), key=lambda kv: -kv[1])[:5]]
    if net_contrib and not d.hot_nets:
        d.hot_nets = [k for k, _ in sorted(net_contrib.items(), key=lambda kv: -kv[1])[:5]]

    # Keep STA-provided dominant when present; otherwise classify from delay shares.
    if d.dominant == "unknown":
        total = d.cell_delay_ns + d.net_delay_ns
        candidates: list[tuple[str, float]] = []
        if total > 0:
            candidates.append(("cell_delay", d.cell_delay_ns / total))
            candidates.append(("net_delay", d.net_delay_ns / total))
        if d.max_fanout >= 20:
            candidates.append(("fanout", min(1.0, d.max_fanout / 64.0)))
            d.notes.append(f"high fanout={d.max_fanout}")
        if d.max_slew_ns is not None and d.max_slew_ns > 0.5:
            candidates.append(("slew", min(1.0, d.max_slew_ns / 2.0)))
            d.notes.append(f"elevated slew={d.max_slew_ns:.3f} ns")
        if d.max_cap is not None and d.max_cap > 0.1:
            candidates.append(("capacitance", min(1.0, d.max_cap / 0.2)))
            d.notes.append(f"elevated cap={d.max_cap}")
        if d.clock_uncertainty_ns is not None and d.arrival_ns:
            share = abs(d.clock_uncertainty_ns) / max(abs(d.arrival_ns), 1e-9)
            if share > 0.05:
                candidates.append(("clock_uncertainty", share))
                d.notes.append(f"clock uncertainty={d.clock_uncertainty_ns:.3f} ns")

        if candidates:
            candidates.sort(key=lambda x: -x[1])
            d.dominant = candidates[0][0]
            d.dominant_share = candidates[0][1]
        elif total > 0:
            d.dominant = "cell_delay" if d.cell_delay_ns >= d.net_delay_ns else "net_delay"
            d.dominant_share = max(d.cell_delay_ns, d.net_delay_ns) / total
    return d


def diagnose_paths_report(path: Path | str, *, limit: int = 20) -> list[PathDiagnosis]:
    """Parse paths_setup.rpt / critical_paths.rpt style files."""
    text = Path(path).read_text(errors="replace")
    chunks: list[str] = []
    if "Path 1:" in text and "Cell delay:" in text:
        # critical_paths.rpt style
        parts = re.split(r"(?=^Path \d+:)", text, flags=re.M)
        chunks = [p for p in parts if p.strip().startswith("Path ")]
    else:
        parts = _PATH_SPLIT.split(text)
        chunks = [p for p in parts if p.strip().startswith("Startpoint:")]
    out = []
    for chunk in chunks[:limit]:
        out.append(diagnose_path_text(chunk))
    return out


def diagnosis_table(paths: list[PathDiagnosis]) -> tuple[list[str], list[list[Any]]]:
    headers = (
        "rank", "slack_ns", "dominant", "share", "cell_ns", "net_ns",
        "depth", "fanout", "slew_ns", "uncert_ns", "group", "endpoint", "hot_cells",
    )
    rows = []
    for i, p in enumerate(paths, 1):
        rows.append([
            i,
            p.slack_ns,
            p.dominant,
            None if p.dominant_share is None else round(p.dominant_share, 3),
            round(p.cell_delay_ns, 4),
            round(p.net_delay_ns, 4),
            p.logic_depth,
            p.max_fanout,
            p.max_slew_ns,
            p.clock_uncertainty_ns,
            p.group,
            p.endpoint,
            ";".join(p.hot_cells[:3]) or "NA",
        ])
    return list(headers), rows
