"""Coordinates the out-of-band Sainsbury's browser login.

One login runs at a time. This owns the ``sainsburys_login_worker`` subprocess,
surfaces its progress to :mod:`browser_interaction_mcp.login_routes`, relays a
verification code to it, and kills it if it overruns.

Nothing here is async or FastMCP-aware: the route handlers call these plain,
quick methods directly. The password is handed to the worker over its stdin
pipe and is not retained here afterwards.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_WORKER_MODULE = "browser_interaction_mcp.sainsburys_login_worker"

#: How long the worker parks on the MFA step waiting for a verification code.
OTP_WAIT_SECONDS = 300.0
#: Hard cap on a whole attempt, a margin above ``OTP_WAIT_SECONDS``. A worker
#: still running past this is killed - the defence against a wedged Chromium
#: that never reaches the OTP step.
MAX_ATTEMPT_SECONDS = 420.0
#: How long a finished attempt's outcome stays shown before the page offers a
#: fresh start.
TERMINAL_RETENTION_SECONDS = 300.0


class LoginState(StrEnum):
    """Where a login attempt has got to."""

    AWAITING_PASSWORD = "awaiting_password"  # noqa: S105 - a state name, not a credential
    LOGGING_IN = "logging_in"
    AWAITING_OTP = "awaiting_otp"
    DONE = "done"
    FAILED = "failed"
    EXPIRED = "expired"


_ACTIVE = frozenset({LoginState.LOGGING_IN, LoginState.AWAITING_OTP})
_TERMINAL = frozenset({LoginState.DONE, LoginState.FAILED, LoginState.EXPIRED})

_MESSAGES = {
    LoginState.AWAITING_PASSWORD: "Enter your Sainsbury's account password.",
    LoginState.LOGGING_IN: "Signing in to Sainsbury's…",
    LoginState.AWAITING_OTP: (
        "Sainsbury's asked for a verification code. Enter it to finish."
    ),
    LoginState.DONE: "Your Sainsbury's session has been refreshed.",
    LoginState.FAILED: "The login did not complete.",
    LoginState.EXPIRED: "The login timed out. Start again when you're ready.",
}


@dataclass(frozen=True)
class LoginStatus:
    """A snapshot of the current attempt, for the page and the poll endpoint."""

    state: LoginState
    detail: str

    @property
    def terminal(self) -> bool:
        """Whether nothing further will happen without a fresh start."""
        return self.state in _TERMINAL


class LoginInProgressError(RuntimeError):
    """Raised when a password is submitted while an attempt is already active."""


@dataclass
class _Attempt:
    ipc_dir: Path
    proc: subprocess.Popen[str]
    started_at: float
    timer: threading.Timer
    state: LoginState = LoginState.LOGGING_IN
    detail: str = _MESSAGES[LoginState.LOGGING_IN]
    finished_at: float | None = None


class SainsburysLoginFlow:
    """The single in-flight Sainsbury's login for this server process."""

    def __init__(
        self,
        *,
        username: str,
        storage_state_path: Path,
        popen: Callable[..., subprocess.Popen[str]] | None = None,
        otp_wait_seconds: float = OTP_WAIT_SECONDS,
        max_attempt_seconds: float = MAX_ATTEMPT_SECONDS,
    ) -> None:
        """Configure the flow.

        Args:
            username: Sainsbury's account username, forwarded to the worker.
            storage_state_path: Where the worker writes the captured session.
            popen: Seam for tests; defaults to :class:`subprocess.Popen`.
            otp_wait_seconds: Passed to the worker as its OTP poll timeout.
            max_attempt_seconds: Kill any attempt still running past this.
        """
        self._username = username
        self._storage_state_path = storage_state_path
        self._popen = popen or subprocess.Popen
        self._otp_wait_seconds = otp_wait_seconds
        self._max_attempt_seconds = max_attempt_seconds
        self._lock = threading.Lock()
        self._attempt: _Attempt | None = None

    # -- called by the route handlers -------------------------------------

    def start(self, password: str) -> None:
        """Begin a login attempt.

        Args:
            password: The account password, handed straight to the worker.

        Raises:
            LoginInProgressError: If an attempt is already logging in or
                waiting for a verification code.
        """
        with self._lock:
            self._refresh_locked()
            if self._attempt is not None and self._attempt.state in _ACTIVE:
                msg = "A login is already in progress."
                raise LoginInProgressError(msg)
            self._teardown_locked()

            ipc_dir = Path(tempfile.mkdtemp(prefix="sainsburys-login-"))
            proc = self._popen(
                [
                    sys.executable,
                    "-m",
                    _WORKER_MODULE,
                    "--storage-state",
                    str(self._storage_state_path),
                    "--ipc-dir",
                    str(ipc_dir),
                    "--otp-timeout",
                    str(self._otp_wait_seconds),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            if proc.stdin is not None:
                proc.stdin.write(
                    json.dumps({"username": self._username, "password": password})
                    + "\n"
                )
                proc.stdin.close()

            timer = threading.Timer(
                self._max_attempt_seconds, self._kill_overrun, [proc]
            )
            timer.daemon = True
            timer.start()
            self._attempt = _Attempt(
                ipc_dir=ipc_dir, proc=proc, started_at=time.monotonic(), timer=timer
            )

    def submit_otp(self, code: str) -> None:
        """Relay a verification code to a parked worker.

        A no-op unless an attempt is actually waiting for one, so a stray
        submission cannot disturb anything.
        """
        if not code:
            return
        with self._lock:
            self._refresh_locked()
            attempt = self._attempt
            if attempt is None or attempt.state is not LoginState.AWAITING_OTP:
                return
            (attempt.ipc_dir / "otp").write_text(code, encoding="utf-8")

    def status(self) -> LoginStatus:
        """Return the current attempt's state, ready for a password if idle."""
        with self._lock:
            self._refresh_locked()
            attempt = self._attempt
            if attempt is None:
                return LoginStatus(
                    LoginState.AWAITING_PASSWORD,
                    _MESSAGES[LoginState.AWAITING_PASSWORD],
                )
            return LoginStatus(attempt.state, attempt.detail)

    def shutdown(self) -> None:
        """Kill any running worker and forget the attempt (tests, shutdown)."""
        with self._lock:
            self._teardown_locked()

    # -- internals ------------------------------------------------------------

    def _refresh_locked(self) -> None:
        """Update the cached attempt state from the worker's status file."""
        attempt = self._attempt
        if attempt is None or attempt.state in _TERMINAL:
            self._expire_stale_terminal_locked()
            return

        status_file = attempt.ipc_dir / "status.json"
        if status_file.is_file():
            try:
                payload = json.loads(status_file.read_text(encoding="utf-8"))
                attempt.state = LoginState(payload["state"])
                attempt.detail = str(payload["detail"])
            except (ValueError, KeyError):
                pass

        exited = attempt.proc.poll() is not None
        if attempt.state not in _TERMINAL and exited:
            attempt.state = LoginState.FAILED
            attempt.detail = _MESSAGES[LoginState.FAILED]

        if attempt.state not in _TERMINAL and (
            time.monotonic() - attempt.started_at > self._max_attempt_seconds
        ):
            self._kill_overrun(attempt.proc)
            attempt.state = LoginState.EXPIRED
            attempt.detail = _MESSAGES[LoginState.EXPIRED]

        if attempt.state in _TERMINAL and attempt.finished_at is None:
            attempt.finished_at = time.monotonic()
            attempt.timer.cancel()

    def _expire_stale_terminal_locked(self) -> None:
        attempt = self._attempt
        if (
            attempt is not None
            and attempt.finished_at is not None
            and time.monotonic() - attempt.finished_at > TERMINAL_RETENTION_SECONDS
        ):
            self._teardown_locked()

    def _teardown_locked(self) -> None:
        attempt = self._attempt
        if attempt is None:
            return
        attempt.timer.cancel()
        self._kill_overrun(attempt.proc)
        shutil.rmtree(attempt.ipc_dir, ignore_errors=True)
        self._attempt = None

    @staticmethod
    def _kill_overrun(proc: subprocess.Popen[str]) -> None:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
