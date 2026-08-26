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

**`add_to_basket` itself is still unverified against the real, authenticated
site** - see the "Not done yet" section of the README - but its selectors are
no longer guesses: they were taken from a real Playwright codegen recording
of a manual login and search-and-add flow, not invented. What that recording
showed, and isn't obvious from the public pages alone:

- The login flow lives at `/gol-ui/oauth/login`, distinct from the public
  groceries site, and its cookie consent button reads "Required only" -
  different text from the "Continue and accept" `products_we_love` dismisses.
  `_dismiss_cookie_banner` matches either.
- MFA, when Sainsbury's asks for it, happens on a *different domain*
  (`account.sainsburys.co.uk/gol/login/mfa`) - not something `add_to_basket`
  itself ever has to handle, since by the time it runs the session is already
  captured, but `scripts/sainsburys_login.py` has to recognise it as "still
  logging in", not "done".
- Search is a `combobox`, filled and submitted with Enter - not a URL query
  parameter.
- Each search result is `data-testid="product-tile-<id>"`, and adding it to
  the basket is `data-testid="add-button"` *inside that same tile* - directly
  from the results, with no separate "Add to basket"-named button and no need
  to open the product page first.

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

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from browser_interaction_mcp.browser import browser_page

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Locator, Page

GROCERIES_URL = "https://www.sainsburys.co.uk/gol-ui/groceries"
MY_ACCOUNT_URL = "https://www.sainsburys.co.uk/gol-ui/MyAccount"

#: Search term `add_to_basket` uses when the caller doesn't give one -
#: verified against the real site: see the module docstring.
DEFAULT_SEARCH_QUERY = "washing up liquid"

_PRODUCTS_WE_LOVE_HEADING = re.compile("products we love", re.IGNORECASE)

# Two different consent-management copies seen on the real site: "Continue
# and accept" on the public groceries homepage, "Required only" on the
# oauth/login flow. Matched broadly ("accept" or "required only" anywhere in
# the name) since consent-management vendors change their copy without
# warning, and this is scoped to a button element already.
_COOKIE_ACCEPT_NAME = re.compile("accept|required only", re.IGNORECASE)

# Non-product headings that can appear inside/around the carousel widget
# (an accessible label for the carousel region, a trailing copyright-terms
# link styled as a heading) - skipped rather than counted as products.
_NON_PRODUCT_HEADINGS = re.compile("^(carousel|copyright terms)$", re.IGNORECASE)

# Sainsbury's own login page, redirected to when a session isn't (or is no
# longer) authenticated - taken from a real login recording, not guessed.
# Checked by path only, not the full URL, since query strings there commonly
# carry a return-to address.
_LOGIN_PATH = "/gol-ui/oauth/login"

# Accessible name of the site search box, from the same recording. Matched by
# prefix since the trailing "...or tab to ..." reads like a hint that could
# change independently of the field's purpose.
_SEARCH_BOX_NAME = re.compile("^Enter search terms", re.IGNORECASE)

# Search results render as `data-testid="product-tile-<id>"`, each containing
# its own `data-testid="add-button"` - adding straight from a results tile,
# with no separate "Add to basket"-named control and no need to open the
# product page first. Confirmed against a real search-and-add recording.
_PRODUCT_TILE_SELECTOR = '[data-testid^="product-tile-"]'
_ADD_BUTTON_TEST_ID = "add-button"


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
    query: str = DEFAULT_SEARCH_QUERY,
    *,
    storage_state_path: Path,
    quantity: int = 1,
) -> str:
    """Search for ``query`` and add its first result to the basket.

    Requires an already-authenticated session - see the module docstring and
    `browser.browser_page`'s `storage_state` parameter. Mirrors a real,
    manually recorded search-and-add flow (site search, then the result
    tile's own "add" control) rather than opening the product's own page -
    see the module docstring for what that recording showed. Still unverified
    end to end against a real, authenticated session - see the README's
    "Not done yet" section.

    Args:
        query: Search term, typed into the site's own search box exactly as
            a person would. Defaults to a term verified against the real
            site - see `DEFAULT_SEARCH_QUERY`.
        storage_state_path: Path to a Playwright `storage_state` JSON file
            holding a logged-in session, captured by
            `scripts/sainsburys_login.py`.
        quantity: How many of the product to add. Clicks the result tile's
            "add" control this many times.

    Returns:
        The added product's name, as shown on its result tile.

    Raises:
        NotLoggedInError: If the saved session is missing or not accepted.
        RuntimeError: If no matching result, or no "add" control on it, is
            found.
    """
    with browser_page(headless=False, storage_state=storage_state_path) as page:
        page.goto(MY_ACCOUNT_URL, wait_until="load", timeout=30_000)
        _dismiss_cookie_banner(page)
        _check_logged_in(page)

        search_box = page.get_by_role("combobox", name=_SEARCH_BOX_NAME).first
        search_box.click()
        search_box.fill(query)
        search_box.press("Enter")

        tile = page.locator(_PRODUCT_TILE_SELECTOR).first
        try:
            _wait_for_page_to_settle(tile)
        except PlaywrightTimeoutError as exc:
            msg = f"No search results found for {query!r}."
            raise RuntimeError(msg) from exc

        product_name = tile.get_by_role("heading").first.inner_text().strip()

        add_button = tile.get_by_test_id(_ADD_BUTTON_TEST_ID)
        if add_button.count() == 0:
            msg = (
                f'No "add" control found on the result for {query!r}. Either '
                "the page has changed, or the product is unavailable."
            )
            raise RuntimeError(msg)

        for _ in range(quantity):
            add_button.click(timeout=15_000)

        return product_name
