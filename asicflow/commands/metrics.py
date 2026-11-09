"""Metric collection, query and run-history command handlers."""
from __future__ import annotations
import os
import sqlite3
import sys
from pathlib import Path
from .. import metrics as metrics_mod
from ..config import ORFS_STAGE_ORDER, RunContext, canon_stage, ctx_from_args as _ctx, resolve_openroad, stage_stem
from ..tables import emit_table
from ..util import FlowError, log as _log, now as _now, run as _run, run_id as _run_id
from .orfs import _snapshot, _track

def cmd_metrics(args):
    ctx = _ctx(args)
    stages = (
        list(ORFS_STAGE_ORDER)
        if args.all_stages
        else (args.stages or ([args.stage] if args.stage else []))
    )
    # Normalize finish→final for metrics stems.
    stages = ["final" if canon_stage(s) == "finish" else s for s in stages]
    env = os.environ.copy()
    env.update(
        {
            "PLATFORM": ctx.platform,
            "FAKERAM_TECH": ctx.fakeram_tech,
            "ORFS_FLOW": str(ctx.orfs_flow),
            "OUTROOT": str(args.outroot or ctx.work_home.parent),
            "RUNS": args.runs or ctx.run_tag,
            "CORNERS": args.corners,
            "HPWL": "1" if args.hpwl else "0",
            "SAIF": args.saif or "",
            "SAIF_SCOPE": args.saif_scope,
            "OPENROAD": resolve_openroad(ctx.orfs_flow),
            "METRICS_DB": str(ctx.metrics_db),
            "ANALYSIS_DIR": str(ctx.analysis_dir),
            "RUN_ID": _run_id(),
        }
    )
    cmd = [sys.executable, "-m", "asicflow.metrics"]
    if args.export:
        cmd += ["--export", args.export]
    cmd += stages
    db = ctx.metrics_db
    run_id = env["RUN_ID"]
    _track(
        db,
        run_id=run_id,
        run_tag=env["RUNS"],
        platform=ctx.platform,
        design=ctx.design_nickname,
        flow_variant=ctx.flow_variant,
        config_snapshot=_snapshot(ctx),
        started_at=_now(),
        ended_at=None,
        stage="metrics:" + (" ".join(stages) or "export"),
        status="running",
        exit_code=None,
        failure_info=None,
        odb_path="NA",
        log_path="NA",
        report_path=str(db.parent),
    )
    code = _run(cmd, env=env, check=False)
    _track(
        db,
        run_id=run_id,
        ended_at=_now(),
        status="success" if code == 0 else "failed",
        exit_code=code,
        failure_info=None if code == 0 else f"metrics exit {code}",
    )
    return code


def cmd_query(args):
    ctx = _ctx(args)
    db = Path(args.db) if args.db else ctx.metrics_db
    if not db.is_file():
        raise FlowError(f"no database: {db}")
    stage = args.stage or "place"
    stem = stage_stem(stage)
    cats = [c.strip() for c in args.category.split(",") if c.strip()]
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT run_tag, stage, corner, category, metric, value, unit, run_id FROM metrics "
        "WHERE platform=? AND run_tag=? AND (stage=? OR stage=?) AND category IN ({}) "
        "ORDER BY collected_at DESC, corner, category, metric".format(
            ",".join("?" * len(cats))
        ),
        (ctx.platform, ctx.run_tag, stem, stage, *cats),
    ).fetchall()
    if not rows:
        have = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT stage FROM metrics WHERE platform=? AND run_tag=?",
                (ctx.platform, ctx.run_tag),
            )
        ]
        con.close()
        raise FlowError(
            f"no rows platform={ctx.platform} run_tag={ctx.run_tag} stage={stem}; have {have}"
        )
    con.close()
    out = ctx.analysis_dir / "query.csv"
    headers = ("run_tag", "stage", "corner", "category", "metric", "value", "unit", "run_id")
    _log(f"DB: {db}")
    emit_table(headers, rows, title=f"query {ctx.platform}/{ctx.run_tag}/{stem}", csv_path=out)
    return 0


def cmd_report(args):
    ctx = _ctx(args)
    db = Path(args.db) if args.db else ctx.metrics_db
    if not db.is_file():
        raise FlowError(f"no database: {db}")
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT run_id, run_tag, platform, stage, status, exit_code, started_at FROM runs "
        "WHERE platform=? ORDER BY started_at DESC LIMIT ?",
        (ctx.platform, args.limit),
    ).fetchall()
    con.close()
    out = ctx.analysis_dir / "runs.csv"
    headers = ("run_id", "run_tag", "platform", "stage", "status", "exit_code", "started_at")
    _log(f"DB: {db}")
    emit_table(headers, rows, title="recent runs", csv_path=out)
    return 0
