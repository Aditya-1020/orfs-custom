# Configuration

ASICFlow uses a hybrid configuration model.

- ORFS design configuration remains a Makefile supplied through DESIGN_CONFIG. ORFS owns its stage implementation and platform-specific flow variables.
- ASICFlow YAML describes framework paths, platform timing-library discovery, design metadata and explicitly enabled extensions.
- Environment variables override YAML paths. CLI arguments override environment-derived run settings.

## Configuration layout

    config/                         # project-managed override root
    ├── flow.yaml
    ├── platforms/
    └── designs/
        └── <DESIGN_NICKNAME>.yaml

Built-in defaults ship under asicflow/configs/. When a config/ directory exists beside the package, it is used as the default override root. Set ASICFLOW_CONFIG_DIR to select another root and ASICFLOW_DESIGN_PROFILE to select a design profile explicitly.

## Required environment

Set ORFS_FLOW to the ORFS flow/ directory. Set PDK_ROOT when the platform profile points into a PDK installation. ROOT_DIR identifies the design project root and defaults to the parent of the installed asicflow package.

The ORFS Makefile remains authoritative for variables such as PLATFORM, DESIGN_NICKNAME and FAKERAM_TECH. ASICFlow reads literal export VAR = value declarations from the design config for context discovery; it does not evaluate arbitrary Make expressions.

## Design metadata

A design profile is named after DESIGN_NICKNAME unless ASICFLOW_DESIGN_PROFILE is set.

    design:
      name: accelerator
      top: sys_top

    rtl:
      root: rtl
      filelist: files.f

    macros:
      - instance: u_buffer.u_mem
        model: sram_64x64
        lef: fakeram/sky130hd/sram_64x64/sram_64x64.lef
        liberty: fakeram/sky130hd/sram_64x64/sram_64x64.lib
        blackbox: fakeram/sky130hd/sram_64x64/sram_64x64.bb.v

Macro model names and instance paths are design data, not framework defaults. If no macros are defined, macro validation and macro-specific placement generation are unavailable.

## Path resolution

Paths in flow.yaml are relative to ROOT_DIR unless absolute. The package's built-in platform profiles are used when no project override exists. Supported format fields include {platform}, {design}, {run_tag}, {flow_variant} and {fakeram_tech}. Environment-variable expansion is supported.

    export ORFS_FLOW=/opt/OpenROAD-flow-scripts/flow
    export PDK_ROOT=/opt/pdk
    export ROOT_DIR=$PWD
    export ASICFLOW_CONFIG_DIR=$PWD/config
    export ASICFLOW_DESIGN_PROFILE=$PWD/config/designs/accelerator.yaml
