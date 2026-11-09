"""OpenROAD Python worker entry point for a single metrics job."""
import json
import os
import sys
from pathlib import Path

from .collector import Collector, SECTIONS, _print_section

def load_design(c):
    """Tcl path (needed for STA); falls back to the plain odb API (design metrics only)."""
    job = c.job
    try:
        from openroad import Tech, Design
        print("[metrics] importing OpenROAD Python API", flush=True)
        # Do not write Design(Tech()) here. Design wraps the OpenROAD singleton,
        # and the Tech wrapper must remain strongly referenced for the lifetime
        # of that Design. A temporary Tech() can be collected immediately and
        # lead to a native crash in some OpenROAD/Python builds.
        c.tech = Tech()
        print("[metrics] Tech initialized", flush=True)
        c.design = Design(c.tech)
        print("[metrics] Design initialized", flush=True)
        for lib in ([job["lib"]] if job.get("lib") else []) + list(job.get("macro_libs", [])):
            try:
                print(f"[metrics] read_liberty: {lib}", flush=True)
                c.tcl(f"read_liberty {{{lib}}}")
            except Exception as exc:
                c.libs_ok = False
                c.fail("read_liberty", exc)
        print(f"[metrics] read_db: {job['odb']}", flush=True)
        c.tcl(f"read_db {{{job['odb']}}}")
        print("[metrics] ODB loaded; obtaining block", flush=True)
        c.block = c.design.getBlock()
        print("[metrics] block obtained", flush=True)
    except Exception as exc:
        c.fail("load_tcl_path", exc)
        c.design, c.libs_ok = None, False
    if c.block is None:
        import odb
        db = odb.dbDatabase.create()
        odb.read_db(db, job["odb"])
        c.block = db.getChip().getBlock()
    if c.block is None:
        raise RuntimeError("no design block in ODB")
    c.dbu = c.block.getDbUnitsPerMicron()
    c.macro_names = [i.getName() for i in c.block.getInsts() if i.isBlock()]
    print(f"\nODB: {job['odb']}\nDesign: {c.block.getName()}\nDBU per micron: {c.dbu}")


def run_sections(c):
    for title, scope, needs_sta, when, fn in SECTIONS:
        if (scope == "design" and not c.job.get("first", True)) or (when and not c.job.get(when)):
            continue
        if needs_sta and not c.sta_ready:
            continue
        _print_section(title)
        c.row_corner = "-" if scope == "design" else c.corner
        try:
            fn(c)
        except Exception as exc:
            c.fail(title, exc)


def finish(c, fatal):
    c.row_corner = c.corner
    result = "fail" if fatal else ("partial" if c.errors else "ok")
    _print_section("Status")
    c.emit("status", "result", result)
    for title, msg in c.errors:
        c.emit("status", f"error.{title}", msg)
    if c.rows_fh:
        c.rows_fh.close()


def worker_main(argv=None):
    """Run one collection job.

    The driver passes its JSON path in ORFS_METRICS_JOB because OpenROAD's
    command line has a single Python command-file positional argument.
    A positional path remains supported for interactive/manual use.
    """
    argv = sys.argv if argv is None else argv
    arg = os.environ.get("ORFS_METRICS_JOB")
    if not arg and len(argv) >= 2:
        arg = argv[1]
    if not arg:
        raise SystemExit(
            "Usage: ORFS_METRICS_JOB=<job.json> openroad -python metrics.py "
            "or: openroad -python metrics.py <job.json | file.odb>"
        )
    if arg.endswith(".json"):
        with open(arg) as fh:
            job = json.load(fh)
    else:  # quick mode: design metrics only, printed to stdout
        job = {"odb": arg, "stage": Path(arg).stem, "run": "-", "corner": "-", "first": True}
    c = Collector(job)
    fatal = False
    try:
        load_design(c)
        run_sections(c)
    except Exception as exc:
        c.fail("fatal", exc)
        fatal = True
    finish(c, fatal)
    return 2 if fatal else 0


def _run_worker():
    code = worker_main(sys.argv)
    sys.stdout.flush()
    os._exit(code)
