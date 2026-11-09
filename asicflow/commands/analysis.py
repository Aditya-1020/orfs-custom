"""Analysis, comparison, sweep and dataset command handlers."""
"""High-level analysis / sweep / summary actions used by the CLI.

Keeps flow.py focused on ORFS orchestration while these entry points own
report generation, baseline comparison, and the implementation dataset.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..analyze import (
    analyze_area_growth,
    analyze_buffers,
    analyze_congestion,
    analyze_fanout,
    analyze_macro_placement,
    analyze_wirelength,
    check_clock_domains,
    check_unconstrained_paths,
    compare_sta_runs,
)
from ..config import RunContext, canon_stage, ctx_from_args as _ctx, stage_stem
from ..util import FlowError, log as _log
from ..dataset import RunDataset, plot_dataset
from ..reports import generate_regression_report, generate_summary, path_group_report
from ..schema import load_snapshot
from ..sweep import (
    compare_implementations,
    find_best_configuration,
    sweep_buffering,
    sweep_clock_period,
    sweep_corners,
    sweep_liberty,
    sweep_placement_density,
    sweep_utilization,
)



def _stage_name(ctx: RunContext, stage: str) -> str:
    """CLI stage -> DB/ODB stem. 'latest' resolves to the newest ODB's stem."""
    if stage == "latest":
        return ctx.latest_stem()
    return stage_stem(canon_stage(stage))


def analysis_out(ctx: RunContext, *parts: str) -> Path:
    path = ctx.analysis_dir.joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_baseline_tag(args, ctx: RunContext) -> str | None:
    tag = getattr(args, "baseline", None) or os.environ.get("BASELINE_TAG")
    if tag:
        return tag
    marker = ctx.analysis_dir / "baseline_tag.txt"
    if marker.is_file():
        return marker.read_text().strip() or None
    return None


def post_sta_reports(
    ctx: RunContext,
    *,
    stem: str,
    baseline_tag: str | None = None,
) -> list[Path]:
    """After sta-pvt: path-group report, summary, regression, dataset ingest."""
    written: list[Path] = []
    sta_root = ctx.analysis_dir / "sta" / stem
    out = analysis_out(ctx, "reports", stem)

    if sta_root.is_dir():
        pg = path_group_report(
            sta_root,
            run_meta={
                "platform": ctx.platform,
                "run_tag": ctx.run_tag,
                "stage": stem,
            },
        )
        paths = pg.write(out, basename="path_groups")
        written.extend(paths.values())
        _log(f"path groups → {paths['json']}")

        # Per-corner unconstrained + CDC checks on first corner with metrics.
        for corner_dir in sorted(p for p in sta_root.iterdir() if p.is_dir()):
            if not (corner_dir / "metrics.json").is_file():
                continue
            corner_out = out / corner_dir.name
            check_unconstrained_paths(corner_dir, out_dir=corner_out)
            check_clock_domains(corner_dir, out_dir=corner_out)
            written.append(corner_out / "unconstrained_paths.json")
            written.append(corner_out / "clock_domains.json")
            _log(f"constraints/CDC → {corner_out}")
            break

    baseline = baseline_tag
    summary = generate_summary(
        db=ctx.metrics_db,
        platform=ctx.platform,
        run_tag=ctx.run_tag,
        stage=stem,
        baseline_tag=baseline,
        sta_root=sta_root if sta_root.is_dir() else None,
        out_dir=out,
    )
    written.append(out / "summary.json")
    _log(f"summary → {out / 'summary.json'}")

    if baseline:
        reg = generate_regression_report(
            db=ctx.metrics_db,
            platform=ctx.platform,
            current_tag=ctx.run_tag,
            baseline_tag=baseline,
            stage=stem,
            out_dir=out,
        )
        written.append(out / "regression.json")
        _log(
            f"regression vs {baseline}: "
            f"{reg.sections.get('counts', {})}"
        )

    # Maintain implementation dataset + tradeoff plot.
    ds_path = ctx.root / "build" / "analysis" / "impl_runs.db"
    ds = RunDataset(ds_path)
    snap = load_snapshot(
        ctx.metrics_db, platform=ctx.platform, run_tag=ctx.run_tag, stage=stem
    )
    # Merge STA fmax from metrics.json when DB timing fmax missing.
    if sta_root.is_dir() and snap.num("timing.fmax_est_mhz") is None:
        for mf in sorted(sta_root.glob("*/metrics.json")):
            data = json.loads(mf.read_text())
            try:
                fmax = float(data.get("fmax_mhz"))
            except (TypeError, ValueError):
                continue
            snap.extras["timing.fmax_est_mhz"] = fmax
            snap.extras["sta.fmax_mhz"] = fmax
            break
    # Also pull congestion from predict features if present.
    ds.ingest_snapshot(snap)
    plot = plot_dataset(ds, analysis_out(ctx, "plots"), platform=ctx.platform, stage=stem)
    _log(f"dataset → {ds_path}; plot → {plot.meta.get('png') or plot.meta.get('svg') or plot.meta.get('csv')}")
    return written


def cmd_analyze(args) -> int:
    ctx = _ctx(args)
    out = analysis_out(ctx, "reports", "analyze")
    which = args.what
    stage = _stage_name(ctx, args.stage)
    db = Path(args.db) if args.db else ctx.metrics_db

    if which == "unconstrained":
        corner_dir = _first_corner(_sta_stem_dir(ctx, args.stage))
        if corner_dir is None:
            raise FlowError(f"no STA corner reports under {ctx.analysis_dir / 'sta'}; run sta-pvt first")
        check_unconstrained_paths(corner_dir, out_dir=out)
    elif which == "clocks":
        corner_dir = _first_corner(_sta_stem_dir(ctx, args.stage))
        if corner_dir is None:
            raise FlowError("no STA corner reports; run sta-pvt first")
        check_clock_domains(corner_dir, out_dir=out)
    elif which == "fanout":
        analyze_fanout(
            db=db,
            platform=ctx.platform,
            run_tag=ctx.run_tag,
            stage=stage,
            threshold=args.threshold,
            sta_corner_dir=_first_corner(_sta_stem_dir(ctx, args.stage)),
            out_dir=out,
        )
    elif which == "congestion":
        analyze_congestion(
            logs_dir=ctx.logs_dir,
            reports_dir=ctx.reports_dir,
            run_tag=ctx.run_tag,
            platform=ctx.platform,
            out_dir=out,
        )
    elif which == "wirelength":
        analyze_wirelength(
            db=db, platform=ctx.platform, run_tag=ctx.run_tag, out_dir=out
        )
    elif which == "buffers":
        analyze_buffers(
            db=db,
            platform=ctx.platform,
            run_tag=ctx.run_tag,
            stage=stage,
            reports_dir=ctx.reports_dir,
            out_dir=out,
        )
    elif which == "macros":
        analyze_macro_placement(
            db=db,
            platform=ctx.platform,
            run_tag=ctx.run_tag,
            stage=stage if stage.startswith("2_") else "2_floorplan",
            halo_um=args.halo,
            out_dir=out,
        )
    elif which == "area":
        analyze_area_growth(
            db=db, platform=ctx.platform, run_tag=ctx.run_tag, out_dir=out
        )
    elif which == "path-groups":
        sta_root = _sta_stem_dir(ctx, args.stage)
        if sta_root is None:
            raise FlowError("no STA results; run sta-pvt first")
        path_group_report(sta_root, run_meta=ctx.snapshot()).write(out, basename="path_groups")
    else:
        raise FlowError(f"unknown analyze target: {which}")
    _log(f"reports → {out}")
    return 0


def cmd_summary(args) -> int:
    ctx = _ctx(args)
    stage = _stage_name(ctx, args.stage)
    out = analysis_out(ctx, "reports", stage)
    baseline = resolve_baseline_tag(args, ctx)
    sta_root = ctx.analysis_dir / "sta" / stage
    generate_summary(
        db=Path(args.db) if args.db else ctx.metrics_db,
        platform=ctx.platform,
        run_tag=ctx.run_tag,
        stage=stage,
        baseline_tag=baseline,
        sta_root=sta_root if sta_root.is_dir() else None,
        out_dir=out,
    )
    _log(f"summary → {out / 'summary.json'}")
    return 0


def cmd_regression(args) -> int:
    ctx = _ctx(args)
    stage = _stage_name(ctx, args.stage)
    baseline = args.baseline or resolve_baseline_tag(args, ctx)
    if not baseline:
        raise FlowError("need --baseline or BASELINE_TAG / analysis/baseline_tag.txt")
    out = analysis_out(ctx, "reports", stage)
    generate_regression_report(
        db=Path(args.db) if args.db else ctx.metrics_db,
        platform=ctx.platform,
        current_tag=ctx.run_tag,
        baseline_tag=baseline,
        stage=stage,
        out_dir=out,
    )
    _log(f"regression → {out / 'regression.json'}")
    return 0


def cmd_compare(args) -> int:
    ctx = _ctx(args)
    out = analysis_out(ctx, "reports", "compare")
    tags = args.tags
    if args.mode == "sta":
        compare_sta_runs(
            db=Path(args.db) if args.db else ctx.metrics_db,
            platform=ctx.platform,
            run_tags=tags,
            stage=_stage_name(ctx, args.stage),
            out_dir=out,
        )
    elif args.mode == "impl":
        compare_implementations(
            db=Path(args.db) if args.db else ctx.metrics_db,
            platform=ctx.platform,
            run_tags=tags,
            stage=_stage_name(ctx, args.stage),
            out_dir=out,
        )
    else:
        find_best_configuration(
            db=Path(args.db) if args.db else ctx.metrics_db,
            platform=ctx.platform,
            run_tags=tags,
            stage=_stage_name(ctx, args.stage),
            out_dir=out,
        )
    _log(f"compare → {out}")
    return 0


def cmd_sweep(args) -> int:
    from .flow import run_orfs_stage
    from .sta import run_sta_pvt

    ctx = _ctx(args)
    out = analysis_out(ctx, "reports", "sweeps")
    execute = args.execute
    runner = (lambda c, s: run_orfs_stage(c, s)) if execute else None

    if args.what == "clock":
        periods = [float(x) for x in args.values]
        sweep_clock_period(ctx, periods, stage=args.stage, execute=execute, runner=runner, out_dir=out)
    elif args.what == "util":
        utils = [float(x) for x in args.values]
        sweep_utilization(ctx, utils, stage=args.stage, execute=execute, runner=runner, out_dir=out)
    elif args.what == "buffering":
        fos = [int(x) for x in args.values]
        sweep_buffering(ctx, fos, stage=args.stage, execute=execute, runner=runner, out_dir=out)
    elif args.what == "density":
        dens = [float(x) for x in args.values]
        sweep_placement_density(ctx, dens, stage=args.stage, execute=execute, runner=runner, out_dir=out)
    elif args.what == "corners":
        sweep_corners(
            ctx,
            stage=args.stage,
            all_corners=True,
            execute=execute,
            sta_runner=run_sta_pvt if execute else None,
            out_dir=out,
        )
    elif args.what == "liberty":
        sweep_liberty(ctx, args.values, stage=args.stage, out_dir=out)
    else:
        raise FlowError(f"unknown sweep: {args.what}")
    _log(f"sweep → {out}")
    return 0


def cmd_dataset(args) -> int:
    ctx = _ctx(args)
    ds = RunDataset(Path(args.db) if args.db else ctx.root / "build" / "analysis" / "impl_runs.db")
    if args.ingest:
        stage = _stage_name(ctx, args.stage)
        ds.ingest_from_metrics_db(
            Path(args.metrics_db) if args.metrics_db else ctx.metrics_db,
            platform=ctx.platform,
            run_tag=ctx.run_tag,
            stage=stage,
        )
        _log(f"ingested {ctx.run_tag}/{stage}")
    out = analysis_out(ctx, "plots")
    plot_dataset(ds, out, platform=ctx.platform, stage=_stage_name(ctx, args.stage))
    csv = ds.export_csv(out / "impl_runs.csv", platform=ctx.platform, stage=_stage_name(ctx, args.stage))
    _log(f"plot/csv → {out} ({csv})")
    return 0


def cmd_set_baseline(args) -> int:
    ctx = _ctx(args)
    tag = args.tag or ctx.run_tag
    marker = ctx.analysis_dir / "baseline_tag.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(tag + "\n")
    # Also snapshot headline metrics if present.
    stage = _stage_name(ctx, args.stage)
    snap = load_snapshot(
        ctx.metrics_db, platform=ctx.platform, run_tag=tag, stage=stage
    )
    payload = {
        "baseline_tag": tag,
        "platform": ctx.platform,
        "stage": stage,
        "headline": snap.headline(),
        "run_id": snap.run_id,
    }
    (ctx.analysis_dir / "baseline.json").write_text(json.dumps(payload, indent=2) + "\n")
    _log(f"baseline → {tag} ({marker})")
    return 0


def _latest_sta_stem_dir(sta_root: Path) -> Path | None:
    """Newest STA stage dir by metrics.json mtime (not by name)."""
    if not sta_root.is_dir():
        return None
    best, best_m = None, -1.0
    for p in sta_root.iterdir():
        for mf in p.glob("*/metrics.json") if p.is_dir() else ():
            m = mf.stat().st_mtime
            if m > best_m:
                best, best_m = p, m
    return best


def _sta_stem_dir(ctx: RunContext, stage: str) -> Path | None:
    sta_root = ctx.analysis_dir / "sta"
    if stage and stage != "latest":
        d = sta_root / _stage_name(ctx, stage)
        if d.is_dir():
            return d
    return _latest_sta_stem_dir(sta_root)


def _first_corner(stem_dir: Path | None) -> Path | None:
    if stem_dir is None:
        return None
    corners = sorted(p for p in stem_dir.iterdir() if p.is_dir() and (p / "metrics.json").is_file())
    return corners[0] if corners else None


def register_analysis_parsers(sub, common) -> None:
    """Attach analyze/summary/regression/compare/sweep/dataset/set-baseline parsers."""
    an = sub.add_parser("analyze", help="design analyses (fanout, cong, macros, …)")
    common(an)
    an.add_argument(
        "what",
        choices=(
            "unconstrained",
            "clocks",
            "fanout",
            "congestion",
            "wirelength",
            "buffers",
            "macros",
            "area",
            "path-groups",
        ),
    )
    an.add_argument("--stage", default=os.environ.get("STAGE", "final"))
    an.add_argument("--db", default=None)
    an.add_argument("--threshold", type=int, default=16)
    an.add_argument("--halo", type=float, default=10.0)
    an.set_defaults(func=cmd_analyze)

    sm = sub.add_parser("summary", help="overall design summary (+ baseline delta)")
    common(sm)
    sm.add_argument("--stage", default=os.environ.get("STAGE", "final"))
    sm.add_argument("--db", default=None)
    sm.add_argument("--baseline", default=None)
    sm.set_defaults(func=cmd_summary)

    rg = sub.add_parser("regression", help="compare run against baseline")
    common(rg)
    rg.add_argument("--stage", default=os.environ.get("STAGE", "final"))
    rg.add_argument("--db", default=None)
    rg.add_argument("--baseline", required=False)
    rg.set_defaults(func=cmd_regression)

    cp = sub.add_parser("compare", help="compare STA/impl variants or rank best")
    common(cp)
    cp.add_argument("mode", choices=("sta", "impl", "best"))
    cp.add_argument("tags", nargs="+")
    cp.add_argument("--stage", default=os.environ.get("STAGE", "final"))
    cp.add_argument("--db", default=None)
    cp.set_defaults(func=cmd_compare)

    sw = sub.add_parser("sweep", help="parameter sweeps (clock/util/buffering/…)")
    common(sw)
    sw.add_argument(
        "what",
        choices=("clock", "util", "buffering", "density", "corners", "liberty"),
    )
    sw.add_argument("values", nargs="*", help="sweep values (or liberty paths)")
    sw.add_argument("--stage", default=os.environ.get("STAGE", "finish"))
    sw.add_argument("--execute", action="store_true", help="actually run ORFS/STA")
    sw.set_defaults(func=cmd_sweep)

    ds = sub.add_parser("dataset", help="ingest runs + plot area vs frequency")
    common(ds)
    ds.add_argument("--ingest", action="store_true")
    ds.add_argument("--stage", default=os.environ.get("STAGE", "final"))
    ds.add_argument("--db", default=None, help="impl_runs.db path")
    ds.add_argument("--metrics-db", default=None)
    ds.set_defaults(func=cmd_dataset)

    bl = sub.add_parser("set-baseline", help="pin baseline tag for regression")
    common(bl)
    bl.add_argument("--tag", default=None, help="default: current --run-tag")
    bl.add_argument("--stage", default=os.environ.get("STAGE", "final"))
    bl.set_defaults(func=cmd_set_baseline)

