"""Every failed tool call reaches the caller as a category, a retry flag, a reason."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from fake_site import FAKE_SITE, FakeSettings
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from browser_mcp_core.errors import (
    ClassifiedError,
    ErrorCategory,
    NotLoggedInError,
    describe,
)
from browser_mcp_core.middleware import ErrorClassificationMiddleware
from browser_mcp_core.server import INSTRUCTIONS, build_server

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from tests_support import Authenticate


def _server(authenticate: Authenticate, **settings: object) -> FastMCP:
    authenticate()
    return build_server(
        FAKE_SITE,
        FakeSettings(rate_limit_burst=50, **settings),  # type: ignore[arg-type]
    )


async def _failure(server: FastMCP, tool: str) -> str:
    async with Client(server) as client:
        with pytest.raises(ToolError) as raised:
            await client.call_tool(tool)
    return str(raised.value)


def test_a_description_leads_with_category_and_retryability() -> None:
    error = ClassifiedError(
        "The page took too long.", ErrorCategory.TIMEOUT, retryable=True
    )

    lines = describe(error).splitlines()

    assert lines[0] == "[timeout | retryable=true] The page took too long."
    assert "repeat" in lines[1]


def test_every_category_says_what_to_do_next() -> None:
    for category in ErrorCategory:
        error = ClassifiedError("x", category, retryable=False)
        assert len(describe(error).splitlines()) == 2, category


def test_a_missing_login_is_not_retryable_and_says_to_stop() -> None:
    error = NotLoggedInError("Sign in again.")

    assert error.category is ErrorCategory.SITE_LOGIN_REQUIRED
    assert not error.retryable
    assert "stop sending" in describe(error).lower()


async def test_a_classified_error_reaches_the_caller_through_masking(
    authenticate: Authenticate,
) -> None:
    server = _server(authenticate)

    @server.tool
    def slow() -> str:
        """Fail the way a page that never loads does."""
        msg = "Waiting for the results timed out."
        raise ClassifiedError(msg, ErrorCategory.TIMEOUT, retryable=True)

    text = await _failure(server, "slow")

    assert text.startswith("[timeout | retryable=true] Waiting for the results")


async def test_an_unclassified_failure_is_internal_and_keeps_its_detail_private(
    authenticate: Authenticate,
) -> None:
    server = _server(authenticate)

    @server.tool
    def explode() -> str:
        """Fail with a message that could carry anything."""
        msg = "connect to 10.0.0.7:5432 refused"
        raise RuntimeError(msg)

    text = await _failure(server, "explode")

    assert text.startswith("[internal | retryable=false]")
    assert "10.0.0.7" not in text


async def test_an_unclassified_failure_shows_its_detail_when_asked_to(
    authenticate: Authenticate,
) -> None:
    server = _server(authenticate, include_error_details=True)

    @server.tool
    def explode() -> str:
        """Fail with a message the operator has opted in to seeing."""
        msg = "connect to 10.0.0.7:5432 refused"
        raise RuntimeError(msg)

    text = await _failure(server, "explode")

    assert text.startswith("[internal | retryable=false]")
    assert "10.0.0.7:5432 refused" in text


async def test_throttling_is_retryable(authenticate: Authenticate) -> None:
    authenticate()
    server = build_server(
        FAKE_SITE, FakeSettings(rate_limit_per_second=0.1, rate_limit_burst=1)
    )
    async with Client(server) as client:
        await client.call_tool("server_info")
        with pytest.raises(ToolError) as raised:
            await client.call_tool("server_info")

    assert str(raised.value).startswith("[rate_limited | retryable=true]")


async def test_the_wrong_caller_is_not_retryable(authenticate: Authenticate) -> None:
    authenticate(user_id=99999999)
    server = build_server(FAKE_SITE, FakeSettings())

    text = await _failure(server, "server_info")

    assert text.startswith("[caller_not_authorised | retryable=false]")


async def test_bad_arguments_are_the_callers_to_fix(
    authenticate: Authenticate,
) -> None:
    server = _server(authenticate)
    async with Client(server) as client:
        with pytest.raises(ToolError) as raised:
            await client.call_tool("fake_action", {"text": ["not", "a", "string"]})

    assert str(raised.value).startswith("[invalid_request | retryable=false]")


def test_the_instructions_explain_the_format_and_probing_first() -> None:
    assert "retryable=" in INSTRUCTIONS
    assert "site_login_required" in INSTRUCTIONS
    assert "send one" in INSTRUCTIONS


async def _through_classifier(exc: Exception) -> Exception:
    """Run one failing call through the classifier alone; return what escaped."""
    middleware = ErrorClassificationMiddleware(include_details=False)
    context = SimpleNamespace(message=SimpleNamespace(name="a_tool"))

    async def call_next(_: object) -> str:
        raise exc

    with pytest.raises((McpError, ToolError)) as raised:
        await middleware.on_call_tool(context, call_next)  # type: ignore[arg-type]
    return raised.value


async def test_a_protocol_error_keeps_its_own_code() -> None:
    error = McpError(ErrorData(code=-32601, message="Method not found"))

    assert await _through_classifier(error) is error


async def test_a_tool_error_written_for_the_caller_keeps_its_message() -> None:
    escaped = await _through_classifier(ToolError("Pick a smaller quantity."))

    assert str(escaped).startswith(
        "[internal | retryable=false] Pick a smaller quantity."
    )
