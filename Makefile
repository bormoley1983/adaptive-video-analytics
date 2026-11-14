PYTHON ?= python3
PYTEST := $(PYTHON) -m pytest
DEV_IMAGE ?= ava/dev:local

.PHONY: help install-dev dev-image lint test test-unit test-integration test-e2e test-infra test-coverage test-packaged lint-docker test-coverage-docker up down status probe db-backup db-restore-drill

help:
	@echo "install-dev       Install the package and test harness"
	@echo "lint              Check production and test code"
	@echo "test-unit         Run isolated, deterministic tests"
	@echo "test-integration  Run internal component-boundary tests"
	@echo "test-e2e          Run complete workflow tests"
	@echo "test-infra        Run container and infrastructure tests"
	@echo "test-coverage     Enforce 90% combined line and branch coverage"
	@echo "test-packaged     Run the packaged API acceptance gate (isolated venv, non-editable install)"
	@echo "dev-image         Build the dev image (inference stack + test tooling)"
	@echo "lint-docker       Run ruff in the dev image"
	@echo "test-coverage-docker  Run the coverage gate in the dev image"
	@echo "up [N=]           Bring up the full study stack (harness + ops); N = camera count"
	@echo "down              Tear down the study stack (volumes preserved)"
	@echo "status            One-screen health table for the study stack"
	@echo "probe             Probe all harness RTSP streams (codec/res/FPS/rate)"
	@echo "db-backup         Logical pg_dump -Fc of the ops postgres (keep-last-N)"
	@echo "db-restore-drill  Drop/recreate the ops DB, restore a dump, verify consistency"

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

# Ops PostgreSQL backup and restore drill. Export AVA_PG_PASSWORD; these
# commands do not load ops/.env. Dumps default to git-ignored ops/backups/
# (override with the same absolute AVA_BACKUP_DIR used by Compose).
db-backup:
	./ops/backup.sh backup

db-restore-drill:
	@test -n "$(DUMP)" || { echo "usage: make db-restore-drill DUMP=<dump-file>"; exit 2; }
	./ops/backup.sh restore-drill $(DUMP)

install-dev:
	$(PYTHON) -m pip install -e '.[dev]'

lint:
	$(PYTHON) -m ruff check src tests

test: test-unit

test-unit:
	$(PYTEST) -q -m "not integration and not e2e and not infra"

test-integration:
	$(PYTEST) -q -m "integration and not packaged"

E2E_VENV ?= .venv-e2e

test-e2e:
	$(PYTHON) -m venv --clear $(E2E_VENV)
	$(E2E_VENV)/bin/pip install --quiet --upgrade pip setuptools wheel
	$(E2E_VENV)/bin/pip install --quiet -r tests/fixtures/packaged_api_requirements.txt
	$(E2E_VENV)/bin/pip install --quiet --no-deps .
	$(E2E_VENV)/bin/python -I -m pytest -q tests/test_api_acceptance_e2e.py -m e2e --override-ini pythonpath=

test-infra:
	$(PYTEST) -q -m infra

test-coverage:
	$(PYTEST) -q -m "not e2e and not infra and not packaged" --cov=ava --cov-branch --cov-report=term-missing

# Packaged API acceptance gate: builds an isolated virtualenv with
# the API-relevant subset of the locked stack, installs the package
# non-editably, and runs the packaged suite with pythonpath overridden to
# empty so pytest does not prepend the source checkout (pyproject.toml sets
# pythonpath = ["src"]). The first test asserts that the imported module
# resolves inside the non-editable installation; it fails (not skips) when
# the gate lacks the intended installation. Docker is required by this gate:
# if it is unavailable the suite skips and the target exits nonzero, so a
# green run always means the package was actually exercised.
PACKAGED_VENV ?= .venv-packaged

test-packaged:
	@rm -rf $(PACKAGED_VENV)
	$(PYTHON) -m venv $(PACKAGED_VENV)
	$(PACKAGED_VENV)/bin/pip install --quiet --upgrade pip setuptools wheel
	$(PACKAGED_VENV)/bin/pip install --quiet -r tests/fixtures/packaged_api_requirements.txt
	$(PACKAGED_VENV)/bin/pip install --quiet --no-deps .
	@out=$$($(PACKAGED_VENV)/bin/python -m pytest \
		tests/test_packaged_api_integration.py \
		--import-mode=importlib --override-ini pythonpath= 2>&1); status=$$?; \
	printf '%s\n' "$$out"; \
	skips=$$(printf '%s\n' "$$out" | grep -oE '[0-9]+ skipped' | head -1 | cut -d' ' -f1); \
	if [ "$$status" -eq 0 ] && [ -n "$$skips" ]; then \
		echo "packaged gate: all tests skipped (Docker unavailable?) — failing"; exit 1; fi; \
	exit $$status

# Reproducible gates in the dev image (B-018): run against the exact locked
# inference stack instead of an ad-hoc host interpreter.
dev-image:
	docker build -f Dockerfile.dev -t $(DEV_IMAGE) .

lint-docker: dev-image
	docker run --rm -v "$$(pwd)":/work -w /work $(DEV_IMAGE) -m ruff check src tests

test-coverage-docker: dev-image
	docker run --rm -v "$$(pwd)":/work -w /work $(DEV_IMAGE) \
		-m pytest -q -m "not e2e and not infra" --cov=ava --cov-branch --cov-report=term-missing
