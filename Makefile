# OddsIQ — common tasks. Run `make help` for the list.
.PHONY: help install test lint format run dashboard audit clean start push

# Prefer the project venv interpreter so `make test` / `make audit` work
# without an activated shell. The docs audit shells out to pytest for its
# test-count check, which fails under a bare system python that lacks pytest.
PYTHON := $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "} {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Editable install with dev tools, plus the Playwright Chromium build
	$(PYTHON) -m pip install -e '.[dev]'
	$(PYTHON) -m playwright install chromium

test:  ## Run the full test suite
	$(PYTHON) -m pytest tests/

lint:  ## Lint and check formatting with ruff
	$(PYTHON) -m ruff check oddsiq/ tests/ scripts/
	$(PYTHON) -m ruff format --check oddsiq/ tests/ scripts/

format:  ## Format the Python code with ruff
	$(PYTHON) -m ruff format oddsiq/ tests/ scripts/

run:  ## Run the pipeline (entry point)
	oddsiq

dashboard:  ## Run the read-only dashboard at http://127.0.0.1:8080
	oddsiq dashboard

start:  ## Pipeline + dashboard together, opens the dashboard in your browser
	./scripts/start.sh

push:  ## Build the linux/amd64 image and push it to the registry
	./scripts/build-and-push.sh

audit:  ## Docs drift check (wikilinks, test counts, config coverage)
	$(PYTHON) scripts/docs-audit.py

clean:  ## Remove Python caches and build artifacts
	rm -rf .pytest_cache .ruff_cache oddsiq.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
