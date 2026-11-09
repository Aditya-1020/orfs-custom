"""ASIC flow orchestration around OpenROAD-flow-scripts.

Public surface (scripts should import from here):

  from asicflow import RunContext, make_context
  from asicflow import generate_summary, generate_regression_report
  from asicflow import analyze_fanout, sweep_clock_period, RunDataset
  from asicflow.schema import SETUP_WNS, load_snapshot
"""

__version__ = "0.2.0"

from .config import RunContext, make_context, stage_stem
from .schema import (
    CATALOGUE,
    HEADLINE,
    MetricKey,
    MetricValue,
    RunSnapshot,
    compare_snapshots,
    load_snapshot,
)

# Analysis
from .analyze import (
    analyze_area_growth,
    analyze_buffers,
    analyze_congestion,
    analyze_fanout,
    analyze_macro_placement,
    analyze_wirelength,
    check_clock_domains,
    check_unconstrained_paths,
    compare_sta_runs,
)

# Reports
from .reports import (
    Report,
    generate_regression_report,
    generate_summary,
    path_group_report,
)

# Sweeps / ranking
from .sweep import (
    compare_implementations,
    find_best_configuration,
    sweep_buffering,
    sweep_clock_period,
    sweep_corners,
    sweep_liberty,
    sweep_placement_density,
    sweep_utilization,
)

# Dataset
from .dataset import RunDataset, plot_area_vs_frequency, plot_dataset

__all__ = [
    "CATALOGUE",
    "HEADLINE",
    "MetricKey",
    "MetricValue",
    "Report",
    "RunContext",
    "RunDataset",
    "RunSnapshot",
    "analyze_area_growth",
    "analyze_buffers",
    "analyze_congestion",
    "analyze_fanout",
    "analyze_macro_placement",
    "analyze_wirelength",
    "check_clock_domains",
    "check_unconstrained_paths",
    "compare_implementations",
    "compare_snapshots",
    "compare_sta_runs",
    "find_best_configuration",
    "generate_regression_report",
    "generate_summary",
    "load_snapshot",
    "make_context",
    "path_group_report",
    "plot_area_vs_frequency",
    "plot_dataset",
    "stage_stem",
    "sweep_buffering",
    "sweep_clock_period",
    "sweep_corners",
    "sweep_liberty",
    "sweep_placement_density",
    "sweep_utilization",
]
