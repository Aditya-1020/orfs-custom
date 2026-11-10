"""Project selection and portable configuration loading."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from .core.paths import resolve_path
from .util import FlowError

PROJECT_ENV = "ORFSX_PROJECT"


def load_project(path: str | Path) -> tuple[Path, dict[str, Any]]:
    config = Path(os.path.expandvars(str(path))).expanduser().resolve()
    if not config.is_file():
        raise FlowError(f"project configuration not found: {config}")
    try:
        data = yaml.safe_load(config.read_text()) or {}
    except yaml.YAMLError as exc:
        raise FlowError(f"invalid YAML project configuration {config}: {exc}") from exc
    if not isinstance(data, dict):
        raise FlowError(f"project configuration must be a YAML mapping: {config}")
    return config, data


def selected_project() -> Path:
    explicit = os.environ.get(PROJECT_ENV)
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return path.resolve()
    state = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    marker = state / "orfsx" / "project"
    if marker.is_file():
        path = Path(marker.read_text().strip()).expanduser()
        if path.is_file():
            return path.resolve()
    raise FlowError("no project selected; run 'orfsx project /path/to/config.yaml' or pass -c")


def select_project(path: str | Path) -> Path:
    config, _ = load_project(path)
    state = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    marker = state / "orfsx" / "project"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(config) + "\n")
    return config


def project_values(config: Path, data: dict[str, Any]) -> dict[str, Any]:
    root = config.parent
    project = data.get("project", {})
    paths = data.get("paths", {})
    physical = data.get("physical", {})
    timing = data.get("timing", {})
    rtl = data.get("rtl", {})

    def resolve(key: str, default: str) -> Path:
        return resolve_path(paths.get(key, default), base=root)

    return {
        "root": root,
        "name": project.get("name", root.name),
        "top": project.get("top", "top"),
        "platform": project.get("platform", "sky130hd"),
        "orfs": resolve("orfs", ""),
        "pdk": resolve("pdk", "pdk"),
        "rtl": resolve("rtl", "rtl"),
        "constraints": resolve("constraints", "constraints"),
        "macros": resolve("macros", "macros"),
        "build": resolve("build", "build"),
        "filelist": resolve_path(rtl.get("filelist", "rtl/files.f"), base=root),
        "include_dirs": [resolve_path(p, base=root) for p in rtl.get("include_dirs", [])],
        "defines": list(rtl.get("defines", [])),
        "design_config": resolve_path(physical.get("design_config", "config/design.mk"), base=root),
        "run_tag": os.environ.get("RUN_TAG", physical.get("run_tag", "base")),
        "flow_variant": os.environ.get("FLOW_VARIANT", physical.get("flow_variant", "base")),
        "placement": os.environ.get("PLACEMENT", physical.get("placement", "rtlmp")),
        "liberty": timing.get("liberty", "auto"),
        "corners": list(timing.get("default_corners", [])),
        "jobs": int(os.environ.get("STA_JOBS", timing.get("jobs", 6))),
        "clock_period": float(timing.get("clock_period", 5.0)),
        "modules": data.get("modules", {}),
        "data": data,
    }


def discover_orfs(config: Path, data: dict[str, Any]) -> Path:
    explicit = os.environ.get("ORFS_FLOW") or data.get("paths", {}).get("orfs")
    candidates = []
    if explicit:
        candidates.append(Path(os.path.expandvars(str(explicit))).expanduser())
    home = Path.home()
    candidates.extend([
        home / "eda" / "orfs-nix" / "OpenROAD-flow-scripts" / "flow",
        home / "eda" / "OpenROAD-flow-scripts" / "flow",
        home / "OpenROAD-flow-scripts" / "flow",
        Path("/opt/OpenROAD-flow-scripts/flow"),
    ])
    for candidate in candidates:
        candidate = candidate.resolve()
        if (candidate / "Makefile").is_file() and (candidate / "platforms").is_dir():
            return candidate
    raise FlowError("ORFS flow directory not found; set paths.orfs in project YAML or ORFS_FLOW")


def discover_pdk(config: Path, data: dict[str, Any]) -> Path:
    explicit = os.environ.get("PDK_ROOT") or data.get("paths", {}).get("pdk")
    candidates = []
    if explicit:
        candidates.append(Path(os.path.expandvars(str(explicit))).expanduser())
    candidates.extend([Path.home() / "eda" / ".volare", Path.home() / ".volare", Path("/opt/pdk")])
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate.is_dir() and (candidate / "sky130A").is_dir():
            return candidate
    if candidates and candidates[0].is_dir():
        return candidates[0].resolve()
    raise FlowError("PDK root not found; set paths.pdk in project YAML or PDK_ROOT")
