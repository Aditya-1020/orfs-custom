"""STA and multi-corner (PVT) analysis using the resolved RunContext."""
from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .config import RunContext, resolve_openroad, stage_major
from .util import FlowError, log as _log, run as _run
# Compact table: short headers, no status (gate fails in run_corner instead).
# setup_ws is blended (often async recovery). Prefer core/apb/async for handoff.
# (metrics_key, header) — keep handoff cols first; terminal drops trailing if narrow.
COLS = (
    ("corner", "corner"),
    ("setup_ws_ns", "ws"),
    ("ws_clk_core_ns", "core"),
    ("ws_clk_apb_ns", "apb"),
    ("ws_async_ns", "async"),
    ("fmax_mhz", "fmax"),
    ("hold_ws_ns", "hold"),
    ("setup_tns_ns", "tns"),
    ("setup_violations", "s_v"),
    ("hold_violations", "h_v"),
    ("hold_tns_ns", "h_tns"),
    ("max_slew_violations", "slew"),
    ("max_cap_violations", "cap"),
    ("parasitics", "par"),
)

# Reports written per corner under analysis/sta/<stem>/<corner>/
CORNER_REPORTS = (
    "paths_setup.rpt",
    "paths_hold.rpt",
    "check_setup.rpt",
    "summary.rpt",
    "max_slew.rpt",
    "max_cap.rpt",
    "max_fanout.rpt",
    "unconstrained.rpt",
    "clock_summary.rpt",
    "recovery_removal.rpt",
    "path_groups.rpt",
    "design_summary.rpt",
    "critical_paths.rpt",
    "reg2reg.rpt",
    "in2reg.rpt",
    "reg2out.rpt",
    "metrics.json",
)


def liberty_corner_dir(ctx: RunContext) -> Path:
    timing = ctx.platform_settings.get("timing", {})
    configured = timing.get("corner_library_dir")
    if configured:
        path = Path(configured).expanduser()
        path = path.resolve() if path.is_absolute() else (ctx.platform_dir / path).resolve()
        if path.is_dir():
            return path
    return ctx.platform_dir / "lib"


def list_corners(ctx: RunContext, *, all_corners: bool) -> list[tuple[str, Path]]:
    timing = ctx.platform_settings.get("timing", {})
    libdir = liberty_corner_dir(ctx)
    pattern = timing.get("liberty_glob", "*.lib")
    prefix = timing.get("corner_prefix", "")
    ignored = tuple(timing.get("ignored_patterns", []))
    found = sorted(path for path in libdir.glob(pattern) if path.is_file())

    corners = []
    for path in found:
        if any(token in path.name for token in ignored):
            continue
        name = path.stem
        if prefix and name.startswith(prefix):
            name = name[len(prefix):]
        corners.append((name, path))

    if all_corners:
        return corners

    defaults = timing.get("default_corners", [])
    if defaults:
        selected = [(name, path) for name, path in corners if name in defaults]
        missing = set(defaults) - {name for name, _ in selected}
        if missing:
            _log(f"warning: missing configured Liberty corners: {sorted(missing)}")
        if selected:
            return selected

    if len(corners) == 1:
        return corners

    default = ctx.default_liberty()
    if default.is_file():
        return [(default.stem.removeprefix(prefix), default)]
    return []


def resolve_stage(ctx: RunContext, stage: str) -> tuple[str, Path, Path]:
    """Resolve an ODB and matching SDC for the requested stage."""
    return ctx.resolve_stage(stage)


def _macro_libs(ctx: RunContext) -> str:
    return " ".join(str(path) for path in ctx.macro_liberty_files())


def sta_env(
    ctx: RunContext,
    *,
    mode: str,
    stem: str,
    odb: Path,
    sdc: Path,
    liberty: Path,
    out_dir: Path,
    corner: str = "",
    paths: int = 20,
) -> dict[str, str]:
    """Build the environment shared by STA corner runs and the interactive shell."""
    env = os.environ.copy()
    env.update(
        {
            "STA_MODE": mode,
            "STA_ODB": str(odb),
            "STA_SDC": str(sdc),
            "STA_LIBERTY": str(liberty),
            "STA_RC_TCL": str(rc_tcl(ctx)),
            "STA_OUT": str(out_dir),
            "STA_MACRO_LIBS": _macro_libs(ctx),
            "STA_PATHS": str(paths),
            "STA_STAGE_MAJOR": str(stage_major(stem)),
            "STAGE": stem,
            "WORK_HOME": str(ctx.work_home),
            "PLATFORM": ctx.platform,
            "DESIGN_NICKNAME": ctx.design_nickname,
            "FLOW_VARIANT": ctx.flow_variant,
            "RUN_TAG": ctx.run_tag,
            "ORFS_FLOW": str(ctx.orfs_flow),
        }
    )
    if corner:
        env["STA_CORNER"] = corner
    extension_loader = ctx.tcl_extension_loader()
    if extension_loader:
        env["STA_EXTRA_TCL"] = str(extension_loader)
    return env


def run_corner(
    ctx: RunContext,
    *,
    stem: str,
    odb: Path,
    sdc: Path,
    corner: str,
    lib: Path,
    out_dir: Path,
    paths: int,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    # Never let a failed/partial OpenROAD invocation reuse reports from an
    # earlier run in this corner directory.
    for report in (*CORNER_REPORTS, "openroad.log"):
        (out_dir / report).unlink(missing_ok=True)

    env = sta_env(
        ctx,
        mode="corner",
        stem=stem,
        odb=odb,
        sdc=sdc,
        liberty=lib,
        out_dir=out_dir,
        corner=corner,
        paths=paths,
    )
    exe = resolve_openroad(ctx.orfs_flow)
    cmd = [exe, "-no_splash", "-exit", str(ctx.tcl_dir / "sta.tcl")]
    _log(f"$ corner={corner}")
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    (out_dir / "openroad.log").write_text(proc.stdout + "\n" + proc.stderr)
    metrics_path = out_dir / "metrics.json"
    if proc.returncode != 0 or not metrics_path.is_file():
        _log(proc.stderr[-2000:] if proc.stderr else proc.stdout[-2000:])
        raise FlowError(f"STA failed for corner {corner} (exit {proc.returncode})")
    metrics = json.loads(metrics_path.read_text())

    # Verify that this invocation's Tcl script reports the exact inputs that
    # Python resolved. This catches stale output and path-selection regressions.
    for key, expected in (("odb", odb), ("sdc", sdc), ("liberty", lib)):
        actual = metrics.get(key)
        if not actual or Path(actual).resolve() != expected.resolve():
            raise FlowError(
                f"STA corner {corner} input mismatch for {key}: "
                f"expected {expected}, got {actual!r}"
            )
    if metrics.get("stage") != stem:
        raise FlowError(
            f"STA corner {corner} stage mismatch: expected {stem}, "
            f"got {metrics.get('stage')!r}"
        )

    if metrics.get("status") not in ("ok", "partial"):
        raise FlowError(
            f"STA corner {corner} produced no meaningful timing "
            f"(status={metrics.get('status')})"
        )
    missing = [r for r in CORNER_REPORTS if not (out_dir / r).is_file()]
    if missing:
        _log(f"warning: corner {corner} missing reports: {', '.join(missing)}")
    return metrics


def write_tables(out_root: Path, rows: list[dict]) -> None:
    from .tables import render_table, write_csv

    csv_path = out_root / "corners.csv"
    txt_path = out_root / "corners.txt"
    keys = [k for k, _ in COLS]
    headers = [h for _, h in COLS]
    body = [[r.get(k, "NA") for k in keys] for r in rows]
    title = "PVT STA (not full signoff MCMM)"
    write_csv(csv_path, headers, body)
    # Terminal: may drop trailing cols if the tty is narrow.
    print(render_table(headers, body, title=title), flush=True)
    print(f"csv → {csv_path}", flush=True)
    # Artifact: always full width.
    txt_path.write_text(render_table(headers, body, title=title, max_width=10_000) + "\n")
    _log(f"table → {txt_path}")


def _sta_jobs(n_corners: int) -> int:
    """How many OpenROAD corner processes to run at once (default: 6)."""
    raw = os.environ.get("STA_JOBS", "6").strip()
    try:
        want = int(raw)
    except ValueError as exc:
        raise FlowError(f"STA_JOBS must be an integer, got {raw!r}") from exc
    return max(1, min(n_corners, want))


def run_sta_pvt(
    ctx: RunContext,
    *,
    stage: str = "latest",
    all_corners: bool = False,
    paths: int = 20,
    baseline_tag: str | None = None,
) -> int:
    """Multi-corner STA using the exact same RunContext as ORFS."""
    ctx.validate_config()
    stem, odb, sdc = resolve_stage(ctx, stage)
    corners = list_corners(ctx, all_corners=all_corners)
    if not corners:
        raise FlowError(f"no liberty corners under {liberty_corner_dir(ctx)}")

    out_root = ctx.analysis_dir / "sta" / stem
    out_root.mkdir(parents=True, exist_ok=True)
    jobs = _sta_jobs(len(corners))
    _log(f"stage={stem} odb={odb} sdc={sdc}")
    _log(f"work_home={ctx.work_home}")
    _log(f"corners={len(corners)} jobs={jobs} → {out_root}")
    _log("note: PVT liberty sweep is not full signoff MCMM")

    # Same per-corner work as before; only the scheduling is concurrent.
    # Table row order stays the list_corners order.
    rows_by_corner: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {
            pool.submit(
                run_corner,
                ctx,
                stem=stem,
                odb=odb,
                sdc=sdc,
                corner=corner,
                lib=lib,
                out_dir=out_root / corner,
                paths=paths,
            ): corner
            for corner, lib in corners
        }
        try:
            for fut in as_completed(futures):
                corner = futures[fut]
                rows_by_corner[corner] = fut.result()
        except Exception:
            for fut in futures:
                fut.cancel()
            raise

    rows = [rows_by_corner[corner] for corner, _ in corners]
    write_tables(out_root, rows)
    _log(f"reports → {out_root}/<corner>/ (reg2reg.rpt, in2reg.rpt, reg2out.rpt, critical_paths.rpt, …)")

    # Dual-consumable summary / regression / path-group rollup + dataset update.
    try:
        from .actions import post_sta_reports

        post_sta_reports(ctx, stem=stem, baseline_tag=baseline_tag)
    except Exception as exc:
        _log(f"warning: post-STA report pack failed: {exc}")
    return 0


def run_sta_shell(
    ctx: RunContext,
    *,
    stage: str = "latest",
    corner: str | None = None,
) -> int:
    """Interactive OpenROAD with the same design load as sta-pvt.

    Uses STAGE (same as sta-pvt). Liberty defaults to the platform nominal
    corner, or the named PVT corner when --corner / STA_CORNER is set.
    """
    ctx.validate_config()
    stem, odb, sdc = resolve_stage(ctx, stage)

    lib = ctx.default_liberty()
    corner_name = corner or os.environ.get("STA_CORNER", "")
    if corner_name:
        matches = [p for c, p in list_corners(ctx, all_corners=True) if c == corner_name]
        if not matches:
            raise FlowError(f"unknown corner {corner_name!r} for PLATFORM={ctx.platform}")
        lib = matches[0]
    else:
        corner_name = "default"

    out = ctx.analysis_dir / "sta" / stem / "shell"
    out.mkdir(parents=True, exist_ok=True)
    env = sta_env(
        ctx,
        mode="shell",
        stem=stem,
        odb=odb,
        sdc=sdc,
        liberty=lib,
        out_dir=out,
        corner=corner_name,
        paths=20,
    )
    exe = resolve_openroad(ctx.orfs_flow)
    _log(f"sta-shell stage={stem} corner={corner_name} odb={odb}")
    # No -exit: stay in OpenROAD after sta.tcl returns.
    return _run([exe, "-no_splash", str(ctx.tcl_dir / "sta.tcl")], env=env, check=False)


def run_presta(ctx: RunContext, *, shell: bool = False) -> int:
    ctx.validate_config()
    ctx.validate_fakeram()
    libs = _macro_libs(ctx)
    env = os.environ.copy()
    env.update(
        {
            "TOP": "sys_top",
            "USE_SLANG": "1",
            "RTL_ROOT": str(ctx.rtl),
            "RTL_FILELIST": str(ctx.rtl_filelist),
            "RTL_FILES": str(ctx.rtl / "cells_clkgate.v"),
            "DEFINES": ctx.pre_sta_define(),
            "LIBERTY": str(ctx.default_liberty()),
            "MACRO_LIBS": libs,
            "SDC_FILE": str(ctx.constraints),
            "OUT_DIR": str(ctx.work_home / "reports" / ctx.platform / "pre_sta"),
        }
    )
    if shell:
        env["SKIP_SYNTH"] = "1"
    exe = resolve_openroad(ctx.orfs_flow)
    flag = [] if shell else ["-exit"]
    return _run([exe, "-no_splash", *flag, str(ctx.tcl_dir / "sta_pre.tcl")], env=env)
