"""Small project-first command layer. The legacy analysis CLI remains available."""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .project import discover_orfs, discover_pdk, load_project, project_values, select_project, selected_project
from .util import FlowError


def _context(config_arg: str | None):
    config = Path(config_arg).expanduser().resolve() if config_arg else selected_project()
    config, data = load_project(config)
    values = project_values(config, data)
    values["orfs"] = discover_orfs(config, data)
    values["pdk"] = discover_pdk(config, data)
    return config, data, values


def _orfs_env(values: dict) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "ROOT_DIR": str(values["root"]),
        "DESIGN_CONFIG": str(values["design_config"]),
        "ORFS_FLOW": str(values["orfs"]),
        "PDK_ROOT": str(values["pdk"]),
        "PLATFORM": str(values["platform"]),
        "WORK_HOME": str(values["build"]),
        "RUN_TAG": str(values["run_tag"]),
        "FLOW_VARIANT": str(values["flow_variant"]),
        "PLACEMENT": str(values["placement"]),
    })
    return env


def _run_make(values: dict, target: str) -> int:
    env = _orfs_env(values)
    command = ["make", "--no-print-directory", "-C", str(values["orfs"]), target]
    command.extend([
        f"DESIGN_CONFIG={values['design_config']}",
        f"WORK_HOME={values['build']}",
        f"FLOW_VARIANT={values['flow_variant']}",
        f"RUN_TAG={values['run_tag']}",
        f"PLATFORM={values['platform']}",
        f"PDK_ROOT={values['pdk']}",
    ])
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
    if values["platform"] == "sky130hd":
        pdk = values["pdk"] / "sky130A/libs.ref/sky130_fd_sc_hd/lib"
        candidates = sorted(pdk.glob("sky130_fd_sc_hd__*.lib")) or candidates
    candidates = [p for p in candidates if not any(x in p.name.lower() for x in ("ccsnoise", "lvf"))]
    typical = [p for p in candidates if "tt_025c_1v80" in p.name.lower() or "typical" in p.name.lower()]
    result = typical[0] if typical else (candidates[0] if candidates else None)
    if result is None:
        raise FlowError(f"no Liberty files found under {platform / 'lib'}")
    return result.resolve()


def _corners(values: dict, all_corners: bool, names: list[str]):
    directory = values["orfs"] / "platforms" / values["platform"] / "lib"
    if values["platform"] == "sky130hd":
        pdk_dir = values["pdk"] / "sky130A/libs.ref/sky130_fd_sc_hd/lib"
        if pdk_dir.is_dir():
            directory = pdk_dir
    libs = sorted(directory.glob("*.lib"))
    libs = [p for p in libs if not any(x in p.name.lower() for x in ("ccsnoise", "lvf"))]
    by_name = {p.stem.replace("sky130_fd_sc_hd__", "").replace("NangateOpenCellLibrary_", ""): p for p in libs}
    if names:
        missing = sorted(set(names) - set(by_name))
        if missing:
            raise FlowError("configured STA corners not found: " + ", ".join(missing))
        return [(name, by_name[name]) for name in names]
    if all_corners:
        return list(by_name.items())
    selected = [(name, by_name[name]) for name in values["corners"] if name in by_name]
    if values["corners"] and len(selected) != len(values["corners"]):
        missing = sorted(set(values["corners"]) - set(by_name))
        raise FlowError("default STA corner libraries missing: " + ", ".join(missing))
    return selected or [(p.stem, p) for p in [_find_liberty(values)]]


def _run_sta(values: dict, stage: str, all_corners: bool, corner_names: list[str]):
    results = values["build"] / "results" / values["platform"] / values["name"] / values["flow_variant"]
    stage_files = {
        "floorplan": ("2_floorplan.odb", "2_floorplan.sdc"),
        "place": ("3_place.odb", "3_place.sdc"),
        "cts": ("4_cts.odb", "4_cts.sdc"),
        "route": ("5_route.odb", "5_route.sdc"),
        "finish": ("6_final.odb", "6_final.sdc"),
    }
    if stage not in stage_files:
        stage = _latest_stage(results)
    odb_name, sdc_name = stage_files[stage]
    odb = results / odb_name
    sdc = results / sdc_name
    if not odb.is_file():
        raise FlowError(f"stage database not found: {odb}; run the stage first")
    if not sdc.is_file():
        candidates = sorted(results.glob("*.sdc"))
        candidates = [p for p in candidates if p.stem.split("_")[0].isdigit() and p.stem <= odb.stem]
        if not candidates:
            raise FlowError(f"no SDC found for {stage} under {results}")
        sdc = candidates[-1]
    corners = _corners(values, all_corners, corner_names)
    out_root = values["build"] / "reports" / "sta" / stage
    for name, liberty in corners:
        out = out_root / name
        out.mkdir(parents=True, exist_ok=True)
        env = _orfs_env(values)
        env.update({
            "STA_MODE": "corner", "STA_ODB": str(odb), "STA_SDC": str(sdc),
            "STA_LIBERTY": str(liberty), "STA_OUT": str(out), "STA_CORNER": name,
            "STA_RC_TCL": str(values["orfs"] / "platforms" / values["platform"] / "setRC.tcl"),
            "STA_MACRO_LIBS": "", "STA_PATHS": "20", "STAGE": odb.stem,
            "STA_STAGE_MAJOR": odb.stem.split("_")[0],
        })
        exe = shutil.which("openroad")
        if not exe:
            raise FlowError("openroad is not in PATH")
        script = Path(__file__).parent / "tcl" / "sta.tcl"
        code = subprocess.call([exe, "-no_splash", "-exit", str(script)], env=env)
        if code:
            return code
    return 0


def _latest_stage(results: Path):
    for stage, odb in [("finish", "6_final.odb"), ("route", "5_route.odb"),
                       ("cts", "4_cts.odb"), ("place", "3_place.odb"),
                       ("floorplan", "2_floorplan.odb")]:
        if (results / odb).is_file():
            return stage
    raise FlowError(f"no stage ODB found under {results}")


def _module_sta(values: dict, name: str):
    module = values["modules"].get(name)
    if not isinstance(module, dict):
        raise FlowError(f"module '{name}' is not defined in project modules")
    top = module.get("top", name)
    filelist = module.get("filelist")
    sources = []
    if filelist:
        list_path = Path(filelist)
        if not list_path.is_absolute():
            list_path = values["root"] / list_path
        if not list_path.is_file():
            raise FlowError(f"module filelist not found: {list_path}")
        for line in list_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("//"):
                path = Path(line)
                sources.append((list_path.parent / path if not path.is_absolute() else path).resolve())
    else:
        sources = [Path(f).resolve() if Path(f).is_absolute() else (values["root"] / f).resolve() for f in module.get("files", [])]
    if not sources or any(not path.is_file() for path in sources):
        missing = [str(path) for path in sources if not path.is_file()]
        raise FlowError("module RTL sources empty or missing: " + ", ".join(missing))
    liberty = _find_liberty(values, module.get("liberty"))
    out = values["build"] / "reports" / "modules" / name
    out.mkdir(parents=True, exist_ok=True)
    yosys = shutil.which("yosys")
    openroad = shutil.which("openroad")
    if not yosys or not openroad:
        raise FlowError("module-sta requires yosys and openroad in PATH")
    quote = shlex.quote
    includes = [Path(p).resolve() if Path(p).is_absolute() else (values["root"] / p).resolve()
                for p in module.get("include_dirs", values["include_dirs"])]
    defines = list(module.get("defines", values["defines"]))
    params = module.get("parameters", {})
    netlist = out / "netlist.v"
    ys = out / "synth.ys"
    lines = [f"read_liberty -lib {quote(str(liberty))}"]
    flags = " ".join([*(f"-I{quote(str(p))}" for p in includes),
                      *(f"-D{quote(str(d))}" for d in defines)])
    lines.append(f"read_verilog -sv {flags} {' '.join(quote(str(p)) for p in sources)}")
    for key, value in params.items():
        lines.append(f"chparam -set {key} {value} {top}")
    lines += [f"hierarchy -check -top {top}", f"synth -top {top}",
              f"dfflibmap -liberty {quote(str(liberty))}",
              f"abc -liberty {quote(str(liberty))}",
              f"tee -o {quote(str(out / 'area.rpt'))} stat -liberty {quote(str(liberty))}",
              f"write_verilog -noattr {quote(str(netlist))}"]
    ys.write_text("\n".join(lines) + "\n")
    log = out / "yosys.log"
    if subprocess.call([yosys, "-l", str(log), str(ys)]) != 0:
        raise FlowError(f"Yosys module synthesis failed; inspect {log}")
    constraint = module.get("constraints")
    sdc = ""
    if constraint:
        candidate = Path(constraint)
        candidate = candidate.resolve() if candidate.is_absolute() else (values["root"] / candidate).resolve()
        if not candidate.is_file():
            raise FlowError(f"module SDC not found: {candidate}")
        sdc = str(candidate)
    env = os.environ.copy()
    env.update({"MODULE_TOP": top, "MODULE_NETLIST": str(netlist), "MODULE_LIBERTY": str(liberty),
                "MODULE_SDC": sdc, "MODULE_OUT": str(out),
                "MODULE_PERIOD": str(module.get("clock_period", values["clock_period"]))})
    script = Path(__file__).parent / "tcl" / "module_sta.tcl"
    if subprocess.call([openroad, "-no_splash", "-exit", str(script)], env=env) != 0:
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
    sta.add_argument("--all", action="store_true")
    sta.add_argument("--corner", action="append", default=[])
    mod = sub.add_parser("module-sta")
    mod.add_argument("module")
    args = parser.parse_args(argv)
    try:
        if args.command == "project":
            print(select_project(args.path))
            return 0
        config, data = load_project(args.config) if args.config else (selected_project(), None)
        if data is None:
            config, data = load_project(config)
        values = project_values(config, data)
        values["orfs"] = discover_orfs(config, data)
        values["pdk"] = discover_pdk(config, data)
        if args.command == "validate":
            for key in ("root", "orfs", "pdk", "platform", "design_config", "build", "filelist"):
                print(f"{key}: {values[key]}")
            if not values["orfs"].is_dir(): raise FlowError(f"ORFS not found: {values['orfs']}")
            if not values["design_config"].is_file(): raise FlowError(f"design Makefile not found: {values['design_config']}")
            return 0
        if args.command == "run": return _run_make(values, args.stage)
        if args.command == "gui": return _run_make(values, "gui_" + ("finish" if args.stage == "final" else args.stage))
        if args.command == "sta": return _run_sta(values, args.stage, args.all, args.corner)
        if args.command == "module-sta": return _module_sta(values, args.module)
    except Exception as exc:
        print(f"orfsx: error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
