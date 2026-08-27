"""The tools exposed over MCP.

Every browser action this server can perform is written out here, in code, and
registered explicitly. Nothing accepts a free-form selector, script or URL from
the caller: a model can only choose *which* pre-approved action runs, never what
that action does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import anyio.from_thread

# Real import, not TYPE_CHECKING: fastmcp resolves tool signatures at runtime
# (get_type_hints) to find which parameter to inject as Context.
from fastmcp import Context  # noqa: TC002
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


class RefreshedSession(BaseModel):
    """Confirmation that a fresh, logged-in Sainsbury's session was saved."""

    message: str = Field(description="Human-readable confirmation of what happened.")


async def _elicit_str(ctx: Context, message: str) -> str | None:
    """Ask the client for one line of text, or ``None`` if it wasn't given.

    A thin wrapper around ``ctx.elicit`` - calling the overloaded method
    directly here, rather than passing it as a bare callable to
    ``anyio.from_thread.run``, is what lets that call site type-check at all.
    Declines and cancellations are both treated as "no answer" rather than
    told apart: both mean the same thing to a caller that only wanted a value
    or nothing.

    Args:
        ctx: The request context to elicit through.
        message: Shown to the person answering.

    Returns:
        What they typed, or ``None`` if they declined, cancelled, or left it
        blank.
    """
    # fastmcp 3.4.7's `elicit` overloads don't resolve correctly under mypy
    # for a plain `str` response_type (confirmed with a standalone repro
    # against Context.elicit directly, independent of this file) - mypy picks
    # the `response_type: None` overload instead. Empirically correct at
    # runtime: exercised end to end by this tool's own tests, which drive a
    # real elicitation round trip through fastmcp's test Client.
    result = await ctx.elicit(message, str)  # type: ignore[arg-type]
    if result.action != "accept" or not result.data:
        return None
    return cast("str", result.data)


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

        Needs an already-authenticated Sainsbury's session: either run
        `scripts/sainsburys_login.py` locally, or call
        `sainsburys_refresh_session` through this same server if you don't
        have routine access to run scripts on wherever it's deployed. Either
        way, `BROWSER_MCP_SAINSBURYS_STORAGE_STATE_PATH` has to point at the
        result. This tool itself never sees a password - it only ever replays
        a session captured one of those two ways. Raises if no session has
        been set up, or if the saved one is no longer accepted.

        Still unverified end to end against a real, authenticated session -
        see `sainsburys.py`'s docstring.
        """
        storage_state_path = settings.sainsburys_storage_state_path
        if storage_state_path is None:
            msg = (
                "BROWSER_MCP_SAINSBURYS_STORAGE_STATE_PATH is not set. Run "
                "scripts/sainsburys_login.py locally, or call "
                "sainsburys_refresh_session, to log in once and capture a "
                "session - then point this setting at the file it saves."
            )
            raise sainsburys.NotLoggedInError(msg)

        product = sainsburys.add_to_basket(query, storage_state_path=storage_state_path)
        return AddedToBasket(product=product)

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "openWorldHint": True,
        },
    )
    def sainsburys_refresh_session(ctx: Context) -> RefreshedSession:
        """Log in to Sainsbury's for real, and save a fresh session to reuse.

        For when there's no routine way to run `scripts/sainsburys_login.py`
        on wherever this server is deployed - a Raspberry Pi behind a tunnel
        is the documented target, and "SSH in and run a script" isn't assumed
        to be available there. This tool drives the real login form itself,
        so - unlike every other tool here - it does handle a password: it
        asks for one through *this client*, via MCP elicitation, not as a
        tool argument, so the value goes straight from you to the server and
        is never part of this conversation or this model's context. The same
        applies to a verification code, if Sainsbury's asks for one (it
        doesn't always). Neither is stored anywhere; only the resulting
        session is, overwriting whatever was at
        `BROWSER_MCP_SAINSBURYS_STORAGE_STATE_PATH`.

        Requires `BROWSER_MCP_SAINSBURYS_USERNAME` and
        `BROWSER_MCP_SAINSBURYS_STORAGE_STATE_PATH` to already be set. See
        `sainsburys.py`'s module docstring for the full reasoning, including
        why this tool is a deliberate, narrow exception to "the server never
        sees a password" everywhere else in this project.
        """
        username = settings.sainsburys_username
        storage_state_path = settings.sainsburys_storage_state_path
        if username is None or storage_state_path is None:
            msg = (
                "BROWSER_MCP_SAINSBURYS_USERNAME and "
                "BROWSER_MCP_SAINSBURYS_STORAGE_STATE_PATH must both be set "
                "before this tool can log in."
            )
            raise sainsburys.NotLoggedInError(msg)

        password = anyio.from_thread.run(
            _elicit_str,
            ctx,
            "Sainsbury's account password, to log in and refresh the saved "
            "session. This goes straight to the server, not into the "
            "conversation.",
        )
        if password is None:
            msg = "No password was provided - not attempting to log in."
            raise sainsburys.NotLoggedInError(msg)

        def get_otp() -> str | None:
            return anyio.from_thread.run(
                _elicit_str,
                ctx,
                "Sainsbury's is asking for a verification code - check your "
                "email or phone and enter it here.",
            )

        sainsburys.refresh_session(
            username.get_secret_value(),
            password,
            storage_state_path=storage_state_path,
            get_otp=get_otp,
        )
        return RefreshedSession(
            message=f"Session refreshed and saved to {storage_state_path}."
        )
