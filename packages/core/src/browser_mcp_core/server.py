"""Construction of the FastMCP server, for whichever site is being served."""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from typing import TYPE_CHECKING

from fastmcp import FastMCP
from fastmcp.server.middleware.authorization import AuthMiddleware
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware

from browser_mcp_core.auth import build_auth_provider, github_user_id_is
from browser_mcp_core.login_flow import LoginFlow
from browser_mcp_core.login_oauth import BrowserGithubAuth, login_session_middleware
from browser_mcp_core.login_routes import register_login_routes
from browser_mcp_core.middleware import (
    SecretRedactionMiddleware,
    ToolCallRateLimitingMiddleware,
)
from browser_mcp_core.redaction import build_redactor
from browser_mcp_core.server_info import register_server_info
from browser_mcp_core.settings import CoreSettings

if TYPE_CHECKING:
    from starlette.middleware import Middleware

    from browser_mcp_core.site import LoginCredentials, LoginPage, Site

INSTRUCTIONS = """
Drives a browser session on the operator's behalf using their own credentials.

Over http, every tool call must be authenticated as the one GitHub account
this server belongs to; calls from anybody else are refused.

Only the actions exposed as tools are available; there is no general-purpose
"navigate to this URL" or "run this script" escape hatch. Tool calls are
rate limited, so expect throttling errors if you issue them in a tight loop.
""".strip()

logger = logging.getLogger(__name__)


def _installed_version(distribution: str) -> str:
    """Return the installed distribution's version, or ``0.0.0+unknown``.

    Args:
        distribution: The site package's distribution name.

    Returns:
        The version string to report from ``server_info``.
    """
    try:
        return package_version(distribution)
    except PackageNotFoundError:  # pragma: no cover - only when run from a tree
        return "0.0.0+unknown"


def build_server[SettingsT: CoreSettings](
    site: Site[SettingsT],
    settings: SettingsT,
) -> FastMCP:
    """Build a fully configured server for one site.

    Args:
        site: The site to serve - its tools, its name, and its login page.
        settings: Configuration to use, of the site's own settings type.

    Returns:
        A server with middleware and tools registered, ready to ``run()``.
    """
    version = _installed_version(site.distribution)

    mcp: FastMCP = FastMCP(
        name=site.server_name,
        instructions=INSTRUCTIONS,
        version=version,
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

    # Registered before the site's, so `server_info` is first in `list_tools`.
    register_server_info(mcp, settings, version)
    site.register_tools(mcp, settings, version)
    _maybe_register_login_page(mcp, site, settings)
    return mcp


def _serveable_login_page[SettingsT: CoreSettings](
    site: Site[SettingsT],
    settings: SettingsT,
) -> tuple[LoginPage[SettingsT], LoginCredentials] | None:
    """Return the login page and its credentials, or ``None`` if it cannot run.

    It needs a site that has one, the http transport (for the GitHub OAuth app
    credentials and a public URL), and whatever the site itself requires. On
    stdio, or before those are configured, there is nothing to serve. Returning
    the values rather than a bool keeps this the single place that decides,
    while still narrowing the optional settings for the caller that uses them.

    Args:
        site: The site being served.
        settings: Runtime configuration.

    Returns:
        The login page and the credentials it needs, or ``None``.
    """
    if site.login_page is None or settings.transport != "http":
        return None
    credentials = site.login_page.credentials(settings)
    if credentials is None:
        return None
    return site.login_page, credentials


def http_middleware[SettingsT: CoreSettings](
    site: Site[SettingsT],
    settings: SettingsT,
) -> list[Middleware]:
    """Return the ASGI middleware the http transport needs.

    Separate from ``build_server`` because starlette middleware belongs to the
    ASGI app, which FastMCP builds at ``run`` time rather than at construction
    time.

    Args:
        site: The site being served.
        settings: Runtime configuration.

    Returns:
        The login page's session middleware, or nothing when there is no login
        page to serve.
    """
    serveable = _serveable_login_page(site, settings)
    if serveable is None:
        return []
    login_page, _ = serveable
    return [login_session_middleware(settings, login_page.path)]


def _maybe_register_login_page[SettingsT: CoreSettings](
    mcp: FastMCP,
    site: Site[SettingsT],
    settings: SettingsT,
) -> None:
    """Add the out-of-band login page when it can be served."""
    serveable = _serveable_login_page(site, settings)
    if serveable is None:
        return
    login_page, credentials = serveable
    flow = LoginFlow(
        slug=site.slug,
        display_name=site.display_name,
        worker_module=login_page.worker_module,
        username=credentials.username.get_secret_value(),
        storage_state_path=credentials.storage_state_path,
    )
    register_login_routes(
        mcp,
        flow,
        BrowserGithubAuth(settings, login_page.path),
        login_path=login_page.path,
        display_name=site.display_name,
    )
