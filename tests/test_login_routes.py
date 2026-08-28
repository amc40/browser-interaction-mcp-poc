"""Integration tests for the /sainsburys-login page, driven over ASGI.

The login subprocess is faked (no Playwright, no browser); the GitHub OAuth
round trip has its own unit tests in test_login_oauth.py, so here the session
cookie is forged directly with the same signing key the app uses.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from pydantic import SecretStr

from browser_interaction_mcp.login_oauth import BrowserGithubAuth
from browser_interaction_mcp.login_routes import _render
from browser_interaction_mcp.sainsburys_login_flow import LoginState, LoginStatus
from browser_interaction_mcp.server import build_server
from browser_interaction_mcp.settings import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_BASE_URL = "https://mcp.example"


class _FakeWorker:
    def __init__(self, argv: list[str]) -> None:
        self.ipc_dir = Path(argv[argv.index("--ipc-dir") + 1])
        self.stdin = _Stdin()
        self._rc: int | None = None

    def poll(self) -> int | None:
        return self._rc

    def kill(self) -> None:
        self._rc = -9

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self._rc or 0

    def report(self, state: str, detail: str = "…") -> None:
        (self.ipc_dir / "status.json").write_text(
            json.dumps({"state": state, "detail": detail}), encoding="utf-8"
        )


class _Stdin:
    def write(self, text: str) -> None:
        self.text = text

    def close(self) -> None:
        pass


@pytest.fixture
def workers(monkeypatch: pytest.MonkeyPatch) -> list[_FakeWorker]:
    created: list[_FakeWorker] = []

    def fake_popen(argv: list[str], **_: object) -> _FakeWorker:
        worker = _FakeWorker(argv)
        created.append(worker)
        return worker

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return created


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        transport="http",
        github_user_id="36701168",
        github_client_id="client-id",
        github_client_secret=SecretStr("client-secret"),
        github_oauth_base_url=_BASE_URL,
        sainsburys_username=SecretStr("shopper@example.com"),
        sainsburys_storage_state_path=tmp_path / "session.json",
    )


@pytest.fixture
async def client(
    settings: Settings,
    workers: list[_FakeWorker],
) -> AsyncIterator[httpx.AsyncClient]:
    del workers
    app = build_server(settings).http_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url=_BASE_URL, follow_redirects=False
    ) as http_client:
        yield http_client


@pytest.fixture
def session_headers(settings: Settings) -> dict[str, str]:
    cookie = BrowserGithubAuth(settings)._issue_cookie("36701168")
    return {"cookie": f"bimcp_login_session={cookie}"}


async def test_the_page_bounces_an_unauthenticated_visitor_to_github(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/sainsburys-login")

    assert response.status_code in (302, 307)
    assert response.headers["location"].startswith(
        "https://github.com/login/oauth/authorize"
    )


async def test_the_signed_in_page_shows_the_password_form(
    client: httpx.AsyncClient, session_headers: dict[str, str]
) -> None:
    response = await client.get("/sainsburys-login", headers=session_headers)

    assert response.status_code == 200
    assert "type=password" in response.text
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers["x-frame-options"] == "DENY"


async def test_the_otp_page_does_not_reload_itself_on_every_poll(
    client: httpx.AsyncClient,
    session_headers: dict[str, str],
    workers: list[_FakeWorker],
) -> None:
    await client.post(
        "/sainsburys-login/password",
        data={"password": "hunter2"},
        headers=session_headers,
    )
    workers[0].report("awaiting_otp", "Enter the code.")

    page = await client.get("/sainsburys-login", headers=session_headers)

    # Reload only when the server-reported state differs from what was rendered.
    assert 'const R="awaiting_otp"' in page.text
    assert "s.state!==R" in page.text


async def test_a_same_site_post_is_allowed_despite_the_http_last_hop(
    client: httpx.AsyncClient,
    session_headers: dict[str, str],
    workers: list[_FakeWorker],
) -> None:
    response = await client.post(
        "/sainsburys-login/password",
        data={"password": "hunter2"},
        headers={**session_headers, "origin": _BASE_URL},
    )

    assert response.status_code == 303
    assert len(workers) == 1


async def test_an_empty_otp_submission_is_ignored(
    client: httpx.AsyncClient,
    session_headers: dict[str, str],
    workers: list[_FakeWorker],
) -> None:
    await client.post(
        "/sainsburys-login/password",
        data={"password": "hunter2"},
        headers=session_headers,
    )
    workers[0].report("awaiting_otp", "Enter the code.")

    await client.post(
        "/sainsburys-login/otp", data={"code": "  "}, headers=session_headers
    )

    assert not (workers[0].ipc_dir / "otp").exists()


async def test_password_then_status_walks_to_done(
    client: httpx.AsyncClient,
    session_headers: dict[str, str],
    workers: list[_FakeWorker],
) -> None:
    submitted = await client.post(
        "/sainsburys-login/password",
        data={"password": "hunter2"},
        headers=session_headers,
    )
    assert submitted.status_code == 303
    assert workers[0].stdin.text.startswith('{"username": "shopper@example.com"')

    logging_in = await client.get("/sainsburys-login/status", headers=session_headers)
    assert logging_in.json()["state"] == "logging_in"

    workers[0].report("done", "Session refreshed.")
    done = await client.get("/sainsburys-login/status", headers=session_headers)
    assert done.json() == {
        "state": "done",
        "detail": "Session refreshed.",
        "terminal": True,
    }


async def test_otp_form_and_relay(
    client: httpx.AsyncClient,
    session_headers: dict[str, str],
    workers: list[_FakeWorker],
) -> None:
    await client.post(
        "/sainsburys-login/password",
        data={"password": "hunter2"},
        headers=session_headers,
    )
    workers[0].report("awaiting_otp", "Enter the code.")

    page = await client.get("/sainsburys-login", headers=session_headers)
    assert "name=code" in page.text

    await client.post(
        "/sainsburys-login/otp", data={"code": "123456"}, headers=session_headers
    )
    assert (workers[0].ipc_dir / "otp").read_text(encoding="utf-8") == "123456"


async def test_the_page_reflects_a_failed_attempt(
    client: httpx.AsyncClient,
    session_headers: dict[str, str],
    workers: list[_FakeWorker],
) -> None:
    await client.post(
        "/sainsburys-login/password",
        data={"password": "hunter2"},
        headers=session_headers,
    )

    logging_in = await client.get("/sainsburys-login", headers=session_headers)
    assert "up to a minute" in logging_in.text

    workers[0].report("failed", "Wrong password.")
    failed = await client.get("/sainsburys-login", headers=session_headers)
    assert "Try again" in failed.text


async def test_the_page_reports_success(
    client: httpx.AsyncClient,
    session_headers: dict[str, str],
    workers: list[_FakeWorker],
) -> None:
    await client.post(
        "/sainsburys-login/password",
        data={"password": "hunter2"},
        headers=session_headers,
    )
    workers[0].report("done", "All set.")

    done = await client.get("/sainsburys-login", headers=session_headers)
    assert "close this tab" in done.text


async def test_a_second_password_submission_is_absorbed(
    client: httpx.AsyncClient,
    session_headers: dict[str, str],
    workers: list[_FakeWorker],
) -> None:
    await client.post(
        "/sainsburys-login/password",
        data={"password": "hunter2"},
        headers=session_headers,
    )
    second = await client.post(
        "/sainsburys-login/password",
        data={"password": "again"},
        headers=session_headers,
    )

    assert second.status_code == 303
    assert len(workers) == 1


async def test_the_oauth_callback_route_is_wired(client: httpx.AsyncClient) -> None:
    response = await client.get("/sainsburys-login/auth/callback?code=x&state=bogus")

    assert response.status_code == 403


async def test_posting_without_a_session_is_forbidden(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/sainsburys-login/password", data={"password": "hunter2"}
    )

    assert response.status_code == 403


async def test_a_cross_site_post_is_forbidden(
    client: httpx.AsyncClient, session_headers: dict[str, str]
) -> None:
    response = await client.post(
        "/sainsburys-login/password",
        data={"password": "hunter2"},
        headers={**session_headers, "origin": "https://evil.example"},
    )

    assert response.status_code == 403


async def test_status_needs_a_session(client: httpx.AsyncClient) -> None:
    response = await client.get("/sainsburys-login/status")

    assert response.status_code == 403


async def test_submitting_an_otp_without_a_session_is_forbidden(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/sainsburys-login/otp", data={"code": "123456"})

    assert response.status_code == 403


def test_login_page_is_not_registered_on_stdio() -> None:
    routes = [
        getattr(route, "path", None)
        for route in build_server(Settings())._additional_http_routes
    ]
    assert "/sainsburys-login" not in routes


def test_a_detail_string_with_braces_does_not_break_the_page() -> None:
    response = _render(LoginStatus(LoginState.FAILED, "bad token {sub} }"))

    assert response.status_code == 200
    assert "bad token {sub} }" in bytes(response.body).decode()
