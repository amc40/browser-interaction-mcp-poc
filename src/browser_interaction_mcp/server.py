"""Construction of the FastMCP server."""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version

from fastmcp import FastMCP
from fastmcp.server.middleware.authorization import AuthMiddleware
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware

from browser_interaction_mcp.auth import build_auth_provider, github_user_id_is
from browser_interaction_mcp.middleware import ToolCallRateLimitingMiddleware
from browser_interaction_mcp.settings import Settings
from browser_interaction_mcp.tools import register_tools

SERVER_NAME = "browser-interaction-mcp"

INSTRUCTIONS = """
Drives a browser session on the operator's behalf using their own credentials.

Over http, every tool call must be authenticated as the one GitHub account
this server belongs to; calls from anybody else are refused.

Only the actions exposed as tools are available; there is no general-purpose
"navigate to this URL" or "run this script" escape hatch. Tool calls are
rate limited, so expect throttling errors if you issue them in a tight loop.
""".strip()

logger = logging.getLogger(__name__)


def _installed_version() -> str:
    """Return the installed package version, or ``0.0.0+unknown`` if unknown."""
    try:
        return package_version("browser-interaction-mcp")
    except PackageNotFoundError:  # pragma: no cover - only when run from a tree
        return "0.0.0+unknown"


def build_server(settings: Settings | None = None) -> FastMCP:
    """Build a fully configured server.

    Args:
        settings: Configuration to use. Read from the environment when omitted.

    Returns:
        A server with middleware and tools registered, ready to ``run()``.
    """
    settings = settings if settings is not None else Settings()

    mcp: FastMCP = FastMCP(
        name=SERVER_NAME,
        instructions=INSTRUCTIONS,
        version=_installed_version(),
        mask_error_details=not settings.include_error_details,
        auth=build_auth_provider(settings),
    )

    # Authorisation comes first: an unauthorised caller should not be able to
    # spend the operator's rate-limit budget, and the budget is server-wide.
    mcp.add_middleware(
        AuthMiddleware(auth=github_user_id_is(settings.github_user_id)),
    )
    # Rate limiting is next, so that throttled calls are rejected before any
    # tool - and therefore any browser - is touched.
    mcp.add_middleware(
        ToolCallRateLimitingMiddleware(
            max_calls_per_second=settings.rate_limit_per_second,
            burst_capacity=settings.rate_limit_burst,
        ),
    )
    mcp.add_middleware(
        ErrorHandlingMiddleware(
            logger=logger,
            include_traceback=settings.include_error_details,
        ),
    )

    register_tools(mcp, settings=settings, version=_installed_version())
    return mcp
