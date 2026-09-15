.DEFAULT_GOAL := check
.PHONY: install check format lint types deps audit test run clean

install:  ## Sync the workspace and install the git hooks
	uv sync --all-groups
	uv run pre-commit install --install-hooks

check: format lint types deps test audit  ## Run every gate CI runs

format:  ## Check formatting (use `uv run ruff format .` to apply)
	uv run ruff format --check --diff .

lint:
	uv run ruff check .

types:
	uv run mypy

# deptry resolves a package's declared dependencies from the pyproject.toml in
# its working directory, so it runs once per workspace member rather than once
# over `packages/`. `--project` keeps both runs in the workspace's one venv.
deps:  ## Unused, missing and misplaced dependencies, plus lockfile freshness
	cd packages/core && uv run --project ../.. deptry src tests
	cd packages/sainsburys && uv run --project ../.. deptry src tests
	uv lock --check

audit:  ## Known vulnerabilities in the locked dependencies
	./scripts/audit.sh

test:
	uv run pytest --cov

run:  ## Start the Sainsbury's server on stdio
	uv run browser-interaction-mcp

clean:
	rm -rf dist .coverage coverage.xml .pytest_cache .ruff_cache .mypy_cache
