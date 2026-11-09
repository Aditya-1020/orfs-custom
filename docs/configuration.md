# Configuration

ASICFlow combines framework YAML with the native ORFS design Makefile.

ORFS owns the implementation stages and all ORFS-specific variables. ASICFlow YAML configures its own paths, platform timing-library discovery, design metadata and extensions.

## Configuration sources

Built-in defaults are installed under `asicflow/configs/`. Project overrides live under `ROOT_DIR/config/` by default, or in a directory selected with `ASICFLOW_CONFIG_DIR`.

```text
config/
├── flow.yaml
├── platforms/
│   ├── sky130hd.yaml
│   └── nangate45.yaml
└── designs/
    └── <DESIGN_NICKNAME>.yaml
```

The built-in and project-level `flow.yaml` and platform profiles are recursively merged. Project values override built-in values. Design profiles are project-specific and are selected by `DESIGN_NICKNAME`, or explicitly with `ASICFLOW_DESIGN_PROFILE`.

## Precedence

For run settings, CLI arguments override environment variables, which override YAML defaults.

The ORFS design Makefile remains authoritative for literal exported values such as `PLATFORM`, `DESIGN_NICKNAME` and `FAKERAM_TECH`. ASICFlow parses literal `export VAR = value` declarations and simple relative includes; it does not evaluate arbitrary Make expressions.

## Required paths

Set `ORFS_FLOW` to the ORFS `flow/` directory. It is required unless `paths.orfs_flow` is explicitly configured in project YAML.

Set `PDK_ROOT` when PDK-resident timing libraries are needed. When unset, the framework uses `ROOT_DIR/pdk` and falls back to the ORFS platform library directory if the configured corner library directory is absent.

`ROOT_DIR` identifies the design project and defaults to the directory containing the installed ASICFlow package. Relative project paths are resolved from this root.

## Design profile

A profile is named after `DESIGN_NICKNAME` unless `ASICFLOW_DESIGN_PROFILE` is set.

```yaml
design:
  name: accelerator
  top: sys_top

rtl:
  root: rtl
  filelist: files.f

constraints:
  default: constraints/constraints_{platform}.sdc

macros:
  - instance: u_buffer.u_mem
    model: sram_64x64
    lef: fakeram/sky130hd/sram_64x64/sram_64x64.lef
    liberty: fakeram/sky130hd/sram_64x64/sram_64x64.lib
    blackbox: fakeram/sky130hd/sram_64x64/sram_64x64.bb.v
```

Each macro entry identifies a hierarchical instance and its model or explicit collateral paths. Macro names and hierarchy paths are design data, not framework defaults. If a design has no macros, macro validation and macro-specific placement generation are not enabled.

## Path formatting

Framework path values support environment-variable expansion and the fields `{platform}`, `{design}`, `{run_tag}`, `{flow_variant}` and `{fakeram_tech}`. Unresolved `${VARIABLE}` placeholders are rejected rather than converted into accidental relative paths.

```bash
export ROOT_DIR=$PWD
export ORFS_FLOW=/opt/OpenROAD-flow-scripts/flow
export PDK_ROOT=/opt/pdk
export ASICFLOW_CONFIG_DIR=$PWD/config
export ASICFLOW_DESIGN_PROFILE=$PWD/config/designs/accelerator.yaml
```
