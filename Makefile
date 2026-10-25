PYTHON ?= python3
PYTEST := $(PYTHON) -m pytest
DEV_IMAGE ?= ava/dev:local

.PHONY: help install-dev dev-image lint test test-unit test-integration test-e2e test-infra test-coverage lint-docker test-coverage-docker up down status probe

help:
	@echo "install-dev       Install the package and test harness"
	@echo "lint              Check production and test code"
	@echo "test-unit         Run isolated, deterministic tests"
	@echo "test-integration  Run internal component-boundary tests"
	@echo "test-e2e          Run complete workflow tests"
	@echo "test-infra        Run container and infrastructure tests"
	@echo "test-coverage     Enforce 90% combined line and branch coverage"
	@echo "dev-image         Build the dev image (inference stack + test tooling)"
	@echo "lint-docker       Run ruff in the dev image"
	@echo "test-coverage-docker  Run the coverage gate in the dev image"
	@echo "up [N=]           Bring up the full study stack (harness + ops); N = camera count"
	@echo "down              Tear down the study stack (volumes preserved)"
	@echo "status            One-screen health table for the study stack"
	@echo "probe             Probe all harness RTSP streams (codec/res/FPS/rate)"

# Study-stack lifecycle (E-3-T5). Thin wrappers over harness/stack.sh, which is
# idempotent and self-healing: it starts MediaMTX first, waits for health, then
# force-recreates any publisher left stuck in `Exited` after a reboot.
up:
	./harness/stack.sh up $(N)

down:
	./harness/stack.sh down

status:
	./harness/stack.sh status

probe:
	./harness/probe.sh

install-dev:
	$(PYTHON) -m pip install -e '.[dev]'

lint:
	$(PYTHON) -m ruff check src tests

test: test-unit

test-unit:
	$(PYTEST) -q -m "not integration and not e2e and not infra"

test-integration:
	$(PYTEST) -q -m integration

test-e2e:
	$(PYTEST) -q -m e2e

test-infra:
	$(PYTEST) -q -m infra

test-coverage:
	$(PYTEST) -q -m "not e2e and not infra" --cov=ava --cov-branch --cov-report=term-missing

# Reproducible gates in the dev image (B-018): run against the exact locked
# inference stack instead of an ad-hoc host interpreter.
dev-image:
	docker build -f Dockerfile.dev -t $(DEV_IMAGE) .

lint-docker: dev-image
	docker run --rm -v "$$(pwd)":/work -w /work $(DEV_IMAGE) -m ruff check src tests

test-coverage-docker: dev-image
	docker run --rm -v "$$(pwd)":/work -w /work $(DEV_IMAGE) \
		-m pytest -q -m "not e2e and not infra" --cov=ava --cov-branch --cov-report=term-missing
