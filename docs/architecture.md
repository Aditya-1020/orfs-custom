# Architecture

ASICFlow is a thin engineering layer over ORFS. ORFS remains responsible for synthesis and physical implementation. ASICFlow owns configuration resolution, command execution, analysis, reports, experiment tracking and extension discovery.

## Layers

- `core`: configuration parsing, path layout, errors and subprocess execution.
- `config.py`: resolves one immutable `RunContext` from YAML, environment variables, CLI overrides and literal ORFS Make exports.
- `sta.py` and `physical.py`: adapters for OpenROAD/OpenSTA operations.
- `analyze`: report interpretation and design analysis.
- `reports`: structured report models and serializers.
- `sweep` and `dataset`: experiment execution, storage and comparison.
- `plugins`: installed Python CLI commands and explicitly enabled Tcl scripts.

## Configuration precedence

For scalar run settings:

1. CLI arguments
2. Environment variables
3. Framework YAML defaults

The ORFS design Makefile is still authoritative for ORFS-specific exports that define its platform, design nickname and macro technology. The framework parses literal exported values; it does not execute or interpret arbitrary Make expressions.

## Extension rules

Python plugins register CLI subcommands through package entry points. Tcl extensions must be named explicitly in YAML or `ASICFLOW_TCL_EXTENSIONS`. The framework does not recursively discover files or execute every script in a directory.

The generated Tcl loader is available to standalone STA and is exposed to ORFS processes through `ASICFLOW_TCL_EXTENSION_LOADER`. A design's Make configuration can source that variable from the ORFS hook appropriate to its version or custom fork.
