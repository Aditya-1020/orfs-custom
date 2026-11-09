import csv
import sqlite3

from asicflow.metric_engine.reporting import export_wide
from asicflow.metric_engine.store import ensure_schema, ingest, record_run


def test_metric_store_preserves_runs_and_replaces_one_run_stage(tmp_path):
    db = tmp_path / "metrics.db"
    record_run(
        db,
        run_id="run-1",
        run_tag="baseline",
        platform="sky130hd",
        started_at="2026-11-09T10:00:00",
        status="running",
    )
    record_run(
        db,
        run_id="run-2",
        run_tag="baseline",
        platform="sky130hd",
        started_at="2026-11-09T11:00:00",
        status="running",
    )
    rows = [
        ("baseline", "3_place", "tt", "timing", "setup_ws", "-0.1", "ns"),
        ("baseline", "3_place", "-", "design", "area_std_um2", "1000", "um2"),
    ]

    ingest(db, "sky130hd", rows, run_id="run-1")
    ingest(db, "sky130hd", rows, run_id="run-1")
    ingest(
        db,
        "sky130hd",
        [("baseline", "3_place", "tt", "timing", "setup_ws", "-0.2", "ns")],
        run_id="run-2",
    )

    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM metrics WHERE run_id='run-1'").fetchone()[0] == 2
        assert con.execute(
            "SELECT value FROM metrics WHERE run_id='run-2' AND metric='setup_ws'"
        ).fetchone()[0] == "-0.2"


def test_metric_export_is_a_reporting_operation(tmp_path):
    db = tmp_path / "metrics.db"
    with sqlite3.connect(db) as con:
        ensure_schema(con)
    ingest(
        db,
        "sky130hd",
        [
            ("baseline", "3_place", "-", "design", "area_std_um2", "1000", "um2"),
            ("baseline", "3_place", "tt", "timing", "setup_ws", "-0.1", "ns"),
        ],
        run_id="run-1",
    )
    output = tmp_path / "metrics.csv"
    export_wide(db, output)

    with output.open(newline="") as stream:
        rows = list(csv.DictReader(stream))

    assert len(rows) == 1
    assert rows[0]["design.area_std_um2"] == "1000"
    assert rows[0]["timing.setup_ws"] == "-0.1"
