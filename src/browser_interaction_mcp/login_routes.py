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
    # same-origin, not no-referrer: under no-referrer some browsers also send
    # `Origin: null` on same-origin form posts, which _same_origin would reject.
    "Referrer-Policy": "same-origin",
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
    # Stage-of-flow stepper, shown while an attempt is still in progress.
    ".stepper{display:flex;justify-content:space-between;margin:0 0 1.75rem;"
    "position:relative}"
    ".stepper::before{content:'';position:absolute;top:.9rem;left:1.8rem;"
    "right:1.8rem;height:2px;background:#ddd;z-index:0}"
    ".step{display:flex;flex-direction:column;align-items:center;gap:.4rem;"
    "flex:1;position:relative;z-index:1}"
    ".step .dot{width:1.8rem;height:1.8rem;border-radius:50%;display:flex;"
    "align-items:center;justify-content:center;font-size:.85rem;font-weight:600;"
    "background:#fff;border:2px solid #ccc;color:#999}"
    ".step .label{font-size:.7rem;color:#777;text-align:center}"
    ".step.done .dot{background:#1a7f37;border-color:#1a7f37;color:#fff}"
    ".step.done .label{color:#1a7f37}"
    ".step.active .dot{border-color:#0552b5;color:#0552b5}"
    ".step.active .label{color:#0552b5;font-weight:600}"
    # Big pass/fail marker, shown once an attempt has finished.
    ".result{display:flex;flex-direction:column;align-items:center;gap:.75rem;"
    "margin:1rem 0 1.75rem}"
    ".result .badge{width:3.5rem;height:3.5rem;border-radius:50%;display:flex;"
    "align-items:center;justify-content:center;font-size:1.8rem;color:#fff;"
    "flex-shrink:0}"
    ".result.ok .badge{background:#1a7f37}"
    ".result.err .badge{background:#b00020}"
    ".result p{margin:0;text-align:center}"
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


#: The stages an attempt visibly passes through, in order. Verification code
#: isn't always asked for, but showing it up front - rather than only once
#: Sainsbury's actually asks - is what makes the current stage legible at a
#: glance instead of a surprise.
_STEPS = (
    (LoginState.AWAITING_PASSWORD, "Password"),
    (LoginState.LOGGING_IN, "Signing in"),
    (LoginState.AWAITING_OTP, "Verification code"),
)


def _stepper(current: LoginState) -> str:
    """Render the 3-stage progress stepper, ``current`` (or later) highlighted."""
    order = [state for state, _ in _STEPS]
    current_index = len(order) if current is LoginState.DONE else order.index(current)
    steps = []
    for i, (_state, label) in enumerate(_STEPS):
        if i < current_index:
            css, dot = "done", "&#10003;"
        elif i == current_index:
            css, dot = "active", str(i + 1)
        else:
            css, dot = "", str(i + 1)
        steps.append(
            f'<div class="step {css}"><div class="dot">{dot}</div>'
            f'<div class="label">{html.escape(label)}</div></div>'
        )
    return f'<div class="stepper">{"".join(steps)}</div>'


def _result(kind: str, glyph: str, status: LoginStatus) -> str:
    """Render the large pass/fail badge shown once an attempt has finished."""
    return (
        f'<div class="result {kind}">'
        f'<div class="badge" aria-hidden="true">{glyph}</div>'
        f'<p id=detail>{html.escape(status.detail)}</p></div>'
    )


def _render(status: LoginStatus) -> HTMLResponse:
    if status.state is LoginState.AWAITING_PASSWORD:
        return _page(
            _stepper(status.state)
            + _detail(status, "muted")
            + _PASSWORD_FORM.format(label="Sign in")
        )
    if status.state is LoginState.LOGGING_IN:
        return _page(
            _stepper(status.state)
            + _detail(status, "muted")
            + "<p class=muted>This can take up to a minute.</p>",
            script=_poll_script("logging_in"),
        )
    if status.state is LoginState.AWAITING_OTP:
        return _page(
            _stepper(status.state) + _detail(status, "") + _OTP_FORM,
            script=_poll_script("awaiting_otp"),
        )
    if status.state is LoginState.DONE:
        return _page(
            _stepper(status.state)
            + _result("ok", "&#10003;", status)
            + "<p class=muted>You can close this tab.</p>"
        )
    # FAILED or EXPIRED: which stage it got to isn't tracked once it's gone
    # wrong, so lead with the unambiguous cross rather than guess a stepper.
    return _page(
        _result("err", "&#10007;", status) + _PASSWORD_FORM.format(label="Try again")
    )


def _same_origin(request: Request) -> bool:
    """Reject a cross-site POST (belt-and-braces alongside the strict cookie).

    Compares hosts only, not scheme: TLS terminates at the tunnel edge and the
    last hop to the app is plain http, so ``request.url.scheme`` is ``http``
    while a browser's ``Origin`` on a legitimate same-site POST is ``https``.

    A missing or ``null`` ``Origin`` is allowed: the ``SameSite=Strict`` session
    cookie (checked alongside this) already guarantees a cross-site request
    can't carry the credential, and some browsers send ``Origin: null`` even on
    a legitimate same-origin form post.
    """
    origin = request.headers.get("origin")
    if origin is None or origin == "null":
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
