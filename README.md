# ASICFlow

ASICFlow is a Python orchestration and analysis layer for OpenROAD-flow-scripts (ORFS). It does not replace ORFS. It provides a reusable run context, tool invocation, physical/timing analysis, experiment tracking and extension points.

## Installation

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

Set the project and tool paths:

```bash
export ROOT_DIR=$PWD
export ORFS_FLOW=/path/to/OpenROAD-flow-scripts/flow
export PDK_ROOT=/path/to/pdk
```

The design root must contain the ORFS `DESIGN_CONFIG` and the RTL, constraints and macro files referenced by it.

## Commands

```bash
make validate PLATFORM=sky130hd DESIGN_CONFIG=design-configs/sky130hd.mk
make synth PLATFORM=sky130hd RUN_TAG=baseline
make place PLATFORM=sky130hd RUN_TAG=baseline
make sta-pvt PLATFORM=sky130hd STAGE=latest
make gui PLATFORM=sky130hd GUI_TARGET=final
```

The Python CLI is also available directly:

```bash
asicflow validate --platform sky130hd --design-config design-configs/sky130hd.mk
asicflow sta-pvt --platform sky130hd --stage latest
```

## Configuration and extensions

See [Architecture](docs/architecture.md), [Configuration](docs/configuration.md), [Path configuration](docs/path-configuration.md) and [Extensions](docs/extensions.md). ORFS Make configuration remains authoritative for ORFS-specific settings; YAML configures ASICFlow behavior.

## Development

```bash
make install
make test
make lint
```

Unit tests do not require OpenROAD or a PDK. Full physical-design integration must be run against the target ORFS installation and platform.
