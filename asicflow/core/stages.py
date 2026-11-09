"""ORFS stage metadata and output resolution."""

from __future__ import annotations

import re
from pathlib import Path

STAGES: dict[str, dict[str, str | None]] = {
    "synth": {"orfs": "synth", "stem": "1_synth", "netlist": "1_2_yosys.v", "odb": None},
    "floorplan": {
        "orfs": "floorplan",
        "stem": "2_floorplan",
        "netlist": "2_floorplan.odb.v",
        "odb": "2_floorplan.odb",
    },
    "place": {"orfs": "place", "stem": "3_place", "netlist": "3_place.odb.v", "odb": "3_place.odb"},
    "cts": {"orfs": "cts", "stem": "4_cts", "netlist": "4_cts.odb.v", "odb": "4_cts.odb"},
    "route": {"orfs": "route", "stem": "5_route", "netlist": "5_route.odb.v", "odb": "5_route.odb"},
    "finish": {"orfs": "finish", "stem": "6_final", "netlist": "6_final.v", "odb": "6_final.odb"},
}

STAGE_ALIASES = {"final": "finish"}
ORFS_STAGE_ORDER = tuple(STAGES)


def canon_stage(stage: str) -> str:
    return STAGE_ALIASES.get(stage, stage)


def stage_stem(stage: str) -> str:
    stage = canon_stage(stage)
    if stage in STAGES:
        return str(STAGES[stage]["stem"])
    if stage.endswith(".odb"):
        return stage[:-4]
    return stage


def stage_major(stem: str) -> int:
    match = re.match(r"^(\d+)_", stem)
    return int(match.group(1)) if match else 0


def _stem_key(stem: str) -> tuple[int, int, str]:
    parts = stem.split("_", 2)
    try:
        major = int(parts[0])
    except (ValueError, IndexError):
        return 999, 999, stem
    try:
        minor = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        minor = 0
    return major, minor, stem


def find_sdc(result_dir: Path, stem: str) -> Path | None:
    exact = result_dir / f"{stem}.sdc"
    if exact.is_file():
        return exact
    candidates = [
        path for path in result_dir.glob("*.sdc")
        if _stem_key(path.stem) <= _stem_key(stem)
    ]
    return max(candidates, key=lambda path: _stem_key(path.stem), default=None)
