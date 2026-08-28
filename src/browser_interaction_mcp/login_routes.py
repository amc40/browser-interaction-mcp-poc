"""The public ``/sainsburys-login`` page: an out-of-band browser login.

`sainsburys_refresh_session` used MCP elicitation to collect the password and
verification code. Claude.ai's MCP client supports tool calls only - no
elicitation - so that never worked from the real client. Instead the operator
visits a stable URL on the server, signs in with GitHub
(:mod:`browser_interaction_mcp.login_oauth`), and types the password straight
into a form here: it goes to the server over HTTPS, never through the model or
the transcript. The login itself runs in a subprocess
(:mod:`browser_interaction_mcp.sainsburys_login_flow`) that parks if a
verification code is needed, so the operator can come back to the same URL
minutes later with the code.

These routes are registered with ``@mcp.custom_route`` and therefore sit
outside FastMCP's ``RequireAuthMiddleware``: the GitHub session cookie is the
whole gate.
"""

from __future__ import annotations

import html
import json
import logging
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from browser_interaction_mcp.login_oauth import CALLBACK_PATH, LOGIN_PATH
from browser_interaction_mcp.sainsburys_login_flow import (
    LoginInProgressError,
    LoginState,
    LoginStatus,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from starlette.requests import Request

    from browser_interaction_mcp.login_oauth import BrowserGithubAuth
    from browser_interaction_mcp.sainsburys_login_flow import SainsburysLoginFlow

logger = logging.getLogger(__name__)

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "connect-src 'self'; form-action 'self'; frame-ancestors 'none'"
    ),
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}

_STYLE = (
    "body{font-family:system-ui,sans-serif;margin:3rem auto;max-width:28rem;"
    "padding:0 1rem;line-height:1.5}"
    "h1{font-size:1.25rem}"
    "input{font-size:1rem;padding:.5rem;width:100%;box-sizing:border-box}"
    "button{font-size:1rem;padding:.5rem 1rem;margin-top:1rem;cursor:pointer}"
    ".muted{color:#555}.err{color:#b00020}"
)

_PASSWORD_FORM = (
    f"<form method=post action='{LOGIN_PATH}/password'>"
    "<input type=password name=password autocomplete=off autofocus "
    "aria-label='Sainsbury&#39;s password'>"
    "<button type=submit>{label}</button></form>"
)

_OTP_FORM = (
    f"<form method=post action='{LOGIN_PATH}/otp'>"
    "<input name=code inputmode=numeric autocomplete=one-time-code autofocus "
    "aria-label='Verification code' required>"
    "<button type=submit>Submit code</button></form>"
)


def _poll_script(rendered_state: str) -> str:
    """A poller that refreshes the detail line and reloads only on a real change.

    ``rendered_state`` is the state the page was built for; reloading only when
    the server reports a *different* state keeps the verification-code field
    from being wiped under the operator every couple of seconds.
    """
    return (
        f"<script>const R={json.dumps(rendered_state)};"
        "setInterval(async()=>{try{"
        f"const r=await fetch('{LOGIN_PATH}/status',{{cache:'no-store'}});"
        "if(!r.ok)return;const s=await r.json();"
        "const d=document.getElementById('detail');if(d)d.textContent=s.detail;"
        "if(s.state!==R)location.reload();"
        "}catch(e){}},2000);</script>"
    )


def _page(body: str, *, script: str = "") -> HTMLResponse:
    doc = (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Refresh Sainsbury's session</title>"
        f"<style>{_STYLE}</style>"
        "<h1>Refresh Sainsbury's session</h1>"
        f"{body}{script}"
    )
    return HTMLResponse(doc, headers=_SECURITY_HEADERS)


def _detail(status: LoginStatus, css_class: str) -> str:
    return f'<p class="{css_class}" id=detail>{html.escape(status.detail)}</p>'


def _render(status: LoginStatus) -> HTMLResponse:
    if status.state is LoginState.AWAITING_PASSWORD:
        return _page(_detail(status, "muted") + _PASSWORD_FORM.format(label="Sign in"))
    if status.state is LoginState.LOGGING_IN:
        return _page(
            _detail(status, "muted")
            + "<p class=muted>This can take up to a minute.</p>",
            script=_poll_script("logging_in"),
        )
    if status.state is LoginState.AWAITING_OTP:
        return _page(
            _detail(status, "") + _OTP_FORM, script=_poll_script("awaiting_otp")
        )
    if status.state is LoginState.DONE:
        return _page(_detail(status, "") + "<p class=muted>You can close this tab.</p>")
    return _page(_detail(status, "err") + _PASSWORD_FORM.format(label="Try again"))


def _same_origin(request: Request) -> bool:
    """Reject a cross-site POST (belt-and-braces alongside the strict cookie).

    Compares hosts only, not scheme: TLS terminates at the tunnel edge and the
    last hop to the app is plain http, so ``request.url.scheme`` is ``http``
    while a browser's ``Origin`` on a legitimate same-site POST is ``https``.
    """
    origin = request.headers.get("origin")
    if origin is None:
        return True
    return urlsplit(origin).netloc == request.headers.get("host")


class _LoginRoutes:
    """The five handlers, sharing the flow and the auth gate."""

    def __init__(self, flow: SainsburysLoginFlow, auth: BrowserGithubAuth) -> None:
        self._flow = flow
        self._auth = auth

    async def page(self, request: Request) -> Response:
        if self._auth.authed_user_id(request) is None:
            return self._auth.begin()
        return _render(self._flow.status())

    async def callback(self, request: Request) -> Response:
        return await self._auth.complete(request)

    async def password(self, request: Request) -> Response:
        if not self._allowed(request):
            return Response("Forbidden", status_code=403)
        form = await request.form()
        try:
            self._flow.start(str(form.get("password") or ""))
        except LoginInProgressError:
            logger.info("password submitted while a login was already in progress")
        return RedirectResponse(LOGIN_PATH, status_code=303)

    async def otp(self, request: Request) -> Response:
        if not self._allowed(request):
            return Response("Forbidden", status_code=403)
        form = await request.form()
        self._flow.submit_otp(str(form.get("code") or "").strip())
        return RedirectResponse(LOGIN_PATH, status_code=303)

    async def status(self, request: Request) -> Response:
        if self._auth.authed_user_id(request) is None:
            return JSONResponse({"state": "unauthorized"}, status_code=403)
        status = self._flow.status()
        return JSONResponse(
            {
                "state": status.state.value,
                "detail": status.detail,
                "terminal": status.terminal,
            },
            headers={"Cache-Control": "no-store"},
        )

    def _allowed(self, request: Request) -> bool:
        return self._auth.authed_user_id(request) is not None and _same_origin(request)


def register_login_routes(
    mcp: FastMCP,
    flow: SainsburysLoginFlow,
    auth: BrowserGithubAuth,
) -> None:
    """Register the ``/sainsburys-login`` routes on ``mcp``.

    Args:
        mcp: The server to add the custom routes to.
        flow: The single-flight login coordinator.
        auth: The GitHub sign-in gate.
    """
    routes = _LoginRoutes(flow, auth)
    mcp.custom_route(LOGIN_PATH, methods=["GET"])(routes.page)
    mcp.custom_route(CALLBACK_PATH, methods=["GET"])(routes.callback)
    mcp.custom_route(f"{LOGIN_PATH}/password", methods=["POST"])(routes.password)
    mcp.custom_route(f"{LOGIN_PATH}/otp", methods=["POST"])(routes.otp)
    mcp.custom_route(f"{LOGIN_PATH}/status", methods=["GET"])(routes.status)
