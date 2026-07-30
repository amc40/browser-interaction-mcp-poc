#!/usr/bin/env bash
# Check every locked dependency, including dev tooling, against the PyPI
# advisory database.
#
# pip-audit reads a requirements file rather than uv.lock, so export the lock to
# that format first. The export is a faithful rendering of uv.lock, so this
# audits exactly what `uv sync` would install.
set -euo pipefail

requirements="$(mktemp)"
trap 'rm -f "${requirements}"' EXIT

uv export \
	--quiet \
	--frozen \
	--all-groups \
	--no-emit-project \
	--format requirements-txt \
	--output-file "${requirements}"

uv run --frozen pip-audit --disable-pip --strict --requirement "${requirements}"
