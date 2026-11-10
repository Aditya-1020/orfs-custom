# ORFS Custom

A project-oriented command layer for OpenROAD-flow-scripts (ORFS). ORFS remains the implementation engine; this repository supplies project configuration, path resolution, stage GUI, multi-corner STA, and standalone RTL module STA.

## Quick start

Install once:

```bash
git clone https://github.com/Aditya-1020/orfs-custom
cd orfs-custom
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

Create a project configuration in your design directory (see `examples/project.yaml`). Point it at the RTL, constraints, ORFS `flow/` directory, and PDK installation. Then select it once:

```bash
orfsx project /path/to/design/config.yaml
```

Run commands from any directory:

```bash
orfsx validate
orfsx run synth
orfsx run place
orfsx gui place
orfsx sta
orfsx sta --all
orfsx sta --corner ss_100C_1v60
orfsx module-sta pe
```

Use `-c /path/to/design/config.yaml` on any command to override the selected project for that invocation. The selected project is stored in the user's XDG config directory, not in the design tree. No PDK or ORFS variables need to be exported per invocation.

## Configuration

See [examples/project.yaml](examples/project.yaml). Paths resolve relative to the configuration file. ORFS design Makefiles remain native Makefiles and can be selected with `physical.design_config`. YAML owns project paths and convenience defaults; the ORFS Makefile owns settings ORFS itself consumes.

The resolver checks explicit `ORFS_FLOW` / `PDK_ROOT` environment overrides first, then YAML, then common installation locations (including Volare). Run `orfsx validate` to see resolved paths before running the flow.

## Outputs

The default work directory is `build/<platform>/<run_tag>/` with ORFS results, logs, reports and analysis grouped beneath it. Existing ORFS filenames are retained.

## Module STA

Each entry under `modules` describes one independent RTL top. Example:

```yaml
modules:
  pe:
    top: pe
    filelist: rtl/pe_files.f
    constraints: constraints/pe.sdc
    liberty: auto
    defines: []
    parameters: {}
    include_dirs: []
```

Run `orfsx module-sta pe`. It generates a Yosys synthesis script, mapped Verilog netlist, area report, OpenSTA setup/hold/path reports, and run logs in `build/reports/modules/pe/`. A module-specific SDC is recommended. Without one, the helper generates basic single-clock constraints from `timing.clock_period`; these are for comparative block analysis, not signoff.

## Development

```bash
python -m pytest
python -m ruff check asicflow tests
```
