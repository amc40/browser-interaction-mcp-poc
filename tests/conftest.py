"""Shared test fixtures."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Protocol

import pytest
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

from browser_interaction_mcp.settings import DEFAULT_GITHUB_USER_ID

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolated_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Keep the developer's own configuration out of the test run.

    Clears ``BROWSER_MCP_*`` variables and runs each test in an empty directory
    so a local ``.env`` file cannot change the outcome.
    """
    for name in os.environ:
        if name.startswith("BROWSER_MCP_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


class Authenticate(Protocol):
    """Presents a verified token to the server, as an authenticated request does."""

    def __call__(
        self,
        user_id: Any = ...,
        login: Any = ...,
        **extra_claims: Any,
    ) -> None:
        """Authenticate subsequent tool calls as that GitHub account.

        Args:
            user_id: Numeric GitHub ID to claim, as the ``sub`` claim. ``None``
                omits it entirely, standing in for a token issued by something
                other than GitHub; a non-string stands in for a malformed one.
            login: GitHub login to claim. Only ever used in error messages.
            extra_claims: Further claims to put on the token.
        """


@pytest.fixture
def authenticate() -> Authenticate:
    """Return a helper that puts a verified token in the request context.

    Real tokens only exist over http, where the transport's auth middleware
    verifies the bearer token with ``GitHubProvider`` and stores the result in
    this same context variable. These tests are about what the server does with
    a verified token, so only the verification step is skipped.

    Call it before opening a client: the server runs in a task that inherits
    the context as it stands when the connection is made. Nothing is torn down,
    because each test runs in its own ``contextvars`` context - which is also
    why the value has to be set from inside the test rather than from here.

    Returns:
        The helper.
    """

    def _authenticate(
        user_id: Any = DEFAULT_GITHUB_USER_ID,
        login: Any = "amc40",
        **extra: Any,
    ) -> None:
        claims: dict[str, Any] = dict(extra)
        if user_id is not None:
            claims["sub"] = user_id
        if login is not None:
            claims["login"] = login
        access_token = AccessToken(
            token="verified-by-github",
            client_id="1",
            scopes=["user"],
            claims=claims,
        )
        auth_context_var.set(AuthenticatedUser(access_token))

    return _authenticate
