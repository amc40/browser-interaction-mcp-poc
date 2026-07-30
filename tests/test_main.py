"""Tests for the console entry point."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP

from browser_interaction_mcp.__main__ import main

if TYPE_CHECKING:
    import pytest


def _capture_run(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace ``FastMCP.run`` with a recorder and return the recorded calls."""
    calls: list[dict[str, Any]] = []

    def fake_run(_self: FastMCP, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(FastMCP, "run", fake_run)
    return calls


def test_serves_over_stdio_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_run(monkeypatch)

    main()

    assert calls == [{"transport": "stdio"}]


def test_serves_over_http_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_run(monkeypatch)
    monkeypatch.setenv("BROWSER_MCP_TRANSPORT", "http")
    monkeypatch.setenv("BROWSER_MCP_HOST", "10.0.0.5")
    monkeypatch.setenv("BROWSER_MCP_PORT", "9999")

    main()

    assert calls == [{"transport": "http", "host": "10.0.0.5", "port": 9999}]


def test_configures_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture_run(monkeypatch)
    monkeypatch.setenv("BROWSER_MCP_LOG_LEVEL", "DEBUG")
    configured: list[str] = []
    monkeypatch.setattr(
        logging,
        "basicConfig",
        lambda **kwargs: configured.append(kwargs["level"]),
    )

    main()

    assert configured == ["DEBUG"]
