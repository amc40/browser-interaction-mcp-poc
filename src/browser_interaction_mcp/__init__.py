"""An unofficial MCP server exposing pre-approved, deterministic browser actions."""

from __future__ import annotations

from browser_interaction_mcp.server import build_server
from browser_interaction_mcp.settings import Settings

__all__ = ["Settings", "build_server"]
