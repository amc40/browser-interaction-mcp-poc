"""The tools exposed over MCP.

Every browser action this server can perform is written out here, in code, and
registered explicitly. Nothing accepts a free-form selector, script or URL from
the caller: a model can only choose *which* pre-approved action runs, never what
that action does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from browser_interaction_mcp.settings import Settings


class ServerInfo(BaseModel):
    """A description of how the running server is configured."""

    version: str = Field(description="Version of the server package.")
    transport: str = Field(description="Transport the server is listening on.")
    rate_limit_per_second: float = Field(
        description="Sustained tool-call rate allowed per client.",
    )
    rate_limit_burst: int = Field(
        description="Calls a client may make back-to-back before throttling.",
    )


def register_tools(mcp: FastMCP, settings: Settings, version: str) -> None:
    """Register every pre-approved tool on ``mcp``.

    Args:
        mcp: The server to register the tools on.
        settings: Runtime configuration, reported by ``server_info``.
        version: Version of the installed package.
    """

    @mcp.tool(
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def server_info() -> ServerInfo:
        """Report the running server's version and rate limits."""
        return ServerInfo(
            version=version,
            transport=settings.transport,
            rate_limit_per_second=settings.rate_limit_per_second,
            rate_limit_burst=settings.rate_limit_burst,
        )

    # Add browser actions below, one function per action. Keep each one
    # deterministic and parameterised only by values you validate here.
