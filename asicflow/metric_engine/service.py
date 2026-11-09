"""Host-side metrics collection use case."""
import glob
import os
import shutil
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .discovery import (
    collect_stage, discover_corners, env, find_runs, list_odbs, parse_args,
    split_words, stem_of,
)
from .reporting import export_wide
from .store import ingest

ROOT = Path(__file__).resolve().parents[2]

def driver_main(run_id=None):
    a = parse_args()
    # Prefer explicit WORK_HOME parent layout: build/runs/<platform>/...
    outroot = Path(a.outroot) if a.outroot else ROOT / "build" / "runs" / a.platform
    analysis = Path(env("ANALYSIS_DIR")) if env("ANALYSIS_DIR") else (
        outroot / "_analysis" if a.outroot else ROOT / "build" / "analysis"
    )
    analysis.mkdir(parents=True, exist_ok=True)
    db_path = Path(a.db) if a.db else (
        Path(env("METRICS_DB")) if env("METRICS_DB") else ROOT / "build" / "analysis" / "metrics.db"
    )
    db_path.parent.mkdir(parents=True, exist_ok=True)

    stages = a.stages or split_words(env("STAGE"))
    if not stages and a.export:  # export only, no collection
        return export_wide(db_path, a.export)
    patterns = split_words(a.runs) or ["*"]
    if not stages:
        counts = list_odbs(outroot, patterns)
        print("ODBs found (count = runs that have it):")
        for stem, n in sorted(counts.items()):
            print(f"{n:7d} {stem}")
        if not sys.stdin.isatty():
            sys.exit("ERROR: give a stage as argument")
        stages = split_words(input("Stage (synth|floorplan|place|cts|route|final, or ODB name): "))

    if not a.openroad:
        try:
            from .config import resolve_openroad
            a.openroad = resolve_openroad(Path(a.orfs_flow))
        except Exception:
            try:
                from asicflow.config import resolve_openroad  # type: ignore
                a.openroad = resolve_openroad(Path(a.orfs_flow))
            except Exception:
                a.openroad = env("OPENROAD", "openroad") or "openroad"
    if not Path(a.openroad).is_file() and not shutil.which(a.openroad):
        sys.exit(f"ERROR: openroad not found ({a.openroad!r}); set OPENROAD= or enter the ORFS nix shell")
    plat_dir = Path(a.orfs_flow) / "platforms" / a.platform
    corners = discover_corners(plat_dir, a.corners)
    ram_tech = env("FAKERAM_TECH") or a.platform
    macro_libs = sorted(glob.glob(str(ROOT / "fakeram" / ram_tech / "*" / "*.lib")))
    runs = find_runs(outroot, patterns)
    if not runs:
        # Also accept a single WORK_HOME pointed at by OUTROOT itself.
        if (outroot / "results").is_dir():
            runs = [outroot]
        else:
            sys.exit(f"ERROR: no run with a results/ directory matches '{a.runs}' under {outroot}")

    print(f"ORFS METRICS\nPlatform : {a.platform}\nStages   : {' '.join(stages)}\n"
          f"Runs     : {a.runs}\nCorners  : {' '.join(corners)}\nHPWL     : {'on' if a.hpwl else 'off'}")
    total = Counter()
    import uuid
    from datetime import timezone as _tz
    rid = run_id or env("RUN_ID") or (
        datetime.now(_tz.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    )
    for stage in stages:
        rows, tally = collect_stage(a, stem_of(stage), runs, corners, plat_dir, macro_libs, analysis)
        total.update(tally)
        if not a.no_db:
            ingest(db_path, a.platform, rows, run_id=rid)
    if not a.no_db:
        print(f"DB  : {db_path}")
    if a.export:
        export_wide(db_path, a.export)
    return 1 if total["fail"] and not (total["ok"] or total["partial"]) else 0
