"""ORFS run/stage discovery and OpenROAD worker process execution."""
import argparse
import csv
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from .reporting import print_summary

NA = "NA"
HEADER = ["run_tag", "stage", "corner", "category", "metric", "value", "unit"]
STAGE_STEMS = {
    "synth": "1_synth", "floorplan": "2_floorplan", "place": "3_place",
    "cts": "4_cts", "route": "5_route", "final": "6_final",
}
KEY_METRICS = (
    ("timing", "setup_ws"), ("timing", "setup_tns"), ("timing", "hold_ws"),
    ("timing", "hold_tns"), ("power", "total.total_W"),
)
WORKER = Path(__file__).resolve().parents[1] / "metrics.py"

def env(name, default=""):
    return os.environ.get(name, default)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("stages", nargs="*", help="stage names or ODB stems (chain)")
    p.add_argument("-r", "--runs", default=env("RUNS", "*"), help="run tag globs, space/comma separated")
    p.add_argument("-c", "--corners", default=env("CORNERS", "default"),
                   help="default | all | space/comma separated corner names")
    p.add_argument("--hpwl", action="store_true", default=env("HPWL", "0") == "1")
    p.add_argument("--saif", default=env("SAIF"))
    p.add_argument("--saif-scope", default=env("SAIF_SCOPE", "tb/dut"))
    p.add_argument("--platform", default=env("PLATFORM", "sky130hd"))
    p.add_argument("--orfs-flow", default=env("ORFS_FLOW", str(Path.home() / "eda/orfs-nix/OpenROAD-flow-scripts/flow")))
    p.add_argument("--outroot", default=env("OUTROOT"))
    p.add_argument("--openroad", default="", help="OpenROAD binary (default: ORFS OPENROAD_EXE or PATH)")
    p.add_argument("--db", help="SQLite path (default <outroot>/_analysis/metrics.db)")
    p.add_argument("--no-db", action="store_true", help="write CSV only")
    p.add_argument("--export", metavar="CSV", help="write the wide ML table from the database")
    p.add_argument("-v", "--verbose", action="store_true", default=env("VERBOSE", "0") == "1")
    return p.parse_args()

# ------------------------------------------------------------------- discovery
def split_words(text):
    return [w for w in re.split(r"[\s,]+", text or "") if w]


def stem_of(stage):
    return STAGE_STEMS.get(stage, re.sub(r"\.odb$", "", stage))


def find_runs(outroot, patterns):
    seen = []
    for pat in patterns:
        for d in sorted(glob.glob(str(outroot / pat))):
            d = Path(d)
            if (d / "results").is_dir() and d not in seen:
                seen.append(d)
    return seen


def find_odb(run_dir, stem):
    hits = sorted(glob.glob(str(run_dir / "results" / "*" / "*" / "*" / f"{stem}.odb")))
    return Path(hits[0]) if hits else None


def _stem_key(stem):
    parts = stem.split("_", 2)
    try:
        major = int(parts[0])
    except (ValueError, IndexError):
        return (999, 999, stem)
    minor = 0
    if len(parts) > 1:
        try:
            minor = int(parts[1])
        except ValueError:
            minor = 0
    return (major, minor, stem)


def find_sdc(result_dir, stem):
    """Nearest SDC at or before the stage. Never pick a later-stage SDC."""
    exact = result_dir / f"{stem}.sdc"
    if exact.is_file():
        return exact
    limit = _stem_key(stem)
    cands = [f for f in result_dir.glob("*.sdc") if _stem_key(f.stem) <= limit]
    if not cands:
        return None
    return max(cands, key=lambda f: _stem_key(f.stem))


def discover_corners(plat_dir, wanted):
    libs = {}
    for lib in sorted(plat_dir.glob("lib/*.lib")):
        libs[lib.stem.split("__")[-1]] = lib
    if not libs:
        sys.exit(f"ERROR: no .lib in {plat_dir}/lib")
    default = next((n for n in libs if re.search("tt|typical", n)), sorted(libs)[0])
    names = {"default": [default], "all": sorted(libs)}.get(wanted) or split_words(wanted)
    for n in names:
        if n not in libs:
            sys.exit(f"ERROR: corner '{n}' not in: {' '.join(libs)}")
    return {n: libs[n] for n in names}


def list_odbs(outroot, patterns):
    counts = Counter()
    for pat in patterns:
        for f in glob.glob(str(outroot / pat / "results" / "*" / "*" / "*" / "*.odb")):
            counts[Path(f).stem] += 1
    return counts


# --------------------------------------------------------------------- one job
def _log_tail(path, lines=12):
    """Return the final log lines for concise failure diagnostics."""
    try:
        return Path(path).read_text(errors="replace").splitlines()[-lines:]
    except OSError:
        return []


def run_job(a, job, log_path):
    """Start one worker process. Return (rows, status, seconds).

    OpenROAD accepts one command file. Pass the JSON job through the process
    environment rather than as a second positional command-line argument.
    """
    job_path = Path(str(job["rows"]).replace(".rows.csv", ".job.json"))
    job_path.write_text(json.dumps(job, indent=1))
    cmd = [a.openroad, "-no_splash", "-python", str(WORKER)]
    child_env = os.environ.copy()
    child_env["ORFS_METRICS_JOB"] = str(job_path.resolve())
    if a.verbose:
        print("   $", " ".join(cmd))
        print(f"      ORFS_METRICS_JOB={job_path}")
    t0 = time.time()
    with open(log_path, "w") as log:
        code = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, env=child_env).returncode
    seconds = time.time() - t0

    rows = []
    rows_file = Path(job["rows"])
    if rows_file.exists():
        with open(rows_file, newline="") as fh:
            rows = [tuple(r) for r in csv.reader(fh) if len(r) == 7]
        rows_file.unlink()

    status = next((r[5] for r in rows if r[3] == "status" and r[4] == "result"), None)
    if status is None or code not in (0, 2):  # no final status row = launch error/crash
        status = "fail"
        rows = [r for r in rows if not (r[3] == "status" and r[4] == "result")]
        rows.append((job["run"], job["stage"], job["corner"], "status", "result", "fail", ""))
        rows.append((job["run"], job["stage"], job["corner"], "status", "error.process",
                     f"openroad exit code {code}; see {Path(log_path).name}", ""))

    if status != "ok":
        have = {(r[2], r[3], r[4]) for r in rows}
        for cat, metric in KEY_METRICS:
            if (job["corner"], cat, metric) not in have:
                rows.append((job["run"], job["stage"], job["corner"], cat, metric, "NA", ""))
        # Keep the exact job description on failure so the invocation is reproducible.
        print(f"   LOG: {log_path}")
        print(f"   JOB: {job_path} (retained because this job failed/was partial)")
        for line in _log_tail(log_path):
            print(f"      {line}")
    else:
        job_path.unlink(missing_ok=True)

    return rows, status, seconds


# ------------------------------------------------------------------ one stage
def collect_stage(a, stem, runs, corners, plat_dir, macro_libs, analysis):
    csv_path, raw_dir = analysis / f"{stem}.csv", analysis / f"{stem}_raw"
    shutil.rmtree(raw_dir, ignore_errors=True)
    raw_dir.mkdir(parents=True)
    rows_all, tally = [], Counter()
    print(f"\n=== STAGE {stem} ===")
    for run_dir in runs:
        run = run_dir.name
        odb = find_odb(run_dir, stem)
        if odb is None:
            rows_all.append((run, stem, "-", "status", "stage_found", "no", ""))
            print(f"[SKIP] {run:<22} {stem}  (no {stem}.odb)")
            tally["skip"] += 1
            continue
        sdc = find_sdc(odb.parent, stem)
        spef = odb.parent / f"{stem}.spef"
        design_done = False  # reset for EVERY run
        for corner, lib in corners.items():
            name = f"{run}_{stem}_{corner}"
            job = {"run": run, "stage": stem, "corner": corner, "first": not design_done,
                   "odb": str(odb), "sdc": str(sdc) if sdc else "",
                   "spef": str(spef) if spef.exists() else "", "lib": str(lib),
                   "macro_libs": macro_libs, "platform_dir": str(plat_dir),
                   "hpwl": a.hpwl, "saif": a.saif, "saif_scope": a.saif_scope,
                   "rows": str(raw_dir / f"{name}.rows.csv"), "rpt": str(raw_dir / f"{name}.rpt")}
            rows, status, seconds = run_job(a, job, raw_dir / f"{name}.log")
            design_done = design_done or any(r[3] == "design" for r in rows)
            rows_all.extend(rows)
            tally[status] += 1
            tag = {"ok": " OK ", "partial": "PART", "fail": "FAIL"}[status]
            print(f"[{tag}] {run:<22} {stem}  {corner}  ({seconds:.0f}s)")
    with open(csv_path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerows(rows_all)
    print_summary(rows_all)
    print(f"\nCSV : {csv_path}\nRAW : {raw_dir}")
    print(f"Completed: {sum(tally[k] for k in ('ok', 'partial', 'fail'))} corner runs | "
          f"ok: {tally['ok']} | partial: {tally['partial']} | failed: {tally['fail']} | skipped: {tally['skip']}")
    return rows_all, tally
