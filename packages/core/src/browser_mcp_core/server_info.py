"""The one tool core registers itself.

``server_info`` reports how the running server is configured. It is not a
browser action against anything - it never opens a page - so it is not part of
the approval surface a site owns, and it would be identical in every site's
``tools.py``. D2's "extract now, because it contains no site-specific content
and never will" applies to it exactly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from browser_mcp_core.settings import CoreSettings


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


def register_server_info(mcp: FastMCP, settings: CoreSettings, version: str) -> None:
    """Register the ``server_info`` tool on ``mcp``.

    Args:
        mcp: The server to register the tool on.
        settings: Runtime configuration, reported by the tool.
        version: Version of the installed site distribution.
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
