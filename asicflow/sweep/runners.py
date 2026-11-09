"""Parameter sweeps that orchestrate ORFS / STA variants and collect metrics."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import RunContext, make_context
from ..reports.base import Report
from ..schema import HEADLINE, load_snapshot


@dataclass
class SweepPoint:
    name: str
    params: dict[str, Any]
    run_tag: str
    status: str = "planned"  # planned | ran | skipped | failed
    metrics: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "params": self.params,
            "run_tag": self.run_tag,
            "status": self.status,
            "metrics": self.metrics,
            "notes": self.notes,
        }


@dataclass
class SweepResult:
    name: str
    points: list[SweepPoint]
    meta: dict[str, Any] = field(default_factory=dict)

    def to_report(self) -> Report:
        rpt = Report(name=self.name, title=f"Sweep: {self.name}", meta=self.meta)
        headers = ["name", "run_tag", "status"] + [m.key for m in HEADLINE[:8]] + ["notes"]
        rows = []
        for p in self.points:
            rows.append(
                [p.name, p.run_tag, p.status]
                + [p.metrics.get(m.key) for m in HEADLINE[:8]]
                + [p.notes]
            )
        rpt.add_table("points", headers, rows)
        rpt.sections["points"] = [p.to_dict() for p in self.points]
        return rpt


def _tag(prefix: str, label: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
    return f"{prefix}_{safe}"[:80]


def _ctx_for(base: RunContext, run_tag: str, **overrides: Any) -> RunContext:
    return make_context(
        platform=base.platform,
        run_tag=run_tag,
        flow_variant=overrides.get("flow_variant", base.flow_variant),
        placement=overrides.get("placement", base.placement),
        design_config=overrides.get("design_config", base.design_config),
        work_home=overrides.get("work_home"),
        design_nickname=base.design_nickname,
        fakeram_tech=base.fakeram_tech,
    )


def _collect_metrics(ctx: RunContext, stage: str, corner: str | None = None) -> dict[str, Any]:
    snap = load_snapshot(
        ctx.metrics_db,
        platform=ctx.platform,
        run_tag=ctx.run_tag,
        stage=stage,
        corner=corner,
    )
    return snap.headline()


def _run_or_plan(
    *,
    ctx: RunContext,
    stage: str,
    execute: bool,
    runner: Callable[[RunContext, str], int] | None,
    env_extra: dict[str, str] | None = None,
) -> tuple[str, str]:
    if not execute:
        return "planned", "dry-run"
    if runner is None:
        return "skipped", "no runner"
    old = {}
    try:
        if env_extra:
            for k, v in env_extra.items():
                old[k] = os.environ.get(k)
                os.environ[k] = str(v)
        code = runner(ctx, stage)
        return ("ran" if code == 0 else "failed"), f"exit={code}"
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def sweep_clock_period(
    ctx: RunContext,
    periods_ns: list[float],
    *,
    stage: str = "finish",
    execute: bool = False,
    runner: Callable[[RunContext, str], int] | None = None,
    out_dir: Path | str | None = None,
) -> SweepResult:
    """Sweep target clock periods and evaluate timing closure."""
    result = SweepResult(
        name="sweep_clock_period",
        points=[],
        meta={"periods_ns": periods_ns, "execute": execute, "stage": stage},
    )
    for period in periods_ns:
        tag = _tag(ctx.run_tag, f"clk{period}ns")
        point = SweepPoint(
            name=f"period_{period}",
            params={"clock_period_ns": period},
            run_tag=tag,
        )
        child = _ctx_for(ctx, tag)
        status, notes = _run_or_plan(
            ctx=child,
            stage=stage,
            execute=execute,
            runner=runner,
            env_extra={"CLOCK_PERIOD": str(period), "ABC_CLOCK_PERIOD_IN_PS": str(period * 1000)},
        )
        point.status = status
        point.notes = notes
        if status == "ran":
            point.metrics = _collect_metrics(child, "6_final" if stage == "finish" else stage)
        result.points.append(point)
    if out_dir:
        result.to_report().write(out_dir, basename="sweep_clock_period")
    return result


def sweep_utilization(
    ctx: RunContext,
    utils: list[float],
    *,
    stage: str = "finish",
    execute: bool = False,
    runner: Callable[[RunContext, str], int] | None = None,
    out_dir: Path | str | None = None,
) -> SweepResult:
    result = SweepResult(
        name="sweep_utilization",
        points=[],
        meta={"utilizations": utils, "execute": execute},
    )
    for util in utils:
        tag = _tag(ctx.run_tag, f"util{util}")
        child = _ctx_for(ctx, tag)
        point = SweepPoint(name=f"util_{util}", params={"core_utilization": util}, run_tag=tag)
        status, notes = _run_or_plan(
            ctx=child,
            stage=stage,
            execute=execute,
            runner=runner,
            env_extra={"CORE_UTILIZATION": str(util)},
        )
        point.status, point.notes = status, notes
        if status == "ran":
            point.metrics = _collect_metrics(child, "6_final" if stage == "finish" else stage)
        result.points.append(point)
    if out_dir:
        result.to_report().write(out_dir, basename="sweep_utilization")
    return result


def sweep_buffering(
    ctx: RunContext,
    fanout_limits: list[int],
    *,
    stage: str = "place",
    execute: bool = False,
    runner: Callable[[RunContext, str], int] | None = None,
    out_dir: Path | str | None = None,
) -> SweepResult:
    result = SweepResult(
        name="sweep_buffering",
        points=[],
        meta={"fanout_limits": fanout_limits, "execute": execute},
    )
    for fo in fanout_limits:
        tag = _tag(ctx.run_tag, f"fo{fo}")
        child = _ctx_for(ctx, tag)
        point = SweepPoint(name=f"fanout_{fo}", params={"max_fanout": fo}, run_tag=tag)
        status, notes = _run_or_plan(
            ctx=child,
            stage=stage,
            execute=execute,
            runner=runner,
            env_extra={"MAX_FANOUT": str(fo), "SYNTH_MAX_FANOUT": str(fo)},
        )
        point.status, point.notes = status, notes
        if status == "ran":
            point.metrics = _collect_metrics(child, stage if stage != "finish" else "6_final")
        result.points.append(point)
    if out_dir:
        result.to_report().write(out_dir, basename="sweep_buffering")
    return result


def sweep_placement_density(
    ctx: RunContext,
    densities: list[float],
    *,
    stage: str = "place",
    execute: bool = False,
    runner: Callable[[RunContext, str], int] | None = None,
    out_dir: Path | str | None = None,
) -> SweepResult:
    result = SweepResult(
        name="sweep_placement_density",
        points=[],
        meta={"densities": densities, "execute": execute},
    )
    for dens in densities:
        tag = _tag(ctx.run_tag, f"dens{dens}")
        child = _ctx_for(ctx, tag)
        point = SweepPoint(name=f"density_{dens}", params={"place_density": dens}, run_tag=tag)
        status, notes = _run_or_plan(
            ctx=child,
            stage=stage,
            execute=execute,
            runner=runner,
            env_extra={"PLACE_DENSITY": str(dens)},
        )
        point.status, point.notes = status, notes
        if status == "ran":
            point.metrics = _collect_metrics(child, "3_place")
        result.points.append(point)
    if out_dir:
        result.to_report().write(out_dir, basename="sweep_placement_density")
    return result


def sweep_corners(
    ctx: RunContext,
    *,
    stage: str = "latest",
    all_corners: bool = True,
    paths: int = 20,
    execute: bool = True,
    sta_runner: Callable[..., int] | None = None,
    out_dir: Path | str | None = None,
) -> SweepResult:
    """Run STA across multiple PVT corners (delegates to sta-pvt)."""
    result = SweepResult(
        name="sweep_corners",
        points=[],
        meta={"stage": stage, "all_corners": all_corners, "execute": execute},
    )
    point = SweepPoint(
        name="pvt",
        params={"all_corners": all_corners, "paths": paths},
        run_tag=ctx.run_tag,
    )
    if execute and sta_runner is not None:
        code = sta_runner(ctx, stage=stage, all_corners=all_corners, paths=paths)
        point.status = "ran" if code == 0 else "failed"
        point.notes = f"exit={code}"
        sta_dir = ctx.analysis_dir / "sta"
        if sta_dir.is_dir():
            corners = []
            for mf in sorted(sta_dir.glob("*/*/metrics.json")):
                data = json.loads(mf.read_text())
                corners.append(data)
            point.metrics = {"corners": len(corners)}
            result.meta["corner_metrics"] = corners
    else:
        point.status = "planned"
        point.notes = "dry-run" if not execute else "no sta_runner"
    result.points.append(point)
    if out_dir:
        result.to_report().write(out_dir, basename="sweep_corners")
    return result


def sweep_liberty(
    ctx: RunContext,
    liberty_paths: list[Path | str],
    *,
    stage: str = "latest",
    out_dir: Path | str | None = None,
) -> SweepResult:
    """Compare timing across explicit Liberty libraries (plan + artifact index)."""
    result = SweepResult(
        name="sweep_liberty",
        points=[],
        meta={"libs": [str(p) for p in liberty_paths], "stage": stage},
    )
    for lib in liberty_paths:
        lib = Path(lib)
        tag = _tag(ctx.run_tag, lib.stem)
        point = SweepPoint(
            name=lib.stem,
            params={"liberty": str(lib)},
            run_tag=tag,
            status="planned" if lib.is_file() else "failed",
            notes="" if lib.is_file() else "missing liberty",
        )
        result.points.append(point)
    if out_dir:
        result.to_report().write(out_dir, basename="sweep_liberty")
    return result
