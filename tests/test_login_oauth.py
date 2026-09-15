"""Tests for the browser-side GitHub sign-in gate.

Driven over ASGI through a minimal app wired the way ``server.py`` wires the
real one - the scoped session middleware plus the two OAuth routes - because
the CSRF state now lives in the session cookie. A test that called ``begin``
and ``complete`` directly would not exercise the binding between them, which is
the property most worth pinning.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from browser_interaction_mcp.login_oauth import (
    CALLBACK_PATH,
    LOGIN_PATH,
    BrowserGithubAuth,
    login_session_middleware,
)
from browser_interaction_mcp.settings import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.requests import Request
    from starlette.responses import Response

_ALLOWED_ID = "36701168"
_BASE_URL = "https://mcp.example"
_BYPASS_PATH = "/mcp"


def _settings() -> Settings:
    return Settings(
        transport="http",
        github_user_id=_ALLOWED_ID,
        github_client_id="client-id",
        github_client_secret=SecretStr("client-secret"),
        github_oauth_base_url=_BASE_URL,
    )


def _github(
    user_id: int = int(_ALLOWED_ID),
    *,
    token_response: httpx.Response | None = None,
    user_response: httpx.Response | None = None,
) -> httpx.MockTransport:
    """A stand-in for GitHub, optionally failing at a chosen step."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("access_token"):
            return token_response or httpx.Response(
                200, json={"access_token": "gho_test", "token_type": "bearer"}
            )
        return user_response or httpx.Response(
            200, json={"id": user_id, "login": "amc40"}
        )

    return httpx.MockTransport(handler)


def _app(transport: httpx.MockTransport) -> Starlette:
    """The gate, wired the way `server.py` wires it."""
    settings = _settings()
    auth = BrowserGithubAuth(settings, transport=transport)

    async def page(request: Request) -> Response:
        signed_in = auth.authed_user_id(request)
        if signed_in is None:
            return await auth.begin(request)
        return PlainTextResponse(signed_in)

    async def callback(request: Request) -> Response:
        return await auth.complete(request)

    async def bypassed(request: Request) -> Response:
        return PlainTextResponse(str("session" in request.scope))

    return Starlette(
        routes=[
            Route(LOGIN_PATH, page),
            Route(CALLBACK_PATH, callback),
            Route(_BYPASS_PATH, bypassed),
        ],
        middleware=[login_session_middleware(settings)],
    )


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with _client(_github()) as http_client:
        yield http_client


def _client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(transport)),
        base_url=_BASE_URL,
        follow_redirects=False,
    )


async def _state_from(client: httpx.AsyncClient) -> str:
    """Start a flow and return the CSRF state GitHub would be redirected with."""
    response = await client.get(LOGIN_PATH)
    location = response.headers["location"]
    return parse_qs(urlsplit(location).query)["state"][0]


# -- the gate ---------------------------------------------------------------


async def test_an_unauthenticated_visitor_is_bounced_to_github(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(LOGIN_PATH)

    location = response.headers["location"]
    assert location.startswith("https://github.com/login/oauth/authorize?")
    query = parse_qs(urlsplit(location).query)
    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == [f"{_BASE_URL}{CALLBACK_PATH}"]
    assert query["allow_signup"] == ["false"]
    assert query["state"]


async def test_the_allowed_account_completes_the_round_trip(
    client: httpx.AsyncClient,
) -> None:
    state = await _state_from(client)

    response = await client.get(f"{CALLBACK_PATH}?code=abc&state={state}")

    assert response.status_code == 303
    assert response.headers["location"] == LOGIN_PATH
    assert (await client.get(LOGIN_PATH)).text == _ALLOWED_ID


async def test_another_github_account_is_refused() -> None:
    async with _client(_github(user_id=999)) as client:
        state = await _state_from(client)

        response = await client.get(f"{CALLBACK_PATH}?code=abc&state={state}")

        assert response.status_code == 403
        assert "not allowed" in response.text
        assert (await client.get(LOGIN_PATH)).status_code != 200


# -- the CSRF state ---------------------------------------------------------


async def test_a_state_issued_to_another_browser_is_refused() -> None:
    """The property the process-global state dict this replaced did not have.

    An attacker who starts their own flow holds a valid state value. Replaying
    it into a victim's browser must fail, because the state is bound to the
    session cookie of the browser that started the flow - not to the server.
    """
    async with _client(_github()) as attacker, _client(_github()) as victim:
        stolen = await _state_from(attacker)

        response = await victim.get(f"{CALLBACK_PATH}?code=abc&state={stolen}")

        assert response.status_code == 403
        assert "expired" in response.text


async def test_a_callback_with_no_session_at_all_is_refused(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(f"{CALLBACK_PATH}?code=abc&state=never-issued")

    assert response.status_code == 403
    assert "expired" in response.text


async def test_a_tampered_session_cookie_is_refused(
    client: httpx.AsyncClient,
) -> None:
    state = await _state_from(client)
    cookie = client.cookies["bimcp_login_session"]
    client.cookies.set(
        "bimcp_login_session", cookie[:-4] + "aaaa", domain="mcp.example"
    )

    response = await client.get(f"{CALLBACK_PATH}?code=abc&state={state}")

    assert response.status_code == 403


# -- GitHub failing ---------------------------------------------------------


async def test_a_failing_token_exchange_is_reported() -> None:
    async with _client(_github(token_response=httpx.Response(500))) as client:
        state = await _state_from(client)

        response = await client.get(f"{CALLBACK_PATH}?code=abc&state={state}")

        assert response.status_code == 403
        assert "sign-in failed" in response.text


async def test_a_rejected_code_is_reported() -> None:
    """GitHub answers a bad code with HTTP 200 and an error body."""
    rejected = httpx.Response(200, json={"error": "bad_verification_code"})
    async with _client(_github(token_response=rejected)) as client:
        state = await _state_from(client)

        response = await client.get(f"{CALLBACK_PATH}?code=abc&state={state}")

        assert response.status_code == 403
        assert "sign-in failed" in response.text


async def test_a_failing_identity_lookup_is_reported() -> None:
    async with _client(_github(user_response=httpx.Response(401))) as client:
        state = await _state_from(client)

        response = await client.get(f"{CALLBACK_PATH}?code=abc&state={state}")

        assert response.status_code == 403
        assert "sign-in failed" in response.text


async def test_an_identity_response_without_an_id_is_reported() -> None:
    nameless = httpx.Response(200, json={"login": "amc40"})
    async with _client(_github(user_response=nameless)) as client:
        state = await _state_from(client)

        response = await client.get(f"{CALLBACK_PATH}?code=abc&state={state}")

        assert response.status_code == 403
        assert "sign-in failed" in response.text


# -- the middleware's blast radius ------------------------------------------


async def test_a_request_outside_the_login_path_gets_no_session(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(_BYPASS_PATH)

    assert response.text == "False"
    assert "set-cookie" not in response.headers


async def test_a_path_that_merely_starts_with_the_prefix_gets_no_session() -> None:
    """`/sainsburys-login-elsewhere` is a different route, not a login route."""

    async def elsewhere(request: Request) -> Response:
        return PlainTextResponse(str("session" in request.scope))

    settings = _settings()
    app = Starlette(
        routes=[Route(f"{LOGIN_PATH}-elsewhere", elsewhere)],
        middleware=[login_session_middleware(settings)],
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=_BASE_URL
    ) as client:
        response = await client.get(f"{LOGIN_PATH}-elsewhere")

    assert response.text == "False"


async def test_the_session_cookie_is_locked_down(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(LOGIN_PATH)

    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=strict" in cookie
    assert f"path={LOGIN_PATH}" in cookie
    assert "max-age=900" in cookie


# -- configuration ----------------------------------------------------------


def test_missing_oauth_credentials_is_a_configuration_error() -> None:
    with pytest.raises(ValueError, match="OAuth app credentials"):
        BrowserGithubAuth(Settings(transport="stdio"))


def test_a_session_without_oauth_credentials_is_a_configuration_error() -> None:
    with pytest.raises(ValueError, match="OAuth app credentials"):
        login_session_middleware(Settings(transport="stdio"))
