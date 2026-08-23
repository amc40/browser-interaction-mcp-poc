"""Browser actions against Sainsbury's public, unauthenticated groceries site.

No login is needed for any of this: the page is public, so nothing here
touches the operator's own session or credentials.
"""

from __future__ import annotations

import re

from playwright.sync_api import Locator, Page, sync_playwright

GROCERIES_URL = "https://www.sainsburys.co.uk/gol-ui/groceries"

_PRODUCTS_WE_LOVE_HEADING = re.compile("products we love", re.IGNORECASE)

# The real Chromium build's own UA with the "Headless" branding stripped -
# truthful about the engine version (avoids UA/feature mismatches), just not
# announcing itself as an automated client. Not a fingerprint-evasion attempt:
# new-headless Chromium still reports navigator.webdriver = true, unchanged
# here.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Accessible names cookie-consent buttons commonly use. Best-effort: if none
# match, the page is left as it is and the heading lookup below either finds
# the section anyway or fails loudly.
_COOKIE_ACCEPT_NAMES = re.compile("accept all|accept cookies", re.IGNORECASE)

# Text that turns up inside a product tile but is not the product's name -
# quantity controls, trolley actions, nutrition badges. Filtered out of
# whatever a tile's accessible name resolves to.
_NON_PRODUCT_TEXT = re.compile(
    r"^(add|remove|increase|decrease|quantity|£|per |favourite)",
    re.IGNORECASE,
)


def _dismiss_cookie_banner(page: Page) -> None:
    button = page.get_by_role("button", name=_COOKIE_ACCEPT_NAMES).first
    if button.count() > 0:
        button.click(timeout=5_000)


def _find_section(page: Page) -> Locator:
    """Return the container holding the "Products we love" carousel.

    Located by heading text rather than a class name or test id: those are
    exactly the kind of thing that changes on a redesign, per
    docs/self-healing.md's "address by stable contract" guidance.

    Args:
        page: The loaded page to search.

    Returns:
        The locator for the section beneath the heading.

    Raises:
        RuntimeError: If no such heading, or no plausible container beneath
            it, can be found.
    """
    heading = page.get_by_role("heading", name=_PRODUCTS_WE_LOVE_HEADING).first
    if heading.count() == 0:
        heading = page.get_by_text(_PRODUCTS_WE_LOVE_HEADING).first
    if heading.count() == 0:
        msg = (
            'No "Products we love" heading found on the page. Either the '
            "section has been renamed/removed, or the page did not load as "
            "expected."
        )
        raise RuntimeError(msg)

    # Walk up from the heading to the nearest ancestor that actually contains
    # several links (product tiles are normally anchors) - a proxy for "this
    # is the carousel", not the heading's own (usually empty) wrapper.
    container = heading.locator("xpath=ancestor::*[count(.//a) >= 3][1]").first
    if container.count() == 0:
        msg = (
            'Found the "Products we love" heading but no container beneath '
            "it with multiple links - the carousel's markup shape is not "
            "what this action expects."
        )
        raise RuntimeError(msg)
    return container


def _item_name(tile: Locator) -> str | None:
    """Best-effort product name for one tile: aria-label, else inner text."""
    aria_label = tile.get_attribute("aria-label")
    candidate = (aria_label or tile.inner_text()).strip()
    first_line = candidate.splitlines()[0].strip() if candidate else ""
    if not first_line or _NON_PRODUCT_TEXT.match(first_line):
        return None
    return first_line


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
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                user_agent=_USER_AGENT,
                viewport={"width": 1366, "height": 900},
                locale="en-GB",
                timezone_id="Europe/London",
                extra_http_headers={"Accept-Language": "en-GB,en;q=0.9"},
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_load_state("networkidle", timeout=20_000)
            _dismiss_cookie_banner(page)

            section = _find_section(page)
            names: list[str] = []
            for tile in section.locator("a").all():
                name = _item_name(tile)
                if name and name not in names:
                    names.append(name)
                if len(names) == count:
                    break
            return names
        finally:
            browser.close()
