"""Site-agnostic core for MCP servers exposing pre-approved browser actions."""

from __future__ import annotations

from browser_mcp_core.errors import NotLoggedInError
from browser_mcp_core.server import build_server, http_middleware
from browser_mcp_core.settings import CoreSettings
from browser_mcp_core.site import LoginCredentials, LoginPage, Site

__all__ = [
    "CoreSettings",
    "LoginCredentials",
    "LoginPage",
    "NotLoggedInError",
    "Site",
    "build_server",
    "http_middleware",
]
