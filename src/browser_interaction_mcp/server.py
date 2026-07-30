"""Construction of the FastMCP server."""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware

from browser_interaction_mcp.middleware import ToolCallRateLimitingMiddleware
from browser_interaction_mcp.settings import Settings
from browser_interaction_mcp.tools import register_tools

SERVER_NAME = "browser-interaction-mcp"

INSTRUCTIONS = """
Drives a browser session on the operator's behalf using their own credentials.

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
    )

    # Rate limiting is applied first so that throttled calls are rejected before
    # any tool - and therefore any browser - is touched.
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
