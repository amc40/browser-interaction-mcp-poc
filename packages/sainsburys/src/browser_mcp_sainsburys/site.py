"""Browser actions against the Sainsbury's groceries site.

Most of this module (`products_we_love`, `search_products`, `add_to_basket`)
never touches a password: they either read a public page, or *reuse* a
session someone else already established, via Playwright's `storage_state` -
cookies and local storage, not credentials - handed to `browser.browser_page`.
See `browser.browser_page`'s docstring for that mechanism and
`docs/deployment.md` §7 for why the captured session is worth protecting as
carefully as the credentials it stands in for.

`SainsburysLoginSteps` is the one part of this module that is the exception,
on purpose: something has to actually log in to produce that session in the
first place. It drives Sainsbury's real login form directly, so it is the one
place in this package a password (and, if asked for, an MFA code) passes
through server code - transiently, for the seconds a login takes, never
written to disk or logged (a `SecretStr` `username` setting means
`redaction.py` scrubs it from logs and errors the same as every other
credential; the same is true of `password` here, passed as a plain string
because it never becomes settings, but treated with the same care). Core
drives those steps (`browser_mcp_core.login_steps.refresh_session`), and two
things reach them, for two different situations:

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
  minutes later with the code. See `browser_mcp_core.login_routes` and
  `browser_mcp_core.login_flow`. (This replaced an MCP-elicitation tool, which
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
  (an Ory-style identity provider). A dead session's account page paints its
  full header *before* a silent session check bounces it there, so "logged in
  or not" is decided by waiting out that redirect - see
  `_raise_if_not_logged_in` - not by anything on screen.
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
  is already trusted). the login steps detect it by the verification-code
  field showing up, and only then calls `get_otp`. Not something
  `add_to_basket` handles: by the time it runs the session is already captured.
- Search is a `combobox`, filled and submitted with Enter - not a URL query
  parameter.
- Each search result is a `data-testid="gw-product-card"` (it was
  `product-tile-<id>` until the 2026-10-04 redesign, which also dropped the id
  from the attribute - it is now the slug of the card's product link), and
  adding it to the basket is `data-testid="gw-add-to-basket"` *inside that
  same card* - directly from the results, with no separate "Add to
  basket"-named button and no need to open the product page first.

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
import logging
import os
import re
import stat
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import quote

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from browser_mcp_core.browser import browser_page
from browser_mcp_core.errors import ClassifiedError, ErrorCategory, NotLoggedInError
from browser_mcp_core.locator_table import resolve
from browser_mcp_sainsburys.locators import LOCATORS

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from playwright.sync_api import Locator, Page

_logger = logging.getLogger(__name__)

GROCERIES_URL = "https://www.sainsburys.co.uk/gol-ui/groceries"
MY_ACCOUNT_URL = "https://www.sainsburys.co.uk/gol-ui/MyAccount"

#: Search term `add_to_basket` uses when the caller doesn't give one -
#: verified against the real site: see the module docstring.
DEFAULT_SEARCH_QUERY = "washing up liquid"

# Which of the page's headings starts the "Products we love" section. Business
# logic rather than a locator, so it lives here and not in `locators.py`: the
# table's `groceries.products_we_love_heading` is what gets *waited for*, and
# this is what the headings are scanned for once it has appeared.
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
# step there, detected by the verification-code field it renders.
LOGIN_URL = "https://www.sainsburys.co.uk/gol-ui/oauth/login"


# A session Sainsbury's no longer accepts is sent to its identity provider:
# first the groceries app's own login hop, then account.sainsburys.co.uk.
_LOGIN_REDIRECT_URL = re.compile(
    r"https://(www\.sainsburys\.co\.uk/gol-ui/oauth/|account\.sainsburys\.co\.uk/)"
)

# The account page paints its header - search box included - for a dead
# session too, and only then runs the silent check that redirects it: traced
# on the Pi, the redirect landed under a second after the search box showed.
# So `_raise_if_not_logged_in` waits for the header, then gives the redirect
# this long to start before believing it.
_HEADER_TIMEOUT_MS = 20_000
_REDIRECT_GRACE_MS = 5_000

_SESSION_INVALID_MESSAGE = (
    "The saved Sainsbury's session is no longer valid - it has expired, or the "
    "account was signed out elsewhere. Ask the operator to re-authenticate by "
    "opening /sainsburys-login on this server and signing in (or, running "
    "locally, by rerunning scripts/sainsburys_login.py), then try again."
)


def _locate(scope: Page | Locator, locator_id: str) -> Locator:
    """Return the locator ``locators.py`` names ``locator_id``.

    The only way this module addresses an element: nothing here builds a
    locator inline, so every element it touches is a row a reviewer can see.
    """
    return resolve(scope, LOCATORS[locator_id])


def _raise_if_not_logged_in(
    page: Page, message: str, *, screenshot_path: Path | None = None
) -> None:
    """Raise ``NotLoggedInError`` if the page is sent to Sainsbury's login.

    Neither the header nor the URL the page first lands on can tell a live
    session from a dead one - see `_REDIRECT_GRACE_MS`. So this waits for the
    header, then for a redirect to login, and only a page that is still off
    the login path after both counts as logged in. If the header never shows
    and nothing redirects, that is assumed logged in too: a genuinely broken
    page then fails downstream with its own, more specific error.
    """
    header_or_login_form = _locate(page, "header.search_box").or_(
        _locate(page, "login.username")
    )
    with contextlib.suppress(PlaywrightTimeoutError):
        header_or_login_form.wait_for(state="visible", timeout=_HEADER_TIMEOUT_MS)
    with contextlib.suppress(PlaywrightTimeoutError):
        page.wait_for_url(
            _LOGIN_REDIRECT_URL, wait_until="commit", timeout=_REDIRECT_GRACE_MS
        )
    if not _LOGIN_REDIRECT_URL.search(page.url):
        return

    if screenshot_path is not None:
        # Best effort: a debug aid must never mask the real failure below.
        # The image could show the typed username or an on-screen OTP, so it's
        # locked to the owner (the caller already keeps it in a 0700 dir).
        with contextlib.suppress(Exception):
            page.screenshot(path=screenshot_path)
            screenshot_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    raise NotLoggedInError(message)


# A result's own page is `/groceries/product/<slug>`, and the slug is its id: the
# result cards carry no id attribute, but every one links there (the table's
# `search.tile_link`).
_PRODUCT_PATH = re.compile(r"/groceries/product/(?P<slug>[^/?#]+)")


@dataclass(frozen=True)
class ProductMatch:
    """One product tile read from a Sainsbury's search results page."""

    name: str
    id: str
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
    headings = _locate(page, "groceries.headings").all()
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
        raise ClassifiedError(msg, ErrorCategory.SITE_CHANGED, retryable=False)

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

        heading = _locate(page, "groceries.products_we_love_heading")
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
    _raise_if_not_logged_in(page, _SESSION_INVALID_MESSAGE)


# Sainsbury's replaces the session's token every time the session is used, so
# the saved session is good for exactly one use unless what the browser ends up
# holding is written back. Two calls at once would both start from the same
# token, and the second to reach Sainsbury's would present one the first had
# already replaced - so only one call uses the session at a time.
_SESSION_LOCK = threading.Lock()


def _modified_ns(path: Path) -> int | None:
    """Return ``path``'s modification time, or ``None`` if it doesn't exist."""
    with contextlib.suppress(FileNotFoundError):
        return path.stat().st_mtime_ns
    return None


def _save_session(page: Page, storage_state_path: Path, loaded_ns: int | None) -> None:
    """Write the session the browser now holds back over ``storage_state_path``.

    Skipped if the page ended up on the identity provider (the session lapsed
    mid-action, and saving it would only replace one dead session with
    another), and if the file changed since it was loaded (a login replaced
    it meanwhile, and is newer than anything this page holds). A failure is
    logged rather than raised: the action has already happened - an item may
    be in the basket - so failing the call now would misreport it.
    """
    if (
        _LOGIN_REDIRECT_URL.search(page.url)
        or _modified_ns(storage_state_path) != loaded_ns
    ):
        return
    # Written beside the real file and renamed over it, so a crash mid-write
    # can't leave a truncated session; created owner-only up front, because
    # the file is as sensitive as the login that produced it.
    temp_path = storage_state_path.with_name(f".{storage_state_path.name}.tmp")
    try:
        temp_path.unlink(missing_ok=True)
        os.close(
            os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
        )
        page.context.storage_state(path=temp_path)
        # Checked again as late as possible: the login runs in another
        # process, which `_SESSION_LOCK` can't hold off.
        if _modified_ns(storage_state_path) != loaded_ns:
            temp_path.unlink()
            return
        temp_path.replace(storage_state_path)
    except (OSError, PlaywrightError):
        temp_path.unlink(missing_ok=True)
        _logger.warning(
            "Could not save the refreshed Sainsbury's session; the next call "
            "will likely need a fresh login.",
            exc_info=True,
        )


@contextlib.contextmanager
def _authenticated_page(storage_state_path: Path) -> Iterator[Page]:
    """Open a page in an already-authenticated Sainsbury's session.

    Shared by every action below that needs to be logged in
    (`search_products`, `add_to_basket`), so "how do we get an
    authenticated page" has exactly one definition - the two callers can't
    drift into checking that differently. Saves the session back afterwards,
    whether or not the action succeeded - see `_SESSION_LOCK`.

    Raises:
        NotLoggedInError: If the saved session is missing or not accepted.
    """
    with _SESSION_LOCK:
        loaded_ns = _modified_ns(storage_state_path)
        with browser_page(
            headless=False,
            storage_state=storage_state_path,
            cookies=_consent_cookies(),
        ) as page:
            with contextlib.suppress(PlaywrightTimeoutError):
                page.goto(MY_ACCOUNT_URL, wait_until="load", timeout=25_000)
            _check_logged_in(page)
            try:
                yield page
            finally:
                _save_session(page, storage_state_path, loaded_ns)


def _raise_if_session_lapsed(page: Page, timeout: PlaywrightTimeoutError) -> None:
    """Raise ``NotLoggedInError`` if ``timeout`` came from a bounce to login.

    A session that lapses just after the check in `_authenticated_page` is
    sent to login mid-action, which surfaces as whatever Playwright was
    waiting on timing out; say so, rather than reporting that wait's failure.
    """
    if _LOGIN_REDIRECT_URL.search(page.url):
        raise NotLoggedInError(_SESSION_INVALID_MESSAGE) from timeout


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
        NotLoggedInError: If the session lapsed between the check in
            `_authenticated_page` and here, bouncing the page to login.
        RuntimeError: If no results are found.
    """
    search_box = _locate(page, "header.search_box")
    try:
        search_box.click()
        search_box.fill(query)
        search_box.press("Enter")
    except PlaywrightTimeoutError as exc:
        _raise_if_session_lapsed(page, exc)
        raise

    tiles = _locate(page, "search.product_tile")
    try:
        _wait_for_page_to_settle(tiles.first)
    except PlaywrightTimeoutError as exc:
        _raise_if_session_lapsed(page, exc)
        msg = f"No search results found for {query!r}."
        raise ClassifiedError(msg, ErrorCategory.NOT_FOUND, retryable=False) from exc
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
        RuntimeError: If no results are found, results rendered but no
            product name could be read from any of them, or a tile a name
            *was* read from had no readable id (see `_product_match`).
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
            raise ClassifiedError(msg, ErrorCategory.SITE_CHANGED, retryable=False)
        return matches


# The results grid can mix in tiles that match the result-tile locator but
# carry no product name heading (sponsored slots, "browse the aisle"
# cards), and renders tiles past the first few lazily. Reading one of those
# with Playwright's default 30s wait hangs the whole call, so a tile whose
# heading hasn't shown within this is treated as "not a product" and skipped.
_TILE_HEADING_TIMEOUT_MS = 4_000

# Ceiling on how many result tiles a single search will inspect - enough to
# cover a full results page, bounded so a pathological page can't turn the
# per-tile waits above into a minutes-long call.
_MAX_RESULT_TILES = 60


def _product_id_from_href(href: str) -> str | None:
    """Return the slug of a product page link, or ``None`` if it is not one."""
    found = _PRODUCT_PATH.search(href)
    return found["slug"] if found else None


def _tile_id(tile: Locator) -> str | None:
    """Return the id of the product a result tile links to, or ``None``.

    ``None`` here means `_product_match` raises rather than silently reporting
    a match with no id, since - unlike a tile with no name heading, which is
    routinely a sponsored slot or similar - a *readable* product tile with no
    parseable id is a sign the markup itself has changed underneath the
    selector's assumption, not a normal "not a product" case.
    """
    link = _locate(tile, "search.tile_link")
    if link.count() == 0:
        return None
    href = link.get_attribute("href")
    return None if href is None else _product_id_from_href(href)


def _product_match(tile: Locator) -> ProductMatch | None:
    """Read one tile's product name, id and image, or ``None`` if it isn't one.

    The one place any of the three is read: `add_to_basket`'s matching
    lookup (`_find_matching_tile`) reuses this for `name` and `id` so a
    tile's name can't be read one way for display and another way for
    matching. Returns ``None`` - rather than blocking on `inner_text` for the
    full default timeout - for a tile whose name heading never appears; see
    `_TILE_HEADING_TIMEOUT_MS`. That's routine (a sponsored slot, a
    "browse the aisle" card), so it's skipped rather than raised.

    Raises:
        RuntimeError: If a name *was* read but no id could be - `id` is never
            optional on a `ProductMatch` this returns, and unlike a missing
            name this isn't an expected shape for a real result tile to have;
            see `_tile_id`.
    """
    heading = _locate(tile, "search.tile_name")
    try:
        heading.wait_for(state="visible", timeout=_TILE_HEADING_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        return None
    name = heading.inner_text().strip()
    if not name:
        return None
    tile_id = _tile_id(tile)
    if tile_id is None:
        msg = (
            f"Read a product name ({name!r}) from a result tile but no id from "
            "its product link - the results page markup has probably changed."
        )
        raise ClassifiedError(msg, ErrorCategory.SITE_CHANGED, retryable=False)
    image = _locate(tile, "search.tile_image")
    # The `src` property rather than the attribute: the attribute is whatever
    # the markup says, and on most of the results page that is a path with no
    # host, which a caller can't render.
    image_url = (image.evaluate("img => img.src") or None) if image.count() else None
    return ProductMatch(name=name, id=tile_id, image_url=image_url)


def _readable_matches(tiles: Locator) -> Iterator[ProductMatch]:
    """Yield a `ProductMatch` for each result tile that reads as a product.

    Tiles that don't (see `_product_match`) are skipped rather than aborting
    the search, so one sponsored slot among the results doesn't sink the call.
    """
    for tile in tiles.all()[:_MAX_RESULT_TILES]:
        match = _product_match(tile)
        if match is not None:
            yield match


# Markers a truncated display value ends in - a name cut short by a display
# surface with a character limit (a chat client rendering a tool result, for
# instance - nothing in this codebase itself truncates a product name) before
# a caller copies it back into `add_to_basket`. `…` is the single-character
# ellipsis some renderers use in place of three dots.
_ELLIPSIS_SUFFIXES = ("...", "…")


def _without_trailing_ellipsis(text: str) -> str | None:
    """Return ``text`` with a trailing ellipsis (and its lead-in space) removed.

    ``None`` if ``text`` doesn't end in one - callers use that to tell "this
    looks like a name truncated by a display surface" from "this is just a
    product name that happens to need no cleanup".
    """
    for suffix in _ELLIPSIS_SUFFIXES:
        if text.endswith(suffix):
            return text[: -len(suffix)].rstrip()
    return None


def _find_matching_tile(
    tiles: Locator, product_name: str, product_id: str | None = None
) -> tuple[Locator, str] | None:
    """Return the first tile matching the given product, with its full name.

    Matches on the same name and id `_product_match` would report for that
    tile - see its docstring for why - and skips tiles that don't read as a
    product.

    If ``product_id`` is given, it's the only thing matched on: the first
    tile whose own id equals it, full stop. An id names one specific product
    the way a byte-exact name can't quite - it isn't reshaped by whatever
    displayed a result to the caller, the way a name can be truncated - so
    there's no fallback to weigh against it.

    Otherwise, tries an exact match on ``product_name`` (whitespace aside)
    first. If ``product_name`` itself ends in an ellipsis, falls back to a
    prefix match against the stripped text - the shape of a name a display
    surface truncated before a caller copied it back (see
    `_ELLIPSIS_SUFFIXES`), for which byte-exact matching can never succeed no
    matter how the name is typed.

    Either way, the tile's own full name is returned rather than
    ``product_name``, so a truncated input still resolves to the real
    product name.
    """
    matches = [
        (tile, match)
        for tile, match in (
            (tile, _product_match(tile)) for tile in tiles.all()[:_MAX_RESULT_TILES]
        )
        if match is not None
    ]

    if product_id is not None:
        for tile, match in matches:
            if match.id == product_id:
                return tile, match.name
        return None

    target = product_name.strip()
    for tile, match in matches:
        if match.name == target:
            return tile, match.name

    prefix = _without_trailing_ellipsis(target)
    if prefix:
        for tile, match in matches:
            if match.name.startswith(prefix):
                return tile, match.name
    return None


def add_to_basket(
    product_name: str,
    *,
    storage_state_path: Path,
    product_id: str | None = None,
    quantity: int = 1,
) -> str:
    """Search for ``product_name`` and add the result matching it.

    Requires an already-authenticated session - see the module docstring and
    `browser.browser_page`'s `storage_state` parameter. Mirrors a real,
    manually recorded search-and-add flow (site search, then the result
    tile's own "add" control) rather than opening the product's own page -
    see the module docstring for what that recording showed. Still unverified
    end to end against a real, authenticated session - see the README's
    "Not done yet" section.

    Which result is "the" match is deliberate rather than picking the first
    or an indexed result: an index can go stale between a search and this
    call (the site re-ranks or re-stocks in between), and blindly taking the
    first result can add the wrong product for an ambiguous query. Naming the
    exact product avoids both, and this accepts two ways to do that:

    - `product_id`, a result's own id from `search_products` (its tile's
      product link's slug, e.g. `"sainsburys-spaghetti-pasta-500g"`) - the
      more robust choice when it's available. It names one specific product
      directly, so it isn't affected by anything happening to `product_name`
      before it gets here.
    - Otherwise, `product_name` must match a result's heading exactly
      (whitespace aside) - typically one just returned by `search_products`.
      If it itself ends in an ellipsis ("..." or "…") - the shape of a name
      some *other* surface truncated (a chat client rendering a long tool
      result, say) before a caller copied it back, not anything this
      codebase does to a name itself - an exact match can never succeed no
      matter how it's typed. Rather than fail every time on a value nobody
      could have gotten right, this falls back to a prefix match against the
      text before the ellipsis.

    Either way, both the search itself and the returned name use the
    result's real, full name rather than a possibly-truncated `product_name`.

    Args:
        product_name: The product's name, exactly as shown on its search
            result tile (e.g. from `search_products`) - or, as a fallback,
            that name with a trailing "..."/"…" if that's all a caller has.
            Used, ellipsis stripped, as the search term regardless of
            whether `product_id` is also given.
        storage_state_path: Path to a Playwright `storage_state` JSON file
            holding a logged-in session, captured by
            `scripts/sainsburys_login.py`.
        product_id: A result's own id, from `search_products`, if the caller
            has one. When given, this is matched instead of `product_name` -
            see above.
        quantity: How many of the product to add. Clicks the result tile's
            "add" control this many times.

    Returns:
        The added product's real, full name, as shown on its result tile -
        not necessarily `product_name` itself, if it matched via `product_id`
        or the ellipsis fallback above.

    Raises:
        NotLoggedInError: If the saved session is missing or not accepted.
        RuntimeError: If no results are found, none match `product_id` or
            `product_name`, the matching result has no "add" control, or a
            tile a name *was* read from had no readable id (see
            `_product_match`).
    """
    with _authenticated_page(storage_state_path) as page:
        # A query ending in literal ellipsis characters wouldn't find much on
        # the real site's own search either, so search on the cleaned-up text
        # whenever `product_name` has the shape of a truncated display value.
        search_query = _without_trailing_ellipsis(product_name.strip()) or product_name
        tiles = _run_search(page, search_query)

        found = _find_matching_tile(tiles, product_name, product_id)
        if found is None:
            if product_id is not None:
                msg = (
                    f"No search result has id {product_id!r}. Call "
                    "search_products first and pass one of its results' "
                    "`id` (or its `name` exactly, if it has none)."
                )
            else:
                msg = (
                    f"No search result exactly matches {product_name!r}. Call "
                    "search_products first and pass one of its product names "
                    "exactly, including capitalisation and punctuation - or "
                    "its `id` instead."
                )
            raise ClassifiedError(msg, ErrorCategory.NOT_FOUND, retryable=False)
        tile, matched_name = found

        add_button = _locate(tile, "search.add_button")
        if add_button.count() == 0:
            msg = (
                f'No "add" control found on the result for {product_name!r}. '
                "Either the page has changed, or the product is unavailable."
            )
            raise ClassifiedError(msg, ErrorCategory.UNAVAILABLE, retryable=False)

        for _ in range(quantity):
            add_button.click(timeout=15_000)

        return matched_name


class SainsburysLoginSteps:
    """Sainsbury's real login, as the steps :mod:`browser_mcp_core` drives.

    This is the one place in this package a password passes through - see the
    module docstring for what that means and why it is accepted. Neither the
    username nor the password is written anywhere by these steps; only the
    resulting session is, by core.

    Each method owns its own waiting, because how these pages settle is a fact
    about Sainsbury's and not something core should have an opinion about:
    `wait_until="load"` is unreliable here (a background poller keeps the
    network busy, so it eats the full timeout), and the login URL bounces
    through a redirect before the real form appears.
    """

    #: Shown on the login page and in the message raised when a verification
    #: code was asked for and not supplied.
    display_name = "Sainsbury's"

    #: Sainsbury's runs Akamai Bot Manager, which fingerprints and blocks
    #: headless Chromium specifically - see the module docstring. That costs a
    #: headed browser under Xvfb, and around 400MB.
    headless = False

    def consent_cookies(self) -> list[dict[str, object]]:
        """Return the OneTrust "choice already made" cookies to seed."""
        return _consent_cookies()

    def open_login_form(self, page: Page) -> None:
        """Navigate to the login form and wait for it to be fillable."""
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        # LOGIN_URL bounces through a redirect to the real form on the account
        # domain; wait for that form before filling anything.
        _locate(page, "login.username").wait_for(state="visible", timeout=30_000)

    def fill_username(self, page: Page, username: str) -> None:
        """Type the account username into the form."""
        _locate(page, "login.username").fill(username)

    def fill_password(self, page: Page, password: str) -> None:
        """Type the account password into the form."""
        _locate(page, "login.password").fill(password)

    def submit(self, page: Page) -> None:
        """Submit the credentials and wait for whatever comes next."""
        _locate(page, "login.submit").click(timeout=15_000)
        page.wait_for_load_state("domcontentloaded", timeout=30_000)

    def otp_requested(self, page: Page) -> bool:
        """Report whether Sainsbury's is asking for a verification code.

        It does not always ask - see the module docstring - so this is a
        question rather than an assumption, and a timeout waiting for the field
        is an answer of "no" rather than a failure.

        Args:
            page: The page to read.

        Returns:
            Whether the MFA field is on screen.
        """
        otp_field = _locate(page, "login.otp")
        with contextlib.suppress(PlaywrightTimeoutError):
            otp_field.wait_for(state="visible", timeout=15_000)
        return otp_field.is_visible()

    def submit_otp(self, page: Page, code: str) -> None:
        """Enter a verification code, submit it, and wait for the redirect."""
        _locate(page, "login.otp").fill(code)
        _locate(page, "login.submit_code").click(timeout=15_000)
        # The code submit kicks off the redirect that actually completes the
        # login; let it finish before navigating away from it.
        page.wait_for_load_state("domcontentloaded", timeout=30_000)

    def confirm(self, page: Page, *, screenshot_path: Path | None) -> None:
        """Check the login worked, raising if it did not.

        Args:
            page: The page to read the outcome from.
            screenshot_path: Where to write a screenshot of the failure page.

        Raises:
            NotLoggedInError: If the session still is not authenticated - most
                likely a wrong password.
        """
        with contextlib.suppress(PlaywrightTimeoutError):
            page.goto(MY_ACCOUNT_URL, wait_until="load", timeout=25_000)
        _raise_if_not_logged_in(
            page,
            "Still not logged in after submitting the login form (and any "
            "verification code) - check the password and, if you were asked "
            "for one, the verification code.",
            screenshot_path=screenshot_path,
        )
