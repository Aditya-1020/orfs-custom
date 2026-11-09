"""Centralized path layout for a project, ORFS run and platform."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class _FormatDefaults(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def resolve_path(
    value: str | Path,
    *,
    base: Path,
    values: Mapping[str, Any] | None = None,
) -> Path:
    """Expand environment variables and format tokens, then resolve a path."""
    fields = _FormatDefaults({key: str(item) for key, item in (values or {}).items()})
    raw = os.path.expanduser(os.path.expandvars(str(value))).format_map(fields)
    path = Path(raw)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


@dataclass(frozen=True)
class PathLayout:
    project_root: Path
    work_home: Path
    platform: str
    design: str
    flow_variant: str
    fakeram_tech: str
    run_tag: str
    settings: Mapping[str, Any]
    package_tcl: Path

    def _path(self, key: str, default: str) -> Path:
        values = {
            "platform": self.platform,
            "design": self.design,
            "flow_variant": self.flow_variant,
            "fakeram_tech": self.fakeram_tech,
            "run_tag": self.run_tag,
        }
        return resolve_path(self.settings.get(key, default), base=self.project_root, values=values)

    @property
    def results_dir(self) -> Path:
        return self.work_home / "results" / self.platform / self.design / self.flow_variant

    @property
    def logs_dir(self) -> Path:
        return self.work_home / "logs" / self.platform / self.design / self.flow_variant

    @property
    def reports_dir(self) -> Path:
        return self.work_home / "reports" / self.platform / self.design / self.flow_variant

    @property
    def analysis_dir(self) -> Path:
        return self.work_home / "analysis"

    @property
    def metrics_db(self) -> Path:
        return self._path("metrics_db", "build/analysis/metrics.db")

    @property
    def placements_static(self) -> Path:
        return self._path("placements_static", "design-configs/placements")

    @property
    def placements_generated(self) -> Path:
        return self.work_home / "placements"

    @property
    def rtl_dir(self) -> Path:
        return self._path("rtl_dir", "rtl")

    @property
    def constraints_dir(self) -> Path:
        return self._path("constraints_dir", "constraints")

    @property
    def fakeram_dir(self) -> Path:
        return self._path("fakeram_dir", "fakeram/{fakeram_tech}")

    @property
    def fakeram_config(self) -> Path:
        return self._path("fakeram_config", "fakeram/{fakeram_tech}_fakeram.cfg")

    @property
    def tcl_dir(self) -> Path:
        value = self.settings.get("tcl_dir")
        return resolve_path(value, base=self.project_root) if value else self.package_tcl

    def platform_dir(self, orfs_flow: Path) -> Path:
        return orfs_flow / "platforms" / self.platform
