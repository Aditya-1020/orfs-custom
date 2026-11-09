"""OpenDB/OpenSTA metric extraction routines."""
import csv
import re
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
