"""Tests for the assembled server."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 - used at runtime to build paths in tests
from typing import TYPE_CHECKING

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from browser_interaction_mcp import sainsburys
from browser_interaction_mcp.server import SERVER_NAME, build_server
from browser_interaction_mcp.settings import Settings

if TYPE_CHECKING:
    from tests.conftest import Authenticate


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


async def test_sainsburys_add_to_basket_refuses_when_the_session_file_is_missing(
    authenticate: Authenticate,
    tmp_path: Path,
) -> None:
    """A configured-but-absent path is a friendly error, not a raw FileNotFoundError."""
    authenticate()
    settings = Settings(
        sainsburys_storage_state_path=tmp_path / "gone.json",
        include_error_details=True,
    )

    async with Client(build_server(settings)) as client:
        with pytest.raises(ToolError, match="No saved Sainsbury's session"):
            await client.call_tool("sainsburys_add_to_basket")


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
