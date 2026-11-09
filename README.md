# ASICFlow

ASICFlow is a Python orchestration and analysis layer for OpenROAD-flow-scripts (ORFS). It does not replace ORFS. It provides a consistent run context, tool invocation, physical/timing analysis, experiment tracking and extension points.

## Installation

    python3 -m venv .venv
    . .venv/bin/activate
    pip install -e '.[dev]'

Set the design root and tool paths:

    export ROOT_DIR=/path/to/design
    export ORFS_FLOW=/path/to/OpenROAD-flow-scripts/flow
    export PDK_ROOT=/path/to/pdk

The design root must contain the ORFS DESIGN_CONFIG and the RTL, constraints and macro files referenced by it.

## Commands

    asicflow validate --platform sky130hd --design-config design-configs/sky130hd.mk
    asicflow synth --platform sky130hd --design-config design-configs/sky130hd.mk
    asicflow place --platform sky130hd --design-config design-configs/sky130hd.mk
    asicflow sta-pvt --platform sky130hd --stage latest
    asicflow analyze congestion --platform sky130hd
    asicflow summary --platform sky130hd

All ORFS stages are forwarded to the configured ORFS installation. Use --work-home, --run-tag, --flow-variant and --placement to control the run.

## Configuration and extensions

See docs/architecture.md, docs/configuration.md and docs/extensions.md. ORFS Make configuration remains authoritative for ORFS-specific settings; YAML configures ASICFlow behavior.

## Development

    pytest
    ruff check asicflow tests

Unit tests do not require OpenROAD or a PDK. Physical-design integration tests must run in an environment with the target ORFS installation and platform data.
