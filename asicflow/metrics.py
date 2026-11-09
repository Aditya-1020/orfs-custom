#!/usr/bin/env python3
"""ORFS metrics: host driver + OpenROAD worker (same file).

Driver:  python3 -m asicflow.metrics place --runs base --platform sky130hd
Worker:  ORFS_METRICS_JOB=job.json openroad -no_splash -python -m asicflow.metrics

Missing values are always "NA". Add a metric with @section + c.emit(...).

Script-facing API (prefer this over raw SQL):
  from asicflow.schema import SETUP_WNS, load_snapshot, compare_snapshots
  snap = load_snapshot(db, platform=..., run_tag=..., stage=...)
  snap.num(SETUP_WNS)  # float | None
"""
import csv
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

NA = "NA"
NUM = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
PHYSICAL_RE = re.compile(r"SPACER|WELLTAP|ENDCAP|PAD|FILL")
BUF_INV_RE = re.compile(r"(?:^|_)(?:clk|dly)?(buf|inv)(?:_|\d)", re.I)  # name heuristic
POWER_ROW_RE = re.compile(r"^([A-Za-z][A-Za-z() ]*?)\s+" + r"\s+".join(["(" + NUM + ")"] * 4), re.M)

# (metric, tcl, regex) -- every one is captured into the .rpt file
SLACK_QUERIES = (
    ("setup_ws", "report_worst_slack -max -digits 3", r"worst slack\s+(?:max\s+)?(" + NUM + ")"),
    ("hold_ws", "report_worst_slack -min -digits 3", r"worst slack\s+(?:min\s+)?(" + NUM + ")"),
    ("setup_tns", "report_tns -max -digits 3", r"tns\s+(?:max\s+)?(" + NUM + ")"),
    ("hold_tns", "report_tns -min -digits 3", r"tns\s+(?:min\s+)?(" + NUM + ")"),
)
# Design-rule checks via report_check_types. Setup/hold path violations must
# use report_checks (see violations section) — report_check_types -max_delay
# quietly reports 0 while WNS/TNS are negative.
DRC_CHECK_TYPES = (
    ("max_slew_violations", "-max_slew"),
    ("max_cap_violations", "-max_capacitance"),
    ("max_fanout_violations", "-max_fanout"),
)
PLACEMENT_STATUSES = ("PLACED", "FIXED", "COVER", "NONE")


def _s(x):
    """OpenDB enums come back as str or as an object with getString()."""
    getter = getattr(x, "getString", None)
    return getter() if callable(getter) else str(x)


def _print_section(text):
    print(f"\n{'-' * 5} {text} {'-' * 5}")


def rx(pattern, text):
    m = re.search(pattern, text or "")
    return m.group(1) if m else None


def rx_float(pattern, text):
    v = rx(pattern, text)
    return float(v) if v is not None else None


def fmt(value, digits=None):
    """Format a metric exactly like Collector.emit; unavailable values stay NA."""
    if value is None or value == "":
        return NA
    if isinstance(value, float):
        if digits == "e":
            return f"{value:.6e}"
        if isinstance(digits, int):
            return f"{value:.{digits}f}"
        return f"{value:.6g}"
    return str(value)


def to_ns(v):
    """OpenSTA may return seconds; convert tiny values to ns."""
    return v * 1e9 if v and abs(v) < 1e-6 else v


def parse_power(text):
    """report_power table -> {label: (internal, switching, leakage, total)} in W."""
    rows = {}
    for m in POWER_ROW_RE.finditer(text or ""):
        label = re.sub(r"\W+", "_", m.group(1).strip().lower()).strip("_")
        rows[label] = tuple(float(m.group(i)) for i in range(2, 6))
    return rows


def parse_paths(text):
    """report_checks text -> list of dicts, one per path."""
    paths = []
    for chunk in (text or "").split("Startpoint: ")[1:]:
        paths.append({
            "startpoint": chunk.split()[0] if chunk.split() else None,
            "endpoint": rx(r"Endpoint: (\S+)", chunk),
            "group": rx(r"Path Group: (\S+)", chunk),
            "slack_ns": rx_float(r"(" + NUM + r")\s+slack", chunk),
            "arrival_ns": rx_float(r"(" + NUM + r")\s+data arrival time", chunk),
            "text": chunk,
        })
    return paths


def cell_kind(master_name):
    m = BUF_INV_RE.search(master_name)
    return {"buf": "buffer", "inv": "inverter"}.get(m.group(1).lower()) if m else None


def term_xy(term, is_bterm=False):
    """Pin centre in dbu; instance centre as fallback for iterms."""
    try:
        bb = term.getBBox()
    except Exception:
        if is_bterm:
            return None
        try:
            bb = term.getInst().getBBox()
        except Exception:
            return None
    return (bb.xMin() + bb.xMax()) / 2.0, (bb.yMin() + bb.yMax()) / 2.0


def eligible_nets(block):
    """Signal/clock nets with at least two pins: (net, iterms, bterms)."""
    for net in block.getNets():
        if _s(net.getSigType()) not in ("SIGNAL", "CLOCK"):
            continue
        iterms, bterms = list(net.getITerms()), list(net.getBTerms())
        if len(iterms) + len(bterms) >= 2:
            yield net, iterms, bterms


# -------------------------------------------------------------------- collector
class Collector:
    def __init__(self, job):
        self.job = job
        self.corner = job.get("corner", "-")
        self.row_corner = "-"
        self.rows_fh = open(job["rows"], "w", newline="", buffering=1) if job.get("rows") else None
        self.writer = csv.writer(self.rows_fh) if self.rows_fh else None
        self.rpt = Path(job["rpt"]) if job.get("rpt") else None
        if self.rpt:
            self.rpt.unlink(missing_ok=True)
        self.tech = None         # keep openroad.Tech alive for the Design lifetime
        self.design = None       # openroad.Design (needed for Tcl / STA)
        self.block = None        # odb dbBlock
        self.dbu = 1
        self.libs_ok = True
        self.sta_ready = False
        self.macro_names = []
        self.errors = []

    # --- output -----------------------------------------------------------
    def emit(self, category, metric, value, unit="", digits=None):
        text = fmt(value, digits)
        print(f"{category}.{metric} = {text} {unit}".rstrip())
        if self.writer:  # one line per row, flushed at once, so a crash keeps earlier rows
            self.writer.writerow([self.job.get("run", "-"), self.job["stage"], self.row_corner,
                                  category, metric, text, unit])

    def emit_many(self, category, prefix, items):
        """items: (metric, value, unit, digits) -> rows named '<prefix>.<metric>'."""
        for metric, value, unit, digits in items:
            self.emit(category, f"{prefix}.{metric}", value, unit, digits)

    def fail(self, title, exc):
        msg = " ".join(str(exc).split())[:300]
        print(f"ERROR [{title}]: {msg}")
        self.errors.append((title, msg))

    # --- helpers ----------------------------------------------------------
    def um(self, dbu_value):
        return dbu_value / self.dbu

    def tcl(self, cmd):
        if self.design is None:
            raise RuntimeError("Tcl interface not available")
        return self.design.evalTclString(cmd)

    def capture(self, title, cmd):
        """Run an OpenSTA report, append it to the .rpt file, return its text (None on error)."""
        tmp = Path(str(self.rpt or "/tmp/metrics_capture") + ".tmp")
        tmp.unlink(missing_ok=True)
        try:
            self.tcl(f"{cmd} > {{{tmp}}}")
            text = tmp.read_text() if tmp.exists() else ""
        except Exception as exc:
            self.fail(title, exc)
            text = None
        tmp.unlink(missing_ok=True)
        if self.rpt:
            with open(self.rpt, "a") as fh:
                fh.write(f"\n===== {title} ({self.corner}) =====\n"
                         f"{'ERROR: capture failed' if text is None else text}\n")
        return text

    def stage_number(self):
        m = re.match(r"(\d+)", self.job["stage"])
        return int(m.group(1)) if m else 0


# --------------------------------------------------------------- section registry
SECTIONS = []  # (title, scope, needs_sta, when, function)


def section(title, scope="corner", needs_sta=False, when=None):
    """scope='design': once per run (first corner only).  scope='corner': every corner.
    needs_sta: skipped unless 'STA setup' succeeded.  when: job key that must be truthy."""
    def register(fn):
        SECTIONS.append((title, scope, needs_sta, when, fn))
        return fn
    return register


# ------------------------------------------------------------------ design scope
@section("Geometry", scope="design")
def geometry(c):
    for name, rect in (("die", c.block.getDieArea()), ("core", c.block.getCoreArea())):
        w, h = c.um(rect.xMax() - rect.xMin()), c.um(rect.yMax() - rect.yMin())
        c.emit("design", f"{name}_w_um", w, "um", 2)
        c.emit("design", f"{name}_h_um", h, "um", 2)
        c.emit("design", f"{name}_area_um2", w * h, "um2", 1)


@section("Instances", scope="design")
def instances(c):
    count, area = Counter(), Counter()
    hist, status, special = Counter(), Counter(), Counter()
    for inst in c.block.getInsts():
        master = inst.getMaster()
        name = master.getName()
        a = master.getWidth() * master.getHeight() / float(c.dbu) ** 2
        if inst.isBlock():
            kind = "macro"
        elif PHYSICAL_RE.search(_s(master.getType())):
            kind = "physical"
        else:
            kind = "std"
        count[kind] += 1
        area[kind] += a
        hist[name] += 1
        status[_s(inst.getPlacementStatus())] += 1
        special_kind = cell_kind(name) if kind == "std" else None
        if special_kind:
            special[special_kind] += 1
            special[special_kind + "_area"] += a

    core = c.block.getCoreArea()
    core_area = c.um(core.xMax() - core.xMin()) * c.um(core.yMax() - core.yMin())
    c.emit("design", "inst_total_count", sum(count.values()), "count")
    for kind in ("std", "macro", "physical"):
        c.emit("design", f"inst_{kind}_count", count[kind], "count")
        c.emit("design", f"area_{kind}_um2", area[kind], "um2", 2)
    for kind in ("buffer", "inverter"):
        c.emit("design", f"inst_{kind}_count", special[kind], "count")
        c.emit("design", f"area_{kind}_um2", special[kind + "_area"], "um2", 2)
    c.emit("design", "inst_unplaced_count", status["NONE"], "count")
    if core_area > 0:
        c.emit("design", "util_all_pct", 100.0 * (area["std"] + area["macro"]) / core_area, "%", 2)
        c.emit("design", "macro_share_of_core_pct", 100.0 * area["macro"] / core_area, "%", 2)
    if core_area > area["macro"]:
        c.emit("design", "util_std_excl_macro_pct",
               100.0 * area["std"] / (core_area - area["macro"]), "%", 2)
    for st in list(PLACEMENT_STATUSES) + sorted(set(status) - set(PLACEMENT_STATUSES)):
        c.emit("placement", f"status.{st}", status[st], "count")
    for master_name in sorted(hist):
        c.emit("cell_usage", master_name, hist[master_name], "count")


@section("Macros", scope="design")
def macros(c):
    die = c.block.getDieArea()
    centres = []
    for inst in c.block.getInsts():
        if not inst.isBlock():
            continue
        bb = inst.getBBox()
        x0, y0, x1, y1 = (c.um(v) for v in (bb.xMin(), bb.yMin(), bb.xMax(), bb.yMax()))
        name = inst.getName()
        c.emit_many("macro", name, (
            ("master", inst.getMaster().getName(), "", None),
            ("x_um", x0, "um", 2), ("y_um", y0, "um", 2),
            ("w_um", x1 - x0, "um", 2), ("h_um", y1 - y0, "um", 2),
            ("orient", _s(inst.getOrient()), "", None),
            ("edge_left_um", x0 - c.um(die.xMin()), "um", 2),
            ("edge_right_um", c.um(die.xMax()) - x1, "um", 2),
            ("edge_bottom_um", y0 - c.um(die.yMin()), "um", 2),
            ("edge_top_um", c.um(die.yMax()) - y1, "um", 2),
        ))
        centres.append((name, (x0 + x1) / 2.0, (y0 + y1) / 2.0))
    for i, (na, xa, ya) in enumerate(centres):
        for nb, xb, yb in centres[i + 1:]:
            c.emit("macro_dist", f"{na} -- {nb}", abs(xa - xb) + abs(ya - yb), "um", 1)


@section("Connectivity", scope="design")
def connectivity(c):
    nets = list(c.block.getNets())
    sig = Counter(_s(n.getSigType()) for n in nets)
    c.emit("net", "count_total", len(nets), "count")
    for sig_type in ("SIGNAL", "CLOCK", "POWER", "GROUND"):
        c.emit("net", f"count_{sig_type.lower()}", sig[sig_type], "count")
    c.emit("net", "iterm_count", len(list(c.block.getITerms())), "count")
    c.emit("net", "bterm_count", len(list(c.block.getBTerms())), "count")
    fanout = [len(i) + len(b) - 1 for _, i, b in eligible_nets(c.block)]
    c.emit("net", "net_count", len(fanout), "count")  # signal/clock nets with >= 2 pins
    c.emit("net", "fanout_avg", sum(fanout) / len(fanout) if fanout else None, "", 3)
    c.emit("net", "fanout_max", max(fanout) if fanout else None, "", 0)


@section("HPWL", scope="design", when="hpwl")
def hpwl(c):
    lengths = []
    for net, iterms, bterms in eligible_nets(c.block):
        pts = [term_xy(t) for t in iterms] + [term_xy(t, True) for t in bterms]
        pts = [p for p in pts if p]
        if len(pts) >= 2:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            lengths.append((c.um(max(xs) - min(xs) + max(ys) - min(ys)), net.getName()))
    total = sum(v for v, _ in lengths)
    c.emit("net", "hpwl_total_um", total, "um", 0)
    c.emit("net", "hpwl_avg_um", total / len(lengths) if lengths else None, "um", 2)
    c.emit("net", "hpwl_max_um", max(lengths)[0] if lengths else None, "um", 1)
    for rank, (value, name) in enumerate(sorted(lengths, reverse=True)[:10], 1):
        c.emit("net", f"long_net{rank}.hpwl_um", value, "um", 1)
        c.emit("net", f"long_net{rank}.name", name)


# ------------------------------------------------------------------ corner scope
@section("STA setup")
def sta_setup(c):
    job = c.job
    if not c.libs_ok:
        raise RuntimeError("liberty load failed or Tcl interface missing")
    if not job.get("sdc"):
        raise RuntimeError("no SDC file found at or before this stage")
    c.tcl(f"read_sdc {{{job['sdc']}}}")
    c.emit("timing", "sdc_file", Path(job["sdc"]).name)
    if c.stage_number() >= 4:
        c.tcl("set_propagated_clock [all_clocks]")

    mode = "none"
    if job.get("spef"):
        try:
            c.tcl(f"read_spef {{{job['spef']}}}")
            mode = "spef"
        except Exception as exc:
            c.fail("read_spef", exc)
    elif c.stage_number() >= 3:
        try:
            c.tcl(f"source {{{job['platform_dir']}/setRC.tcl}}")
            c.tcl("estimate_parasitics -placement")
            mode = "placement"
        except Exception as exc:
            c.fail("estimate_parasitics", exc)
    c.emit("timing", "parasitics", mode)

    c.period = None
    try:
        c.emit("timing", "clock_count", int(c.tcl("llength [all_clocks]")), "count")
        c.period = to_ns(float(c.tcl("get_property [lindex [all_clocks] 0] period")))
    except Exception as exc:
        c.fail("clock_period", exc)
    c.emit("timing", "clock_period_ns", c.period, "ns", 3)
    c.sta_ready = True


def emit_paths(c, tag, paths):
    for k, p in enumerate(paths, 1):
        hit = [m for m in c.macro_names if m in p["text"]]
        c.emit_many("worst_path", f"{tag}{k}", (
            ("startpoint", p["startpoint"], "", None), ("endpoint", p["endpoint"], "", None),
            ("group", p["group"], "", None), ("slack_ns", p["slack_ns"], "ns", 3),
            ("arrival_ns", p["arrival_ns"], "ns", 3),
            ("macros_on_path", " ".join(hit) or "none", "", None),
        ))


@section("Timing", needs_sta=True)
def timing(c):
    setup_txt = c.capture("setup_paths",
                          "report_checks -path_delay max -group_path_count 5 "
                          "-fields {fanout slew cap} -digits 3")
    hold_txt = c.capture("hold_paths", "report_checks -path_delay min -group_path_count 3 -digits 3")
    values = {m: rx_float(pat, c.capture(m, cmd)) for m, cmd, pat in SLACK_QUERIES}
    for metric, txt in (("setup_ws", setup_txt), ("hold_ws", hold_txt)):  # fallback: path reports
        slacks = [p["slack_ns"] for p in parse_paths(txt) if p["slack_ns"] is not None]
        if values[metric] is None and slacks:
            values[metric] = min(slacks)
    for metric, value in values.items():
        c.emit("timing", metric, value, "ns", 3)
    ws = values["setup_ws"]
    if ws is not None and c.period is not None and c.period - ws > 0:
        c.emit("timing", "fmax_est_mhz", 1000.0 / (c.period - ws), "MHz", 1)
    else:
        c.emit("timing", "fmax_est_mhz", None, "MHz")
    emit_paths(c, "setup", parse_paths(setup_txt))
    emit_paths(c, "hold", parse_paths(hold_txt))


@section("Violations", needs_sta=True)
def violations(c):
    # Path setup/hold: count violating endpoints (report_check_types -max_delay is wrong).
    for metric, delay in (("setup_violations", "max"), ("hold_violations", "min")):
        txt = c.capture(
            metric,
            f"report_checks -path_delay {delay} -slack_max 0 "
            "-group_path_count 100000 -format end",
        )
        c.emit("timing", metric, None if txt is None else txt.count("VIOLATED"), "count")
    for metric, flag in DRC_CHECK_TYPES:
        txt = c.capture(metric, f"report_check_types {flag} -violators")
        c.emit("timing", metric, None if txt is None else txt.count("VIOLATED"), "count")


@section("Clock skew", needs_sta=True)
def clock_skew(c):
    for kind in ("setup", "hold"):
        txt = c.capture(f"clock_skew_{kind}", f"report_clock_skew -{kind}")
        c.emit("timing", f"clock_{kind}_skew_ns", rx_float(r"(" + NUM + rf")\s+{kind} skew", txt), "ns", 3)
    c.capture("check_setup", "check_setup -verbose")


@section("Power", needs_sta=True)
def power(c):
    source = "vectorless"
    if c.job.get("saif"):
        try:
            c.tcl(f"read_saif -scope {c.job.get('saif_scope', 'tb/dut')} {{{c.job['saif']}}}")
            source = "saif"
        except Exception as exc:
            source = "saif_error"
            c.fail("read_saif", exc)
    c.emit("power", "activity_source", source)
    rows = parse_power(c.capture("power", "report_power"))
    for label, values in rows.items():
        c.emit_many("power", label, [(n, v, "W", "e") for n, v in
                                     zip(("internal_W", "switching_W", "leakage_W", "total_W"), values)])
    if "total" not in rows:
        c.emit("power", "total.total_W", None, "W")
        raise RuntimeError("report_power: no Total row found")
    c.capture("highest_power_instances", "report_power -highest_power_instances 20")


# ------------------------------------------------------------------------ driver
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

# === driver (host python3) ===
import argparse
import glob
import shutil
import sqlite3
import subprocess
import time
from collections import defaultdict
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
WORKER = Path(__file__).resolve()
HEADER = ["run_tag", "stage", "corner", "category", "metric", "value", "unit"]
STAGE_STEMS = {
    "synth": "1_synth",
    "floorplan": "2_floorplan",
    "place": "3_place",
    "cts": "4_cts",
    "route": "5_route",
    "final": "6_final",
}
KEY_METRICS = (
    ("timing", "setup_ws"),
    ("timing", "setup_tns"),
    ("timing", "hold_ws"),
    ("timing", "hold_tns"),
    ("power", "total.total_W"),
)
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


def print_summary(rows):
    design = {(r[0], r[4]): r[5] for r in rows if r[2] == "-" and r[3] in ("design", "net")}
    value = {(r[0], r[2], r[4]): r[5] for r in rows if r[2] != "-" and r[3] in ("timing", "power", "status")}
    keys = sorted({(r[0], r[2]) for r in rows if r[2] != "-" and r[3] == "status" and r[4] == "result"})
    if not keys:
        return

    def get(table, key, scale=1.0, fmt="{:.3f}"):
        try:
            return fmt.format(float(table[key]) * scale)
        except (KeyError, ValueError):
            return "NA"

    try:
        from .tables import emit_table
    except ImportError:
        from asicflow.tables import emit_table  # type: ignore

    headers = ("RUN", "CORNER", "STATUS", "SETUP_WS", "SETUP_TNS", "HOLD_WS", "POWER_MW", "UTIL_%", "HPWL_MM")
    body = [
        (
            run,
            corner,
            value[(run, corner, "result")].upper(),
            get(value, (run, corner, "setup_ws")),
            get(value, (run, corner, "setup_tns")),
            get(value, (run, corner, "hold_ws")),
            get(value, (run, corner, "total.total_W"), 1000),
            get(design, (run, "util_all_pct"), 1, "{:.2f}"),
            get(design, (run, "hpwl_total_um"), 0.001, "{:.2f}"),
        )
        for run, corner in keys
    ]
    print()
    emit_table(headers, body, title="metrics summary")


# -------------------------------------------------------------------- database
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



def _want_worker(argv):
    if os.environ.get("ORFS_METRICS_JOB"):
        return True
    if len(argv) >= 2 and not str(argv[1]).startswith("-"):
        suf = Path(argv[1]).suffix.lower()
        if suf in {".json", ".odb"}:
            return True
    return False


if __name__ == "__main__":
    if _want_worker(sys.argv):
        _run_worker()
    else:
        sys.exit(driver_main())
