"""Subprocess that performs one real Sainsbury's login.

Spawned by :mod:`browser_interaction_mcp.sainsburys_login_flow` for the
out-of-band browser login flow (see :mod:`browser_interaction_mcp.login_routes`).
It runs in its own process - not a thread - specifically so a hung Chromium can
be killed outright by the parent.

The username and password arrive as a single JSON line on stdin, never as
command-line arguments (which ``ps`` would show). Progress is written to
``<ipc-dir>/status.json``; if Sainsbury's asks for a verification code this
polls ``<ipc-dir>/otp`` for one, for up to ``--otp-timeout`` seconds. Neither
the password nor the code is written anywhere by this process - only the
resulting ``storage_state`` is, by ``sainsburys.refresh_session``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from browser_interaction_mcp import sainsburys

if TYPE_CHECKING:
    from collections.abc import Callable

#: How often the OTP file is polled while the login is parked on the MFA step.
_OTP_POLL_INTERVAL = 1.0


def _write_status(ipc_dir: Path, state: str, detail: str) -> None:
    """Atomically publish the worker's current state to ``status.json``."""
    payload = json.dumps({"state": state, "detail": detail})
    tmp = ipc_dir / "status.json.tmp"
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(ipc_dir / "status.json")


def _make_get_otp(ipc_dir: Path, timeout: float) -> Callable[[], str | None]:
    """Build the ``get_otp`` callback ``refresh_session`` calls on the MFA step."""
    otp_path = ipc_dir / "otp"

    def get_otp() -> str | None:
        _write_status(
            ipc_dir,
            "awaiting_otp",
            "Sainsbury's asked for a verification code. Enter it to finish.",
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if otp_path.is_file():
                code = otp_path.read_text(encoding="utf-8").strip()
                otp_path.unlink(missing_ok=True)
                if code:
                    _write_status(ipc_dir, "logging_in", "Checking the code…")
                return code or None
            time.sleep(_OTP_POLL_INTERVAL)
        return None

    return get_otp


def main(argv: list[str] | None = None) -> int:
    """Run one login attempt end to end; return 0 on success."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-state", required=True, type=Path)
    parser.add_argument("--ipc-dir", required=True, type=Path)
    parser.add_argument("--otp-timeout", type=float, default=300.0)
    args = parser.parse_args(argv)

    try:
        credentials = json.loads(sys.stdin.readline() or "{}")
        username = str(credentials["username"])
        password = str(credentials["password"])
    except (ValueError, KeyError):
        _write_status(args.ipc_dir, "failed", "The login could not be started.")
        return 1
    if not password:
        _write_status(args.ipc_dir, "failed", "No password was provided.")
        return 1

    _write_status(args.ipc_dir, "logging_in", "Signing in to Sainsbury's…")
    try:
        sainsburys.refresh_session(
            username,
            password,
            storage_state_path=args.storage_state,
            get_otp=_make_get_otp(args.ipc_dir, args.otp_timeout),
        )
    except sainsburys.NotLoggedInError as exc:
        # These messages are fixed strings from sainsburys.py - no credential
        # is ever interpolated into them - so they are safe to surface.
        _write_status(args.ipc_dir, "failed", str(exc))
        return 1
    except Exception:  # noqa: BLE001 - top-level worker: report generically, never leak
        _write_status(args.ipc_dir, "failed", "The login could not be completed.")
        return 1

    _write_status(args.ipc_dir, "done", "Your Sainsbury's session has been refreshed.")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by running it as a subprocess
    raise SystemExit(main())
