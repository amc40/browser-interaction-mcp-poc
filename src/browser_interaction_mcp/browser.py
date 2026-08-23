"""Shared browser sessions for every action, parameterised by headless/headed.

Headless is the default every action should reach for: no virtual display, no
extra process, and noticeably less memory than a real (headed) Chromium
instance under Xvfb. Headed exists only for the sites that specifically
fingerprint headless Chromium and block it - see sainsburys.py's module
docstring for how that was diagnosed - and each action opts into it
individually, action by action, rather than the server paying the Xvfb cost
for every call regardless of whether that call needs it.
"""

from __future__ import annotations

import contextlib
import os
import selectors
import subprocess
from typing import TYPE_CHECKING

from playwright.sync_api import sync_playwright

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Protocol

    from playwright.sync_api import Browser, Page, ViewportSize

    class _PollableProcess(Protocol):
        """The one thing `_read_display_number` needs from a process handle.

        Narrower than `subprocess.Popen[bytes]` on purpose, so a test double
        only has to implement `poll()` rather than stand in for the whole
        class.
        """

        def poll(self) -> int | None:
            """Return the process's exit code, or None while still running."""
            ...


#: A realistic desktop Chrome UA. Not a fingerprint-evasion attempt: it's the
#: browser's own engine version, just without the "Headless" branding that
#: legacy headless Chromium (still what Playwright launches by default) adds.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_VIEWPORT: ViewportSize = {"width": 1366, "height": 900}
_LOCALE = "en-GB"
_TIMEZONE_ID = "Europe/London"
_EXTRA_HTTP_HEADERS = {"Accept-Language": "en-GB,en;q=0.9"}


def _new_context(browser: Browser) -> Page:
    context = browser.new_context(
        user_agent=USER_AGENT,
        viewport=_VIEWPORT,
        locale=_LOCALE,
        timezone_id=_TIMEZONE_ID,
        extra_http_headers=_EXTRA_HTTP_HEADERS,
    )
    return context.new_page()


@contextlib.contextmanager
def _virtual_display(timeout: float = 5.0) -> Iterator[str]:
    """Start a short-lived Xvfb display, and stop it again on exit.

    Only entered for headed sessions - headless Chromium needs no display at
    all - and only for as long as one browser session needs it, rather than
    for the server's whole lifetime, which is why this isn't just the whole
    service wrapped in `xvfb-run`.

    Asks Xvfb to pick its own free display number and report it back over a
    pipe (`-displayfd`), rather than this process scanning `/tmp/.X*-lock`
    itself: that would mean checking for a file and then creating it as two
    separate steps, racing any other process doing the same thing at the same
    moment - `-displayfd` makes picking the number Xvfb's own atomic problem,
    not ours.
    """
    read_fd, write_fd = os.pipe()
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell, no user input
        [
            "/usr/bin/Xvfb",
            "-displayfd",
            str(write_fd),
            "-screen",
            "0",
            "1366x900x24",
            "-nolisten",
            "tcp",
        ],
        pass_fds=(write_fd,),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.close(write_fd)
    try:
        display = f":{_read_display_number(read_fd, process, timeout)}"
        try:
            yield display
        finally:
            process.terminate()
            process.wait(timeout=5)
    finally:
        os.close(read_fd)


def _read_display_number(
    read_fd: int,
    process: _PollableProcess,
    timeout: float,
) -> str:
    with selectors.DefaultSelector() as selector:
        selector.register(read_fd, selectors.EVENT_READ)
        if not selector.select(timeout=timeout):
            return_code = process.poll()
            status = (
                f"exited with {return_code}"
                if return_code is not None
                else "still running"
            )
            msg = (
                f"Timed out after {timeout}s waiting for Xvfb to report its "
                f"display number (process {status})."
            )
            raise RuntimeError(msg)
    reported = os.read(read_fd, 32).strip()
    if not reported:
        msg = "Xvfb closed its -displayfd pipe without reporting a display number."
        raise RuntimeError(msg)
    return reported.decode("ascii")


@contextlib.contextmanager
def browser_page(*, headless: bool) -> Iterator[Page]:
    """Open one page in a fresh browser and context, closing both on exit.

    Args:
        headless: False for sites that block headless Chromium specifically
            (runs under a short-lived Xvfb display instead); True otherwise,
            for the lower memory footprint - prefer True unless a specific
            site has already been shown to need otherwise.

    Yields:
        A ready-to-use page, in a fresh, empty browser context.
    """
    with contextlib.ExitStack() as stack:
        launch_env: dict[str, str | float | bool] | None = None
        if not headless:
            display = stack.enter_context(_virtual_display())
            launch_env = {**os.environ, "DISPLAY": display}

        playwright = stack.enter_context(sync_playwright())
        browser = playwright.chromium.launch(headless=headless, env=launch_env)
        stack.callback(browser.close)
        yield _new_context(browser)
