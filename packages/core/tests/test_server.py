"""Tests for the assembled server, against a fake site."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fake_site import FAKE_SERVER_NAME, FAKE_SITE, FakeSettings
from fastmcp import Client
from fastmcp.exceptions import ToolError

from browser_mcp_core.server import build_server

if TYPE_CHECKING:
    from tests_support import Authenticate


async def test_server_exposes_only_registered_tools(
    authenticate: Authenticate,
) -> None:
    authenticate()

    async with Client(build_server(FAKE_SITE, FakeSettings())) as client:
        tools = await client.list_tools()

    # server_info is core's; everything after it is the site's, in the order
    # its register_tools added them.
    assert [tool.name for tool in tools] == ["server_info", "fake_action"]


async def test_server_info_reports_configuration(authenticate: Authenticate) -> None:
    authenticate()
    settings = FakeSettings(rate_limit_per_second=3.5, rate_limit_burst=7)

    async with Client(build_server(FAKE_SITE, settings)) as client:
        result = await client.call_tool("server_info")

    assert result.data.transport == "stdio"
    assert result.data.rate_limit_per_second == 3.5
    assert result.data.rate_limit_burst == 7
    assert result.data.version


async def test_server_is_named_and_documented() -> None:
    async with Client(build_server(FAKE_SITE, FakeSettings())) as client:
        initialization = client.initialize_result

    assert initialization.serverInfo.name == FAKE_SERVER_NAME
    assert initialization.instructions is not None
    assert "rate limited" in initialization.instructions
    assert "authenticated" in initialization.instructions


async def test_calls_are_rate_limited_by_default(authenticate: Authenticate) -> None:
    authenticate()
    # One call per second sustained, with a burst of two, so the third
    # back-to-back call must be rejected.
    settings = FakeSettings(rate_limit_per_second=1.0, rate_limit_burst=2)

    async with Client(build_server(FAKE_SITE, settings)) as client:
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

    async with Client(build_server(FAKE_SITE, FakeSettings())) as client:
        await client.call_tool("server_info")

        with pytest.raises(ToolError):
            await client.call_tool("server_info")
