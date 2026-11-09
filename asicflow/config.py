"""Immutable run context: platform, paths, and stage registry.

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

from .util import FlowError, log

PKG_DIR = Path(__file__).resolve().parent
ROOT = PKG_DIR.parent
TCL_DIR = PKG_DIR / "tcl"

# Single stage registry: ORFS target, ODB stem, netlist name, optional dump ODB.
STAGES: dict[str, dict[str, str | None]] = {
    "synth": {
        "orfs": "synth",
        "stem": "1_synth",
        "netlist": "1_2_yosys.v",
        "odb": None,
    },
    "floorplan": {
        "orfs": "floorplan",
        "stem": "2_floorplan",
        "netlist": "2_floorplan.odb.v",
        "odb": "2_floorplan.odb",
    },
    "place": {
        "orfs": "place",
        "stem": "3_place",
        "netlist": "3_place.odb.v",
        "odb": "3_place.odb",
    },
    "cts": {
        "orfs": "cts",
        "stem": "4_cts",
        "netlist": "4_cts.odb.v",
        "odb": "4_cts.odb",
    },
    "route": {
        "orfs": "route",
        "stem": "5_route",
        "netlist": "5_route.odb.v",
        "odb": "5_route.odb",
    },
    "finish": {
        "orfs": "finish",
        "stem": "6_final",
        "netlist": "6_final.v",
        "odb": "6_final.odb",
    },
}

# Accepted spellings -> canonical STAGES key.
STAGE_ALIASES = {"final": "finish"}

ORFS_STAGE_ORDER = ("synth", "floorplan", "place", "cts", "route", "finish")

# Default liberty basename under ORFS platforms/<PLATFORM>/lib/
DEFAULT_LIBERTY = {
    "sky130hd": "sky130_fd_sc_hd__tt_025C_1v80.lib",
    "nangate45": "NangateOpenCellLibrary_typical.lib",
}

PRE_STA_DEFINE = {
    "sky130hd": "OPENROAD_CLKGATE_SKY130",
    "nangate45": "OPENROAD_CLKGATE_NANGATE45",
}

MEM_MACROS = ("sram_sp_64x64", "sram_sp_64x256")
DEFAULT_MACROS = (
    "u_wbuf_mem.g_64x64.u_mem",
    "u_abuf_mem.g_64x64.u_mem",
    "u_rbuf_mem.g_256x64.u_mem",
)

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


def canon_stage(stage: str) -> str:
    """Map aliases (final) to the canonical STAGES key (finish)."""
    return STAGE_ALIASES.get(stage, stage)


def stage_stem(stage: str) -> str:
    stage = canon_stage(stage)
    if stage in STAGES:
        return str(STAGES[stage]["stem"])
    if stage.endswith(".odb"):
        return stage[:-4]
    return stage


def stage_major(stem: str) -> int:
    m = re.match(r"^(\d+)_", stem)
    return int(m.group(1)) if m else 0


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


def _stem_key(stem: str) -> tuple[int, int, str]:
    """ORFS-aware sort key so 3_place precedes 3_3_place_gp, and 4_* is later."""
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


def find_sdc(result_dir: Path, stem: str) -> Path | None:
    """Nearest SDC at or before stage stem. Never pick a later-stage SDC."""
    exact = result_dir / f"{stem}.sdc"
    if exact.is_file():
        return exact
    limit = _stem_key(stem)
    cands = [f for f in result_dir.glob("*.sdc") if _stem_key(f.stem) <= limit]
    if not cands:
        return None
    return max(cands, key=lambda f: _stem_key(f.stem))


def resolve_openroad(orfs_flow: Path | None = None) -> str:
    env = os.environ.get("OPENROAD")
    if env and Path(env).is_file():
        return env
    flow = orfs_flow or _env_path(
        "ORFS_FLOW",
        Path.home() / "eda/orfs-nix/OpenROAD-flow-scripts/flow",
    )
    try:
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

    @property
    def results_dir(self) -> Path:
        return (
            self.work_home
            / "results"
            / self.platform
            / self.design_nickname
            / self.flow_variant
        )

    @property
    def logs_dir(self) -> Path:
        return (
            self.work_home
            / "logs"
            / self.platform
            / self.design_nickname
            / self.flow_variant
        )

    @property
    def reports_dir(self) -> Path:
        return (
            self.work_home
            / "reports"
            / self.platform
            / self.design_nickname
            / self.flow_variant
        )

    @property
    def analysis_dir(self) -> Path:
        return self.work_home / "analysis"

    @property
    def metrics_db(self) -> Path:
        return self.root / "build" / "analysis" / "metrics.db"

    @property
    def placements_static(self) -> Path:
        return self.root / "design-configs" / "placements"

    @property
    def placements_generated(self) -> Path:
        return self.work_home / "placements"

    @property
    def rtl(self) -> Path:
        return self.root / "rtl"

    @property
    def constraints(self) -> Path:
        return self.root / "constraints" / f"constraints_{self.platform}.sdc"

    @property
    def rtl_filelist(self) -> Path:
        return self.rtl / "files.f"

    @property
    def fakeram_dir(self) -> Path:
        return self.root / "fakeram" / self.fakeram_tech

    @property
    def fakeram_cfg(self) -> Path:
        return self.root / "fakeram" / f"{self.fakeram_tech}_fakeram.cfg"

    @property
    def platform_dir(self) -> Path:
        return self.orfs_flow / "platforms" / self.platform

    @property
    def tcl_dir(self) -> Path:
        return TCL_DIR

    def default_liberty(self) -> Path:
        name = DEFAULT_LIBERTY.get(self.platform)
        if name:
            return self.platform_dir / "lib" / name
        libs = sorted(self.platform_dir.glob("lib/*.lib"))
        if not libs:
            raise FileNotFoundError(f"no liberty under {self.platform_dir}/lib")
        return libs[0]

    def pre_sta_define(self) -> str:
        return PRE_STA_DEFINE.get(self.platform, "OPENROAD_CLKGATE")

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
            "work_home": str(self.work_home),
        }

    def validate_config(self) -> None:
        if not self.design_config.is_file():
            raise FlowError(f"DESIGN_CONFIG not found: {self.design_config}")
        if not self.orfs_flow.is_dir():
            raise FlowError(f"ORFS_FLOW not found: {self.orfs_flow}")
        if not self.platform_dir.is_dir():
            raise FlowError(f"Platform dir missing: {self.platform_dir}")

    def validate_fakeram(self) -> None:
        missing = []
        for m in MEM_MACROS:
            lef = self.fakeram_dir / m / f"{m}.lef"
            lib = self.fakeram_dir / m / f"{m}.lib"
            bb = self.fakeram_dir / m / f"{m}.bb.v"
            if not lef.is_file() or not lib.is_file() or not bb.is_file():
                missing.append(m)
        if missing:
            raise FileNotFoundError(
                f"SRAM macros incomplete under {self.fakeram_dir}: {', '.join(missing)}. "
                f"Run: make fakeram PLATFORM={self.platform}"
            )


def make_context(
    platform: str = "sky130hd",
    run_tag: str = "base",
    flow_variant: str | None = None,
    placement: str = "rtlmp",
    design_config: str | Path | None = None,
    work_home: str | Path | None = None,
    design_nickname: str | None = None,
    fakeram_tech: str | None = None,
) -> RunContext:
    """Resolve one RunContext. Relative paths are from the repository root."""
    platform = platform.strip()
    # Accept historical aliases.
    if platform == "sky130":
        platform = "sky130hd"
    root = repo_root()
    orfs = _env_path(
        "ORFS_FLOW",
        Path.home() / "eda/orfs-nix/OpenROAD-flow-scripts/flow",
    )
    pdk = _env_path("PDK_ROOT", Path.home() / "eda/.volare")

    if design_config:
        cfg = Path(design_config)
        if not cfg.is_absolute():
            cfg = (root / cfg).resolve()
        else:
            cfg = cfg.resolve()
    else:
        cfg = (root / "design-configs" / f"{platform}.mk").resolve()

    exports = parse_mk_exports(cfg) if cfg.is_file() else {}
    nick = design_nickname or exports.get("DESIGN_NICKNAME") or "systolic_array"
    fr_tech = fakeram_tech or exports.get("FAKERAM_TECH") or platform
    plat = exports.get("PLATFORM") or platform

    fv = flow_variant if flow_variant not in (None, "") else "base"

    if work_home:
        wh = Path(work_home)
        if not wh.is_absolute():
            wh = (root / wh).resolve()
        else:
            wh = wh.resolve()
    else:
        wh = (root / "build" / "runs" / plat / nick / run_tag).resolve()

    return RunContext(
        root=root,
        orfs_flow=orfs,
        pdk_root=pdk,
        platform=plat,
        design_nickname=nick,
        fakeram_tech=fr_tech,
        run_tag=run_tag,
        flow_variant=fv,
        placement=placement or "rtlmp",
        design_config=cfg,
        work_home=wh,
    )


def ctx_from_args(args) -> RunContext:
    """The one place CLI args become a RunContext (flow + actions share this)."""
    return make_context(
        platform=getattr(args, "platform", "sky130hd"),
        run_tag=getattr(args, "run_tag", "base"),
        flow_variant=getattr(args, "flow_variant", None),
        placement=getattr(args, "placement", "rtlmp"),
        design_config=getattr(args, "design_config", None),
        work_home=getattr(args, "work_home", None),
    )
