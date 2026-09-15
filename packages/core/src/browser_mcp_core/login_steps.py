"""The site-supplied half of a real login, and the generic driver around it.

Logging in for real is the one thing every site does differently and the one
thing whose *shape* is identical everywhere: open the form, type a username,
type a password, submit, find out whether an MFA code is being asked for,
submit one if so, then check whether any of it worked. :func:`refresh_session`
owns that shape - including the parts most easily got wrong twice, like
chmod'ing the captured session - and a site owns the steps.

The split is what docs/scaling-plan.md D2 means by "the site supplying a
``LoginSteps`` implementation". Note what is *not* here: no page-interaction
helpers, no waiting strategy, no consent-banner handling beyond asking the site
for its cookies. Those are patterns learned from one site, and D2's rule is to
promote on the second occurrence rather than generalise from the first.
"""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING, Protocol

from browser_mcp_core.browser import browser_page
from browser_mcp_core.errors import NotLoggedInError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from playwright.sync_api import Page


class LoginSteps(Protocol):
    """One site's real login, decomposed into the steps :func:`refresh_session` drives.

    Every method is handed the same page, in order. A step that has to wait for
    something owns that wait: core has no opinion about how a given site's
    pages settle, which is exactly the opinion that should not be generalised
    from one example.
    """

    @property
    def display_name(self) -> str:
        """The site's name as a person would write it, e.g. ``Sainsbury's``.

        Used in the login page's copy and in the message raised when a
        verification code was asked for and not supplied.
        """

    @property
    def headless(self) -> bool:
        """Whether this site's login can run in headless Chromium.

        ``False`` for the sites that fingerprint and block it - which costs a
        headed browser under Xvfb, and around 400MB. See docs/scaling-plan.md
        D7 for why that number decides how many apps a host can hold.
        """

    def consent_cookies(self) -> list[dict[str, object]]:
        """Return cookies to seed before the first navigation, if any.

        Typically a consent-management platform's "a choice has been made"
        cookies, so a blocking banner never renders in the first place.
        """

    def open_login_form(self, page: Page) -> None:
        """Navigate to the login form and wait until it can be filled in."""

    def fill_username(self, page: Page, username: str) -> None:
        """Type the account username into the form."""

    def fill_password(self, page: Page, password: str) -> None:
        """Type the account password into the form."""

    def submit(self, page: Page) -> None:
        """Submit the credentials and wait for whatever comes next."""

    def otp_requested(self, page: Page) -> bool:
        """Report whether the site is asking for a verification code.

        Not every site asks every time - Sainsbury's does not - so this is a
        question, not an assumption. A site with unconditional MFA can simply
        return ``True``.
        """

    def submit_otp(self, page: Page, code: str) -> None:
        """Enter a verification code, submit it, and wait for the result."""

    def confirm(self, page: Page, *, screenshot_path: Path | None) -> None:
        """Check the login worked, raising if it did not.

        Args:
            page: The page to read the outcome from.
            screenshot_path: Where to write a screenshot of the failure page,
                if the site captures one. The failure page is usually the only
                thing that says *why*.

        Raises:
            NotLoggedInError: If the session is not authenticated.
        """


def refresh_session(  # noqa: PLR0913 - the steps, plus one login's inputs
    steps: LoginSteps,
    username: str,
    password: str,
    *,
    storage_state_path: Path,
    get_otp: Callable[[], str | None],
    failure_screenshot_path: Path | None = None,
) -> None:
    """Log in for real, and overwrite ``storage_state_path`` with the result.

    The one function in core that handles a password. Neither ``username`` nor
    ``password`` is written anywhere by this function or anything it calls;
    only the resulting session is.

    Args:
        steps: The site's own login steps.
        username: Account username, typed into the form exactly as a person
            would.
        password: Account password, typed into the form.
        storage_state_path: Where to write the resulting Playwright
            ``storage_state`` JSON. Overwritten if it already exists.
        get_otp: Called only if the site asks for a verification code - which
            is not guaranteed to happen. Should return the code to submit, or
            ``None`` if the operator declined or none was available; either
            way, ``None`` aborts the refresh rather than submitting an empty
            code.
        failure_screenshot_path: If set, passed to the site's ``confirm`` step
            as somewhere to write a screenshot of the failure page.

    Raises:
        NotLoggedInError: If MFA was required but ``get_otp`` returned ``None``,
            or if the session still is not authenticated afterwards - most
            likely a wrong password.
    """
    with browser_page(headless=steps.headless, cookies=steps.consent_cookies()) as page:
        steps.open_login_form(page)
        steps.fill_username(page, username)
        steps.fill_password(page, password)
        steps.submit(page)

        if steps.otp_requested(page):
            otp = get_otp()
            if otp is None:
                msg = (
                    f"{steps.display_name} asked for a verification code, and "
                    f"none was provided - not completing the login."
                )
                raise NotLoggedInError(msg)
            steps.submit_otp(page, otp)

        steps.confirm(page, screenshot_path=failure_screenshot_path)
        page.context.storage_state(path=storage_state_path)

    # rw for the owner only: this file is as sensitive as the login that
    # produced it. Done here, not by each caller, so every site gets it for
    # free and no site can forget it.
    storage_state_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
