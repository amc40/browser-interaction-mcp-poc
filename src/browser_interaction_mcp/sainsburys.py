"""Browser actions against Sainsbury's public, unauthenticated groceries site.

No login is needed for any of this: the page is public, so nothing here
touches the operator's own session or credentials.

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
    from playwright.sync_api import Locator, Page

GROCERIES_URL = "https://www.sainsburys.co.uk/gol-ui/groceries"

_PRODUCTS_WE_LOVE_HEADING = re.compile("products we love", re.IGNORECASE)

# OneTrust's own button text here is "Continue and accept" - kept broad
# ("accept" anywhere in the name) since consent-management vendors change
# their copy without warning, and this is scoped to a button element already.
_COOKIE_ACCEPT_NAME = re.compile("accept", re.IGNORECASE)

# Non-product headings that can appear inside/around the carousel widget
# (an accessible label for the carousel region, a trailing copyright-terms
# link styled as a heading) - skipped rather than counted as products.
_NON_PRODUCT_HEADINGS = re.compile("^(carousel|copyright terms)$", re.IGNORECASE)


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
