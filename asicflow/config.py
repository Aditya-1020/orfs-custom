"""Resolve ORFS configuration and expose one immutable run context.

PLATFORM is the authoritative technology identifier. Design-config .mk files
own DESIGN_NICKNAME and FAKERAM_TECH. Python consumes the resolved context;
it does not maintain a parallel technology database.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .core.configuration import deep_merge, load_configuration, load_yaml
from .core.stages import (
    ORFS_STAGE_ORDER,
    STAGES,
    canon_stage,
    find_sdc,
    stage_major,
    stage_stem,
)
from .core.paths import PathLayout, resolve_path
from .plugins.tcl import resolve_tcl_extensions, write_tcl_loader
from .util import FlowError, log

PKG_DIR = Path(__file__).resolve().parent
ROOT = PKG_DIR.parent
TCL_DIR = PKG_DIR / "tcl"

_EXPORT_RE = re.compile(
    r"^export\s+(\w+)\s*(?:\?|:)?=\s*(.*?)\s*$",
    re.MULTILINE,
)
_INCLUDE_RE = re.compile(
    r"^include\s+\$\(dir\s+\$\(abspath\s+\$\(lastword\s+\$\(MAKEFILE_LIST\)\)\)\)\)/(\S+)",
    re.MULTILINE,
)
_INCLUDE_SIMPLE = re.compile(r"^include\s+(\S+)", re.MULTILINE)


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser().resolve() if raw else default.resolve()


def repo_root() -> Path:
    return _env_path("ROOT_DIR", ROOT)


def parse_mk_exports(path: Path, *, _seen: set[Path] | None = None) -> dict[str, str]:
    """Collect `export VAR = value` from a design config and its local includes."""
    path = path.resolve()
    seen = _seen if _seen is not None else set()
    if path in seen or not path.is_file():
        return {}
    seen.add(path)
    text = path.read_text()
    out: dict[str, str] = {}
    for m in _EXPORT_RE.finditer(text):
        val = m.group(2).strip()
        # Skip Make expansions we cannot resolve here.
        if "$(" in val or "${" in val:
            continue
        out[m.group(1)] = val
    # Follow simple relative includes next to this file.
    for m in _INCLUDE_RE.finditer(text):
        inc = (path.parent / m.group(1)).resolve()
        for k, v in parse_mk_exports(inc, _seen=seen).items():
            out.setdefault(k, v)
    for m in _INCLUDE_SIMPLE.finditer(text):
        raw = m.group(1)
        if "$(" in raw:
            continue
        inc = Path(raw)
        if not inc.is_absolute():
            inc = path.parent / inc
        for k, v in parse_mk_exports(inc.resolve(), _seen=seen).items():
            out.setdefault(k, v)
    return out


def resolve_openroad(orfs_flow: Path | None = None) -> str:
    env = os.environ.get("OPENROAD")
    if env and Path(env).is_file():
        return env
    flow = orfs_flow
    if flow is None:
        configured = os.environ.get("ORFS_FLOW")
        flow = Path(configured).expanduser().resolve() if configured else None
    try:
        if flow is None:
            raise FileNotFoundError("ORFS flow path is not configured")
        out = subprocess.check_output(
            ["make", "--no-print-directory", "-C", str(flow), "print-OPENROAD_EXE"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        for line in out.splitlines():
            if "OPENROAD_EXE" in line:
                exe = line.split(":", 1)[-1].split("=", 1)[-1].strip()
            elif line.startswith("/") and "openroad" in line:
                exe = line.strip()
            else:
                continue
            if Path(exe).is_file():
                return exe
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        pass
    return shutil.which("openroad") or "openroad"


@dataclass(frozen=True)
class RunContext:
    """One immutable configuration/run context per invocation."""

    root: Path
    orfs_flow: Path
    pdk_root: Path
    platform: str
    design_nickname: str
    fakeram_tech: str
    run_tag: str
    flow_variant: str
    placement: str
    design_config: Path
    work_home: Path

    config_dir: Path
    flow_settings: dict[str, Any]
    path_settings: dict[str, Any]
    platform_settings: dict[str, Any]
    design_settings: dict[str, Any]

    @property
    def path_layout(self) -> PathLayout:
        return PathLayout(
            project_root=self.root,
            work_home=self.work_home,
            platform=self.platform,
            design=self.design_nickname,
            flow_variant=self.flow_variant,
            fakeram_tech=self.fakeram_tech,
            run_tag=self.run_tag,
            settings=self.path_settings,
            package_tcl=TCL_DIR,
        )

    @property
    def results_dir(self) -> Path:
        return self.path_layout.results_dir

    @property
    def logs_dir(self) -> Path:
        return self.path_layout.logs_dir

    @property
    def reports_dir(self) -> Path:
        return self.path_layout.reports_dir

    @property
    def analysis_dir(self) -> Path:
        return self.path_layout.analysis_dir

    @property
    def metrics_db(self) -> Path:
        return self.path_layout.metrics_db

    @property
    def placements_static(self) -> Path:
        return self.path_layout.placements_static

    @property
    def placements_generated(self) -> Path:
        return self.path_layout.placements_generated

    @property
    def rtl(self) -> Path:
        return self.path_layout.rtl_dir

    @property
    def constraints(self) -> Path:
        configured = self.design_settings.get("constraints", {}).get("default")
        if configured:
            return self.resolve_profile_path(configured)
        return self.path_layout.constraints_dir / f"constraints_{self.platform}.sdc"

    @property
    def rtl_filelist(self) -> Path:
        rtl_settings = self.design_settings.get("rtl", {})
        rtl_root = self.resolve_profile_path(rtl_settings.get("root", "rtl"))
        filelist = Path(rtl_settings.get("filelist", "files.f")).expanduser()
        return filelist.resolve() if filelist.is_absolute() else (rtl_root / filelist).resolve()

    @property
    def fakeram_dir(self) -> Path:
        return self.path_layout.fakeram_dir

    @property
    def fakeram_cfg(self) -> Path:
        return self.path_layout.fakeram_config

    @property
    def platform_dir(self) -> Path:
        return self.path_layout.platform_dir(self.orfs_flow)

    @property
    def tcl_dir(self) -> Path:
        return self.path_layout.tcl_dir

    @property
    def macro_instances(self) -> tuple[str, ...]:
        return tuple(
            str(item["instance"])
            for item in self.design_settings.get("macros", [])
            if isinstance(item, dict) and item.get("instance")
        )

    def resolve_profile_path(self, value: str | Path) -> Path:
        return resolve_path(
            value,
            base=self.root,
            values={
                "platform": self.platform,
                "design": self.design_nickname,
                "fakeram_tech": self.fakeram_tech,
                "run_tag": self.run_tag,
                "flow_variant": self.flow_variant,
            },
        )

    def macro_liberty_files(self) -> list[Path]:
        paths: list[Path] = []
        for item in self.design_settings.get("macros", []):
            if not isinstance(item, dict):
                continue
            value = item.get("liberty")
            if value:
                path = self.resolve_profile_path(value)
            elif item.get("model"):
                model = str(item["model"])
                path = self.fakeram_dir / model / f"{model}.lib"
            else:
                continue
            if path not in paths:
                paths.append(path)
        return paths

    def default_liberty(self) -> Path:
        value = self.platform_settings.get("timing", {}).get("default_liberty")
        if value:
            path = Path(value).expanduser()
            return path.resolve() if path.is_absolute() else (self.platform_dir / path).resolve()
        libs = sorted(self.platform_dir.glob("lib/*.lib"))
        if len(libs) == 1:
            return libs[0]
        raise FlowError(
            f"set timing.default_liberty in "
            f"{self.config_dir / 'platforms' / (self.platform + '.yaml')}; "
            f"found {len(libs)} Liberty files under {self.platform_dir / 'lib'}"
        )

    def pre_sta_define(self) -> str:
        return str(self.platform_settings.get("defines", {}).get("clock_gating", ""))

    def tcl_extension_loader(self) -> Path | None:
        extensions = resolve_tcl_extensions(
            project_root=self.root,
            flow_settings=self.flow_settings,
            design_settings=self.design_settings,
        )
        return write_tcl_loader(self.analysis_dir / "tcl_extensions.tcl", extensions)

    def netlist_paths(self, stage: str) -> tuple[Path, Path | None]:
        stage = canon_stage(stage)
        if stage not in STAGES:
            raise FlowError(f"Unsupported STAGE={stage!r}")
        info = STAGES[stage]
        odb = self.results_dir / info["odb"] if info["odb"] else None
        return self.results_dir / str(info["netlist"]), odb

    def stage_odbs(self) -> list[tuple[str, Path, int]]:
        """(stem, odb, mtime_ns) for every stage ODB present, in flow order."""
        out = []
        for name in ORFS_STAGE_ORDER:
            stem = str(STAGES[name]["stem"])
            odb = self.results_dir / f"{stem}.odb"
            if odb.is_file():
                out.append((stem, odb, odb.stat().st_mtime_ns))
        return out

    def latest_stem(self) -> str:
        """Stem of the most recently written stage ODB (mtime, not furthest stage).

        ORFS never deletes later-stage outputs, so an old 6_final.odb can sit next
        to a fresh 3_place.odb. 'latest' must follow what was built last.
        """
        chain = self.stage_odbs()
        if not chain:
            raise FlowError(
                f"no stage ODB under {self.results_dir} — run make synth|place|… first"
            )
        return max(chain, key=lambda t: (t[2], stage_major(t[0])))[0]

    def resolve_stage(
        self, stage: str = "latest", *, allow_stale: bool | None = None
    ) -> tuple[str, Path, Path]:
        """Return (stem, odb, sdc) for STA/analysis. Refuses stale explicit stages."""
        chain = self.stage_odbs()
        if not chain:
            raise FlowError(
                f"no stage ODB under {self.results_dir} — run make synth|place|… first"
            )
        if stage == "latest":
            stem = self.latest_stem()
            odb = self.results_dir / f"{stem}.odb"
        else:
            stem = stage_stem(stage)
            odb = self.results_dir / f"{stem}.odb"
            if not odb.is_file():
                have = ", ".join(s for s, _, _ in chain)
                raise FlowError(f"missing {odb.name}; available: {have} (or STAGE=latest)")
            if allow_stale is None:
                allow_stale = os.environ.get("STA_ALLOW_STALE") == "1"
            mt = odb.stat().st_mtime_ns
            newer = [s for s, _, m in chain if stage_major(s) < stage_major(stem) and m > mt]
            if newer and not allow_stale:
                raise FlowError(
                    f"{odb.name} is STALE: earlier stage(s) {newer} were rebuilt after it. "
                    f"Re-run the flow, or set STA_ALLOW_STALE=1."
                )
        sdc = find_sdc(self.results_dir, stem)
        if sdc is None:
            raise FlowError(f"no SDC at or before {stem} in {self.results_dir}")
        age_min = (datetime.now().timestamp() - odb.stat().st_mtime) / 60
        log(f"stage input: {odb} (modified {age_min:.0f} min ago)")
        return stem, odb, sdc

    def odb_path(self, stage: str) -> Path:
        return self.results_dir / f"{stage_stem(stage)}.odb"

    def sdc_path(self, stage: str) -> Path | None:
        return find_sdc(self.results_dir, stage_stem(stage))

    def resolve_placement_tcl(self) -> Path | None:
        """Single owner for MACRO_PLACEMENT_TCL resolution."""
        name = self.placement
        if not name or name == "rtlmp":
            return None
        for candidate in (
            self.placements_static / f"{name}.tcl",
            self.placements_generated / f"{name}.tcl",
        ):
            if candidate.is_file():
                return candidate
        return None

    def orfs_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["DESIGN_CONFIG"] = str(self.design_config)
        env["WORK_HOME"] = str(self.work_home)
        env["FLOW_VARIANT"] = self.flow_variant
        env["ROOT_DIR"] = str(self.root)
        env["ORFS_FLOW"] = str(self.orfs_flow)
        env["PDK_ROOT"] = str(self.pdk_root)
        env["PLATFORM"] = self.platform
        env["RUN_TAG"] = self.run_tag
        if self.placement and self.placement != "rtlmp":
            env["PLACEMENT"] = self.placement
            tcl = self.resolve_placement_tcl()
            if tcl is None:
                raise FileNotFoundError(
                    f"MACRO_PLACEMENT_TCL not found for PLACEMENT={self.placement!r} "
                    f"(checked {self.placements_static} and {self.placements_generated})"
                )
            env["MACRO_PLACEMENT_TCL"] = str(tcl)
        else:
            env["PLACEMENT"] = "rtlmp"
            env.pop("MACRO_PLACEMENT_TCL", None)

        env["ASICFLOW_CONFIG_DIR"] = str(self.config_dir)
        extension_loader = self.tcl_extension_loader()
        if extension_loader:
            env["ASICFLOW_TCL_EXTENSION_LOADER"] = str(extension_loader)
        else:
            env.pop("ASICFLOW_TCL_EXTENSION_LOADER", None)
        return env

    def snapshot(self) -> dict[str, str]:
        return {
            "platform": self.platform,
            "design_nickname": self.design_nickname,
            "fakeram_tech": self.fakeram_tech,
            "run_tag": self.run_tag,
            "flow_variant": self.flow_variant,
            "placement": self.placement,
            "design_config": str(self.design_config),
            "config_dir": str(self.config_dir),
            "work_home": str(self.work_home),
        }

    def validate_config(self) -> None:
        if not self.design_config.is_file():
            raise FlowError(f"DESIGN_CONFIG not found: {self.design_config}")
        if not self.orfs_flow.is_dir():
            raise FlowError(f"ORFS_FLOW not found: {self.orfs_flow}")
        if not self.platform_dir.is_dir():
            raise FlowError(f"Platform dir missing: {self.platform_dir}")
        liberty = self.default_liberty()
        if not liberty.is_file():
            raise FlowError(f"default Liberty file not found: {liberty}")

    def validate_fakeram(self) -> None:
        missing: list[str] = []
        for item in self.design_settings.get("macros", []):
            if not isinstance(item, dict):
                raise FlowError("each design macro entry must be a mapping")
            model = item.get("model")
            defaults = {
                "lef": self.fakeram_dir / str(model) / f"{model}.lef" if model else None,
                "liberty": self.fakeram_dir / str(model) / f"{model}.lib" if model else None,
                "blackbox": self.fakeram_dir / str(model) / f"{model}.bb.v" if model else None,
            }
            if not model and not all(item.get(key) for key in defaults):
                raise FlowError(
                    f"macro {item.get('instance', '<unnamed>')} needs a model or explicit LEF, Liberty and blackbox paths"
                )
            for key, fallback in defaults.items():
                configured = item.get(key)
                candidate = self.resolve_profile_path(configured) if configured else fallback
                if candidate is None or not candidate.is_file():
                    missing.append(str(candidate))
        if missing:
            raise FlowError("macro collateral is incomplete:\n  " + "\n  ".join(missing))

def make_context(
    platform: str | None = None,
    run_tag: str | None = None,
    flow_variant: str | None = None,
    placement: str | None = None,
    design_config: str | Path | None = None,
    work_home: str | Path | None = None,
    design_nickname: str | None = None,
    fakeram_tech: str | None = None,
    config_dir: str | Path | None = None,
    design_profile: str | Path | None = None,
) -> RunContext:
    """Resolve framework YAML, ORFS Make config and CLI/environment overrides."""
    root = repo_root()
    cfg_root_value = config_dir or os.environ.get("ASICFLOW_CONFIG_DIR") or root / "config"
    cfg_root = Path(os.path.expandvars(str(cfg_root_value))).expanduser()
    config_root = cfg_root.resolve() if cfg_root.is_absolute() else (root / cfg_root).resolve()

    builtin_config = PKG_DIR / "configs"
    flow_settings = deep_merge(
        load_yaml(builtin_config / "flow.yaml"),
        load_yaml(config_root / "flow.yaml"),
    )
    defaults = flow_settings.get("defaults", {})
    path_settings = flow_settings.get("paths", {})

    selected_platform = (
        platform
        or os.environ.get("PLATFORM")
        or os.environ.get("TECH")
        or defaults.get("platform")
    )
    if not selected_platform:
        raise FlowError("platform is unset; pass --platform or configure defaults.platform")
    selected_platform = str(selected_platform).strip()
    if selected_platform == "sky130":
        selected_platform = "sky130hd"

    raw_design_config = design_config or os.environ.get("DESIGN_CONFIG")
    cfg = Path(raw_design_config).expanduser() if raw_design_config else (
        root / "design-configs" / f"{selected_platform}.mk"
    )
    cfg = cfg.resolve() if cfg.is_absolute() else (root / cfg).resolve()
    exports = parse_mk_exports(cfg) if cfg.is_file() else {}

    selected_platform = exports.get("PLATFORM") or selected_platform
    nickname = design_nickname or exports.get("DESIGN_NICKNAME") or root.name
    macro_tech = fakeram_tech or exports.get("FAKERAM_TECH") or selected_platform

    profile_value = design_profile or os.environ.get("ASICFLOW_DESIGN_PROFILE")
    profile_path = None
    if profile_value:
        profile_path = Path(os.path.expandvars(str(profile_value))).expanduser()
        if not profile_path.is_absolute():
            profile_path = (root / profile_path).resolve()

    flow_settings, platform_settings, design_settings = load_configuration(
        config_root,
        platform=selected_platform,
        design_name=nickname,
        design_profile=profile_path,
        defaults_dir=builtin_config,
    )
    defaults = flow_settings.get("defaults", {})
    path_settings = flow_settings.get("paths", {})

    selected_run_tag = run_tag or os.environ.get("RUN_TAG") or defaults.get("run_tag", "base")
    selected_variant = (
        flow_variant
        or os.environ.get("FLOW_VARIANT")
        or defaults.get("flow_variant", "base")
    )
    values = {
        "platform": selected_platform,
        "design": nickname,
        "run_tag": selected_run_tag,
        "flow_variant": selected_variant,
        "fakeram_tech": macro_tech,
    }

    orfs_value = os.environ.get("ORFS_FLOW") or path_settings.get("orfs_flow")
    if not orfs_value:
        raise FlowError(
            "ORFS_FLOW is unset; set it in the environment or paths.orfs_flow in config/flow.yaml"
        )
    orfs = resolve_path(orfs_value, base=root, values=values)

    pdk_value = os.environ.get("PDK_ROOT") or path_settings.get("pdk_root") or "pdk"
    pdk = resolve_path(pdk_value, base=root, values=values)

    work_value = (
        work_home
        or os.environ.get("WORK_HOME")
        or path_settings.get("work_home", "build/runs/{platform}/{design}/{run_tag}")
    )
    wh = resolve_path(work_value, base=root, values=values)

    return RunContext(
        root=root,
        orfs_flow=orfs,
        pdk_root=pdk,
        platform=str(selected_platform),
        design_nickname=str(nickname),
        fakeram_tech=str(macro_tech),
        run_tag=str(selected_run_tag),
        flow_variant=str(selected_variant),
        placement=placement or os.environ.get("PLACEMENT", "rtlmp"),
        design_config=cfg,
        work_home=wh,
        config_dir=config_root,
        flow_settings=flow_settings,
        path_settings=path_settings,
        platform_settings=platform_settings,
        design_settings=design_settings,
    )


def ctx_from_args(args) -> RunContext:
    """Convert CLI arguments to one immutable run context."""
    return make_context(
        platform=getattr(args, "platform", None),
        run_tag=getattr(args, "run_tag", None),
        flow_variant=getattr(args, "flow_variant", None),
        placement=getattr(args, "placement", "rtlmp"),
        design_config=getattr(args, "design_config", None),
        work_home=getattr(args, "work_home", None),
        config_dir=getattr(args, "config_dir", None),
        design_profile=getattr(args, "design_profile", None),
    )
