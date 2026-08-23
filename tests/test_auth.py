"""Tests for who is allowed to use the server.

These drive a real server through a real client, so what they assert is what an
MCP client would actually see.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server import context
from fastmcp.server.auth.providers.github import GitHubProvider, GitHubTokenVerifier
from pydantic import SecretStr

from browser_interaction_mcp.auth import build_auth_provider
from browser_interaction_mcp.server import build_server
from browser_interaction_mcp.settings import Settings

if TYPE_CHECKING:
    from fastmcp.utilities.token_cache import TokenCache

    from tests.conftest import Authenticate


async def _call_server_info(settings: Settings | None = None) -> None:
    """Call the one registered tool, letting any error propagate."""
    async with Client(build_server(settings)) as client:
        await client.call_tool("server_info")


async def test_the_allowed_account_may_call_tools(authenticate: Authenticate) -> None:
    authenticate("36701168")

    await _call_server_info()


async def test_a_caller_with_no_token_is_refused() -> None:
    with pytest.raises(ToolError, match="restricted to a single GitHub account"):
        await _call_server_info()


async def test_another_github_account_is_refused(authenticate: Authenticate) -> None:
    authenticate("99999999", login="someone-else")

    with pytest.raises(ToolError, match="'someone-else' \\(id 99999999\\)"):
        await _call_server_info()


async def test_the_allowed_account_is_never_disclosed(
    authenticate: Authenticate,
) -> None:
    """A rejected caller learns that they failed, not who would have passed."""
    authenticate("99999999", login="someone-else")

    with pytest.raises(ToolError) as refusal:
        await _call_server_info()

    assert "36701168" not in str(refusal.value)


async def test_a_renamed_login_does_not_change_who_is_authorised(
    authenticate: Authenticate,
) -> None:
    """The whole reason the check is on the ID: logins are not identities."""
    authenticate("36701168", login="renamed-since")

    await _call_server_info()


async def test_taking_over_a_freed_login_does_not_grant_access(
    authenticate: Authenticate,
) -> None:
    """Someone who registers a login the operator abandoned is still a stranger."""
    authenticate("99999999", login="amc40")

    with pytest.raises(ToolError, match="is not authorised"):
        await _call_server_info()


async def test_a_token_without_a_subject_claim_is_refused(
    authenticate: Authenticate,
) -> None:
    """A verified token that is not a GitHub one proves nothing here."""
    authenticate(None)

    with pytest.raises(ToolError, match="restricted to a single GitHub account"):
        await _call_server_info()


@pytest.mark.parametrize("subject", [36701168, "", None])
async def test_a_malformed_subject_claim_is_refused(
    authenticate: Authenticate,
    subject: object,
) -> None:
    authenticate(subject)

    with pytest.raises(ToolError, match="restricted to a single GitHub account"):
        await _call_server_info()


async def test_the_allowed_account_is_configurable(
    authenticate: Authenticate,
) -> None:
    authenticate("12345")

    await _call_server_info(Settings(github_user_id="12345"))


async def test_unauthorised_callers_are_not_shown_the_tools() -> None:
    """``AuthMiddleware`` filters the listing as well as guarding the calls."""
    async with Client(build_server()) as client:
        assert await client.list_tools() == []


async def test_authorised_callers_are_shown_the_tools(
    authenticate: Authenticate,
) -> None:
    authenticate()

    async with Client(build_server()) as client:
        tools = await client.list_tools()

    # The exact registered set is test_server.py's concern; this only checks
    # that an authorised caller is shown tools at all (contrast with
    # test_unauthorised_callers_are_shown_no_tools above).
    assert tools


async def test_authorisation_precedes_rate_limiting(
    authenticate: Authenticate,
) -> None:
    """Refused callers must not be able to exhaust the shared rate-limit budget.

    The bucket is server-wide, so a stranger who spent it would deny the
    operator their own server.
    """
    server = build_server(Settings(rate_limit_burst=1))

    async with Client(server) as client:
        for _ in range(5):
            with pytest.raises(ToolError):
                await client.call_tool("server_info")

    # The same server, and so the same bucket: it is untouched, so the operator
    # still gets the one call they are entitled to.
    authenticate()
    async with Client(server) as client:
        await client.call_tool("server_info")


async def test_stdio_callers_are_waved_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The accepted cost of docs/sdr/0001, pinned so an upgrade cannot move it.

    ``AuthMiddleware`` skips its checks when the request arrived over stdio,
    which has no way to carry a token. Anyone with local shell access can
    therefore drive the server, and this asserts exactly that.
    """
    monkeypatch.setattr(context, "_current_transport", ContextVar("transport"))
    context._current_transport.set("stdio")  # noqa: SLF001

    await _call_server_info()


def test_http_transport_authenticates_against_github() -> None:
    settings = Settings(
        transport="http",
        github_client_id="Ov23liExample",
        github_client_secret=SecretStr("not-a-real-secret"),
        port=9001,
    )

    provider = build_auth_provider(settings)

    assert isinstance(provider, GitHubProvider)
    assert str(provider.base_url).startswith("http://127.0.0.1:9001")


def _token_cache(provider: GitHubProvider) -> TokenCache:
    """Return the cache the provider verifies tokens through."""
    verifier = provider._token_validator  # noqa: SLF001
    assert isinstance(verifier, GitHubTokenVerifier)
    return verifier._cache  # noqa: SLF001


def _http_settings(**overrides: object) -> Settings:
    return Settings(
        transport="http",
        github_client_id="Ov23liExample",
        github_client_secret=SecretStr("not-a-real-secret"),
        **overrides,  # type: ignore[arg-type]
    )


def test_token_verification_is_cached_by_default() -> None:
    """Uncached, every request costs two GitHub API calls."""
    provider = build_auth_provider(_http_settings())

    assert provider is not None
    assert _token_cache(provider).enabled


def test_the_cache_lifetime_is_configurable() -> None:
    provider = build_auth_provider(_http_settings(github_token_cache_seconds=60))

    assert provider is not None
    assert _token_cache(provider)._ttl == 60  # noqa: SLF001


def test_caching_can_be_turned_off() -> None:
    """Zero trades the GitHub API budget back for instant revocation."""
    provider = build_auth_provider(_http_settings(github_token_cache_seconds=0))

    assert provider is not None
    assert not _token_cache(provider).enabled


def test_stdio_transport_has_no_auth_provider() -> None:
    """Stdio has nowhere to put a bearer token, so no OAuth flow is possible."""
    assert build_auth_provider(Settings()) is None


def test_stdio_is_left_unauthenticated_loudly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The trade-off in docs/sdr/0001 is silent unless startup says so."""
    with caplog.at_level("WARNING"):
        build_auth_provider(Settings())

    assert "cannot authenticate callers" in caplog.text
