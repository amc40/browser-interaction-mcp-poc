"""Who is allowed to use this server.

Both halves of that question are answered by FastMCP rather than by code here.
Authentication — proving a caller is a particular GitHub user — is
``GitHubProvider``, which runs the OAuth flow and verifies the resulting token
against the GitHub API. Authorisation — deciding that the proven identity is
*the* permitted one — is an ``AuthCheck``, the callable FastMCP's
``AuthMiddleware`` applies to every component on the server.

All this module contributes is the one-line policy in :func:`github_user_id_is`.
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


def github_user_id_is(user_id: str) -> AuthCheck:
    """Build a check that passes only for one GitHub account.

    The account is identified by its numeric ID, taken from the token's ``sub``
    claim, rather than by its login. Logins can be changed, and a login freed by
    a rename can be registered by somebody else; the ID never changes hands.

    Args:
        user_id: Numeric ID of the only GitHub account permitted to use the
            server.

    Returns:
        An auth check for ``AuthMiddleware``, which applies it to every tool.
    """

    def check(context: AuthContext) -> bool:
        """Allow the request only if its token belongs to the allowed account.

        Args:
            context: The token, if any, and the component being accessed.

        Returns:
            True, the only way to be authorised.

        Raises:
            AuthorizationError: If the caller is not the allowed GitHub account.
                Raising rather than returning False is how an auth check
                explains itself; the message reaches the client.
        """
        claims = context.token.claims if context.token else {}
        claimed = claims.get("sub")

        if not isinstance(claimed, str) or not claimed:
            msg = (
                "This server is restricted to a single GitHub account, so every "
                "call must present a token verified against the GitHub API."
            )
            raise AuthorizationError(msg)

        if claimed != user_id:
            # Name the rejected account but not the allowed one. Both halves are
            # the caller's own identity, so telling them costs nothing and turns
            # a misconfigured ID into an obvious diagnosis rather than a silent
            # lockout.
            login = claims.get("login")
            msg = (
                f"GitHub account {login!r} (id {claimed}) is not authorised to "
                "use this server."
            )
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
        # Verification is two GitHub API calls, and without a cache that is two
        # per request. See docs/deployment.md for the revocation delay it buys.
        cache_ttl_seconds=settings.github_token_cache_seconds,
    )
