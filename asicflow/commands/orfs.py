"""ORFS execution and placement/prediction command handlers."""
from __future__ import annotations
import argparse
import json
import os
import traceback
from pathlib import Path
from .. import metrics as metrics_mod
from ..config import ORFS_STAGE_ORDER, RunContext, canon_stage, ctx_from_args as _ctx, stage_stem
from ..physical import run_prediction, write_placement, write_presets
from ..sta import run_sta_pvt
from ..util import FlowError, log as _log, now as _now, run as _run, run_id as _run_id

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
