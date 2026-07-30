"""Shared test fixtures."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolated_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Keep the developer's own configuration out of the test run.

    Clears ``BROWSER_MCP_*`` variables and runs each test in an empty directory
    so a local ``.env`` file cannot change the outcome.
    """
    for name in os.environ:
        if name.startswith("BROWSER_MCP_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
