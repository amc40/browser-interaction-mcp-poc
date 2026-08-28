"""The tools exposed over MCP.

Every browser action this server can perform is written out here, in code, and
registered explicitly. Nothing accepts a free-form selector, script or URL from
the caller: a model can only choose *which* pre-approved action runs, never what
that action does.

Refreshing the Sainsbury's session is deliberately *not* a tool. It needs a
password, and Claude.ai's MCP client cannot collect one out of band, so it is
done through a browser page instead - see
:mod:`browser_interaction_mcp.login_routes`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from browser_interaction_mcp import sainsburys

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


class ProductsWeLove(BaseModel):
    """Product names under Sainsbury's groceries homepage "Products we love"."""

    products: list[str] = Field(
        description='Product names, in the order shown under "Products we love".',
    )


class AddedToBasket(BaseModel):
    """Confirmation that a product was added to the Sainsbury's basket."""

    product: str = Field(description="Name of the product added, as shown on its page.")


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

    @mcp.tool(
        annotations={"readOnlyHint": True, "openWorldHint": True},
    )
    def sainsburys_products_we_love() -> ProductsWeLove:
        """Return the first 5 product names under Sainsbury's "Products we love".

        Reads the public, unauthenticated groceries homepage
        (sainsburys.co.uk/gol-ui/groceries) - no login or credentials involved.
        """
        return ProductsWeLove(products=sainsburys.products_we_love())

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "openWorldHint": True,
        },
    )
    def sainsburys_add_to_basket(
        query: str = sainsburys.DEFAULT_SEARCH_QUERY,
    ) -> AddedToBasket:
        """Search Sainsbury's and add the first result to the basket.

        `query` is only ever typed into Sainsbury's own site search, exactly
        as a person would - it cannot reach a page, selector or script this
        server hasn't approved in code.

        Needs an already-authenticated Sainsbury's session. Capture one by
        running `scripts/sainsburys_login.py` locally, or - on a deployed
        server - by visiting `<server>/sainsburys-login` in a browser and
        signing in there. Either way `BROWSER_MCP_SAINSBURYS_STORAGE_STATE_PATH`
        has to point at the result. This tool itself never sees a password -
        it only replays a captured session, and raises if none is set up or
        the saved one is no longer accepted.
        """
        storage_state_path = settings.sainsburys_storage_state_path
        if storage_state_path is None or not storage_state_path.is_file():
            msg = (
                "No saved Sainsbury's session. Run scripts/sainsburys_login.py "
                "locally, or visit /sainsburys-login on this server to sign in, "
                "then point BROWSER_MCP_SAINSBURYS_STORAGE_STATE_PATH at the file "
                "it writes."
            )
            raise sainsburys.NotLoggedInError(msg)

        product = sainsburys.add_to_basket(query, storage_state_path=storage_state_path)
        return AddedToBasket(product=product)
