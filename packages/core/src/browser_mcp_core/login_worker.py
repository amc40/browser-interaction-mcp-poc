"""Runs one real login as a subprocess, for whichever site asked for it.

Spawned by :mod:`browser_mcp_core.login_flow` for the out-of-band browser login
(see :mod:`browser_mcp_core.login_routes`). It runs in its own process - not a
thread - specifically so a hung Chromium can be killed outright by the parent.

A site does not write one of these. It writes a two-line module that calls
:func:`run` with its own :class:`~browser_mcp_core.login_steps.LoginSteps`, and
names that module in its :class:`~browser_mcp_core.site.LoginPage`.

The username and password arrive as a single JSON line on stdin, never as
command-line arguments (which ``ps`` would show). Progress is written to
``<ipc-dir>/status.json``; if the site asks for a verification code this polls
``<ipc-dir>/otp`` for one, for up to ``--otp-timeout`` seconds. Neither the
password nor the code is written anywhere by this process - only the resulting
``storage_state`` is, by :func:`browser_mcp_core.login_steps.refresh_session`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from browser_mcp_core.errors import NotLoggedInError
from browser_mcp_core.login_flow import LoginState, login_messages
from browser_mcp_core.login_steps import refresh_session

if TYPE_CHECKING:
    from collections.abc import Callable

    from browser_mcp_core.login_steps import LoginSteps

#: How often the OTP file is polled while the login is parked on the MFA step.
_OTP_POLL_INTERVAL = 1.0


def _write_status(ipc_dir: Path, state: LoginState, detail: str) -> None:
    """Atomically publish the worker's current state to ``status.json``."""
    payload = json.dumps({"state": state.value, "detail": detail})
    tmp = ipc_dir / "status.json.tmp"
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(ipc_dir / "status.json")


def _make_get_otp(
    ipc_dir: Path, timeout: float, messages: dict[LoginState, str]
) -> Callable[[], str | None]:
    """Build the ``get_otp`` callback ``refresh_session`` calls on the MFA step."""
    otp_path = ipc_dir / "otp"

    def get_otp() -> str | None:
        _write_status(
            ipc_dir, LoginState.AWAITING_OTP, messages[LoginState.AWAITING_OTP]
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if otp_path.is_file():
                code = otp_path.read_text(encoding="utf-8").strip()
                otp_path.unlink(missing_ok=True)
                if code:
                    _write_status(ipc_dir, LoginState.LOGGING_IN, "Checking the code…")
                return code or None
            time.sleep(_OTP_POLL_INTERVAL)
        return None

    return get_otp


def run(steps: LoginSteps, argv: list[str] | None = None) -> int:
    """Run one login attempt end to end; return 0 on success.

    Args:
        steps: The site's login steps.
        argv: Command line to parse. Read from ``sys.argv`` when omitted.

    Returns:
        0 if the session was captured, 1 otherwise. Nothing is raised out of
        here: a failure is reported to the operator through ``status.json``,
        and the exception itself never is, because its text is not guaranteed
        to be free of the password that was just typed.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-state", required=True, type=Path)
    parser.add_argument("--ipc-dir", required=True, type=Path)
    parser.add_argument("--otp-timeout", type=float, default=300.0)
    args = parser.parse_args(argv)

    messages = login_messages(steps.display_name)
    try:
        credentials = json.loads(sys.stdin.readline() or "{}")
        username = str(credentials["username"])
        password = str(credentials["password"])
    except (ValueError, KeyError):
        _write_status(
            args.ipc_dir, LoginState.FAILED, "The login could not be started."
        )
        return 1
    if not password:
        _write_status(args.ipc_dir, LoginState.FAILED, "No password was provided.")
        return 1

    _write_status(args.ipc_dir, LoginState.LOGGING_IN, messages[LoginState.LOGGING_IN])
    try:
        refresh_session(
            steps,
            username,
            password,
            storage_state_path=args.storage_state,
            get_otp=_make_get_otp(args.ipc_dir, args.otp_timeout, messages),
            failure_screenshot_path=args.ipc_dir / "failure.png",
        )
    except NotLoggedInError as exc:
        # These messages are fixed strings from core and the site's steps - no
        # credential is ever interpolated into them - so they are safe to
        # surface.
        _write_status(args.ipc_dir, LoginState.FAILED, str(exc))
        return 1
    except Exception:  # noqa: BLE001 - top-level worker: report generically, never leak
        _write_status(
            args.ipc_dir, LoginState.FAILED, "The login could not be completed."
        )
        return 1

    _write_status(args.ipc_dir, LoginState.DONE, messages[LoginState.DONE])
    return 0
