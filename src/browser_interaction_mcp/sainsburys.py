"""Browser actions against the Sainsbury's groceries site.

Most of this module (`products_we_love`) reads the public homepage: no login
is needed, and nothing there touches the operator's own session or
credentials. `add_to_basket` is the exception - adding an item to a basket
that persists needs to be logged in as somebody, and this project's answer to
"as whom" is deliberately narrow: never the operator's password.
`add_to_basket` only ever *reuses* a session captured ahead of time by
`scripts/sainsburys_login.py`, which the operator runs locally, logs in by
hand, and which then hands Playwright's `storage_state` - cookies and local
storage - to `browser.browser_page`. The server never sees a password, an
OTP, or anything it could be tricked into typing into a phishing page; it can
only replay a session someone else already established. See
`browser.browser_page`'s docstring for the mechanism and
`docs/deployment.md` §7 for why that captured session is worth protecting as
carefully as the credentials it stands in for.

**`add_to_basket` is unverified against the real site** - see the
"Not done yet" section of the README. It cannot be exercised without a
captured session, which this sandbox has no credentials to create; treat its
locators as a first draft to validate with `scripts/sainsburys_add_to_basket.py`
once one exists, the way `products_we_love` itself was (see the commit that
first added it, before the fixes that followed from running it for real).

Verified against the real page from the deployment host (not this dev
sandbox, whose network path Sainsbury's Akamai edge blocks outright - see
docs/self-healing.md and the commit history here for how that was diagnosed).
Two things learned there that aren't obvious from the site alone:

- Akamai's Bot Manager blocks *headless* Chromium specifically - real,
  visible-mode Chromium under a virtual display gets through cleanly with
  the same navigation. `products_we_love` asks browser.browser_page for a
  headed session for exactly this reason; switching it to headless will
  reintroduce the block.
- Product names under "Products we love" are themselves heading elements,
  immediately following the section's own heading in document order - not
  text pulled from the tile links.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from browser_interaction_mcp.browser import browser_page

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Locator, Page

GROCERIES_URL = "https://www.sainsburys.co.uk/gol-ui/groceries"

# A real product page, guessed from Sainsbury's URL pattern rather than
# verified by loading it - see the module docstring. Good enough to give
# `add_to_basket` a default that isn't obviously a placeholder; wrong the
# moment the real page is checked and this can be swapped for it.
DEFAULT_PRODUCT_URL = (
    "https://www.sainsburys.co.uk/gol-ui/product/"
    "sainsburys-british-semi-skimmed-milk-2-27l-4pt"
)

_PRODUCTS_WE_LOVE_HEADING = re.compile("products we love", re.IGNORECASE)

# OneTrust's own button text here is "Continue and accept" - kept broad
# ("accept" anywhere in the name) since consent-management vendors change
# their copy without warning, and this is scoped to a button element already.
_COOKIE_ACCEPT_NAME = re.compile("accept", re.IGNORECASE)

# Non-product headings that can appear inside/around the carousel widget
# (an accessible label for the carousel region, a trailing copyright-terms
# link styled as a heading) - skipped rather than counted as products.
_NON_PRODUCT_HEADINGS = re.compile("^(carousel|copyright terms)$", re.IGNORECASE)

# Sainsbury's own login page, redirected to when a session isn't (or is no
# longer) authenticated. Checked by path only, not the full URL, since query
# strings there commonly carry a return-to address.
_LOGIN_PATH = "/gol-ui/login"

_ADD_TO_BASKET_NAME = re.compile("add to basket", re.IGNORECASE)


class NotLoggedInError(RuntimeError):
    """Raised when an action needs an authenticated session that isn't set up.

    Covers both cases the operator can hit: no captured session at all, and
    one that Sainsbury's no longer accepts (expired, signed out elsewhere).
    Either way the fix is the same - rerun the login script - so both raise
    this rather than being told apart.
    """


def _dismiss_cookie_banner(page: Page) -> None:
    button = page.get_by_role("button", name=_COOKIE_ACCEPT_NAME).first
    if button.count() > 0:
        button.click(timeout=15_000)


def _heading_texts(page: Page) -> list[str]:
    headings = page.get_by_role("heading").all()
    return [heading.inner_text().strip() for heading in headings]


def _names_after_heading(headings: list[str], count: int) -> list[str]:
    """Return up to ``count`` product names following the section heading.

    Args:
        headings: Every heading's text, in document order.
        count: How many product names to return.

    Returns:
        Up to ``count`` product names, in the order they appear on the page.

    Raises:
        RuntimeError: If no "Products we love" heading is present at all.
    """
    index = next(
        (
            i
            for i, text in enumerate(headings)
            if _PRODUCTS_WE_LOVE_HEADING.search(text)
        ),
        None,
    )
    if index is None:
        msg = (
            'No "Products we love" heading found on the page. Either the '
            "section has been renamed/removed, or the page did not load as "
            "expected."
        )
        raise RuntimeError(msg)

    names: list[str] = []
    for text in headings[index + 1 :]:
        if not text or _NON_PRODUCT_HEADINGS.match(text) or text in names:
            continue
        names.append(text)
        if len(names) == count:
            break
    return names


def _wait_for_page_to_settle(heading: Locator) -> None:
    """Wait for the page to be usable, without relying on "networkidle".

    The real site never reaches Playwright's networkidle state within a sane
    timeout (some background poller keeps the network busy indefinitely), so
    waiting for it would mean eating a full timeout on every call. Waiting for
    the section heading itself to appear is both faster and a more direct
    signal that the page is actually ready.
    """
    heading.wait_for(state="visible", timeout=20_000)


def products_we_love(url: str = GROCERIES_URL, count: int = 5) -> list[str]:
    """Return the first ``count`` product names under "Products we love".

    Args:
        url: Page to load. Defaults to the live groceries homepage.
        count: How many product names to return.

    Returns:
        Up to ``count`` product names, in the order they appear on the page.

    Raises:
        RuntimeError: If the section cannot be located on the loaded page.
    """
    with browser_page(headless=False) as page:
        page.goto(url, wait_until="load", timeout=30_000)
        _dismiss_cookie_banner(page)

        heading = page.get_by_role("heading", name=_PRODUCTS_WE_LOVE_HEADING).first
        _wait_for_page_to_settle(heading)

        return _names_after_heading(_heading_texts(page), count)


def _check_logged_in(page: Page) -> None:
    """Raise if navigation landed on the login page instead of the product.

    Args:
        page: The page after `goto`, following any redirect.

    Raises:
        NotLoggedInError: If the current page is Sainsbury's login page -
            the session in `storage_state` is missing, expired, or was
            never authenticated to begin with.
    """
    if _LOGIN_PATH in page.url:
        msg = (
            "Redirected to the Sainsbury's login page: the saved session is "
            "missing or no longer accepted. Run scripts/sainsburys_login.py "
            "locally to log in by hand and capture a fresh one - see "
            "browser.browser_page's docstring for how it's used from there."
        )
        raise NotLoggedInError(msg)


def add_to_basket(
    url: str = DEFAULT_PRODUCT_URL,
    *,
    storage_state_path: Path,
    quantity: int = 1,
) -> str:
    """Add ``quantity`` of the product at ``url`` to the basket.

    Requires an already-authenticated session - see the module docstring and
    `browser.browser_page`'s `storage_state` parameter. This action is
    unverified against the real site (see the module docstring); expect its
    locators to need fixing against the actual page before it works.

    Args:
        url: Product page to load. Defaults to a single guessed product -
            see `DEFAULT_PRODUCT_URL`.
        storage_state_path: Path to a Playwright `storage_state` JSON file
            holding a logged-in session, captured by
            `scripts/sainsburys_login.py`.
        quantity: How many of the product to add. Clicks the "Add to basket"
            control this many times, since that is what a first-time add
            does on Sainsbury's own product pages - there is no quantity
            field until at least one is already in the basket.

    Returns:
        The product's name, as shown on the page, confirming what was added.

    Raises:
        NotLoggedInError: If the saved session is missing or not accepted.
        RuntimeError: If the "Add to basket" control cannot be found.
    """
    with browser_page(headless=False, storage_state=storage_state_path) as page:
        page.goto(url, wait_until="load", timeout=30_000)
        _dismiss_cookie_banner(page)
        _check_logged_in(page)

        name_heading = page.get_by_role("heading", level=1).first
        _wait_for_page_to_settle(name_heading)
        product_name = name_heading.inner_text().strip()

        add_button = page.get_by_role("button", name=_ADD_TO_BASKET_NAME).first
        if add_button.count() == 0:
            msg = (
                '"Add to basket" control not found on the page. Either the '
                "product page has changed, or the product is unavailable."
            )
            raise RuntimeError(msg)

        for _ in range(quantity):
            add_button.click(timeout=15_000)

        return product_name
