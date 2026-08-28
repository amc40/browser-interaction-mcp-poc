"""Tests for the out-of-band Sainsbury's login coordinator.

The real worker subprocess (Playwright, a real browser) is never launched: a
fake ``popen`` stands in, and the test drives the status file the worker would
otherwise write.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from browser_interaction_mcp import sainsburys_login_flow
from browser_interaction_mcp.sainsburys_login_flow import (
    LoginInProgressError,
    LoginState,
    SainsburysLoginFlow,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


class _FakeStdin:
    def __init__(self) -> None:
        self.written = ""
        self.closed = False

    def write(self, text: str) -> None:
        self.written += text

    def close(self) -> None:
        self.closed = True


class _FakeWorker:
    """Stands in for the login subprocess, driven by the test."""

    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.ipc_dir = Path(argv[argv.index("--ipc-dir") + 1])
        self.stdin = _FakeStdin()
        self._returncode: int | None = None
        self.killed = False

    # -- the surface SainsburysLoginFlow uses -----------------------------

    def poll(self) -> int | None:
        return self._returncode

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self._returncode or 0

    # -- test helpers ----------------------------------------------------

    @property
    def credentials(self) -> dict[str, str]:
        parsed: dict[str, str] = json.loads(self.stdin.written)
        return parsed

    def report(self, state: str, detail: str = "…") -> None:
        (self.ipc_dir / "status.json").write_text(
            json.dumps({"state": state, "detail": detail}), encoding="utf-8"
        )

    def exit(self, code: int = 0) -> None:
        self._returncode = code


@pytest.fixture
def flow_factory() -> Iterator[object]:
    flows: list[SainsburysLoginFlow] = []
    workers: list[_FakeWorker] = []

    def make(storage_state_path: Path) -> tuple[SainsburysLoginFlow, list[_FakeWorker]]:
        def popen(argv: list[str], **_: object) -> _FakeWorker:
            worker = _FakeWorker(argv)
            workers.append(worker)
            return worker

        flow = SainsburysLoginFlow(
            username="shopper@example.com",
            storage_state_path=storage_state_path,
            popen=popen,  # type: ignore[arg-type]
        )
        flows.append(flow)
        return flow, workers

    yield make

    for flow in flows:
        flow.shutdown()


def test_idle_flow_is_ready_for_a_password(
    flow_factory: object, tmp_path: Path
) -> None:
    flow, _ = flow_factory(tmp_path / "session.json")  # type: ignore[operator]

    assert flow.status().state is LoginState.AWAITING_PASSWORD


def test_start_launches_the_worker_with_the_credentials(
    flow_factory: object, tmp_path: Path
) -> None:
    flow, workers = flow_factory(tmp_path / "session.json")  # type: ignore[operator]

    flow.start("hunter2")

    assert flow.status().state is LoginState.LOGGING_IN
    assert workers[0].credentials == {
        "username": "shopper@example.com",
        "password": "hunter2",
    }
    assert workers[0].stdin.closed


def test_worker_success_is_surfaced_as_done(
    flow_factory: object, tmp_path: Path
) -> None:
    flow, workers = flow_factory(tmp_path / "session.json")  # type: ignore[operator]
    flow.start("hunter2")

    workers[0].report("done", "Your Sainsbury's session has been refreshed.")
    workers[0].exit(0)

    status = flow.status()
    assert status.state is LoginState.DONE
    assert status.terminal


def test_otp_is_relayed_only_while_the_worker_waits_for_one(
    flow_factory: object, tmp_path: Path
) -> None:
    flow, workers = flow_factory(tmp_path / "session.json")  # type: ignore[operator]
    flow.start("hunter2")

    # Nothing is waiting yet: the code is dropped.
    flow.submit_otp("000000")
    assert not (workers[0].ipc_dir / "otp").exists()

    workers[0].report("awaiting_otp", "Enter the code.")
    assert flow.status().state is LoginState.AWAITING_OTP

    # An empty submission must not reach the worker (it would abort the login).
    flow.submit_otp("")
    assert not (workers[0].ipc_dir / "otp").exists()

    flow.submit_otp("123456")
    assert (workers[0].ipc_dir / "otp").read_text(encoding="utf-8") == "123456"


def test_a_second_password_while_active_is_refused(
    flow_factory: object, tmp_path: Path
) -> None:
    flow, _ = flow_factory(tmp_path / "session.json")  # type: ignore[operator]
    flow.start("hunter2")

    with pytest.raises(LoginInProgressError):
        flow.start("another")


def test_a_fresh_attempt_after_a_terminal_one_is_allowed(
    flow_factory: object, tmp_path: Path
) -> None:
    flow, workers = flow_factory(tmp_path / "session.json")  # type: ignore[operator]
    flow.start("hunter2")
    workers[0].report("failed", "Wrong password.")
    workers[0].exit(1)
    assert flow.status().state is LoginState.FAILED

    flow.start("correct-horse")

    assert flow.status().state is LoginState.LOGGING_IN
    assert len(workers) == 2
    assert not workers[0].ipc_dir.exists()  # old scratch dir cleaned up


def test_worker_exiting_without_a_verdict_is_a_failure(
    flow_factory: object, tmp_path: Path
) -> None:
    flow, workers = flow_factory(tmp_path / "session.json")  # type: ignore[operator]
    flow.start("hunter2")

    workers[0].exit(1)  # died before writing any status

    assert flow.status().state is LoginState.FAILED


def test_an_overrunning_worker_is_killed(flow_factory: object, tmp_path: Path) -> None:
    flow, workers = flow_factory(tmp_path / "session.json")  # type: ignore[operator]
    flow.start("hunter2")
    workers[0].report("logging_in")
    flow._attempt.started_at -= 10_000

    status = flow.status()

    assert status.state is LoginState.EXPIRED
    assert workers[0].killed


def test_finished_attempts_are_forgotten_after_the_retention_window(
    flow_factory: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sainsburys_login_flow, "TERMINAL_RETENTION_SECONDS", -1.0)
    flow, workers = flow_factory(tmp_path / "session.json")  # type: ignore[operator]
    flow.start("hunter2")
    workers[0].report("done")
    workers[0].exit(0)
    assert flow.status().state is LoginState.DONE

    assert flow.status().state is LoginState.AWAITING_PASSWORD


def test_a_garbled_status_file_leaves_the_last_known_state(
    flow_factory: object, tmp_path: Path
) -> None:
    flow, workers = flow_factory(tmp_path / "session.json")  # type: ignore[operator]
    flow.start("hunter2")
    workers[0].report("logging_in", "Signing in…")
    assert flow.status().state is LoginState.LOGGING_IN

    (workers[0].ipc_dir / "status.json").write_text("{ broken", encoding="utf-8")

    assert flow.status().state is LoginState.LOGGING_IN


def test_start_tolerates_a_worker_without_a_stdin_pipe(
    tmp_path: Path,
) -> None:
    class _NoStdinWorker(_FakeWorker):
        def __init__(self, argv: list[str]) -> None:
            super().__init__(argv)
            self.stdin = None  # type: ignore[assignment]

    flow = SainsburysLoginFlow(
        username="shopper@example.com",
        storage_state_path=tmp_path / "session.json",
        popen=lambda argv, **_: _NoStdinWorker(argv),  # type: ignore[arg-type]
    )
    try:
        flow.start("hunter2")
        assert flow.status().state is LoginState.LOGGING_IN
    finally:
        flow.shutdown()


def test_shutdown_kills_a_running_worker(flow_factory: object, tmp_path: Path) -> None:
    flow, workers = flow_factory(tmp_path / "session.json")  # type: ignore[operator]
    flow.start("hunter2")

    flow.shutdown()

    assert workers[0].killed
    assert flow.status().state is LoginState.AWAITING_PASSWORD
