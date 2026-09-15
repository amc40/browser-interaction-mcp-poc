"""GitHub sign-in gate for the out-of-band Sainsbury's login page.

FastMCP's ``GitHubProvider`` runs the MCP *client* OAuth handshake (bearer
tokens in headers). This is the separate, plain-browser web flow that guards
``/sainsburys-login``: a person visiting that URL is bounced through GitHub, and
only the one configured ``github_user_id`` is let in.

Neither half of the security-critical plumbing is this module's to get right.
The handshake is Authlib's; the session is starlette's ``SessionMiddleware``,
which signs the cookie and bounds its lifetime. What is left here is the policy:
which account is allowed, and what the page does about the ones that are not.

**The CSRF state must stay in the session, never in an Authlib ``cache``.**
Authlib's cache-backed state storage does not tie a state value to the browser
that started the flow, which is a one-click account-takeover CSRF - Snyk found
it, and it is CVE-2025-68158 and CVE-2026-41425. The session-backed path used
here is the one those advisories name as safe. It is also the bug this module
used to have: the state lived in a process-global dict, and only the
single-account check below stopped it from mattering.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, override

import httpx
from authlib.common.errors import AuthlibBaseError
from authlib.integrations.base_client.errors import MismatchingStateError
from authlib.integrations.starlette_client import OAuth
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import HTMLResponse, RedirectResponse, Response

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.types import ASGIApp, Receive, Scope, Send

    from browser_interaction_mcp.settings import Settings

LOGIN_PATH = "/sainsburys-login"
CALLBACK_PATH = "/sainsburys-login/auth/callback"

_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105 - a URL
_API_BASE_URL = "https://api.github.com/"

_SESSION_KEY = "github_user_id"
_SESSION_TTL_SECONDS = 900
_HTTP_TIMEOUT_SECONDS = 10.0


class ScopedSessionMiddleware(SessionMiddleware):
    """``SessionMiddleware``, but active only under one path prefix.

    Starlette sessions are ASGI middleware, so the only place to register one is
    the whole app - and the whole app here is mostly the MCP endpoint, which
    authenticates with bearer tokens in headers and has no use for a cookie
    session. Bypassing by path keeps ``scope["session"]`` from existing at all
    on an MCP request, so this middleware's blast radius is visible in the code
    rather than argued about in a comment.
    """

    def __init__(  # noqa: PLR0913 - each one is a distinct cookie attribute
        self,
        app: ASGIApp,
        *,
        prefix: str,
        secret_key: str,
        session_cookie: str,
        path: str,
        max_age: int,
        same_site: Literal["lax", "strict", "none"],
        https_only: bool,
    ) -> None:
        """Wrap ``app``.

        Args:
            app: The application to wrap.
            prefix: Path prefix whose requests get a session. Everything else
                bypasses this middleware entirely.
            secret_key: Key the session cookie is signed with.
            session_cookie: Name of the session cookie.
            path: ``Path`` attribute set on the cookie.
            max_age: How long the cookie stays valid, in seconds.
            same_site: ``SameSite`` attribute set on the cookie.
            https_only: Whether to set the ``Secure`` attribute.
        """
        super().__init__(
            app,
            secret_key=secret_key,
            session_cookie=session_cookie,
            path=path,
            max_age=max_age,
            same_site=same_site,
            https_only=https_only,
        )
        self._prefix = prefix

    @override
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle the request with a session, or hand it straight on without."""
        path = scope.get("path", "")
        # A path-segment match, not a bare prefix: `/sainsburys-login-elsewhere`
        # is a different route and has no business being handed a session.
        under_prefix = path == self._prefix or path.startswith(f"{self._prefix}/")
        if scope["type"] == "http" and under_prefix:
            await super().__call__(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def login_session_middleware(settings: Settings) -> Middleware:
    """Build the signed-cookie session the login page's gate rides on.

    The cookie is keyed off the GitHub client secret rather than a setting of
    its own, so rotating the OAuth app's secret also invalidates every open
    login session - which is the behaviour you want from a rotation.

    Args:
        settings: Provides the signing key and whether the public URL is https.

    Returns:
        Middleware to hand to ``FastMCP.run``.

    Raises:
        ValueError: If the GitHub OAuth app credentials are not configured.
    """
    if settings.github_client_secret is None:
        msg = "The login page's session needs the GitHub OAuth app credentials."
        raise ValueError(msg)
    return Middleware(
        ScopedSessionMiddleware,
        prefix=LOGIN_PATH,
        secret_key=settings.github_client_secret.get_secret_value(),
        session_cookie="bimcp_login_session",
        # The cookie's own Path, so a browser never sends it to the MCP endpoint.
        path=LOGIN_PATH,
        max_age=_SESSION_TTL_SECONDS,
        same_site="strict",
        https_only=settings.oauth_base_url.startswith("https://"),
    )


class BrowserGithubAuth:
    """The browser-side GitHub OAuth gate for the login page."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Configure the gate.

        Args:
            settings: Provides the OAuth app credentials, the public base URL,
                and the one allowed ``github_user_id``.
            transport: Seam for tests - an ``httpx`` transport used for the
                token exchange and the identity lookup.

        Raises:
            ValueError: If the GitHub OAuth app credentials are not configured.
        """
        if settings.github_client_id is None or settings.github_client_secret is None:
            msg = "BrowserGithubAuth needs the GitHub OAuth app credentials."
            raise ValueError(msg)
        self._allowed_user_id = settings.github_user_id
        base_url = settings.oauth_base_url.rstrip("/")
        self._redirect_uri = f"{base_url}{CALLBACK_PATH}"

        oauth = OAuth()
        oauth.register(
            name="github",
            client_id=settings.github_client_id,
            client_secret=settings.github_client_secret.get_secret_value(),
            authorize_url=_AUTHORIZE_URL,
            access_token_url=_TOKEN_URL,
            api_base_url=_API_BASE_URL,
            client_kwargs={
                # No scope at all. All this gate reads is the account's id, and
                # GitHub returns that on any authenticated /user call whatever
                # was granted - see the same reasoning in auth.py.
                "scope": "",
                # GitHub documents client_id/client_secret in the form body.
                # Authlib would otherwise default to HTTP Basic, which GitHub
                # also accepts, but there is no reason to differ from the docs.
                "token_endpoint_auth_method": "client_secret_post",
                "transport": transport,
                "timeout": _HTTP_TIMEOUT_SECONDS,
            },
        )
        self._github = oauth.github

    def authed_user_id(self, request: Request) -> str | None:
        """Return the signed-in GitHub id from the session, or ``None``.

        Args:
            request: The request whose session to read.

        Returns:
            The allowed account's id if this request carries a live session for
            it, otherwise ``None``.
        """
        stored = request.session.get(_SESSION_KEY)
        return stored if stored == self._allowed_user_id else None

    async def begin(self, request: Request) -> Response:
        """Start the OAuth flow: redirect the browser to GitHub.

        Args:
            request: The request to stash the CSRF state on the session of.

        Returns:
            A redirect to GitHub's authorize endpoint.
        """
        redirect: Response = await self._github.authorize_redirect(
            request, self._redirect_uri, allow_signup="false"
        )
        return redirect

    async def complete(self, request: Request) -> Response:
        """Handle GitHub's callback: verify identity, open the session.

        Args:
            request: GitHub's callback, carrying ``code`` and ``state``.

        Returns:
            A redirect back to the login page, or a 403 explaining the refusal.
        """
        try:
            token = await self._github.authorize_access_token(request)
        except MismatchingStateError:
            # The state is absent, stale, or belongs to a different browser.
            return self._deny("That sign-in link has expired. Reload the page.")
        except (AuthlibBaseError, httpx.HTTPError):
            return self._deny("GitHub sign-in failed. Try again.")

        try:
            response = await self._github.get(
                "user", token=token, headers={"Accept": "application/vnd.github+json"}
            )
            response.raise_for_status()
            user_id = str(response.json()["id"])
        except (AuthlibBaseError, httpx.HTTPError, KeyError, ValueError):
            return self._deny("GitHub sign-in failed. Try again.")

        if user_id != self._allowed_user_id:
            return self._deny("This GitHub account is not allowed to use this page.")

        request.session[_SESSION_KEY] = user_id
        return RedirectResponse(LOGIN_PATH, status_code=303)

    @staticmethod
    def _deny(message: str) -> HTMLResponse:
        return HTMLResponse(
            f"<!doctype html><meta charset=utf-8><title>Sign-in</title>"
            f"<body style='font-family:system-ui;margin:3rem auto;max-width:28rem'>"
            f"<p>{message}</p></body>",
            status_code=403,
        )
