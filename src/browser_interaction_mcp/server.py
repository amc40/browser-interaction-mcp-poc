"""Construction of the FastMCP server."""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from typing import TYPE_CHECKING

from fastmcp import FastMCP
from fastmcp.server.middleware.authorization import AuthMiddleware
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware

from browser_interaction_mcp.auth import build_auth_provider, github_user_id_is
from browser_interaction_mcp.login_oauth import (
    BrowserGithubAuth,
    login_session_middleware,
)
from browser_interaction_mcp.login_routes import register_login_routes
from browser_interaction_mcp.middleware import (
    SecretRedactionMiddleware,
    ToolCallRateLimitingMiddleware,
)
from browser_interaction_mcp.redaction import build_redactor
from browser_interaction_mcp.sainsburys_login_flow import SainsburysLoginFlow
from browser_interaction_mcp.settings import Settings
from browser_interaction_mcp.tools import register_tools

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import SecretStr
    from starlette.middleware import Middleware

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

    # Redaction wraps everything, so that a secret cannot escape through an
    # error raised by any layer below it. It admits and rejects nobody, so it
    # does not disturb the ordering the two middlewares below rely on.
    mcp.add_middleware(SecretRedactionMiddleware(build_redactor(settings)))
    # Authorisation comes next: an unauthorised caller should not be able to
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
    _maybe_register_login_page(mcp, settings)
    return mcp


def _login_page_settings(settings: Settings) -> tuple[SecretStr, Path] | None:
    """Return the settings the login page needs, or ``None`` if it cannot run.

    It needs the http transport (for the GitHub OAuth app credentials and a
    public URL) and both Sainsbury's settings. On stdio, or before those are
    configured, there is nothing to serve. Returning the values rather than a
    bool keeps this the single place that decides, while still narrowing the
    optional settings for the caller that uses them.

    Args:
        settings: Runtime configuration.

    Returns:
        The Sainsbury's username and storage-state path, or ``None``.
    """
    if (
        settings.transport != "http"
        or settings.sainsburys_username is None
        or settings.sainsburys_storage_state_path is None
    ):
        return None
    return settings.sainsburys_username, settings.sainsburys_storage_state_path


def http_middleware(settings: Settings) -> list[Middleware]:
    """Return the ASGI middleware the http transport needs.

    Separate from ``build_server`` because starlette middleware belongs to the
    ASGI app, which FastMCP builds at ``run`` time rather than at construction
    time.

    Args:
        settings: Runtime configuration.

    Returns:
        The login page's session middleware, or nothing when there is no login
        page to serve.
    """
    if _login_page_settings(settings) is None:
        return []
    return [login_session_middleware(settings)]


def _maybe_register_login_page(mcp: FastMCP, settings: Settings) -> None:
    """Add the out-of-band Sainsbury's login page when it can be served."""
    configured = _login_page_settings(settings)
    if configured is None:
        return
    username, storage_state_path = configured
    flow = SainsburysLoginFlow(
        username=username.get_secret_value(),
        storage_state_path=storage_state_path,
    )
    register_login_routes(mcp, flow, BrowserGithubAuth(settings))
