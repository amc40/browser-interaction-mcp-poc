.DEFAULT_GOAL := check
.PHONY: install check format lint types deps audit test run clean

install:  ## Sync the environment and install the git hooks
	uv sync --all-groups
	uv run pre-commit install --install-hooks

check: format lint types deps test audit  ## Run every gate CI runs

format:  ## Check formatting (use `uv run ruff format .` to apply)
	uv run ruff format --check --diff .

lint:
	uv run ruff check .

types:
	uv run mypy

deps:  ## Unused, missing and misplaced dependencies, plus lockfile freshness
	uv run deptry src tests
	uv lock --check

audit:  ## Known vulnerabilities in the locked dependencies
	./scripts/audit.sh

test:
	uv run pytest --cov

run:  ## Start the server on stdio
	uv run browser-interaction-mcp

clean:
	rm -rf dist .coverage coverage.xml .pytest_cache .ruff_cache .mypy_cache
