"""Persistent dataset of implementation runs for tradeoff exploration."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..schema import (
    AREA_STD,
    CONG_WEIGHTED,
    FMAX_MHZ,
    HPWL,
    POWER_TOTAL,
    SETUP_WNS,
    UTIL_ALL,
    RunSnapshot,
    load_snapshot,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS impl_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    run_tag TEXT NOT NULL,
    stage TEXT NOT NULL,
    corner TEXT NOT NULL DEFAULT '-',
    run_id TEXT,
    collected_at TEXT NOT NULL,
    area_um2 REAL,
    freq_mhz REAL,
    setup_ws_ns REAL,
    util_pct REAL,
    power_w REAL,
    congestion REAL,
    hpwl_um REAL,
    payload TEXT NOT NULL,
    UNIQUE(platform, run_tag, stage, corner, run_id)
);
CREATE INDEX IF NOT EXISTS idx_impl_plat ON impl_runs (platform, stage);
"""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class ImplRecord:
    platform: str
    run_tag: str
    stage: str
    corner: str = "-"
    run_id: str | None = None
    area_um2: float | None = None
    freq_mhz: float | None = None
    setup_ws_ns: float | None = None
    util_pct: float | None = None
    power_w: float | None = None
    congestion: float | None = None
    hpwl_um: float | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    collected_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


class RunDataset:
    """SQLite-backed store of implementation Pareto points."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path)
        try:
            con.executescript(SCHEMA)
            con.commit()
        finally:
            con.close()

    def ingest_snapshot(self, snap: RunSnapshot) -> ImplRecord:
        rec = ImplRecord(
            platform=snap.platform,
            run_tag=snap.run_tag,
            stage=snap.stage,
            corner=snap.corner,
            run_id=snap.run_id,
            area_um2=snap.num(AREA_STD),
            freq_mhz=snap.num(FMAX_MHZ),
            setup_ws_ns=snap.num(SETUP_WNS),
            util_pct=snap.num(UTIL_ALL),
            power_w=snap.num(POWER_TOTAL),
            congestion=snap.num(CONG_WEIGHTED),
            hpwl_um=snap.num(HPWL),
            payload=snap.as_flat(),
        )
        self.upsert(rec)
        return rec

    def ingest_from_metrics_db(
        self,
        metrics_db: Path | str,
        *,
        platform: str,
        run_tag: str,
        stage: str,
        corner: str | None = None,
    ) -> ImplRecord:
        snap = load_snapshot(
            metrics_db,
            platform=platform,
            run_tag=run_tag,
            stage=stage,
            corner=corner,
        )
        return self.ingest_snapshot(snap)

    def upsert(self, rec: ImplRecord) -> None:
        con = sqlite3.connect(self.path)
        try:
            with con:
                con.execute(
                    "INSERT INTO impl_runs ("
                    "platform, run_tag, stage, corner, run_id, collected_at,"
                    "area_um2, freq_mhz, setup_ws_ns, util_pct, power_w, congestion, hpwl_um, payload"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(platform, run_tag, stage, corner, run_id) DO UPDATE SET "
                    "collected_at=excluded.collected_at,"
                    "area_um2=excluded.area_um2, freq_mhz=excluded.freq_mhz,"
                    "setup_ws_ns=excluded.setup_ws_ns, util_pct=excluded.util_pct,"
                    "power_w=excluded.power_w, congestion=excluded.congestion,"
                    "hpwl_um=excluded.hpwl_um, payload=excluded.payload",
                    (
                        rec.platform,
                        rec.run_tag,
                        rec.stage,
                        rec.corner,
                        rec.run_id or "",
                        rec.collected_at,
                        rec.area_um2,
                        rec.freq_mhz,
                        rec.setup_ws_ns,
                        rec.util_pct,
                        rec.power_w,
                        rec.congestion,
                        rec.hpwl_um,
                        json.dumps(rec.payload, sort_keys=True),
                    ),
                )
        finally:
            con.close()

    def list_records(
        self,
        *,
        platform: str | None = None,
        stage: str | None = None,
    ) -> list[ImplRecord]:
        con = sqlite3.connect(self.path)
        try:
            sql = "SELECT platform, run_tag, stage, corner, run_id, collected_at, area_um2, freq_mhz, setup_ws_ns, util_pct, power_w, congestion, hpwl_um, payload FROM impl_runs WHERE 1=1"
            params: list[Any] = []
            if platform:
                sql += " AND platform=?"
                params.append(platform)
            if stage:
                sql += " AND stage=?"
                params.append(stage)
            sql += " ORDER BY collected_at"
            rows = con.execute(sql, params).fetchall()
        finally:
            con.close()
        out = []
        for r in rows:
            out.append(
                ImplRecord(
                    platform=r[0],
                    run_tag=r[1],
                    stage=r[2],
                    corner=r[3],
                    run_id=r[4] or None,
                    collected_at=r[5],
                    area_um2=r[6],
                    freq_mhz=r[7],
                    setup_ws_ns=r[8],
                    util_pct=r[9],
                    power_w=r[10],
                    congestion=r[11],
                    hpwl_um=r[12],
                    payload=json.loads(r[13] or "{}"),
                )
            )
        return out

    def export_csv(self, path: Path | str, **filters: Any) -> Path:
        from ..tables import write_csv

        recs = self.list_records(platform=filters.get("platform"), stage=filters.get("stage"))
        headers = (
            "platform", "run_tag", "stage", "corner", "area_um2", "freq_mhz",
            "setup_ws_ns", "util_pct", "power_w", "congestion", "hpwl_um",
        )
        rows = [
            [r.platform, r.run_tag, r.stage, r.corner, r.area_um2, r.freq_mhz,
             r.setup_ws_ns, r.util_pct, r.power_w, r.congestion, r.hpwl_um]
            for r in recs
        ]
        return write_csv(Path(path), headers, rows)
