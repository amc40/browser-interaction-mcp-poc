"""Who is allowed to use this server.

Both halves of that question are answered by FastMCP rather than by code here.
Authentication — proving a caller is a particular GitHub user — is
``GitHubProvider``, which runs the OAuth flow and verifies the resulting token
against the GitHub API. Authorisation — deciding that the proven identity is
*the* permitted one — is an ``AuthCheck``, the callable FastMCP's
``AuthMiddleware`` applies to every component on the server.

All this module contributes is the one-line policy in :func:`github_login_is`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastmcp.exceptions import AuthorizationError
from fastmcp.server.auth.providers.github import GitHubProvider

if TYPE_CHECKING:
    from fastmcp.server.auth import AuthCheck, AuthContext

    from browser_interaction_mcp.settings import Settings

logger = logging.getLogger(__name__)


def github_login_is(login: str) -> AuthCheck:
    """Build a check that passes only for one GitHub account.

    Args:
        login: The only GitHub login permitted to use the server. Matched
            case-insensitively, as GitHub logins themselves are.

    Returns:
        An auth check for ``AuthMiddleware``, which applies it to every tool.
    """
    allowed = login.casefold()

    def check(context: AuthContext) -> bool:
        """Allow the request only if its token belongs to the allowed user.

        Args:
            context: The token, if any, and the component being accessed.

        Returns:
            True, the only way to be authorised.

        Raises:
            AuthorizationError: If the caller is not the allowed GitHub user.
                Raising rather than returning False is how an auth check
                explains itself; the message reaches the client.
        """
        claimed = context.token.claims.get("login") if context.token else None

        if not isinstance(claimed, str):
            msg = (
                "This server is restricted to a single GitHub account, so every "
                "call must present a token verified against the GitHub API."
            )
            raise AuthorizationError(msg)

        if claimed.casefold() != allowed:
            # Name the rejected login but not the allowed one: the caller
            # already knows who they are, and does not need to be told whose
            # account would have worked.
            msg = f"GitHub user {claimed!r} is not authorised to use this server."
            raise AuthorizationError(msg)

        return True

    return check


def build_auth_provider(settings: Settings) -> GitHubProvider | None:
    """Build the GitHub OAuth provider, if the transport can carry one.

    Args:
        settings: Runtime configuration.

    Returns:
        A provider that verifies bearer tokens against the GitHub API, or
        ``None`` on stdio, which has nowhere to put a bearer token.
    """
    client_id = settings.github_client_id
    client_secret = settings.github_client_secret

    if settings.transport != "http" or client_id is None or client_secret is None:
        # Settings validation rejects an http transport with no OAuth app, so
        # in practice this is the stdio case. See docs/sdr/0001 for why stdio
        # is left unauthenticated rather than being refused outright.
        logger.warning(
            "Serving over %s, which cannot authenticate callers: anyone with "
            "local shell access can drive this server. Serve over http to "
            "require a GitHub login.",
            settings.transport,
        )
        return None

    return GitHubProvider(
        client_id=client_id,
        client_secret=client_secret.get_secret_value(),
        base_url=settings.oauth_base_url,
    )
