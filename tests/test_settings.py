"""Tests for configuration loading."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr, ValidationError

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
    assert settings.github_user_id == "36701168"
    assert settings.github_token_cache_seconds == 300
    assert settings.github_client_id is None
    assert settings.github_client_secret is None
    assert settings.sainsburys_storage_state_path is None


def test_environment_overrides_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BROWSER_MCP_TRANSPORT", "http")
    monkeypatch.setenv("BROWSER_MCP_PORT", "9001")
    monkeypatch.setenv("BROWSER_MCP_RATE_LIMIT_PER_SECOND", "0.25")
    monkeypatch.setenv("BROWSER_MCP_GITHUB_CLIENT_ID", "Ov23liExample")
    monkeypatch.setenv("BROWSER_MCP_GITHUB_CLIENT_SECRET", "not-a-real-secret")

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
        ("BROWSER_MCP_GITHUB_USER_ID", ""),
        ("BROWSER_MCP_GITHUB_USER_ID", "amc40"),
        ("BROWSER_MCP_GITHUB_TOKEN_CACHE_SECONDS", "-1"),
        ("BROWSER_MCP_GITHUB_CLIENT_ID", ""),
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


def test_the_user_id_must_be_an_id_not_a_login() -> None:
    """A login here would never match the `sub` claim, locking the operator out."""
    with pytest.raises(ValidationError):
        Settings(github_user_id="amc40")


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


@pytest.mark.parametrize(
    ("client_id", "client_secret"),
    [
        (None, None),
        ("Ov23liExample", None),
        (None, SecretStr("not-a-real-secret")),
    ],
)
def test_http_transport_requires_a_github_oauth_app(
    client_id: str | None,
    client_secret: SecretStr | None,
) -> None:
    """Half-configured OAuth would bind a port nobody could be authenticated on."""
    with pytest.raises(ValidationError, match="GitHub OAuth"):
        Settings(
            transport="http",
            github_client_id=client_id,
            github_client_secret=client_secret,
        )


def test_stdio_transport_needs_no_oauth_app() -> None:
    """Stdio cannot run an OAuth flow, so it has nothing to configure."""
    assert Settings(transport="stdio").github_client_id is None


def test_oauth_base_url_defaults_to_the_bind_address() -> None:
    settings = Settings(
        transport="http",
        github_client_id="Ov23liExample",
        github_client_secret=SecretStr("not-a-real-secret"),
        port=9001,
    )

    assert settings.oauth_base_url == "http://127.0.0.1:9001"


def test_oauth_base_url_can_be_overridden() -> None:
    """A tunnelled or proxied server is reached at a URL it cannot infer."""
    settings = Settings(
        transport="http",
        github_client_id="Ov23liExample",
        github_client_secret=SecretStr("not-a-real-secret"),
        github_oauth_base_url="https://mcp.example.com",
    )

    assert settings.oauth_base_url == "https://mcp.example.com"


def test_sainsburys_storage_state_path_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    storage_state_path = tmp_path / "sainsburys_storage_state.json"
    monkeypatch.setenv(
        "BROWSER_MCP_SAINSBURYS_STORAGE_STATE_PATH",
        str(storage_state_path),
    )

    settings = Settings()

    assert settings.sainsburys_storage_state_path == storage_state_path


def test_the_client_secret_is_not_printed() -> None:
    """A secret that stringifies plainly ends up in logs and tracebacks."""
    settings = Settings(
        transport="http",
        github_client_id="Ov23liExample",
        github_client_secret=SecretStr("hunter2-not-a-real-secret"),
    )

    assert "hunter2" not in repr(settings)
    assert settings.github_client_secret is not None
    assert settings.github_client_secret.get_secret_value() == (
        "hunter2-not-a-real-secret"
    )
