"""Tests for the assembled server."""

from __future__ import annotations

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
