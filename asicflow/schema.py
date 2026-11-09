"""Canonical metric keys and typed run snapshots for scripts and reports.

Scripts should prefer MetricKey / RunSnapshot over raw SQL column names.
Values missing from a run are always None (never silent zeros).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence


NA = "NA"


@dataclass(frozen=True)
class MetricKey:
    """Stable identity for one collected metric."""

    category: str
    metric: str
    unit: str = ""
    # True  → larger is better (WNS, fmax)
    # False → smaller is better (TNS magnitude as more-negative worse, area, power, cong)
    # None  → not ranked
    higher_is_better: bool | None = None
    description: str = ""

    @property
    def key(self) -> str:
        return f"{self.category}.{self.metric}"

    def __str__(self) -> str:
        return self.key


# ---- canonical catalogue -------------------------------------------------
SETUP_WNS = MetricKey("timing", "setup_ws", "ns", True, "Setup worst negative slack (blended)")
SETUP_TNS = MetricKey("timing", "setup_tns", "ns", True, "Setup total negative slack")
HOLD_WNS = MetricKey("timing", "hold_ws", "ns", True, "Hold worst slack")
HOLD_TNS = MetricKey("timing", "hold_tns", "ns", True, "Hold total negative slack")
FMAX_MHZ = MetricKey("timing", "fmax_est_mhz", "MHz", True, "Estimated Fmax from WNS")
CLK_PERIOD = MetricKey("timing", "clock_period_ns", "ns", None, "Primary clock period")
SETUP_VIOL = MetricKey("timing", "setup_violations", "count", False, "Setup path violations")
HOLD_VIOL = MetricKey("timing", "hold_violations", "count", False, "Hold path violations")
SLEW_VIOL = MetricKey("timing", "max_slew_violations", "count", False, "Max-slew violators")
CAP_VIOL = MetricKey("timing", "max_cap_violations", "count", False, "Max-cap violators")
FANOUT_VIOL = MetricKey("timing", "max_fanout_violations", "count", False, "Max-fanout violators")

AREA_STD = MetricKey("design", "area_std_um2", "um2", False, "Stdcell area")
AREA_MACRO = MetricKey("design", "area_macro_um2", "um2", False, "Macro area")
UTIL_ALL = MetricKey("design", "util_all_pct", "%", False, "Core utilization (std+macro)")
UTIL_STD = MetricKey("design", "util_std_excl_macro_pct", "%", False, "Stdcell util excluding macros")
INST_STD = MetricKey("design", "inst_std_count", "count", False, "Stdcell instance count")
INST_BUF = MetricKey("design", "inst_buffer_count", "count", False, "Buffer count")
INST_INV = MetricKey("design", "inst_inverter_count", "count", False, "Inverter count")

HPWL = MetricKey("net", "hpwl_total_um", "um", False, "Total HPWL")
FANOUT_MAX = MetricKey("net", "fanout_max", "", False, "Max fanout")
FANOUT_AVG = MetricKey("net", "fanout_avg", "", False, "Average fanout")

POWER_TOTAL = MetricKey("power", "total.total_W", "W", False, "Total power")

CONG_WEIGHTED = MetricKey("predict.feat", "gp_cong_weighted_last", "", False, "GP weighted congestion")
OVERFLOW = MetricKey("predict.feat", "gp_overflow_final", "", False, "GP overflow")
PREDICT_SCORE = MetricKey("predict", "score", "", True, "Routability heuristic score")

# STA corner JSON keys (from asicflow.sta / sta.tcl metrics.json)
STA_SETUP_WS = MetricKey("sta", "setup_ws_ns", "ns", True)
STA_SETUP_TNS = MetricKey("sta", "setup_tns_ns", "ns", True)
STA_HOLD_WS = MetricKey("sta", "hold_ws_ns", "ns", True)
STA_FMAX = MetricKey("sta", "fmax_mhz", "MHz", True)
STA_CORE_WS = MetricKey("sta", "ws_clk_core_ns", "ns", True)
STA_APB_WS = MetricKey("sta", "ws_clk_apb_ns", "ns", True)

# Headline metrics used for baseline deltas on every run.
HEADLINE: tuple[MetricKey, ...] = (
    SETUP_WNS,
    SETUP_TNS,
    HOLD_WNS,
    HOLD_TNS,
    FMAX_MHZ,
    AREA_STD,
    UTIL_ALL,
    HPWL,
    POWER_TOTAL,
    SETUP_VIOL,
    HOLD_VIOL,
    SLEW_VIOL,
    CAP_VIOL,
    FANOUT_VIOL,
    CONG_WEIGHTED,
)

REGRESSION_KEYS: tuple[str, ...] = tuple(m.key for m in HEADLINE)

CATALOGUE: dict[str, MetricKey] = {m.key: m for m in (
    SETUP_WNS, SETUP_TNS, HOLD_WNS, HOLD_TNS, FMAX_MHZ, CLK_PERIOD,
    SETUP_VIOL, HOLD_VIOL, SLEW_VIOL, CAP_VIOL, FANOUT_VIOL,
    AREA_STD, AREA_MACRO, UTIL_ALL, UTIL_STD, INST_STD, INST_BUF, INST_INV,
    HPWL, FANOUT_MAX, FANOUT_AVG, POWER_TOTAL,
    CONG_WEIGHTED, OVERFLOW, PREDICT_SCORE,
    STA_SETUP_WS, STA_SETUP_TNS, STA_HOLD_WS, STA_FMAX, STA_CORE_WS, STA_APB_WS,
)}


@dataclass
class MetricValue:
    key: MetricKey
    value: float | str | None
    unit: str = ""
    corner: str = "-"
    stage: str = ""
    raw: str | None = None

    @property
    def numeric(self) -> float | None:
        if self.value is None or self.value == NA:
            return None
        try:
            return float(self.value)
        except (TypeError, ValueError):
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key.key,
            "category": self.key.category,
            "metric": self.key.metric,
            "value": self.value if self.value is not None else NA,
            "value_num": self.numeric,
            "unit": self.unit or self.key.unit,
            "corner": self.corner,
            "stage": self.stage,
        }


@dataclass
class RunSnapshot:
    """Flat metric map for one (platform, run_tag, stage[, corner]) view."""

    platform: str
    run_tag: str
    stage: str
    corner: str = "-"
    run_id: str | None = None
    values: dict[str, MetricValue] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)

    def get(self, key: MetricKey | str) -> MetricValue | None:
        k = key.key if isinstance(key, MetricKey) else key
        return self.values.get(k)

    def num(self, key: MetricKey | str) -> float | None:
        mv = self.get(key)
        return None if mv is None else mv.numeric

    def as_flat(self) -> dict[str, Any]:
        out = {
            "platform": self.platform,
            "run_tag": self.run_tag,
            "stage": self.stage,
            "corner": self.corner,
            "run_id": self.run_id,
        }
        for k, mv in self.values.items():
            out[k] = mv.numeric if mv.numeric is not None else mv.value
        out.update(self.extras)
        return out

    def headline(self) -> dict[str, Any]:
        flat = self.as_flat()
        return {k: flat.get(k) for k in REGRESSION_KEYS}


def _to_float(text: Any) -> float | None:
    try:
        if text is None or text == NA or text == "":
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def load_snapshot(
    db: Path | str,
    *,
    platform: str,
    run_tag: str,
    stage: str,
    corner: str | None = None,
    run_id: str | None = None,
) -> RunSnapshot:
    """Load latest metrics for a run into a RunSnapshot."""
    db = Path(db)
    snap = RunSnapshot(platform=platform, run_tag=run_tag, stage=stage, corner=corner or "-")
    if not db.is_file():
        return snap
    con = sqlite3.connect(db)
    try:
        params: list[Any] = [platform, run_tag, stage]
        sql = (
            "SELECT run_id, corner, category, metric, value, value_num, unit, collected_at "
            "FROM metrics WHERE platform=? AND run_tag=? AND stage=?"
        )
        if run_id:
            sql += " AND run_id=?"
            params.append(run_id)
        if corner is not None:
            sql += " AND (corner=? OR corner='-')"
            params.append(corner)
        sql += " ORDER BY collected_at DESC"
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()

    seen: set[tuple[str, str, str]] = set()
    for rid, corn, cat, metric, value, value_num, unit, _ts in rows:
        if snap.run_id is None:
            snap.run_id = rid
        # Prefer exact corner rows; design-scope corner '-' always accepted.
        if corner is not None and corn not in (corner, "-"):
            continue
        sig = (corn, cat, metric)
        if sig in seen:
            continue
        seen.add(sig)
        key = f"{cat}.{metric}"
        mk = CATALOGUE.get(key) or MetricKey(cat, metric, unit or "")
        num = value_num if value_num is not None else _to_float(value)
        # Prefer corner-specific for timing/power; design rows stay.
        existing = snap.values.get(key)
        if existing and existing.corner != "-" and corn == "-":
            continue
        snap.values[key] = MetricValue(
            key=mk,
            value=num if num is not None else value,
            unit=unit or mk.unit,
            corner=corn,
            stage=stage,
            raw=None if value is None else str(value),
        )
    if corner is not None:
        snap.corner = corner
    return snap


def snapshots_for_tags(
    db: Path | str,
    *,
    platform: str,
    run_tags: Sequence[str],
    stage: str,
    corner: str | None = None,
) -> list[RunSnapshot]:
    return [
        load_snapshot(db, platform=platform, run_tag=t, stage=stage, corner=corner)
        for t in run_tags
    ]


def iter_catalogue() -> Iterator[MetricKey]:
    yield from CATALOGUE.values()


def compare_snapshots(
    current: RunSnapshot,
    baseline: RunSnapshot,
    keys: Iterable[MetricKey | str] | None = None,
) -> list[dict[str, Any]]:
    """Return list of delta dicts consumable by scripts."""
    want = list(keys) if keys is not None else list(HEADLINE)
    out = []
    for item in want:
        mk = item if isinstance(item, MetricKey) else CATALOGUE.get(item) or MetricKey(
            *item.split(".", 1) if "." in item else ("", item)
        )
        key = mk.key
        c = current.num(mk)
        b = baseline.num(mk)
        abs_d = pct = None
        if c is not None and b is not None:
            abs_d = c - b
            pct = ((c - b) / abs(b) * 100.0) if b != 0 else None
        improved = None
        if abs_d is not None and mk.higher_is_better is not None:
            improved = abs_d > 0 if mk.higher_is_better else abs_d < 0
        out.append({
            "key": key,
            "unit": mk.unit,
            "baseline": b,
            "current": c,
            "abs_delta": abs_d,
            "pct_delta": pct,
            "improved": improved,
            "higher_is_better": mk.higher_is_better,
        })
    return out
