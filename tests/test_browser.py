"""Tests for shared browser session management.

Nothing here spawns a real Xvfb or a real browser: `subprocess.Popen` and
`sync_playwright` are both faked at the boundary, so these stay fast and
offline. `_virtual_display`'s use of a real `os.pipe()` is kept genuine
though - it's the mechanism under test, not something worth faking away.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from browser_interaction_mcp import browser

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping


# ---------------------------------------------------------------------------
# _read_display_number
# ---------------------------------------------------------------------------
@dataclass
class FakeProcess:
    """Stands in for the subprocess.Popen handle `_read_display_number` polls."""

    exit_code: int | None = None

    def poll(self) -> int | None:
        """Return the pre-set exit code, or None if "still running"."""
        return self.exit_code


def test_reads_the_display_number_once_written() -> None:
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"42\n")
        os.close(write_fd)

        assert browser._read_display_number(read_fd, FakeProcess(), timeout=1) == "42"
    finally:
        os.close(read_fd)


def test_raises_on_timeout_while_the_process_is_still_running() -> None:
    read_fd, write_fd = os.pipe()
    try:
        with pytest.raises(RuntimeError, match="still running"):
            browser._read_display_number(read_fd, FakeProcess(), timeout=0.05)
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_raises_on_timeout_mentioning_an_early_exit() -> None:
    read_fd, write_fd = os.pipe()
    try:
        with pytest.raises(RuntimeError, match="exited with 1"):
            browser._read_display_number(
                read_fd, FakeProcess(exit_code=1), timeout=0.05
            )
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_raises_when_the_pipe_closes_with_nothing_written() -> None:
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    try:
        with pytest.raises(RuntimeError, match="without reporting"):
            browser._read_display_number(read_fd, FakeProcess(), timeout=1)
    finally:
        os.close(read_fd)


# ---------------------------------------------------------------------------
# _virtual_display
# ---------------------------------------------------------------------------
@dataclass
class FakePopen:
    """Stands in for subprocess.Popen.

    Writes a canned display number itself, the way a real Xvfb child would
    through its inherited copy of the fd.
    """

    argv: list[str]
    pass_fds: tuple[int, ...] = ()
    terminated: bool = False
    waited: bool = False

    def __post_init__(self) -> None:
        """Write the canned display number, as Xvfb's child process would."""
        os.write(self.pass_fds[0], b"7\n")

    def terminate(self) -> None:
        """Record that the process was asked to stop."""
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        """Record that the caller waited for exit, and report success."""
        del timeout
        self.waited = True
        return 0


def test_virtual_display_yields_a_colon_prefixed_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    popens: list[FakePopen] = []

    def fake_popen(
        argv: list[str],
        *,
        pass_fds: tuple[int, ...],
        **_kwargs: object,
    ) -> FakePopen:
        popen = FakePopen(argv, pass_fds=pass_fds)
        popens.append(popen)
        return popen

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with browser._virtual_display() as display:
        assert display == ":7"
        assert popens[0].argv[0] == "/usr/bin/Xvfb"
        assert "-displayfd" in popens[0].argv

    assert popens[0].terminated
    assert popens[0].waited


# ---------------------------------------------------------------------------
# browser_page
# ---------------------------------------------------------------------------
@dataclass
class FakePage:
    """A no-op stand-in page; browser_page only has to yield it, not use it."""


@dataclass
class FakeContext:
    """Stands in for a Playwright BrowserContext."""

    def new_page(self) -> FakePage:
        """Return the fake page."""
        return FakePage()


@dataclass
class FakeBrowser:
    """Stands in for a Playwright Browser, recording its launch env and close."""

    closed: bool = False
    context_kwargs: Mapping[str, object] = field(default_factory=dict)

    def new_context(self, **kwargs: object) -> FakeContext:
        """Record the context kwargs and return the fake context."""
        self.context_kwargs = kwargs
        return FakeContext()

    def close(self) -> None:
        """Record that the browser was closed."""
        self.closed = True


@dataclass
class FakeChromium:
    """Stands in for `playwright.chromium`, recording how it was launched."""

    browser_to_return: FakeBrowser
    launch_kwargs: Mapping[str, object] = field(default_factory=dict)

    def launch(self, **kwargs: object) -> FakeBrowser:
        """Record the launch kwargs and return the fake browser."""
        self.launch_kwargs = kwargs
        return self.browser_to_return


@dataclass
class FakePlaywright:
    """Stands in for the object `sync_playwright()` yields."""

    chromium: FakeChromium


@dataclass
class FakeSyncPlaywrightContextManager:
    """Stands in for the object `sync_playwright` itself returns."""

    playwright: FakePlaywright

    def __enter__(self) -> FakePlaywright:
        """Enter the fake context, returning the fake playwright object."""
        return self.playwright

    def __exit__(self, *exc_info: object) -> None:
        """Exit the fake context; nothing to clean up."""
        del exc_info


def _wire(monkeypatch: pytest.MonkeyPatch) -> FakeChromium:
    """Monkeypatch `sync_playwright` to serve a fresh fake browser/chromium."""
    fake_browser = FakeBrowser()
    chromium = FakeChromium(browser_to_return=fake_browser)
    playwright = FakePlaywright(chromium=chromium)
    monkeypatch.setattr(
        browser,
        "sync_playwright",
        lambda: FakeSyncPlaywrightContextManager(playwright),
    )
    return chromium


def test_headless_session_never_starts_a_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chromium = _wire(monkeypatch)

    def fail_if_called() -> object:
        msg = "headless sessions must not start Xvfb"
        raise AssertionError(msg)

    monkeypatch.setattr(browser, "_virtual_display", fail_if_called)

    with browser.browser_page(headless=True) as page:
        assert isinstance(page, FakePage)

    assert chromium.launch_kwargs == {"headless": True, "env": None}
    assert chromium.browser_to_return.closed


def test_headed_session_starts_a_display_and_sets_it_in_the_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chromium = _wire(monkeypatch)

    @contextlib.contextmanager
    def fake_virtual_display() -> Iterator[str]:
        yield ":55"

    monkeypatch.setattr(browser, "_virtual_display", fake_virtual_display)

    with browser.browser_page(headless=False):
        pass

    assert chromium.launch_kwargs["headless"] is False
    launch_env = chromium.launch_kwargs["env"]
    assert isinstance(launch_env, dict)
    assert launch_env["DISPLAY"] == ":55"
    assert chromium.browser_to_return.closed


def test_context_is_created_with_the_shared_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chromium = _wire(monkeypatch)

    with browser.browser_page(headless=True):
        pass

    kwargs = chromium.browser_to_return.context_kwargs
    assert kwargs["user_agent"] == browser.USER_AGENT
    assert kwargs["locale"] == "en-GB"
    assert kwargs["timezone_id"] == "Europe/London"


def test_context_has_no_storage_state_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chromium = _wire(monkeypatch)

    with browser.browser_page(headless=True):
        pass

    assert chromium.browser_to_return.context_kwargs["storage_state"] is None


def test_storage_state_is_passed_through_to_the_new_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chromium = _wire(monkeypatch)

    with browser.browser_page(headless=True, storage_state="session.json"):
        pass

    assert chromium.browser_to_return.context_kwargs["storage_state"] == "session.json"
