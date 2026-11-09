PYTHON ?= python3
PIP ?= $(PYTHON) -m pip
ASICFLOW ?= $(PYTHON) -m asicflow

ROOT_DIR ?= $(CURDIR)
ORFS_FLOW ?=
PDK_ROOT ?=
PLATFORM ?=
RUN_TAG ?=
FLOW_VARIANT ?=
PLACEMENT ?=
DESIGN_CONFIG ?=
WORK_HOME ?=
CONFIG_DIR ?=
DESIGN_PROFILE ?=
STAGE ?= latest
GUI_TARGET ?= final
INTERACTIVE ?=

export ROOT_DIR ORFS_FLOW PDK_ROOT

ASICFLOW_ARGS = $(if $(strip $(PLATFORM)),--platform "$(PLATFORM)") \
                $(if $(strip $(RUN_TAG)),--run-tag "$(RUN_TAG)") \
                $(if $(strip $(FLOW_VARIANT)),--flow-variant "$(FLOW_VARIANT)") \
                $(if $(strip $(PLACEMENT)),--placement "$(PLACEMENT)") \
                $(if $(strip $(DESIGN_CONFIG)),--design-config "$(DESIGN_CONFIG)") \
                $(if $(strip $(WORK_HOME)),--work-home "$(WORK_HOME)") \
                $(if $(strip $(CONFIG_DIR)),--config-dir "$(CONFIG_DIR)") \
                $(if $(strip $(DESIGN_PROFILE)),--design-profile "$(DESIGN_PROFILE)")

.PHONY: help install validate synth floorplan place cts route finish flow gui sta-pvt sta-shell presta metrics predict place-gen report test lint

help:
	@printf '%s\n' \
		'make install' \
		'make validate [PLATFORM=... DESIGN_CONFIG=...]' \
		'make synth|floorplan|place|cts|route|finish [RUN_TAG=...]' \
		'make flow [RUN_TAG=...]' \
		'make gui GUI_TARGET=final' \
		'make sta-pvt STAGE=latest [STA_ARGS=...]' \
		'make sta-shell STAGE=latest [CORNER=...]' \
		'make metrics STAGES="place cts finish"' \
		'make place-gen NAME=... SIDE=left|right' \
		'make test | lint'

install:
	$(PIP) install -e '.[dev]'

validate:
	$(ASICFLOW) validate $(ASICFLOW_ARGS)

synth floorplan place cts route finish:
	$(ASICFLOW) $@ $(ASICFLOW_ARGS) $(if $(STA),--sta)

flow:
	$(ASICFLOW) flow $(ASICFLOW_ARGS) $(if $(STA),--sta)

gui:
	$(ASICFLOW) gui $(GUI_TARGET) $(ASICFLOW_ARGS)

sta-pvt:
	$(ASICFLOW) sta-pvt $(ASICFLOW_ARGS) --stage "$(STAGE)" $(STA_ARGS)

sta-shell:
	$(ASICFLOW) sta-shell $(ASICFLOW_ARGS) --stage "$(STAGE)" $(if $(CORNER),--corner "$(CORNER)")

presta:
	$(ASICFLOW) presta $(ASICFLOW_ARGS) $(if $(INTERACTIVE),--shell)

metrics:
	$(ASICFLOW) metrics $(ASICFLOW_ARGS) $(if $(ALL_STAGES),--all-stages) $(STAGES)

predict:
	$(ASICFLOW) predict $(ASICFLOW_ARGS)

place-gen:
	$(ASICFLOW) place-gen $(ASICFLOW_ARGS) $(if $(PRESETS),--presets,--name "$(NAME)" --side "$(SIDE)" --gap "$(or $(GAP),20)" --chan "$(or $(CHAN),60)")

report:
	$(ASICFLOW) report $(ASICFLOW_ARGS) $(if $(LIMIT),--limit "$(LIMIT)")

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check asicflow/core asicflow/plugins asicflow/config.py asicflow/sweep/rank.py tests

clean:
	@find . -type d -name "__pycache__" -prune -exec rm -rf -- {} +