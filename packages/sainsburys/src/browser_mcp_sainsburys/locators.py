"""Every element Sainsbury's browser actions touch, by locator id.

Data only, on purpose: this file is the whole surface a ``claude/heal-*``
branch may change (see `browser_mcp_core.locator_table`), and a test holds it
to declarations so a change here can only ever re-address an element, never
add behaviour. How long to wait, what the consent cookies say, which URL to
open and which headings count as products all live in `site.py`.

Taken from real Playwright codegen recordings of a login and a
search-and-add flow against the live site, and from the public groceries page -
see `site.py`'s module docstring for what those showed.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from browser_mcp_core.locator_table import ByCss, ByRole, ByTestId, LocatorSpec

if TYPE_CHECKING:
    from collections.abc import Mapping

LOCATORS: Mapping[str, LocatorSpec] = {
    # --- The login form, on account.sainsburys.co.uk -------------------------
    "login.username": LocatorSpec(
        ByTestId("username"),
        "Login form's username field. Being on screen is also how the site's "
        "code tells the login form from a signed-in page.",
    ),
    "login.password": LocatorSpec(
        ByTestId("password"),
        "Login form's password field.",
    ),
    "login.submit": LocatorSpec(
        ByTestId("log-in"),
        "Login form's submit button.",
    ),
    "login.otp": LocatorSpec(
        ByTestId("OTP_FIELD"),
        "Verification-code field, shown after the password only when MFA is "
        "asked for; its appearing is how an MFA request is detected.",
    ),
    "login.submit_code": LocatorSpec(
        ByTestId("submit-code"),
        "Submit button beside the verification-code field.",
    ),
    # --- The signed-in site header -------------------------------------------
    "header.search_box": LocatorSpec(
        # Matched by prefix: the trailing "...or tab to ..." reads like a hint
        # that could change independently of what the field is for.
        ByRole("combobox", name=re.compile("^Enter search terms", re.IGNORECASE)),
        "Site search box, in the header of every signed-in page. Typed into to "
        "search; being on screen is also how a signed-in page is recognised.",
        first=True,
    ),
    # --- Search results --------------------------------------------------------
    "search.product_tile": LocatorSpec(
        # The 2026-10-04 redesign (tests/fixtures/search_results.html) dropped
        # the `product-tile-<id>` testids for a fixed one with no id in it.
        # The site's code still reads each tile's id out of the old prefix, so
        # every readable tile now raises "no id" until that code changes too.
        ByTestId("gw-product-card"),
        "Every search result's product card, in the order the page lists them. "
        "Sponsored banners in the grid are not product cards and are not matched.",
    ),
    "search.tile_name": LocatorSpec(
        ByRole("heading"),
        "Within one result tile: the heading holding the product's name.",
        first=True,
    ),
    "search.tile_image": LocatorSpec(
        ByCss("img"),
        "Within one result tile: the product image. Best effort - not confirmed "
        "against a recording, and a tile without one is allowed.",
        first=True,
    ),
    "search.add_button": LocatorSpec(
        ByTestId("add-button"),
        "Within one result tile: its own add-to-basket control, clicked once per "
        "unit of quantity.",
    ),
    # --- The public groceries page ---------------------------------------------
    "groceries.products_we_love_heading": LocatorSpec(
        ByRole("heading", name=re.compile("products we love", re.IGNORECASE)),
        'The "Products we love" section heading, waited for as the sign the page '
        "has loaded. The site's code separately looks for this text among the "
        "page's headings to find where the section starts.",
        first=True,
    ),
    "groceries.headings": LocatorSpec(
        ByRole("heading"),
        "Every heading on the page, in document order. Product names in the "
        '"Products we love" section are headings following the section\'s own.',
    ),
}
