"""Tests for the Sainsbury's login subprocess entry point."""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING

from browser_interaction_mcp import sainsburys
from browser_interaction_mcp import sainsburys_login_worker as worker

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest


def _run(
    monkeypatch: pytest.MonkeyPatch,
    ipc_dir: Path,
    *,
    stdin: str,
    refresh: Callable[..., None],
    otp_timeout: str = "300",
) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    monkeypatch.setattr(sainsburys, "refresh_session", refresh)
    return worker.main(
        [
            "--storage-state",
            str(ipc_dir / "session.json"),
            "--ipc-dir",
            str(ipc_dir),
            "--otp-timeout",
            otp_timeout,
        ]
    )


def _status(ipc_dir: Path) -> dict[str, str]:
    parsed: dict[str, str] = json.loads(
        (ipc_dir / "status.json").read_text(encoding="utf-8")
    )
    return parsed


def test_a_successful_login_reports_done(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[tuple[str, str]] = []

    def refresh(username: str, password: str, **_: object) -> None:
        seen.append((username, password))

    code = _run(
        monkeypatch,
        tmp_path,
        stdin='{"username": "a@b.com", "password": "hunter2"}',
        refresh=refresh,
    )

    assert code == 0
    assert seen == [("a@b.com", "hunter2")]
    assert _status(tmp_path)["state"] == "done"


def test_a_login_failure_surfaces_the_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def refresh(*_: object, **__: object) -> None:
        msg = "check the password is correct"
        raise sainsburys.NotLoggedInError(msg)

    code = _run(
        monkeypatch,
        tmp_path,
        stdin='{"username": "a@b.com", "password": "wrong"}',
        refresh=refresh,
    )

    assert code == 1
    assert _status(tmp_path) == {
        "state": "failed",
        "detail": "check the password is correct",
    }


def test_an_unexpected_error_is_reported_generically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def refresh(*_: object, **__: object) -> None:
        msg = "chromium exploded with secret hunter2"
        raise RuntimeError(msg)

    code = _run(
        monkeypatch,
        tmp_path,
        stdin='{"username": "a@b.com", "password": "hunter2"}',
        refresh=refresh,
    )

    assert code == 1
    status = _status(tmp_path)
    assert status["state"] == "failed"
    assert "hunter2" not in status["detail"]


def test_a_missing_password_fails_before_launching_a_browser(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def refresh(*_: object, **__: object) -> None:
        msg = "should not be called"
        raise AssertionError(msg)

    code = _run(
        monkeypatch,
        tmp_path,
        stdin='{"username": "a@b.com", "password": ""}',
        refresh=refresh,
    )

    assert code == 1
    assert _status(tmp_path)["detail"] == "No password was provided."


def test_unparseable_stdin_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code = _run(
        monkeypatch,
        tmp_path,
        stdin="not json",
        refresh=lambda *_, **__: None,
    )

    assert code == 1
    assert _status(tmp_path)["state"] == "failed"


def test_get_otp_returns_a_code_written_to_the_ipc_file(tmp_path: Path) -> None:
    (tmp_path / "otp").write_text("123456", encoding="utf-8")

    get_otp = worker._make_get_otp(tmp_path, timeout=5.0)

    assert get_otp() == "123456"
    assert not (tmp_path / "otp").exists()
    # Acknowledge the code before returning so the page leaves the OTP form.
    assert _status(tmp_path)["state"] == "logging_in"


def test_get_otp_gives_up_after_the_timeout(tmp_path: Path) -> None:
    get_otp = worker._make_get_otp(tmp_path, timeout=0.0)

    assert get_otp() is None


def test_get_otp_treats_a_blank_code_file_as_no_code(tmp_path: Path) -> None:
    (tmp_path / "otp").write_text("   ", encoding="utf-8")

    get_otp = worker._make_get_otp(tmp_path, timeout=5.0)

    assert get_otp() is None
    assert _status(tmp_path)["state"] == "awaiting_otp"


def test_get_otp_polls_until_the_code_appears(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_sleep(_seconds: float) -> None:
        (tmp_path / "otp").write_text("654321", encoding="utf-8")

    monkeypatch.setattr("time.sleep", fake_sleep)
    get_otp = worker._make_get_otp(tmp_path, timeout=5.0)

    assert get_otp() == "654321"
