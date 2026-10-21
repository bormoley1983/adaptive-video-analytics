PYTHON ?= python3
PYTEST := $(PYTHON) -m pytest

.PHONY: help install-dev lint test test-unit test-integration test-e2e test-infra test-coverage

help:
	@echo "install-dev       Install the package and test harness"
	@echo "lint              Check production and test code"
	@echo "test-unit         Run isolated, deterministic tests"
	@echo "test-integration  Run internal component-boundary tests"
	@echo "test-e2e          Run complete workflow tests"
	@echo "test-infra        Run container and infrastructure tests"
	@echo "test-coverage     Enforce 90% combined line and branch coverage"

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
