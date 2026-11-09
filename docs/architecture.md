# Architecture

ASICFlow is an engineering layer over ORFS, not a replacement for it. ORFS owns synthesis and physical implementation; ASICFlow owns run context, tool invocation, analysis, reports, experiment tracking and extensions.

## Package boundaries

- `core/configuration.py`: YAML loading and recursive overrides.
- `core/paths.py`: deterministic path resolution and run directory layout.
- `core/stages.py`: ORFS stage metadata, stage aliases and stage output resolution.
- `core/runner.py`: subprocess boundary for EDA tools and helper programs.
- `config.py`: resolves one immutable `RunContext` from YAML, environment, CLI and literal ORFS Make exports.
- `sta.py`, `physical.py` and `metrics.py`: OpenROAD/OpenSTA integration and metric collection.
- `analyze`, `reports`, `sweep` and `dataset`: analysis and experiment logic.
- `plugins`: installed Python CLI commands and explicitly enabled Tcl extensions.

## Configuration ownership

Built-in package YAML provides platform defaults. Project YAML overlays framework settings. The ORFS design Makefile remains authoritative for ORFS-specific variables. The framework does not evaluate arbitrary Makefile logic.

## Extension rules

Python plugins register CLI subcommands through the `asicflow.plugins` package entry-point group. Tcl extensions must be explicitly listed in YAML or `ASICFLOW_TCL_EXTENSIONS`. The framework never recursively scans a directory and sources all scripts.

The generated Tcl loader is loaded by standalone STA and exposed to ORFS processes as `ASICFLOW_TCL_EXTENSION_LOADER`. A design Makefile must source that loader from a hook supported by its ORFS version or custom fork.
