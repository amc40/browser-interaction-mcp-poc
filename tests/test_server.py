"""Tests for the assembled server."""

from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from browser_interaction_mcp.server import SERVER_NAME, build_server
from browser_interaction_mcp.settings import Settings


async def test_server_exposes_only_registered_tools() -> None:
    async with Client(build_server()) as client:
        tools = await client.list_tools()

    assert [tool.name for tool in tools] == ["server_info"]


async def test_server_info_reports_configuration() -> None:
    settings = Settings(rate_limit_per_second=3.5, rate_limit_burst=7)

    async with Client(build_server(settings)) as client:
        result = await client.call_tool("server_info")

    assert result.data.transport == "stdio"
    assert result.data.rate_limit_per_second == 3.5
    assert result.data.rate_limit_burst == 7
    assert result.data.version


async def test_server_is_named_and_documented() -> None:
    async with Client(build_server()) as client:
        initialization = client.initialize_result

    assert initialization.serverInfo.name == SERVER_NAME
    assert initialization.instructions is not None
    assert "rate limited" in initialization.instructions


async def test_calls_are_rate_limited_by_default() -> None:
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
) -> None:
    """A server built straight from the environment is still throttled."""
    monkeypatch.setenv("BROWSER_MCP_RATE_LIMIT_BURST", "1")

    async with Client(build_server()) as client:
        await client.call_tool("server_info")

        with pytest.raises(ToolError):
            await client.call_tool("server_info")
