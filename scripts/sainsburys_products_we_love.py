#!/usr/bin/env python3
"""Proof of concept: fetch "Products we love" names off Sainsbury's groceries homepage.

**Unverified.** The selectors below were never checked against the real page:
this sandbox's outbound traffic is proxied through Google Cloud (see `via:
google` in a plain IP echo), which Sainsbury's Akamai edge blocks outright
regardless of browser fingerprint, so every attempt from here returns its
"Access Denied" page rather than the real site. Run this somewhere with
ordinary consumer/ISP egress - the Pi this project deploys to, or your own
machine - and report back what actually happens so the selectors below can be
corrected against real markup.

Deliberately a standalone script rather than a registered MCP tool: tools.py
is for actions this server exposes to a model as pre-approved and trustworthy.
Nothing here has been confirmed to do the right thing yet, so it does not
belong there until it has been run for real.

Usage:
    uv run scripts/sainsburys_products_we_love.py
"""

from __future__ import annotations

import re
import sys

from playwright.sync_api import Locator, Page, sync_playwright

URL = "https://www.sainsburys.co.uk/gol-ui/groceries"
SECTION_HEADING = re.compile("products we love", re.IGNORECASE)
ITEM_COUNT = 5

# The real Chromium build's own UA with the "Headless" branding stripped -
# truthful about the engine version (avoids UA/feature mismatches), just not
# announcing itself as an automated client. Not a fingerprint-evasion attempt:
# new-headless Chromium still reports navigator.webdriver = true, unchanged
# here.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Accessible names cookie-consent buttons commonly use. Best-effort: if none
# match, the page is left as it is and the heading lookup below either finds
# the section anyway or fails loudly.
COOKIE_ACCEPT_NAMES = re.compile("accept all|accept cookies", re.IGNORECASE)

# Text that turns up inside a product tile but is not the product's name -
# quantity controls, trolley actions, nutrition badges. Filtered out of
# whatever a tile's accessible name resolves to.
NON_PRODUCT_TEXT = re.compile(
    r"^(add|remove|increase|decrease|quantity|£|per |favourite)",
    re.IGNORECASE,
)


def _dismiss_cookie_banner(page: Page) -> None:
    button = page.get_by_role("button", name=COOKIE_ACCEPT_NAMES).first
    if button.count() > 0:
        button.click(timeout=5_000)


def _find_section(page: Page) -> Locator:
    """Return the container holding the "Products we love" carousel.

    Located by heading text rather than a class name or test id: those are
    exactly the kind of thing that changes on a redesign, per
    docs/self-healing.md's "address by stable contract" guidance. A role-based
    heading lookup is the closest available approximation without having seen
    the real markup to find an actual data-testid or aria label.
    """
    heading = page.get_by_role("heading", name=SECTION_HEADING).first
    if heading.count() == 0:
        heading = page.get_by_text(SECTION_HEADING).first
    if heading.count() == 0:
        msg = (
            'No "Products we love" heading found on the page. Either the '
            "section has been renamed/removed, or the page did not load as "
            "expected - inspect a screenshot/HTML dump before trusting "
            "anything downstream of this."
        )
        raise RuntimeError(msg)

    # Walk up from the heading to the nearest ancestor that actually contains
    # several links (product tiles are normally anchors) - a proxy for "this
    # is the carousel", not the heading's own (usually empty) wrapper.
    container = heading.locator(
        "xpath=ancestor::*[count(.//a) >= 3][1]",
    ).first
    if container.count() == 0:
        msg = (
            'Found the "Products we love" heading but no container beneath '
            "it with multiple links - the carousel's markup shape is not "
            "what this script expects."
        )
        raise RuntimeError(msg)
    return container


def _item_name(tile: Locator) -> str | None:
    """Best-effort product name for one tile: aria-label, else inner text."""
    aria_label = tile.get_attribute("aria-label")
    candidate = (aria_label or tile.inner_text()).strip()
    first_line = candidate.splitlines()[0].strip() if candidate else ""
    if not first_line or NON_PRODUCT_TEXT.match(first_line):
        return None
    return first_line


def products_we_love(url: str = URL, count: int = ITEM_COUNT) -> list[str]:
    """Return the first ``count`` product names under "Products we love".

    Args:
        url: Page to load. Defaults to the live groceries homepage.
        count: How many product names to return.

    Returns:
        Up to ``count`` product names, in the order they appear on the page.
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                user_agent=USER_AGENT,
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


def main() -> int:
    try:
        names = products_we_love()
    except Exception as exc:  # noqa: BLE001 - top-level script, report and exit
        print(f"Failed: {exc}", file=sys.stderr)
        return 1

    if not names:
        print("No product names found.", file=sys.stderr)
        return 1

    for name in names:
        print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
