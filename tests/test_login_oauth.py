"""Tests for the browser-side GitHub sign-in gate."""

from __future__ import annotations

import hashlib
import hmac
from typing import TYPE_CHECKING

import httpx
import pytest
from pydantic import SecretStr
from starlette.requests import Request

from browser_interaction_mcp import login_oauth
from browser_interaction_mcp.login_oauth import BrowserGithubAuth
from browser_interaction_mcp.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Callable

_ALLOWED_ID = "36701168"


def _settings() -> Settings:
    return Settings(
        transport="http",
        github_user_id=_ALLOWED_ID,
        github_client_id="client-id",
        github_client_secret=SecretStr("client-secret"),
        github_oauth_base_url="https://mcp.example",
    )


def _request(*, cookies: dict[str, str] | None = None, query: str = "") -> Request:
    headers = {}
    if cookies:
        headers["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/sainsburys-login/auth/callback",
            "raw_path": b"/sainsburys-login/auth/callback",
            "query_string": query.encode(),
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
            "server": ("mcp.example", 443),
            "client": ("test", 1),
        }
    )


def _github(id_returned: int) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("access_token"):
            return httpx.Response(200, json={"access_token": "gho_test"})
        return httpx.Response(200, json={"id": id_returned, "login": "amc40"})

    return httpx.MockTransport(handler)


def test_no_cookie_is_not_authenticated() -> None:
    auth = BrowserGithubAuth(_settings())

    assert auth.authed_user_id(_request()) is None


def test_a_freshly_issued_cookie_authenticates() -> None:
    auth = BrowserGithubAuth(_settings())
    cookie = auth._issue_cookie(_ALLOWED_ID)

    assert auth.authed_user_id(_request(cookies={"bimcp_login_session": cookie})) == (
        _ALLOWED_ID
    )


def test_a_tampered_cookie_is_rejected() -> None:
    auth = BrowserGithubAuth(_settings())
    body, _sig = auth._issue_cookie(_ALLOWED_ID).split(".")
    forged = f"{body}.{'a' * 43}"

    assert (
        auth.authed_user_id(_request(cookies={"bimcp_login_session": forged})) is None
    )


def test_a_cookie_without_a_signature_is_rejected() -> None:
    auth = BrowserGithubAuth(_settings())

    assert (
        auth.authed_user_id(_request(cookies={"bimcp_login_session": "no-dot-here"}))
        is None
    )


def test_a_correctly_signed_but_non_json_cookie_is_rejected() -> None:
    auth = BrowserGithubAuth(_settings())
    body = login_oauth._b64url(b"not json at all")
    signature = login_oauth._b64url(
        hmac.new(auth._signing_key, body.encode(), hashlib.sha256).digest()
    )

    assert (
        auth.authed_user_id(
            _request(cookies={"bimcp_login_session": f"{body}.{signature}"})
        )
        is None
    )


def test_an_expired_cookie_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(login_oauth, "_SESSION_TTL_SECONDS", -10)
    auth = BrowserGithubAuth(_settings())
    stale = auth._issue_cookie(_ALLOWED_ID)

    assert auth.authed_user_id(_request(cookies={"bimcp_login_session": stale})) is None


def test_begin_redirects_to_github_with_a_tracked_state() -> None:
    auth = BrowserGithubAuth(_settings())

    response = auth.begin()

    location = response.headers["location"]
    assert location.startswith("https://github.com/login/oauth/authorize?")
    assert "client_id=client-id" in location
    state = location.split("state=")[1].split("&")[0]
    assert state in auth._states


async def test_callback_sets_a_session_cookie_for_the_allowed_account() -> None:
    auth = BrowserGithubAuth(_settings(), transport=_github(int(_ALLOWED_ID)))
    state = auth.begin().headers["location"].split("state=")[1].split("&")[0]

    response = await auth.complete(_request(query=f"code=abc&state={state}"))

    assert response.status_code == 303
    assert response.headers["location"] == "/sainsburys-login"
    assert "bimcp_login_session=" in response.headers["set-cookie"]
    assert state not in auth._states


async def test_callback_denies_another_github_account() -> None:
    auth = BrowserGithubAuth(_settings(), transport=_github(999))
    state = auth.begin().headers["location"].split("state=")[1].split("&")[0]

    response = await auth.complete(_request(query=f"code=abc&state={state}"))

    assert response.status_code == 403


async def test_callback_rejects_an_unknown_state() -> None:
    auth = BrowserGithubAuth(_settings(), transport=_github(int(_ALLOWED_ID)))

    response = await auth.complete(_request(query="code=abc&state=never-issued"))

    assert response.status_code == 403


async def test_callback_reports_a_github_failure(
    make_failing_transport: Callable[[], httpx.MockTransport],
) -> None:
    auth = BrowserGithubAuth(_settings(), transport=make_failing_transport())
    state = auth.begin().headers["location"].split("state=")[1].split("&")[0]

    response = await auth.complete(_request(query=f"code=abc&state={state}"))

    assert response.status_code == 403


@pytest.fixture
def make_failing_transport() -> Callable[[], httpx.MockTransport]:
    def make() -> httpx.MockTransport:
        return httpx.MockTransport(lambda _request: httpx.Response(500))

    return make


def test_missing_oauth_credentials_is_a_configuration_error() -> None:
    with pytest.raises(ValueError, match="OAuth app credentials"):
        BrowserGithubAuth(Settings(transport="stdio"))
