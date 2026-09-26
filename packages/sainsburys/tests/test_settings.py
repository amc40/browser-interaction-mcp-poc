"""Tests for the settings this site adds on top of core's."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr, ValidationError

from browser_mcp_sainsburys.settings import SainsburysSettings

if TYPE_CHECKING:
    from pathlib import Path


def test_the_site_settings_default_to_absent() -> None:
    """Both are optional: the public actions work before anyone has logged in."""
    settings = SainsburysSettings()

    assert settings.sainsburys_storage_state_path is None
    assert settings.sainsburys_username is None


def test_core_settings_are_inherited_unchanged() -> None:
    """The site subclasses rather than composes, so core's fields come too.

    That is also what keeps `redaction.build_redactor` complete - it walks one
    class's `model_fields` for every SecretStr the server holds.
    """
    settings = SainsburysSettings()

    assert settings.transport == "stdio"
    assert settings.rate_limit_burst == 5
    assert settings.github_user_id == "36701168"


def test_an_empty_username_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SainsburysSettings(sainsburys_username=SecretStr(""))


def test_sainsburys_storage_state_path_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    storage_state_path = tmp_path / "sainsburys_storage_state.json"
    monkeypatch.setenv(
        "BROWSER_MCP_SAINSBURYS_STORAGE_STATE_PATH",
        str(storage_state_path),
    )

    settings = SainsburysSettings()

    assert settings.sainsburys_storage_state_path == storage_state_path


def test_sainsburys_username_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BROWSER_MCP_SAINSBURYS_USERNAME", "alan@example.com")

    settings = SainsburysSettings()

    assert settings.sainsburys_username is not None
    assert settings.sainsburys_username.get_secret_value() == "alan@example.com"


def test_sainsburys_username_is_not_printed() -> None:
    """A SecretStr - not because it grants access alone, but for redaction.py."""
    settings = SainsburysSettings(sainsburys_username=SecretStr("alan+mcp@example.com"))

    assert "alan+mcp" not in repr(settings)


def test_the_client_secret_is_not_printed() -> None:
    """A secret that stringifies plainly ends up in logs and tracebacks."""
    settings = SainsburysSettings(
        transport="http",
        github_client_id="Ov23liExample",
        github_client_secret=SecretStr("hunter2-not-a-real-secret"),
    )

    assert "hunter2" not in repr(settings)
    assert settings.github_client_secret is not None
    assert settings.github_client_secret.get_secret_value() == (
        "hunter2-not-a-real-secret"
    )
