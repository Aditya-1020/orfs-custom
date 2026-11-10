PYTHON ?= python3
PIP ?= $(PYTHON) -m pip
ORFSX ?= orfsx
CONFIG ?=
STAGE ?= synth
GUI_STAGE ?= place
MODULE ?=
STA_ARGS ?=
ARGS = $(if $(strip $(CONFIG)),-c "$(CONFIG)",)

.PHONY: help install project validate synth floorplan place cts route finish flow gui sta module-sta test lint clean

help:
	@printf '%s\n' \
		'make install' \
		'make project CONFIG=/path/to/project.yaml' \
		'make validate CONFIG=/path/to/project.yaml' \
		'make synth|floorplan|place|cts|route|finish CONFIG=...' \
		'make gui GUI_STAGE=place CONFIG=...' \
		'make sta STA_ARGS="--all" CONFIG=...' \
		'make module-sta MODULE=pe CONFIG=...' \
		'make test | lint'

install:
	$(PIP) install -e '.[dev]'

project:
	@test -n "$(CONFIG)" || (echo "set CONFIG=/path/to/project.yaml" >&2; exit 2)
	$(ORFSX) project "$(CONFIG)"

validate:
	$(ORFSX) $(ARGS) validate

synth floorplan place cts route finish:
	$(ORFSX) $(ARGS) run $@

flow:
	$(ORFSX) $(ARGS) run flow

gui:
	$(ORFSX) $(ARGS) gui "$(GUI_STAGE)"

sta:
	$(ORFSX) $(ARGS) sta $(STA_ARGS)

module-sta:
	@test -n "$(MODULE)" || (echo "set MODULE=<module-name>" >&2; exit 2)
	$(ORFSX) $(ARGS) module-sta "$(MODULE)"

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check asicflow tests

clean:
	@test -n "$(CONFIG)" || (echo "set CONFIG=/path/to/project.yaml" >&2; exit 2)
	rm -rf "$$(dirname "$(CONFIG)")/build"
