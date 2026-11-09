"""STA, netlist and maintenance command handlers."""
from __future__ import annotations
from pathlib import Path
from ..config import ctx_from_args as _ctx
from ..sta import run_presta, run_sta_pvt, run_sta_shell
from ..util import FlowError, log as _log
from ..lef_fix import fix_file
from .analysis import resolve_baseline_tag
from .orfs import orfs

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
    for p in args.files:
        _log(fix_file(Path(p)))
    return 0
