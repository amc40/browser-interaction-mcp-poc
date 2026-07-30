"""Tests for configuration loading."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from browser_interaction_mcp.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path


def test_defaults_are_conservative() -> None:
    settings = Settings()

    assert settings.transport == "stdio"
    assert settings.host == "127.0.0.1"
    assert settings.rate_limit_per_second == 1.0
    assert settings.rate_limit_burst == 5
    assert settings.include_error_details is False


def test_environment_overrides_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BROWSER_MCP_TRANSPORT", "http")
    monkeypatch.setenv("BROWSER_MCP_PORT", "9001")
    monkeypatch.setenv("BROWSER_MCP_RATE_LIMIT_PER_SECOND", "0.25")

    settings = Settings()

    assert settings.transport == "http"
    assert settings.port == 9001
    assert settings.rate_limit_per_second == 0.25


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("BROWSER_MCP_TRANSPORT", "carrier-pigeon"),
        ("BROWSER_MCP_PORT", "0"),
        ("BROWSER_MCP_RATE_LIMIT_PER_SECOND", "0"),
        ("BROWSER_MCP_RATE_LIMIT_BURST", "0"),
        ("BROWSER_MCP_LOG_LEVEL", "CHATTY"),
    ],
)
def test_invalid_configuration_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings()


def test_unknown_options_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(rate_limit_per_minute=10)  # type: ignore[call-arg]


def test_settings_are_immutable() -> None:
    settings = Settings()

    with pytest.raises(ValidationError):
        settings.rate_limit_per_second = 100.0


def test_env_file_is_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("BROWSER_MCP_LOG_LEVEL=DEBUG\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert Settings().log_level == "DEBUG"
