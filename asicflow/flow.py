"""
ORFS orchestration CLI.

  make X  ==  python3 -m asicflow X --platform … --run-tag …

Pipeline: place-gen? → synth → floorplan → place → predict → cts → route → finish
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import traceback
from pathlib import Path

from .actions import register_analysis_parsers, resolve_baseline_tag
from .config import (
    ORFS_STAGE_ORDER,
    RunContext,
    canon_stage,
    ctx_from_args as _ctx,
    resolve_openroad,
    stage_stem,
)
from .util import FlowError, log as _log, now as _now, run as _run, run_id as _run_id
from .physical import run_prediction, write_placement, write_presets
from . import metrics as metrics_mod
from .sta import run_presta, run_sta_pvt, run_sta_shell
from .tables import emit_table
from .plugins.registry import register_cli_plugins


def _snapshot(ctx: RunContext) -> str:
    return json.dumps(ctx.snapshot(), sort_keys=True)


def _track(db: Path, **fields):
    db.parent.mkdir(parents=True, exist_ok=True)
    metrics_mod.record_run(db, **fields)


def orfs(ctx: RunContext, target: str) -> int:
    ctx.validate_config()
    cmd = [
        "make",
        "--no-print-directory",
        "-C",
        str(ctx.orfs_flow),
        f"DESIGN_CONFIG={ctx.design_config}",
        f"WORK_HOME={ctx.work_home}",
        f"FLOW_VARIANT={ctx.flow_variant}",
        target,
    ]
    env = ctx.orfs_env()
    if ctx.placement and ctx.placement != "rtlmp":
        cmd.append(f"PLACEMENT={ctx.placement}")
        cmd.append(f"MACRO_PLACEMENT_TCL={env['MACRO_PLACEMENT_TCL']}")
    return _run(cmd, env=env)


def run_orfs_stage(ctx: RunContext, stage: str) -> int:
    ctx.validate_fakeram()
    run_id = _run_id()
    db = ctx.metrics_db
    started = _now()
    odb = ctx.odb_path(stage)
    log = ctx.logs_dir / f"{stage_stem(stage)}.log"
    fields = dict(
        run_id=run_id,
        run_tag=ctx.run_tag,
        platform=ctx.platform,
        design=ctx.design_nickname,
        flow_variant=ctx.flow_variant,
        config_snapshot=_snapshot(ctx),
        stage=stage,
        odb_path=str(odb),
        log_path=str(log),
        report_path=str(ctx.reports_dir),
        started_at=started,
    )
    _track(db, **fields, ended_at=None, status="running", exit_code=None, failure_info=None)
    code, err = 1, None
    try:
        code = orfs(ctx, stage)
    except Exception as exc:
        err = f"{exc}\n{traceback.format_exc()}"
        code = 1
        raise
    finally:
        fields["ended_at"] = _now()
        fields["status"] = "success" if code == 0 else "failed"
        fields["exit_code"] = code
        fields["failure_info"] = err
        fields["odb_path"] = str(odb) if odb.exists() else "NA"
        fields["log_path"] = str(log) if log.exists() else "NA"
        _track(db, **fields)
    return code


def cmd_validate(args):
    ctx = _ctx(args)
    ctx.validate_config()
    try:
        ctx.validate_fakeram()
        fr = "ok"
    except (FileNotFoundError, FlowError) as exc:
        fr = f"MISSING ({exc})"
    for k, v in (
        ("platform", ctx.platform),
        ("design_nickname", ctx.design_nickname),
        ("fakeram_tech", ctx.fakeram_tech),
        ("config", ctx.design_config),
        ("work_home", ctx.work_home),
        ("results", ctx.results_dir),
        ("flow_variant", ctx.flow_variant),
        ("run_tag", ctx.run_tag),
        ("placement", ctx.placement),
        ("fakeram", fr),
    ):
        _log(f"{k}={v}")
    return 0


def _maybe_sta_pvt(args, stage: str) -> int:
    if not getattr(args, "sta", False) and os.environ.get("STA_AFTER", "0") != "1":
        return 0
    sta_stage = canon_stage(stage)
    if sta_stage == "synth":
        _log("sta: skipped (synth stage has no ODB)")
        return 0
    return cmd_sta_pvt(
        argparse.Namespace(
            **{**vars(args), "stage": sta_stage, "all_corners": False, "paths": 20}
        )
    )


def cmd_stage(args):
    code = run_orfs_stage(_ctx(args), args.stage)
    if code != 0:
        return code
    sta_code = _maybe_sta_pvt(args, args.stage)
    return sta_code if sta_code else code


def cmd_flow(args):
    code = run_orfs_stage(_ctx(args), "finish")
    if code != 0:
        return code
    sta_code = _maybe_sta_pvt(args, "finish")
    return sta_code if sta_code else code


def cmd_gui(args):
    ctx = _ctx(args)
    t = args.target if args.target.startswith("gui_") else f"gui_{args.target}"
    return orfs(ctx, t)


def cmd_place_gen(args):
    ctx = _ctx(args)
    if args.presets:
        for path in write_presets(ctx):
            _log(f"wrote {path}")
        return 0
    if not args.side or not args.name:
        raise FlowError("place-gen needs --side and --name (or --presets)")
    macros = tuple(args.macros) if args.macros else ctx.macro_instances
    if len(macros) != 3:
        raise FlowError(
            "place-gen requires three macro instance paths; define them in the design YAML profile or pass --macros"
        )
    path = write_placement(
        ctx,
        args.name,
        side=args.side,
        gap=args.gap,
        chan=args.chan,
        macros=macros,
        generated=True,
    )
    _log(f"wrote {path}")
    _log(f"next: make place PLACEMENT={args.name} RUN_TAG={ctx.run_tag} PLATFORM={ctx.platform}")
    return 0


def cmd_predict(args):
    ctx = _ctx(args)
    odb = ctx.odb_path("place")
    out = ctx.analysis_dir / f"predict_{ctx.run_tag}.json"
    db = ctx.metrics_db
    run_id = _run_id()
    _track(
        db,
        run_id=run_id,
        run_tag=ctx.run_tag,
        platform=ctx.platform,
        design=ctx.design_nickname,
        flow_variant=ctx.flow_variant,
        config_snapshot=_snapshot(ctx),
        started_at=_now(),
        ended_at=None,
        stage="predict",
        status="running",
        exit_code=None,
        failure_info=None,
        odb_path=str(odb),
        log_path=str(out),
        report_path=str(ctx.analysis_dir),
    )
    code, err = 0, None
    try:
        result = run_prediction(
            odb_path=odb,
            run_tag=ctx.run_tag,
            logs_dir=ctx.logs_dir,
            reports_dir=ctx.reports_dir,
            metrics_db=db,
            out_json=out,
            platform=ctx.platform,
        )
        metrics_mod.ingest(
            db, ctx.platform, result.metric_rows(run_tag=ctx.run_tag), run_id=run_id
        )
        _log(
            f"predict: routable={result.routable} score={result.score:.4g} "
            f"(placement_routability_heuristic; {result.notes})"
        )
        _log(f"json → {out}")
        if args.fail_if_unroutable and not result.routable:
            code, err = 2, f"unroutable score={result.score}"
    except Exception as exc:
        code, err = 1, f"{exc}\n{traceback.format_exc()}"
        raise
    finally:
        _track(
            db,
            run_id=run_id,
            ended_at=_now(),
            status="success" if code == 0 else "failed",
            exit_code=code,
            failure_info=err,
        )
    return code


def cmd_place_predict(args):
    code = run_orfs_stage(_ctx(args), "place")
    return code if code else cmd_predict(args)


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


def cmd_netlist(args):
    ctx = _ctx(args)
    ctx.validate_config()
    netlist, odb = ctx.netlist_paths(args.stage)
    if netlist.is_file():
        _log(f"NETLIST={netlist}")
        return 0
    if odb and odb.is_file():
        orfs(ctx, odb.name + ".v")
        if not netlist.is_file():
            raise FlowError(f"dump failed: {netlist}")
        _log(f"NETLIST={netlist}")
        return 0
    raise FlowError(f"missing {netlist}; run stage {args.stage} first")


def cmd_sta_pvt(args):
    ctx = _ctx(args)
    code = run_sta_pvt(
        ctx,
        stage=getattr(args, "stage", "latest"),
        all_corners=getattr(args, "all_corners", False),
        paths=getattr(args, "paths", 20),
        baseline_tag=resolve_baseline_tag(args, ctx),
    )
    return code


def cmd_sta_shell(args):
    return run_sta_shell(
        _ctx(args),
        stage=getattr(args, "stage", "latest"),
        corner=getattr(args, "corner", None),
    )


def cmd_presta(args):
    return run_presta(_ctx(args), shell=args.shell)


def cmd_fix_lef(args):
    from .lef_fix import fix_file

    for p in args.files:
        _log(fix_file(Path(p)))
    return 0


def _common(sp):
    sp.add_argument(
        "--platform",
        default=os.environ.get("PLATFORM", os.environ.get("TECH")),
        help="ORFS platform; defaults to config/flow.yaml",
    )
    sp.add_argument("--run-tag", default=os.environ.get("RUN_TAG"))
    sp.add_argument(
        "--flow-variant",
        default=os.environ.get("FLOW_VARIANT"),
        help="ORFS FLOW_VARIANT (independent of RUN_TAG; default: base)",
    )
    sp.add_argument("--placement", default=os.environ.get("PLACEMENT", "rtlmp"))
    sp.add_argument("--design-config", default=os.environ.get("DESIGN_CONFIG"))
    sp.add_argument("--work-home", default=os.environ.get("WORK_HOME"))
    sp.add_argument("--config-dir", default=os.environ.get("ASICFLOW_CONFIG_DIR"))
    sp.add_argument("--design-profile", default=os.environ.get("ASICFLOW_DESIGN_PROFILE"))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="asicflow",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="print resolved paths")
    _common(v)
    v.set_defaults(func=cmd_validate)

    for name in ORFS_STAGE_ORDER:
        s = sub.add_parser(name, help=f"ORFS through {name}")
        _common(s)
        s.add_argument("--sta", action="store_true", help="run sta-pvt after stage")
        s.set_defaults(func=cmd_stage, stage=name)

    f = sub.add_parser("flow", help="ORFS finish (full chip)")
    _common(f)
    f.add_argument("--sta", action="store_true", help="run sta-pvt after finish")
    f.set_defaults(func=cmd_flow)

    pg = sub.add_parser("place-gen", help="write placement Tcl")
    _common(pg)
    pg.add_argument("--presets", action="store_true")
    pg.add_argument("--side", choices=("left", "right"))
    pg.add_argument("--name")
    pg.add_argument("--gap", type=float, default=20.0)
    pg.add_argument("--chan", type=float, default=60.0)
    pg.add_argument("--macros", nargs=3, metavar=("WBUF", "ABUF", "RBUF"))
    pg.set_defaults(func=cmd_place_gen)

    pr = sub.add_parser("predict", help="post-place routability heuristic (needs place)")
    _common(pr)
    pr.add_argument("--fail-if-unroutable", action="store_true")
    pr.set_defaults(func=cmd_predict)

    pp = sub.add_parser("place-predict", help="place then predict")
    _common(pp)
    pp.add_argument("--fail-if-unroutable", action="store_true")
    pp.set_defaults(func=cmd_place_predict)

    m = sub.add_parser("metrics", help="ODB → metrics.db")
    _common(m)
    m.add_argument("--stage", default=os.environ.get("STAGE"))
    m.add_argument("stages", nargs="*")
    m.add_argument("--all-stages", action="store_true")
    m.add_argument("--runs", default=os.environ.get("METRICS_RUNS") or os.environ.get("RUNS"))
    m.add_argument("--corners", default=os.environ.get("METRICS_CORNERS", "default"))
    m.add_argument("--hpwl", action="store_true", default=os.environ.get("METRICS_HPWL") == "1")
    m.add_argument("--saif", default=os.environ.get("METRICS_SAIF", ""))
    m.add_argument("--saif-scope", default=os.environ.get("METRICS_SAIF_SCOPE", "tb/dut"))
    m.add_argument("--outroot", default=os.environ.get("METRICS_OUTROOT"))
    m.add_argument("--export", default=None)
    m.set_defaults(func=cmd_metrics)

    q = sub.add_parser("query", help="read metrics.db")
    _common(q)
    q.add_argument("--stage", default="place")
    q.add_argument("--category", default="timing")
    q.add_argument("--db", default=None)
    q.set_defaults(func=cmd_query)

    r = sub.add_parser("report", help="recent runs table")
    _common(r)
    r.add_argument("--db", default=None)
    r.add_argument("--limit", type=int, default=20)
    r.set_defaults(func=cmd_report)

    n = sub.add_parser("netlist", help="path / dump stage netlist")
    _common(n)
    n.add_argument("--stage", default=os.environ.get("STAGE", "synth"))
    n.set_defaults(func=cmd_netlist)

    g = sub.add_parser("gui", help="ORFS gui_<stage>")
    _common(g)
    g.add_argument("target")
    g.set_defaults(func=cmd_gui)

    spvt = sub.add_parser("sta-pvt", help="multi-corner STA (not full signoff MCMM)")
    _common(spvt)
    spvt.add_argument("--stage", default=os.environ.get("STAGE", "latest"))
    spvt.add_argument("--all-corners", action="store_true")
    spvt.add_argument("--paths", type=int, default=int(os.environ.get("STA_PATHS", "20")))
    spvt.add_argument(
        "--baseline",
        default=os.environ.get("BASELINE_TAG"),
        help="baseline run-tag for post-STA regression (or analysis/baseline_tag.txt)",
    )
    spvt.set_defaults(func=cmd_sta_pvt)

    register_analysis_parsers(sub, _common)

    sh = sub.add_parser(
        "sta-shell",
        help="interactive OpenROAD with the same design load as sta-pvt",
    )
    _common(sh)
    sh.add_argument("--stage", default=os.environ.get("STAGE", "latest"))
    sh.add_argument(
        "--corner",
        default=os.environ.get("STA_CORNER"),
        help="optional PVT corner name (default: platform nominal liberty)",
    )
    sh.set_defaults(func=cmd_sta_shell)

    ps = sub.add_parser("presta", help="pre-layout Yosys+STA")
    _common(ps)
    ps.add_argument("--shell", action="store_true")
    ps.set_defaults(func=cmd_presta)

    fl = sub.add_parser("fix-lef", help="fix sky130 fakeram LEF OBS layers")
    fl.add_argument("files", nargs="+")
    fl.set_defaults(func=cmd_fix_lef)

    register_cli_plugins(sub, _common)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if hasattr(args, "platform") and args.platform:
            if args.platform.strip() == "sky130":
                args.platform = "sky130hd"
        return int(args.func(args) or 0)
    except BrokenPipeError:
        return 0
    except Exception as exc:
        _log(f"ERROR: {exc}")
        if os.environ.get("FLOW_DEBUG"):
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
