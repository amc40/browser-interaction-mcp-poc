"""Tests for the assembled server."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 - used at runtime to build paths in tests
from typing import TYPE_CHECKING

import pytest
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from fastmcp.exceptions import ToolError
from pydantic import SecretStr

from browser_interaction_mcp import sainsburys
from browser_interaction_mcp.server import SERVER_NAME, build_server
from browser_interaction_mcp.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastmcp.client.elicitation import ElicitationHandler

    from tests.conftest import Authenticate


def _accept_with(value: str) -> ElicitationHandler:
    """Build an elicitation handler that always accepts with ``value``."""

    async def handler(
        message: str, response_type: object, params: object, context: object
    ) -> str:
        del message, response_type, params, context
        return value

    return handler


def _decline() -> ElicitationHandler:
    """Build an elicitation handler that always declines."""

    async def handler(
        message: str, response_type: object, params: object, context: object
    ) -> ElicitResult[None]:
        del message, response_type, params, context
        return ElicitResult(action="decline")

    return handler


async def test_server_exposes_only_registered_tools(
    authenticate: Authenticate,
) -> None:
    authenticate()

    async with Client(build_server()) as client:
        tools = await client.list_tools()

    assert [tool.name for tool in tools] == [
        "server_info",
        "sainsburys_products_we_love",
        "sainsburys_add_to_basket",
        "sainsburys_refresh_session",
    ]


async def test_server_info_reports_configuration(authenticate: Authenticate) -> None:
    authenticate()
    settings = Settings(rate_limit_per_second=3.5, rate_limit_burst=7)

    async with Client(build_server(settings)) as client:
        result = await client.call_tool("server_info")

    assert result.data.transport == "stdio"
    assert result.data.rate_limit_per_second == 3.5
    assert result.data.rate_limit_burst == 7
    assert result.data.version


async def test_sainsburys_products_we_love_wires_to_the_browser_action(
    monkeypatch: pytest.MonkeyPatch,
    authenticate: Authenticate,
) -> None:
    """Only checks the wiring - test_sainsburys.py covers the scraping itself.

    Calling the tool should reach `sainsburys.products_we_love` and wrap
    whatever it returns.
    """
    authenticate()
    monkeypatch.setattr(
        sainsburys,
        "products_we_love",
        lambda: ["Chocolate Digestives 400g", "Semi Skimmed Milk 2L"],
    )

    async with Client(build_server()) as client:
        result = await client.call_tool("sainsburys_products_we_love")

    assert result.data.products == [
        "Chocolate Digestives 400g",
        "Semi Skimmed Milk 2L",
    ]


async def test_sainsburys_add_to_basket_wires_to_the_browser_action(
    monkeypatch: pytest.MonkeyPatch,
    authenticate: Authenticate,
    tmp_path: Path,
) -> None:
    """Only checks the wiring - test_sainsburys.py covers the action itself."""
    authenticate()
    storage_state_path = tmp_path / "session.json"
    storage_state_path.write_text("{}", encoding="utf-8")
    settings = Settings(sainsburys_storage_state_path=storage_state_path)
    seen_calls: list[tuple[str, Path]] = []

    def fake_add_to_basket(query: str, *, storage_state_path: Path) -> str:
        seen_calls.append((query, storage_state_path))
        return "Chocolate Digestives 400g"

    monkeypatch.setattr(sainsburys, "add_to_basket", fake_add_to_basket)

    async with Client(build_server(settings)) as client:
        result = await client.call_tool(
            "sainsburys_add_to_basket", {"query": "washing up liquid"}
        )

    assert result.data.product == "Chocolate Digestives 400g"
    assert seen_calls == [("washing up liquid", storage_state_path)]


async def test_sainsburys_add_to_basket_refuses_without_a_saved_session(
    authenticate: Authenticate,
) -> None:
    authenticate()
    settings = Settings(sainsburys_storage_state_path=None, include_error_details=True)

    async with Client(build_server(settings)) as client:
        with pytest.raises(ToolError, match=r"sainsburys_login\.py"):
            await client.call_tool("sainsburys_add_to_basket")


async def test_sainsburys_refresh_session_wires_to_the_browser_action(
    monkeypatch: pytest.MonkeyPatch,
    authenticate: Authenticate,
    tmp_path: Path,
) -> None:
    """Only checks the wiring - test_sainsburys.py covers the login flow itself."""
    authenticate()
    storage_state_path = tmp_path / "session.json"
    settings = Settings(
        sainsburys_username=SecretStr("alan@example.com"),
        sainsburys_storage_state_path=storage_state_path,
    )
    seen_calls: list[tuple[str, str, Path]] = []

    def fake_refresh_session(
        username: str,
        password: str,
        *,
        storage_state_path: Path,
        get_otp: object,
    ) -> None:
        del get_otp
        seen_calls.append((username, password, storage_state_path))

    monkeypatch.setattr(sainsburys, "refresh_session", fake_refresh_session)

    async with Client(
        build_server(settings), elicitation_handler=_accept_with("hunter2")
    ) as client:
        result = await client.call_tool("sainsburys_refresh_session")

    assert seen_calls == [("alan@example.com", "hunter2", storage_state_path)]
    assert str(storage_state_path) in result.data.message


async def test_sainsburys_refresh_session_passes_an_otp_through_get_otp(
    monkeypatch: pytest.MonkeyPatch,
    authenticate: Authenticate,
    tmp_path: Path,
) -> None:
    authenticate()
    settings = Settings(
        sainsburys_username=SecretStr("alan@example.com"),
        sainsburys_storage_state_path=tmp_path / "session.json",
    )
    seen_otps: list[str | None] = []

    def fake_refresh_session(
        username: str,
        password: str,
        *,
        storage_state_path: Path,
        get_otp: Callable[[], str | None],
    ) -> None:
        del username, password, storage_state_path
        seen_otps.append(get_otp())

    monkeypatch.setattr(sainsburys, "refresh_session", fake_refresh_session)

    responses = iter(["hunter2", "123456"])

    async def handler(
        message: str, response_type: object, params: object, context: object
    ) -> str:
        del message, response_type, params, context
        return next(responses)

    async with Client(build_server(settings), elicitation_handler=handler) as client:
        await client.call_tool("sainsburys_refresh_session")

    assert seen_otps == ["123456"]


async def test_sainsburys_refresh_session_refuses_without_username_configured(
    authenticate: Authenticate,
    tmp_path: Path,
) -> None:
    authenticate()
    settings = Settings(
        sainsburys_username=None,
        sainsburys_storage_state_path=tmp_path / "session.json",
        include_error_details=True,
    )

    async with Client(build_server(settings)) as client:
        with pytest.raises(ToolError, match="BROWSER_MCP_SAINSBURYS_USERNAME"):
            await client.call_tool("sainsburys_refresh_session")


async def test_sainsburys_refresh_session_refuses_without_a_storage_state_path(
    authenticate: Authenticate,
) -> None:
    authenticate()
    settings = Settings(
        sainsburys_username=SecretStr("alan@example.com"),
        sainsburys_storage_state_path=None,
        include_error_details=True,
    )

    async with Client(build_server(settings)) as client:
        with pytest.raises(
            ToolError, match="BROWSER_MCP_SAINSBURYS_STORAGE_STATE_PATH"
        ):
            await client.call_tool("sainsburys_refresh_session")


async def test_sainsburys_refresh_session_raises_when_the_password_is_declined(
    monkeypatch: pytest.MonkeyPatch,
    authenticate: Authenticate,
    tmp_path: Path,
) -> None:
    authenticate()
    settings = Settings(
        sainsburys_username=SecretStr("alan@example.com"),
        sainsburys_storage_state_path=tmp_path / "session.json",
        include_error_details=True,
    )

    def fail_if_called(*args: object, **kwargs: object) -> None:
        del args, kwargs
        msg = "refresh_session should not run without a password"
        raise AssertionError(msg)

    monkeypatch.setattr(sainsburys, "refresh_session", fail_if_called)

    async with Client(build_server(settings), elicitation_handler=_decline()) as client:
        with pytest.raises(ToolError, match="No password was provided"):
            await client.call_tool("sainsburys_refresh_session")


async def test_server_is_named_and_documented() -> None:
    async with Client(build_server()) as client:
        initialization = client.initialize_result

    assert initialization.serverInfo.name == SERVER_NAME
    assert initialization.instructions is not None
    assert "rate limited" in initialization.instructions
    assert "authenticated" in initialization.instructions


async def test_calls_are_rate_limited_by_default(authenticate: Authenticate) -> None:
    authenticate()
    # One call per second sustained, with a burst of two, so the third
    # back-to-back call must be rejected.
    settings = Settings(rate_limit_per_second=1.0, rate_limit_burst=2)

    async with Client(build_server(settings)) as client:
        await client.call_tool("server_info")
        await client.call_tool("server_info")

        with pytest.raises(ToolError):
            await client.call_tool("server_info")


async def test_rate_limit_applies_without_explicit_settings(
    monkeypatch: pytest.MonkeyPatch,
    authenticate: Authenticate,
) -> None:
    """A server built straight from the environment is still throttled."""
    authenticate()
    monkeypatch.setenv("BROWSER_MCP_RATE_LIMIT_BURST", "1")

    async with Client(build_server()) as client:
        await client.call_tool("server_info")

        with pytest.raises(ToolError):
            await client.call_tool("server_info")
