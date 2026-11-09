"""CLI compatibility entry point; handlers live under asicflow.commands."""
from __future__ import annotations
import argparse
import os
import sys
import traceback
from .actions import register_analysis_parsers
from .config import ORFS_STAGE_ORDER
from .commands.orfs import *
from .commands.metrics import cmd_metrics, cmd_query, cmd_report
from .commands.sta import cmd_netlist, cmd_sta_pvt, cmd_sta_shell, cmd_presta, cmd_fix_lef
from .plugins.registry import register_cli_plugins
from .util import log as _log

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
