"""GitHub sign-in gate for the out-of-band Sainsbury's login page.

FastMCP's ``GitHubProvider`` runs the MCP *client* OAuth handshake (bearer
tokens in headers). This is the separate, plain-browser web flow that guards
``/sainsburys-login``: a person visiting that URL is bounced through GitHub, and
only the one configured ``github_user_id`` is handed a signed session cookie.

The cookie is signed (HMAC-SHA256, keyed off the GitHub client secret) so it
cannot be forged, carries only a subject id and an expiry, and is scoped to the
login path. It gates the page; it grants no action on its own.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import httpx
from starlette.responses import HTMLResponse, RedirectResponse, Response

if TYPE_CHECKING:
    from starlette.requests import Request

    from browser_interaction_mcp.settings import Settings

_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105 - a URL
_USER_URL = "https://api.github.com/user"

LOGIN_PATH = "/sainsburys-login"
CALLBACK_PATH = "/sainsburys-login/auth/callback"

_COOKIE_NAME = "bimcp_login_session"
_SESSION_TTL_SECONDS = 900
_STATE_TTL_SECONDS = 600
_HTTP_TIMEOUT_SECONDS = 10.0


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


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
                token exchange and user lookup.
        """
        if settings.github_client_id is None or settings.github_client_secret is None:
            msg = "BrowserGithubAuth needs the GitHub OAuth app credentials."
            raise ValueError(msg)
        self._client_id = settings.github_client_id
        self._client_secret = settings.github_client_secret.get_secret_value()
        self._allowed_user_id = settings.github_user_id
        self._base_url = settings.oauth_base_url.rstrip("/")
        self._transport = transport
        self._signing_key = hmac.new(
            self._client_secret.encode(), b"sainsburys-login-session", hashlib.sha256
        ).digest()
        self._states: dict[str, float] = {}

    # -- gate ---------------------------------------------------------------

    def authed_user_id(self, request: Request) -> str | None:
        """Return the signed-in GitHub id from the session cookie, or ``None``."""
        token = request.cookies.get(_COOKIE_NAME)
        if not token:
            return None
        try:
            body, signature = token.rsplit(".", 1)
        except ValueError:
            return None
        expected = _b64url(
            hmac.new(self._signing_key, body.encode(), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            payload = json.loads(_b64url_decode(body))
        except ValueError:
            return None
        if float(payload.get("exp", 0)) < time.time():
            return None
        sub = payload.get("sub")
        return sub if isinstance(sub, str) else None

    def begin(self) -> RedirectResponse:
        """Start the OAuth flow: redirect the browser to GitHub."""
        self._sweep_states()
        state = secrets.token_urlsafe(24)
        self._states[state] = time.monotonic() + _STATE_TTL_SECONDS
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": f"{self._base_url}{CALLBACK_PATH}",
                "state": state,
                "scope": "",
                "allow_signup": "false",
            }
        )
        return RedirectResponse(f"{_AUTHORIZE_URL}?{query}")

    async def complete(self, request: Request) -> Response:
        """Handle GitHub's callback: verify identity, set the session cookie."""
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        if not code or not state or self._states.pop(state, None) is None:
            return self._deny("That sign-in link has expired. Reload the page.")
        self._sweep_states()

        try:
            user_id = await self._exchange(code)
        except (httpx.HTTPError, ValueError, KeyError):
            return self._deny("GitHub sign-in failed. Try again.")

        if user_id != self._allowed_user_id:
            return self._deny("This GitHub account is not allowed to use this page.")

        response = RedirectResponse(LOGIN_PATH, status_code=303)
        response.set_cookie(
            _COOKIE_NAME,
            self._issue_cookie(user_id),
            max_age=_SESSION_TTL_SECONDS,
            httponly=True,
            secure=self._base_url.startswith("https://"),
            samesite="strict",
            path=LOGIN_PATH,
        )
        return response

    # -- internals --------------------------------------------------------

    async def _exchange(self, code: str) -> str:
        async with httpx.AsyncClient(
            transport=self._transport, timeout=_HTTP_TIMEOUT_SECONDS
        ) as client:
            token_response = await client.post(
                _TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "code": code,
                },
                headers={"Accept": "application/json"},
            )
            token_response.raise_for_status()
            access_token = token_response.json()["access_token"]

            user_response = await client.get(
                _USER_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            user_response.raise_for_status()
            return str(user_response.json()["id"])

    def _issue_cookie(self, user_id: str) -> str:
        body = _b64url(
            json.dumps(
                {"sub": user_id, "exp": time.time() + _SESSION_TTL_SECONDS}
            ).encode()
        )
        signature = _b64url(
            hmac.new(self._signing_key, body.encode(), hashlib.sha256).digest()
        )
        return f"{body}.{signature}"

    def _sweep_states(self) -> None:
        now = time.monotonic()
        self._states = {s: exp for s, exp in self._states.items() if exp > now}

    @staticmethod
    def _deny(message: str) -> HTMLResponse:
        return HTMLResponse(
            f"<!doctype html><meta charset=utf-8><title>Sign-in</title>"
            f"<body style='font-family:system-ui;margin:3rem auto;max-width:28rem'>"
            f"<p>{message}</p></body>",
            status_code=403,
        )
