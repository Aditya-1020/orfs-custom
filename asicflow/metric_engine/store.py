"""SQLite persistence, schema migration and metric export."""
import csv
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    run_tag TEXT NOT NULL,
    platform TEXT NOT NULL,
    design TEXT,
    flow_variant TEXT,
    config_snapshot TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    stage TEXT,
    status TEXT NOT NULL,
    exit_code INTEGER,
    failure_info TEXT,
    odb_path TEXT,
    log_path TEXT,
    report_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_tag ON runs (platform, run_tag, started_at);

CREATE TABLE IF NOT EXISTS metrics (
    run_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    run_tag TEXT NOT NULL,
    stage TEXT NOT NULL,
    corner TEXT NOT NULL,
    category TEXT NOT NULL,
    metric TEXT NOT NULL,
    value TEXT,
    value_num REAL,
    unit TEXT,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (run_id, stage, corner, category, metric)
);
CREATE INDEX IF NOT EXISTS idx_metrics_lookup ON metrics (platform, run_tag, stage, category, metric);
CREATE INDEX IF NOT EXISTS idx_metrics_run ON metrics (run_id);
"""

def _metrics_pk_cols(con):
    rows = con.execute("PRAGMA table_info(metrics)").fetchall()
    # pk columns ordered by pk index
    return [r[1] for r in sorted(rows, key=lambda r: r[5] or 0) if r[5]]


def ensure_schema(con):
    """Create or safely migrate the metrics schema. Preserves existing rows."""
    con.executescript(
        "CREATE TABLE IF NOT EXISTS runs ("
        "run_id TEXT PRIMARY KEY, run_tag TEXT NOT NULL, platform TEXT NOT NULL,"
        "design TEXT, flow_variant TEXT, config_snapshot TEXT,"
        "started_at TEXT NOT NULL, ended_at TEXT, stage TEXT, status TEXT NOT NULL,"
        "exit_code INTEGER, failure_info TEXT, odb_path TEXT, log_path TEXT, report_path TEXT);"
    )
    # Add optional columns to older runs tables.
    run_cols = {r[1] for r in con.execute("PRAGMA table_info(runs)")}
    for col, decl in (("design", "TEXT"), ("flow_variant", "TEXT")):
        if col not in run_cols:
            try:
                con.execute(f"ALTER TABLE runs ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass

    exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='metrics'"
    ).fetchone()
    if not exists:
        con.executescript(SCHEMA)
        return

    pk = _metrics_pk_cols(con)
    cols = {r[1] for r in con.execute("PRAGMA table_info(metrics)")}
    # Desired PK is run_id-first. Migrate if the old composite tag PK is still in use.
    needs_migrate = pk != ["run_id", "stage", "corner", "category", "metric"]
    if not needs_migrate:
        con.executescript(
            "CREATE INDEX IF NOT EXISTS idx_metrics_lookup "
            "ON metrics (platform, run_tag, stage, category, metric);"
            "CREATE INDEX IF NOT EXISTS idx_metrics_run ON metrics (run_id);"
            "CREATE INDEX IF NOT EXISTS idx_runs_tag ON runs (platform, run_tag, started_at);"
        )
        return

    # Backup-preserving rebuild.
    con.execute("ALTER TABLE metrics RENAME TO metrics_legacy")
    con.executescript(SCHEMA)
    # Copy legacy rows; synthesize a run_id when missing so history is retained.
    leg_cols = {r[1] for r in con.execute("PRAGMA table_info(metrics_legacy)")}
    has_run_id = "run_id" in leg_cols
    rows = con.execute("SELECT * FROM metrics_legacy").fetchall()
    leg_names = [r[1] for r in con.execute("PRAGMA table_info(metrics_legacy)")]
    for row in rows:
        d = dict(zip(leg_names, row))
        rid = d.get("run_id") if has_run_id else None
        if not rid:
            rid = (
                f"legacy-{d.get('platform','')}-{d.get('run_tag','')}-"
                f"{d.get('stage','')}-{d.get('corner','')}-{d.get('collected_at','')}"
            )
        con.execute(
            "INSERT OR IGNORE INTO metrics "
            "(run_id, platform, run_tag, stage, corner, category, metric, value, value_num, unit, collected_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                rid,
                d.get("platform"),
                d.get("run_tag"),
                d.get("stage"),
                d.get("corner"),
                d.get("category"),
                d.get("metric"),
                d.get("value"),
                d.get("value_num"),
                d.get("unit"),
                d.get("collected_at") or "",
            ),
        )
    con.execute("DROP TABLE metrics_legacy")
    _ = cols  # kept for clarity of migration path

def to_float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def ingest(db_path, platform, rows, run_id=None):
    """Insert metrics for this run_id. Prior runs with the same tag are retained."""
    if not run_id:
        raise ValueError("ingest requires run_id so historical metrics are preserved")
    now = datetime.now().isoformat(timespec="seconds")
    con = sqlite3.connect(db_path)
    ensure_schema(con)
    with con:
        # Idempotent rewrite only for (run_id, stage, corner) groups in this batch.
        for stage, corner in sorted({(r[1], r[2]) for r in rows}):
            con.execute(
                "DELETE FROM metrics WHERE run_id=? AND stage=? AND corner=?",
                (run_id, stage, corner),
            )
        con.executemany(
            "INSERT INTO metrics "
            "(run_id, platform, run_tag, stage, corner, category, metric, value, value_num, unit, collected_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (run_id, platform, r[0], r[1], r[2], r[3], r[4], r[5], to_float(r[5]), r[6], now)
                for r in rows
            ],
        )
    con.close()


def record_run(db_path, **fields):
    """Insert or update a flow/metrics run row. fields must include run_id."""
    con = sqlite3.connect(db_path)
    ensure_schema(con)
    cols = [
        "run_id", "run_tag", "platform", "design", "flow_variant", "config_snapshot",
        "started_at", "ended_at", "stage", "status", "exit_code", "failure_info",
        "odb_path", "log_path", "report_path",
    ]
    vals = {c: fields.get(c) for c in cols}
    with con:
        exists = con.execute("SELECT 1 FROM runs WHERE run_id=?", (vals["run_id"],)).fetchone()
        if not exists:
            con.execute(
                f"INSERT INTO runs ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                [vals[c] for c in cols],
            )
        else:
            sets = [c for c in cols if c != "run_id" and c in fields]
            con.execute(
                f"UPDATE runs SET {', '.join(c+'=?' for c in sets)} WHERE run_id=?",
                [fields[c] for c in sets] + [vals["run_id"]],
            )
    con.close()


def export_wide(db_path, out_csv):
    """One row per (platform, run, stage, corner); columns '<category>.<metric>'.
    Design-level rows (corner '-') are repeated on every corner row of that run/stage."""
    con = sqlite3.connect(db_path)
    design, per_corner = defaultdict(dict), defaultdict(dict)
    for plat, run, stage, corner, col, val in con.execute(
            "SELECT platform, run_tag, stage, corner, category||'.'||metric, value "
            "FROM metrics ORDER BY platform, run_tag, stage, corner, category, metric"):
        if corner == "-":
            design[(plat, run, stage)][col] = val
        else:
            per_corner[(plat, run, stage, corner)][col] = val
    con.close()
    table = [{"platform": k[0], "run_tag": k[1], "stage": k[2], "corner": k[3],
              **design.get(k[:3], {}), **v} for k, v in sorted(per_corner.items())]
    columns = sorted({c for row in table for c in row} - {"platform", "run_tag", "stage", "corner"})
    with open(out_csv, "w", newline="") as fh:
        writer = csv.DictWriter(fh, ["platform", "run_tag", "stage", "corner"] + columns)
        writer.writeheader()
        writer.writerows(table)
    print(f"WIDE: {out_csv}  ({len(table)} rows x {len(columns)} metric columns)")


# ------------------------------------------------------------------------ main
