"""Project-first ORFS commands."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .project import (
    discover_orfs,
    discover_pdk,
    load_project,
    project_values,
    select_project,
    selected_project,
)
from .util import FlowError


def _context(config_arg: str | None):
    config = Path(config_arg).expanduser().resolve() if config_arg else selected_project()
    config, data = load_project(config)
    values = project_values(config, data)
    values["orfs"] = discover_orfs(config, data)
    values["pdk"] = discover_pdk(config, data, values["platform"])
    return config, data, values


def _orfs_env(values: dict) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "ROOT_DIR": str(values["root"]),
        "DESIGN_CONFIG": str(values["design_config"]),
        "ORFS_FLOW": str(values["orfs"]),
        "PLATFORM": str(values["platform"]),
        "WORK_HOME": str(values["work_home"]),
        "RUN_TAG": str(values["run_tag"]),
        "FLOW_VARIANT": str(values["flow_variant"]),
        "PLACEMENT": str(values["placement"]),
    })
    if values["pdk"] is not None:
        env["PDK_ROOT"] = str(values["pdk"])
    return env


def _run_make(values: dict, target: str) -> int:
    target = "finish" if target == "flow" else target
    env = _orfs_env(values)
    command = ["make", "--no-print-directory", "-C", str(values["orfs"]), target]
    command.extend([
        f"DESIGN_CONFIG={values['design_config']}",
        f"WORK_HOME={values['work_home']}",
        f"FLOW_VARIANT={values['flow_variant']}",
        f"PLATFORM={values['platform']}",
    ])
    if values["pdk"] is not None:
        command.append(f"PDK_ROOT={values['pdk']}")
    if values["placement"] != "rtlmp":
        command.append(f"PLACEMENT={values['placement']}")
    return subprocess.call(command, env=env)


def _find_liberty(values: dict, override: str | None = None) -> Path:
    platform = values["orfs"] / "platforms" / values["platform"]
    requested = override or values["liberty"]
    if requested != "auto":
        candidate = Path(requested).expanduser()
        if not candidate.is_absolute():
            candidate = platform / candidate
        if not candidate.is_file():
            raise FlowError(f"Liberty file does not exist: {candidate}")
        return candidate.resolve()
    candidates = sorted((platform / "lib").glob("*.lib"))
    if values["platform"] == "sky130hd" and values["pdk"] is not None:
        pdk = values["pdk"] / "sky130A" / "libs.ref" / "sky130_fd_sc_hd" / "lib"
        candidates = sorted(pdk.glob("sky130_fd_sc_hd__*.lib")) or candidates
    candidates = [p for p in candidates if not any(x in p.name.lower() for x in ("ccsnoise", "lvf"))]
    typical = [p for p in candidates if "tt_025c_1v80" in p.name.lower() or "typical" in p.name.lower()]
    result = typical[0] if typical else (candidates[0] if candidates else None)
    if result is None:
        raise FlowError(f"no Liberty files found under {platform / 'lib'}")
    return result.resolve()


def _run_sta(config: Path, values: dict, stage: str, all_corners: bool, corner_names: list[str]):
    from .config import RunContext
    from .sta import run_sta_pvt

    if all_corners and corner_names:
        raise FlowError("use either --all or --corner, not both")
    platform_dir = values["orfs"] / "platforms" / values["platform"]
    if values["platform"] == "sky130hd" and values["pdk"] is not None:
        corner_dir = values["pdk"] / "sky130A" / "libs.ref" / "sky130_fd_sc_hd" / "lib"
    else:
        corner_dir = platform_dir / "lib"
    if values["platform"] == "sky130hd":
        prefix, pattern = "sky130_fd_sc_hd__", "sky130_fd_sc_hd__*.lib"
        default_lib = _find_liberty(values)
    elif values["platform"] == "nangate45":
        prefix, pattern = "NangateOpenCellLibrary_", "NangateOpenCellLibrary_*.lib"
        default_lib = _find_liberty(values)
    else:
        prefix, pattern = "", "*.lib"
        default_lib = _find_liberty(values)

    platform_settings = {
        "timing": {
            "default_liberty": str(default_lib),
            "corner_library_dir": str(corner_dir),
            "liberty_glob": pattern,
            "corner_prefix": prefix,
            "ignored_patterns": ["ccsnoise", "lvf"],
            "default_corners": values["corners"],
        }
    }
    data = values["data"]
    design_settings = {
        "design": {"name": values["name"], "top": values["top"]},
        "rtl": {"root": str(values["rtl"]), "filelist": str(values["filelist"])},
        "constraints": {
            "default": str(values["constraints"] / f"constraints_{values['platform']}.sdc")
        },
        "macros": data.get("macros", []),
        "extensions": data.get("extensions", {}),
    }
    path_settings = {
        "analysis_dir": "{work_home}/reports",
        "metrics_db": "{work_home}/reports/metrics.db",
        "placements_static": "placements",
        "placements_generated": "{work_home}/placements",
        "rtl_dir": str(values["rtl"]),
        "constraints_dir": str(values["constraints"]),
        "fakeram_dir": str(values["macros"]),
    }
    ctx = RunContext(
        root=values["root"],
        orfs_flow=values["orfs"],
        pdk_root=values["pdk"] or values["orfs"],
        platform=values["platform"],
        design_nickname=values["name"],
        fakeram_tech=values["platform"],
        run_tag=values["run_tag"],
        flow_variant=values["flow_variant"],
        placement=values["placement"],
        design_config=values["design_config"],
        work_home=values["work_home"],
        config_dir=config.parent,
        flow_settings={"extensions": data.get("extensions", {})},
        path_settings=path_settings,
        platform_settings=platform_settings,
        design_settings=design_settings,
    )
    return run_sta_pvt(
        ctx,
        stage=stage,
        all_corners=all_corners,
        corner_names=corner_names or None,
        jobs=values["jobs"],
        post_reports=False,
    )


def _module_sta(values: dict, name: str) -> int:
    module = values["modules"].get(name)
    if not isinstance(module, dict):
        raise FlowError(f"module '{name}' is not defined in project modules")
    top = str(module.get("top", name))
    rtl_root = Path(module.get("rtl_root", values["rtl"]))
    rtl_root = rtl_root.resolve() if rtl_root.is_absolute() else (values["root"] / rtl_root).resolve()
    filelist = module.get("filelist")
    sources: list[Path] = []
    if filelist:
        list_path = Path(filelist)
        list_path = list_path.resolve() if list_path.is_absolute() else (values["root"] / list_path).resolve()
        if not list_path.is_file():
            raise FlowError(f"module filelist not found: {list_path}")
        for line in list_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith(("#", "//")):
                source = Path(line).expanduser()
                sources.append((source if source.is_absolute() else rtl_root / source).resolve())
    else:
        for item in module.get("files", []):
            source = Path(item).expanduser()
            sources.append((source if source.is_absolute() else values["root"] / source).resolve())
    if not sources or any(not path.is_file() for path in sources):
        missing = [str(path) for path in sources if not path.is_file()]
        raise FlowError("module RTL sources empty or missing: " + ", ".join(missing))

    liberty = _find_liberty(values, module.get("liberty"))
    out = values["work_home"] / "reports" / "modules" / name
    out.mkdir(parents=True, exist_ok=True)
    yosys, openroad = shutil.which("yosys"), shutil.which("openroad")
    if not yosys or not openroad:
        raise FlowError("module-sta requires yosys and openroad in PATH")

    quote = lambda value: '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'
    includes = [
        Path(p).resolve() if Path(p).is_absolute() else (values["root"] / p).resolve()
        for p in module.get("include_dirs", values["include_dirs"])
    ]
    defines = list(module.get("defines", values["defines"]))
    parameters = module.get("parameters", {})
    frontend = str(module.get("frontend", "slang")).lower()
    lines = [f"read_liberty -lib {quote(liberty)}"]
    if frontend == "slang":
        lines.append("plugin -i slang")
        flags = [f"-I {quote(p)}" for p in includes]
        flags += [f"-D {quote(d)}" for d in defines]
        flags += [f"-G {quote(f'{key}={value}')}" for key, value in parameters.items()]
        lines.append(f"read_slang --top {top} {' '.join(flags)} {' '.join(quote(p) for p in sources)}")
    elif frontend == "verilog":
        flags = [f"-I{quote(p)}" for p in includes]
        flags += [f"-D{quote(d)}" for d in defines]
        lines.append(f"read_verilog -sv {' '.join(flags)} {' '.join(quote(p) for p in sources)}")
        for key, value in parameters.items():
            lines.append(f"chparam -set {key} {value} {top}")
        lines.append(f"hierarchy -check -top {top}")
    else:
        raise FlowError("module frontend must be 'slang' or 'verilog'")

    lines += [
        f"synth -top {top}",
        f"dfflibmap -liberty {quote(liberty)}",
        f"abc -liberty {quote(liberty)}",
        f"tee -o {quote(out / 'area.rpt')} stat -liberty {quote(liberty)}",
        f"write_verilog -noattr {quote(out / 'netlist.v')}",
    ]
    ys = out / "synth.ys"
    ys.write_text("\n".join(lines) + "\n")
    yosys_log = out / "yosys.log"
    proc = subprocess.run([yosys, "-l", str(yosys_log), str(ys)], capture_output=True, text=True)
    if proc.returncode:
        raise FlowError(f"Yosys module synthesis failed; inspect {yosys_log}\n{proc.stderr[-1500:]}")

    import re

    netlist_path = out / "netlist.v"
    netlist_path.write_text(re.sub(r"\\bsigned\\s+", "", netlist_path.read_text()))

    constraint = module.get("constraints")
    sdc = ""
    if constraint:
        candidate = Path(constraint).expanduser()
        candidate = candidate.resolve() if candidate.is_absolute() else (values["root"] / candidate).resolve()
        if not candidate.is_file():
            raise FlowError(f"module SDC not found: {candidate}")
        sdc = str(candidate)
    env = os.environ.copy()
    env.update({
        "MODULE_TOP": top,
        "MODULE_NETLIST": str(out / "netlist.v"),
        "MODULE_LIBERTY": str(liberty),
        "MODULE_SDC": sdc,
        "MODULE_OUT": str(out),
        "MODULE_PERIOD": str(module.get("clock_period", values["clock_period"])),
        "MODULE_CLOCK": str(module.get("clock", "")),
    })
    script = Path(__file__).parent / "tcl" / "module_sta.tcl"
    proc = subprocess.run([openroad, "-no_splash", "-exit", str(script)], env=env, capture_output=True, text=True)
    (out / "opensta.log").write_text(proc.stdout + "\n" + proc.stderr)
    if proc.returncode:
        raise FlowError(f"OpenSTA module analysis failed; inspect {out / 'opensta.log'}")
    print(f"module STA reports: {out}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="orfsx", description="Project-first ORFS runner")
    parser.add_argument("-c", "--config", help="project YAML; defaults to selected project")
    sub = parser.add_subparsers(dest="command", required=True)
    project = sub.add_parser("project")
    project.add_argument("path")
    validate = sub.add_parser("validate")
    run = sub.add_parser("run")
    run.add_argument("stage", choices=["synth", "floorplan", "place", "cts", "route", "finish", "flow"])
    gui = sub.add_parser("gui")
    gui.add_argument("stage", choices=["synth", "floorplan", "place", "cts", "route", "finish", "final"])
    sta = sub.add_parser("sta")
    sta.add_argument("--stage", default="latest")
    sta_group = sta.add_mutually_exclusive_group()
    sta_group.add_argument("--all", action="store_true")
    sta_group.add_argument("--corner", action="append", default=[])
    mod = sub.add_parser("module-sta")
    mod.add_argument("module")
    args = parser.parse_args(argv)
    try:
        if args.command == "project":
            print(select_project(args.path))
            return 0
        config, data, values = _context(args.config)
        if args.command == "validate":
            for key in ("root", "orfs", "pdk", "platform", "design_config", "work_home", "filelist"):
                print(f"{key}: {values[key]}")
            if not values["orfs"].is_dir():
                raise FlowError(f"ORFS not found: {values['orfs']}")
            if not values["design_config"].is_file():
                raise FlowError(f"design Makefile not found: {values['design_config']}")
            if not values["filelist"].is_file():
                raise FlowError(f"RTL filelist not found: {values['filelist']}")
            _find_liberty(values)
            return 0
        if args.command == "run":
            return _run_make(values, args.stage)
        if args.command == "gui":
            stage = "final" if args.stage in ("finish", "final") else args.stage
            return _run_make(values, "gui_" + stage)
        if args.command == "sta":
            return _run_sta(config, values, args.stage, args.all, args.corner)
        if args.command == "module-sta":
            return _module_sta(values, args.module)
    except Exception as exc:
        print(f"orfsx: error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
