#!/usr/bin/env python3
"""Capture an authenticated Sainsbury's session for later, logged-in actions.

For `browser_interaction_mcp.sainsburys.add_to_basket` and anything else that
needs to act as a signed-in Sainsbury's account.

**Run this locally, by hand, with your own Sainsbury's credentials - never
inside the MCP server, never in an automated environment, and never in a
session that isn't yours.** It opens a real, visible Chromium window (headed,
because Sainsbury's blocks headless Chromium outright - see `browser.py` and
`sainsburys.py`'s docstrings), points it at Sainsbury's own login page, and
waits for *you* to type your own credentials into *Sainsbury's own page* and
complete login yourself - including an MFA/OTP step, if Sainsbury's asks for
one; it doesn't always, seemingly depending on whether the device or network
is already trusted. Nothing this script does can see, store, or transmit what
you type: it only watches for the page to leave the login flow, then asks
Playwright to dump the resulting cookies and local storage - the "session
context" - to a JSON file.

That file is the entire output. It contains session tokens, not a password,
but a session token is enough to act as you until it expires or is revoked -
treat it exactly as sensitively as the login itself:

- Never commit it. It matches no repository path that ships, but keep it
  outside the repository entirely to be sure.
- Set restrictive file permissions on it (this script does so on POSIX).
- Revoke it by signing out of that session on Sainsbury's, or by simply
  deleting the file and rerunning this script when it has expired.

Usage:
    uv run scripts/sainsburys_login.py [output-path]

`output-path` defaults to `sainsburys_storage_state.json` in the current
directory. Point `BROWSER_MCP_SAINSBURYS_STORAGE_STATE_PATH` at wherever you
save it.
"""

from __future__ import annotations

import re
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from browser_interaction_mcp.browser import browser_page

if TYPE_CHECKING:
    from playwright.sync_api import Page

# Kept in sync with sainsburys.py by hand rather than imported: this script
# is deliberately outside the tested, type-checked package (see
# pyproject.toml). Taken from a real login recording, not guessed - see
# sainsburys.py's module docstring.
_LOGIN_PATH = "/gol-ui/oauth/login"
LOGIN_URL = f"https://www.sainsburys.co.uk{_LOGIN_PATH}"

# MFA, when Sainsbury's asks for it, happens on a different domain entirely -
# also from the real recording. Still "not logged in yet", same as the login
# page itself.
_MFA_PATH = "/gol/login/mfa"

_MY_ACCOUNT_URL = "https://www.sainsburys.co.uk/gol-ui/MyAccount"
_CONSENT_BUTTON_NAME = re.compile("accept|required only", re.IGNORECASE)

DEFAULT_OUTPUT = Path("sainsburys_storage_state.json")
_LOGIN_TIMEOUT_SECONDS = 300.0


def _still_logging_in(url: str) -> bool:
    """Return whether ``url`` is still part of the login flow, not past it."""
    return _LOGIN_PATH in url or _MFA_PATH in url


def _wait_for_login(page: Page, timeout_seconds: float) -> bool:
    """Block until the page has navigated past both the login and MFA steps.

    Args:
        page: The Playwright page the operator is logging in on.
        timeout_seconds: How long to wait before giving up.

    Returns:
        True once logged in, False if the timeout elapsed first.
    """
    try:
        page.wait_for_url(
            lambda url: not _still_logging_in(url),
            timeout=timeout_seconds * 1000,
        )
    except PlaywrightTimeoutError:
        return False
    return True


def _dismiss_consent_banner_if_present(page: Page) -> None:
    """Best-effort dismissal of whichever consent banner copy is showing.

    Not load-bearing for capturing the session - only for leaving the
    captured page in a clean state - so a missing button is not an error.
    """
    button = page.get_by_role("button", name=_CONSENT_BUTTON_NAME).first
    if button.count() > 0:
        button.click(timeout=15_000)


def main(argv: list[str]) -> int:
    output = Path(argv[0]) if argv else DEFAULT_OUTPUT

    print(
        "Opening a real Chromium window. Log in with your own Sainsbury's "
        "credentials, complete an additional verification step if you're "
        "asked for one, and wait for the page to leave the login screen. "
        "Nothing you type is seen by this script.",
        file=sys.stderr,
    )

    with browser_page(headless=False) as page:
        page.goto(LOGIN_URL, wait_until="load", timeout=30_000)
        _dismiss_consent_banner_if_present(page)

        if not _wait_for_login(page, timeout_seconds=_LOGIN_TIMEOUT_SECONDS):
            print(
                f"Still logging in after {_LOGIN_TIMEOUT_SECONDS:.0f}s - not "
                "saving anything. Rerun and try again.",
                file=sys.stderr,
            )
            return 1

        # Normalise onto a known, stable authenticated page before capturing -
        # the real flow shows a second consent banner after the login
        # redirect, which this also clears.
        page.goto(_MY_ACCOUNT_URL, wait_until="load", timeout=30_000)
        _dismiss_consent_banner_if_present(page)

        page.context.storage_state(path=output)

    output.chmod(stat.S_IRUSR | stat.S_IWUSR)  # rw for the owner only

    print(f"Session captured to {output}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
