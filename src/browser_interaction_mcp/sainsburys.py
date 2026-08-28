"""Browser actions against the Sainsbury's groceries site.

Most of this module (`products_we_love`, `search_products`, `add_to_basket`)
never touches a password: they either read a public page, or *reuse* a
session someone else already established, via Playwright's `storage_state` -
cookies and local storage, not credentials - handed to `browser.browser_page`.
See `browser.browser_page`'s docstring for that mechanism and
`docs/deployment.md` §7 for why the captured session is worth protecting as
carefully as the credentials it stands in for.

`refresh_session` is the one function in this module that is the exception,
on purpose: something has to actually log in to produce that session in the
first place. It drives Sainsbury's real login form directly, so it is the one
place in this project a password (and, if asked for, an MFA code) passes
through server code - transiently, for the seconds this call takes, never
written to disk or logged (a `SecretStr` `username` setting means
`redaction.py` scrubs it from logs and errors the same as every other
credential; the same is true of `password` here, passed as a plain string
because it never becomes settings, but treated with the same care). Two
things call it, for two different situations:

- `scripts/sainsburys_login.py`, run locally, by hand, when the operator has
  a machine to run it from. Password and any OTP are read from the terminal
  (`getpass`, never echoed) and passed straight through.
- The `/sainsburys-login` browser page, for when they don't - a Raspberry Pi
  behind a tunnel is the documented deployment target, and "SSH in and run a
  script" isn't assumed to be routinely available there. The operator signs
  in with GitHub, then types the password into a form served by this same
  server: the value goes straight to the server over HTTPS, never through the
  model or the transcript. The login runs in a subprocess that parks if a
  verification code is asked for, so the operator can return to the page
  minutes later with the code. See `login_routes.py` and
  `sainsburys_login_flow.py`. (This replaced an MCP-elicitation tool, which
  Claude.ai's MCP client turned out not to support.)

Both are a real, deliberate narrowing of the "the server never sees a
password" property `add_to_basket` and `products_we_love` still hold —
accepted here because the alternative, for an operator without routine host
access, is no way to refresh a session at all.

**`add_to_basket` itself is still unverified against the real, authenticated
site** - see the "Not done yet" section of the README - but its selectors are
no longer guesses: they were taken from a real Playwright codegen recording
of a manual login and search-and-add flow, not invented. What that recording
showed, and isn't obvious from the public pages alone:

- `www.sainsburys.co.uk/gol-ui/oauth/login` is only a redirect shell now: it
  bounces to the real form on `account.sainsburys.co.uk/gol/login?login_challenge=...`
  (an Ory-style identity provider). It redirects through login-shaped URLs
  even on the *success* path (a silent session check on the way to an account
  page), so `refresh_session` decides "logged in or not" by whether the login
  *form* is on screen, never by the URL.
- The consent banner is OneTrust, injected asynchronously *after* the load
  event, with a full-page backdrop that blocks every click until it's
  actioned. Rather than race to dismiss it, `_consent_cookies` seeds the
  cookies OneTrust writes on a "Continue without accepting" choice
  (strictly-necessary only, every optional category refused) before the first
  navigation, so it never renders. They go on the registrable domain
  (`.sainsburys.co.uk`); a `.www.sainsburys.co.uk` cookie is not what OneTrust
  reads on that host.
- MFA, when Sainsbury's asks for it, is a further step after the password -
  not guaranteed to appear (it seems to depend on whether the device/network
  is already trusted). `refresh_session` detects it by the verification-code
  field showing up, and only then calls `get_otp`. Not something
  `add_to_basket` handles: by the time it runs the session is already captured.
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

import contextlib
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import quote

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from browser_interaction_mcp.browser import browser_page

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from playwright.sync_api import Locator, Page

GROCERIES_URL = "https://www.sainsburys.co.uk/gol-ui/groceries"
MY_ACCOUNT_URL = "https://www.sainsburys.co.uk/gol-ui/MyAccount"

#: Search term `add_to_basket` uses when the caller doesn't give one -
#: verified against the real site: see the module docstring.
DEFAULT_SEARCH_QUERY = "washing up liquid"

_PRODUCTS_WE_LOVE_HEADING = re.compile("products we love", re.IGNORECASE)

# The consent banner is OneTrust, injected asynchronously after the load event,
# with a full-page backdrop that silently blocks every click until it's
# actioned. Rather than race to dismiss it on each page, we seed the cookies it
# writes when a choice is made, before the first navigation, so it never
# renders. They go on the registrable domain, which is what OneTrust reads on
# every *.sainsburys.co.uk host - a `.www.sainsburys.co.uk` cookie is not.
_CONSENT_COOKIE_DOMAIN = ".sainsburys.co.uk"

# Non-product headings that can appear inside/around the carousel widget
# (an accessible label for the carousel region, a trailing copyright-terms
# link styled as a heading) - skipped rather than counted as products.
_NON_PRODUCT_HEADINGS = re.compile("^(carousel|copyright terms)$", re.IGNORECASE)

# `www.sainsburys.co.uk/gol-ui/oauth/login` is only a shell now: it bounces to
# the real form on `account.sainsburys.co.uk/gol/login?login_challenge=...` (an
# Ory-style identity provider). MFA, when Sainsbury's asks for it, is a further
# step there. Both are detected by the elements they render, not their URL:
# these SPAs redirect through login-shaped URLs even on the *success* path (a
# silent session check), so a URL match gives false failures.
LOGIN_URL = "https://www.sainsburys.co.uk/gol-ui/oauth/login"


def _raise_if_not_logged_in(
    page: Page, message: str, *, screenshot_path: Path | None = None
) -> None:
    """Raise ``NotLoggedInError`` if the login form is the thing on screen.

    Checks for the form itself, not just the URL: a *successful* visit to an
    account page bounces back through the identity provider's ``/gol/login``
    URL for a silent session check, so a URL match on its own gives false
    failures. An unauthenticated visit ends with the form actually rendered.
    """
    with contextlib.suppress(PlaywrightTimeoutError):
        page.wait_for_load_state("domcontentloaded", timeout=15_000)
    page.wait_for_timeout(2_000)
    if not page.get_by_test_id(_USERNAME_TEST_ID).is_visible():
        return
    if screenshot_path is not None:
        # Best effort: a debug aid must never mask the real failure below.
        # The image could show the typed username or an on-screen OTP, so it's
        # locked to the owner (the caller already keeps it in a 0700 dir).
        with contextlib.suppress(Exception):
            page.screenshot(path=screenshot_path)
            screenshot_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    raise NotLoggedInError(message)


# Login form field test ids, from the same recording.
_USERNAME_TEST_ID = "username"
_PASSWORD_TEST_ID = "password"  # noqa: S105 - a DOM test id, not a credential
_LOG_IN_TEST_ID = "log-in"
_OTP_TEST_ID = "OTP_FIELD"
_SUBMIT_CODE_TEST_ID = "submit-code"

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


@dataclass(frozen=True)
class ProductMatch:
    """One product tile read from a Sainsbury's search results page."""

    name: str
    image_url: str | None


def _consent_cookies() -> list[dict[str, object]]:
    """Cookies telling Sainsbury's OneTrust the consent choice is already made.

    Seeded before the first navigation so the blocking consent banner never
    renders. `groups=1:1,2:0,3:0,4:0` is strictly-necessary only - every
    optional category refused. The shape and field set were taken from a real
    "Continue without accepting" click on the live login flow.
    """
    now = datetime.now(UTC)
    stamp = quote(
        now.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)"),
    )
    consent = (
        f"isGpcEnabled=0&datestamp={stamp}&version=202507.1.0&isIABGlobal=false"
        f"&hosts=&consentId={uuid.uuid4()}&interactionCount=1"
        "&landingPath=NotLandingPage&groups=1%3A1%2C2%3A0%2C3%3A0%2C4%3A0"
    )
    closed = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    expires = int((now + timedelta(days=365)).timestamp())
    common = {
        "domain": _CONSENT_COOKIE_DOMAIN,
        "path": "/",
        "expires": expires,
        "sameSite": "Lax",
    }
    return [
        {"name": "OptanonAlertBoxClosed", "value": closed, **common},
        {"name": "OptanonConsent", "value": consent, **common},
    ]


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
    with browser_page(headless=False, cookies=_consent_cookies()) as page:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)

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
    _raise_if_not_logged_in(
        page,
        (
            "Redirected to the Sainsbury's login page: the saved session is "
            "missing or no longer accepted. Run scripts/sainsburys_login.py "
            "locally, or visit /sainsburys-login on the deployed server, to "
            "log in and capture a fresh one - see browser.browser_page's "
            "docstring for how it's used from there."
        ),
    )


@contextlib.contextmanager
def _authenticated_page(storage_state_path: Path) -> Iterator[Page]:
    """Open a page in an already-authenticated Sainsbury's session.

    Shared by every action below that needs to be logged in
    (`search_products`, `add_to_basket`), so "how do we get an
    authenticated page" has exactly one definition - the two callers can't
    drift into checking that differently.

    Raises:
        NotLoggedInError: If the saved session is missing or not accepted.
    """
    with browser_page(
        headless=False,
        storage_state=storage_state_path,
        cookies=_consent_cookies(),
    ) as page:
        with contextlib.suppress(PlaywrightTimeoutError):
            page.goto(MY_ACCOUNT_URL, wait_until="load", timeout=25_000)
        _check_logged_in(page)
        yield page


def _run_search(page: Page, query: str) -> Locator:
    """Type ``query`` into the site search and return the results' tiles.

    Args:
        page: An already-authenticated page - see `_check_logged_in`.
        query: Search term, typed into the site's own search box exactly as
            a person would.

    Returns:
        A locator matching every result tile, in the order the page lists
        them.

    Raises:
        RuntimeError: If no results are found.
    """
    search_box = page.get_by_role("combobox", name=_SEARCH_BOX_NAME).first
    search_box.click()
    search_box.fill(query)
    search_box.press("Enter")

    tiles = page.locator(_PRODUCT_TILE_SELECTOR)
    try:
        _wait_for_page_to_settle(tiles.first)
    except PlaywrightTimeoutError as exc:
        msg = f"No search results found for {query!r}."
        raise RuntimeError(msg) from exc
    return tiles


def search_products(
    query: str = DEFAULT_SEARCH_QUERY,
    *,
    storage_state_path: Path,
    count: int = 5,
) -> list[ProductMatch]:
    """Search for ``query`` and return the top ``count`` results, unadded.

    Read-only counterpart to `add_to_basket`, meant to be shown to a person -
    e.g. as a Markdown list with the images inlined - so they can choose the
    exact product name to pass to `add_to_basket`. An index into this list
    isn't used for that instead because it can go stale between the two
    calls (the site re-ranks or re-stocks between requests); a name naming
    the specific product survives that.

    Requires an already-authenticated session, for the same reason
    `add_to_basket` does: only the logged-in results page has been verified
    against a real recording - see the module docstring.

    Args:
        query: Search term, typed into the site's own search box exactly as
            a person would. Defaults to a term verified against the real
            site - see `DEFAULT_SEARCH_QUERY`.
        storage_state_path: Path to a Playwright `storage_state` JSON file
            holding a logged-in session, captured by
            `scripts/sainsburys_login.py`.
        count: How many results to return.

    Returns:
        Up to ``count`` matches, in the order the results page lists them -
        fewer if the page mixes in non-product tiles (sponsored slots), which
        are skipped. `image_url` is best-effort - the tile's first `<img>`,
        unlike the other selectors here hasn't been confirmed against a real
        recording - and is `None` if the tile has no image or no `src`.

    Raises:
        NotLoggedInError: If the saved session is missing or not accepted.
        RuntimeError: If no results are found, or results rendered but no
            product name could be read from any of them.
    """
    with _authenticated_page(storage_state_path) as page:
        tiles = _run_search(page, query)
        matches: list[ProductMatch] = []
        for match in _readable_matches(tiles):
            matches.append(match)
            if len(matches) == count:
                break
        if not matches:
            msg = (
                f"Found result tiles for {query!r} but read a product name from "
                "none of them - the results page markup has probably changed."
            )
            raise RuntimeError(msg)
        return matches


# The results grid mixes in tiles that share the `product-tile-` testid prefix
# but carry no product name heading (sponsored slots, "browse the aisle"
# cards), and renders tiles past the first few lazily. Reading one of those
# with Playwright's default 30s wait hangs the whole call, so a tile whose
# heading hasn't shown within this is treated as "not a product" and skipped.
_TILE_HEADING_TIMEOUT_MS = 4_000

# Ceiling on how many result tiles a single search will inspect - enough to
# cover a full results page, bounded so a pathological page can't turn the
# per-tile waits above into a minutes-long call.
_MAX_RESULT_TILES = 60


def _product_match(tile: Locator) -> ProductMatch | None:
    """Read one tile's product name and image, or ``None`` if it isn't one.

    The one place either field is read: `add_to_basket`'s exact-match lookup
    reuses this for `name` so a tile's name can't be read one way for display
    and another way for matching. Returns ``None`` - rather than blocking on
    `inner_text` for the full default timeout - for a tile whose name heading
    never appears; see `_TILE_HEADING_TIMEOUT_MS`.
    """
    heading = tile.get_by_role("heading").first
    try:
        heading.wait_for(state="visible", timeout=_TILE_HEADING_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        return None
    name = heading.inner_text().strip()
    if not name:
        return None
    image = tile.locator("img").first
    image_url = image.get_attribute("src") if image.count() > 0 else None
    return ProductMatch(name=name, image_url=image_url)


def _readable_matches(tiles: Locator) -> Iterator[ProductMatch]:
    """Yield a `ProductMatch` for each result tile that reads as a product.

    Tiles that don't (see `_product_match`) are skipped rather than aborting
    the search, so one sponsored slot among the results doesn't sink the call.
    """
    for tile in tiles.all()[:_MAX_RESULT_TILES]:
        match = _product_match(tile)
        if match is not None:
            yield match


def _find_exact_match(tiles: Locator, product_name: str) -> Locator | None:
    """Return the first tile whose product name exactly equals ``product_name``.

    Matches on the same name `_product_match` would report for that tile -
    see its docstring for why - and skips tiles that don't read as a product.
    """
    target = product_name.strip()
    for tile in tiles.all()[:_MAX_RESULT_TILES]:
        match = _product_match(tile)
        if match is not None and match.name == target:
            return tile
    return None


def add_to_basket(
    product_name: str,
    *,
    storage_state_path: Path,
    quantity: int = 1,
) -> str:
    """Search for ``product_name`` and add the result matching it exactly.

    Requires an already-authenticated session - see the module docstring and
    `browser.browser_page`'s `storage_state` parameter. Mirrors a real,
    manually recorded search-and-add flow (site search, then the result
    tile's own "add" control) rather than opening the product's own page -
    see the module docstring for what that recording showed. Still unverified
    end to end against a real, authenticated session - see the README's
    "Not done yet" section.

    `product_name` must match a result's heading exactly (whitespace
    trimmed) - typically one just returned by `search_products`. That's
    deliberate rather than picking the first or an indexed result: an index
    can go stale between a search and this call (the site re-ranks or
    re-stocks in between), and blindly taking the first result can add the
    wrong product for an ambiguous query. Naming the exact product avoids
    both.

    Args:
        product_name: The product's name, exactly as shown on its search
            result tile (e.g. from `search_products`). Also used, as-is, as
            the search term.
        storage_state_path: Path to a Playwright `storage_state` JSON file
            holding a logged-in session, captured by
            `scripts/sainsburys_login.py`.
        quantity: How many of the product to add. Clicks the result tile's
            "add" control this many times.

    Returns:
        The added product's name, as shown on its result tile.

    Raises:
        NotLoggedInError: If the saved session is missing or not accepted.
        RuntimeError: If no results are found, none match `product_name`
            exactly, or the matching result has no "add" control.
    """
    with _authenticated_page(storage_state_path) as page:
        tiles = _run_search(page, product_name)

        tile = _find_exact_match(tiles, product_name)
        if tile is None:
            msg = (
                f"No search result exactly matches {product_name!r}. Call "
                "search_products first and pass one of its product names "
                "exactly, including capitalisation and punctuation."
            )
            raise RuntimeError(msg)

        add_button = tile.get_by_test_id(_ADD_BUTTON_TEST_ID)
        if add_button.count() == 0:
            msg = (
                f'No "add" control found on the result for {product_name!r}. '
                "Either the page has changed, or the product is unavailable."
            )
            raise RuntimeError(msg)

        for _ in range(quantity):
            add_button.click(timeout=15_000)

        return product_name.strip()


def refresh_session(
    username: str,
    password: str,
    *,
    storage_state_path: Path,
    get_otp: Callable[[], str | None],
    failure_screenshot_path: Path | None = None,
) -> None:
    """Log in for real, and overwrite ``storage_state_path`` with the result.

    The one function in this module that handles a password - see the module
    docstring for what that means and why it's accepted here. Neither
    `username` nor `password` is written anywhere by this function or
    anything it calls; only the resulting session is.

    Args:
        username: Sainsbury's account email/username, typed into the login
            form exactly as a person would.
        password: Account password, typed into the login form.
        storage_state_path: Where to write the resulting Playwright
            `storage_state` JSON. Overwritten if it already exists.
        get_otp: Called only if Sainsbury's redirects to its MFA step -
            not guaranteed to happen, see the module docstring. Should
            return the verification code to submit, or `None` if the
            operator declined or none was available; either way, `None`
            aborts the refresh rather than submitting an empty code.
        failure_screenshot_path: If set, a screenshot of the page is written
            here when the login is judged to have failed - a debugging aid,
            since the failure page is the only thing that says *why*.

    Raises:
        NotLoggedInError: If MFA was required but `get_otp` returned `None`,
            or if the session still isn't authenticated after everything
            above - most likely a wrong password.
    """
    with browser_page(headless=False, cookies=_consent_cookies()) as page:
        # `wait_until="load"` is unreliable on these pages - a background poller
        # keeps the network busy, so it eats the full timeout. Wait for the DOM
        # and then for the specific element that matters instead.
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        # LOGIN_URL bounces through a redirect to the real form on the account
        # domain; wait for that form before filling anything.
        username_field = page.get_by_test_id(_USERNAME_TEST_ID)
        username_field.wait_for(state="visible", timeout=30_000)

        username_field.fill(username)
        page.get_by_test_id(_PASSWORD_TEST_ID).fill(password)
        page.get_by_test_id(_LOG_IN_TEST_ID).click(timeout=15_000)
        page.wait_for_load_state("domcontentloaded", timeout=30_000)

        otp_field = page.get_by_test_id(_OTP_TEST_ID)
        with contextlib.suppress(PlaywrightTimeoutError):
            otp_field.wait_for(state="visible", timeout=15_000)
        if otp_field.is_visible():
            otp = get_otp()
            if otp is None:
                msg = (
                    "Sainsbury's asked for a verification code, and none was "
                    "provided - not completing the login."
                )
                raise NotLoggedInError(msg)
            otp_field.fill(otp)
            page.get_by_test_id(_SUBMIT_CODE_TEST_ID).click(timeout=15_000)
            # The code submit kicks off the redirect that actually completes
            # the login; let it finish before navigating away from it.
            page.wait_for_load_state("domcontentloaded", timeout=30_000)

        with contextlib.suppress(PlaywrightTimeoutError):
            page.goto(MY_ACCOUNT_URL, wait_until="load", timeout=25_000)
        _raise_if_not_logged_in(
            page,
            "Still not logged in after submitting the login form (and any "
            "verification code) - check the password and, if you were asked "
            "for one, the verification code.",
            screenshot_path=failure_screenshot_path,
        )

        page.context.storage_state(path=storage_state_path)

    # rw for the owner only: this file is as sensitive as the login that
    # produced it. Done here, not by each caller, so both
    # scripts/sainsburys_login.py and the sainsburys_refresh_session tool get
    # it for free.
    storage_state_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
