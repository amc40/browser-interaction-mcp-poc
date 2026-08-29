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

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from browser_interaction_mcp import sainsburys

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

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


class ProductSearchResult(BaseModel):
    """One product returned by a Sainsbury's search, unadded."""

    name: str = Field(description="Product name, exactly as shown on its result tile.")
    image_url: str | None = Field(
        description=(
            "URL of the product's image, if one could be read from its result tile."
        ),
    )


class ProductSearchResults(BaseModel):
    """The top matches for a Sainsbury's search, for a caller to choose from."""

    results: list[ProductSearchResult] = Field(
        description="Matches, in the order Sainsbury's results page lists them.",
    )


class AddedToBasket(BaseModel):
    """Confirmation that a product was added to the Sainsbury's basket."""

    product: str = Field(description="Name of the product added, as shown on its page.")


def _guard_session[T](action: Callable[[], T]) -> T:
    """Run a browser action, surfacing a missing or stale session usefully.

    `sainsburys.NotLoggedInError` is a plain `RuntimeError`, so a server with
    error masking on - every deployed one - would collapse its message to a
    bare "internal error". Re-raising as `ToolError` keeps the message (which
    tells the caller to get the operator to re-authenticate at
    `/sainsburys-login`) intact through the mask.
    """
    try:
        return action()
    except sainsburys.NotLoggedInError as exc:
        raise ToolError(str(exc)) from exc


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

    def _require_storage_state() -> Path:
        """Return the configured session path, or raise if none is usable."""
        storage_state_path = settings.sainsburys_storage_state_path
        if storage_state_path is None or not storage_state_path.is_file():
            msg = (
                "No saved Sainsbury's session. Run scripts/sainsburys_login.py "
                "locally, or visit /sainsburys-login on this server to sign in, "
                "then point BROWSER_MCP_SAINSBURYS_STORAGE_STATE_PATH at the file "
                "it writes."
            )
            raise sainsburys.NotLoggedInError(msg)
        return storage_state_path

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
        annotations={"readOnlyHint": True, "openWorldHint": True},
    )
    def sainsburys_search(
        query: str = sainsburys.DEFAULT_SEARCH_QUERY,
    ) -> ProductSearchResults:
        """Search Sainsbury's and return the top 5 matches, without adding any.

        Nothing is added to the basket - call this first to see what a
        query actually matches, then pass one result's `name` *exactly* to
        `sainsburys_add_to_basket`. Present the results to the person as a
        Markdown list with each `image_url` inlined
        (`![name](image_url)`) so they can see titles and pictures before
        choosing, rather than picking on their behalf.

        `query` is only ever typed into Sainsbury's own site search, exactly
        as a person would - it cannot reach a page, selector or script this
        server hasn't approved in code.

        Needs an already-authenticated Sainsbury's session - see
        `sainsburys_add_to_basket`'s docstring for how to capture one.
        """
        product_matches = _guard_session(
            lambda: sainsburys.search_products(
                query, storage_state_path=_require_storage_state()
            ),
        )
        return ProductSearchResults(
            results=[
                ProductSearchResult(name=match.name, image_url=match.image_url)
                for match in product_matches
            ],
        )

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "openWorldHint": True,
        },
    )
    def sainsburys_add_to_basket(product_name: str) -> AddedToBasket:
        """Search Sainsbury's for `product_name` and add the exact match.

        `product_name` must be the exact name of one of `sainsburys_search`'s
        results (whitespace aside) - not a description, not an index. An
        index isn't accepted because it can go stale between the two calls
        (the site can re-rank or re-stock in between); the exact name can't.
        Call `sainsburys_search` first if you don't already have one.

        If the name you have was itself cut short somewhere and ends in
        "..." or "…" (e.g. it was truncated by whatever displayed
        `sainsburys_search`'s results to you), pass it as-is rather than
        guessing at the rest - it's matched as a prefix against the real
        result, and the response reports the product's real, full name.

        `product_name` is only ever typed into Sainsbury's own site search,
        exactly as a person would - it cannot reach a page, selector or
        script this server hasn't approved in code.

        Needs an already-authenticated Sainsbury's session. Capture one by
        running `scripts/sainsburys_login.py` locally, or - on a deployed
        server - by visiting `<server>/sainsburys-login` in a browser and
        signing in there. Either way `BROWSER_MCP_SAINSBURYS_STORAGE_STATE_PATH`
        has to point at the result. This tool itself never sees a password -
        it only replays a captured session, and raises if none is set up or
        the saved one is no longer accepted.
        """
        product = _guard_session(
            lambda: sainsburys.add_to_basket(
                product_name, storage_state_path=_require_storage_state()
            ),
        )
        return AddedToBasket(product=product)
