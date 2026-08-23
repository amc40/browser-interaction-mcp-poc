"""Tests for the Sainsbury's browser action, against a fake Playwright.

Nothing here touches a real browser or the real site: a fake stands in for the
`sync_playwright()` chain, shaped only as far as `sainsburys.py` actually calls
into it. That keeps these fast and offline, at the cost of not proving the
real page still looks like this - see scripts/sainsburys_products_we_love.py
for validating that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from browser_interaction_mcp import sainsburys

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass
class FakeLocator:
    """A resolved Playwright locator: already narrowed, so `.first` is itself."""

    count_: int = 1
    child: FakeLocator | None = None
    items: list[FakeLocator] = field(default_factory=list)
    attributes: Mapping[str, str | None] = field(default_factory=dict)
    text: str = ""
    clicked: bool = False

    @property
    def first(self) -> FakeLocator:
        """Return this locator, already treated as narrowed to one match."""
        return self

    def count(self) -> int:
        """Return how many elements this locator matched."""
        return self.count_

    def click(self, *, timeout: int | None = None) -> None:
        """Record that this locator was clicked."""
        del timeout
        self.clicked = True

    def locator(self, selector: str) -> FakeLocator:
        """Return the one pre-wired child locator, regardless of selector."""
        if self.child is None:
            msg = f"unexpected locator({selector!r}) call"
            raise AssertionError(msg)
        return self.child

    def all(self) -> list[FakeLocator]:
        """Return the pre-wired list of matched locators."""
        return self.items

    def get_attribute(self, name: str) -> str | None:
        """Return the pre-wired attribute value, if any."""
        return self.attributes.get(name)

    def inner_text(self) -> str:
        """Return the pre-wired text content."""
        return self.text


def _tile(*, aria_label: str | None = None, text: str = "") -> FakeLocator:
    """Build a product-tile-shaped locator, as `.locator("a").all()` would return."""
    return FakeLocator(attributes={"aria-label": aria_label}, text=text)


@dataclass
class FakePage:
    """A page that only knows the three lookups `sainsburys.py` performs."""

    cookie_button: FakeLocator
    heading_role: FakeLocator
    heading_text: FakeLocator
    goto_calls: list[str] = field(default_factory=list)
    load_states: list[str] = field(default_factory=list)

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        """Record the navigated-to URL."""
        del wait_until, timeout
        self.goto_calls.append(url)

    def wait_for_load_state(self, state: str, *, timeout: int) -> None:
        """Record the load state waited for."""
        del timeout
        self.load_states.append(state)

    def get_by_role(self, role: str, *, name: object) -> FakeLocator:
        """Return the pre-wired locator for the requested role."""
        del name
        if role == "button":
            return self.cookie_button
        if role == "heading":
            return self.heading_role
        msg = f"unexpected role {role!r}"
        raise AssertionError(msg)

    def get_by_text(self, text: object) -> FakeLocator:
        """Return the pre-wired fallback heading locator."""
        del text
        return self.heading_text


@dataclass
class FakeContext:
    """A browser context that always hands back the one fake page."""

    page: FakePage
    closed: bool = False

    def new_page(self) -> FakePage:
        """Return the fake page."""
        return self.page

    def close(self) -> None:
        """Record that the context was closed."""
        self.closed = True


@dataclass
class FakeBrowser:
    """A browser that always hands back the one fake context."""

    context: FakeContext
    closed: bool = False

    def new_context(self, **kwargs: object) -> FakeContext:
        """Return the fake context."""
        del kwargs
        return self.context

    def close(self) -> None:
        """Record that the browser was closed."""
        self.closed = True


@dataclass
class FakeChromium:
    """Stands in for `playwright.chromium`."""

    browser: FakeBrowser

    def launch(self, *, headless: bool) -> FakeBrowser:
        """Return the fake browser."""
        del headless
        return self.browser


@dataclass
class FakePlaywright:
    """Stands in for the object `sync_playwright()` yields."""

    browser: FakeBrowser

    @property
    def chromium(self) -> FakeChromium:
        """Return a chromium launcher wrapping the fake browser."""
        return FakeChromium(self.browser)


@dataclass
class FakeSyncPlaywrightContextManager:
    """Stands in for the object `sync_playwright` itself returns."""

    playwright: FakePlaywright

    def __enter__(self) -> FakePlaywright:
        """Enter the fake context, returning the fake playwright object."""
        return self.playwright

    def __exit__(self, *exc_info: object) -> None:
        """Exit the fake context; nothing to clean up."""
        del exc_info


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    page: FakePage,
) -> FakeBrowser:
    """Monkeypatch `sync_playwright` to serve `page`, and return its browser."""
    context = FakeContext(page=page)
    browser = FakeBrowser(context=context)
    playwright = FakePlaywright(browser=browser)
    monkeypatch.setattr(
        sainsburys,
        "sync_playwright",
        lambda: FakeSyncPlaywrightContextManager(playwright),
    )
    return browser


def _page_with_section(
    *,
    tiles: list[FakeLocator],
    cookie_banner: bool = False,
) -> FakePage:
    """Build a page whose "Products we love" heading resolves to `tiles`."""
    anchors = FakeLocator(items=tiles)
    container = FakeLocator(child=anchors)
    return FakePage(
        cookie_button=FakeLocator(count_=1 if cookie_banner else 0),
        heading_role=FakeLocator(count_=1, child=container),
        heading_text=FakeLocator(count_=0),
    )


def test_returns_names_in_order_deduplicated_and_filtered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tiles = [
        _tile(aria_label="Chocolate Digestives 400g"),
        _tile(text="Freshly Baked Sourdough\n£1.50"),
        _tile(aria_label="Add"),  # filtered: a cart control, not a product
        _tile(aria_label="Chocolate Digestives 400g"),  # duplicate, skipped
        _tile(aria_label="Semi Skimmed Milk 2L"),
        _tile(aria_label="Free Range Eggs x6"),
        _tile(aria_label="Vine Tomatoes 400g"),  # never reached: count already 5
    ]
    page = _page_with_section(tiles=tiles)
    _wire(monkeypatch, page)

    names = sainsburys.products_we_love()

    assert names == [
        "Chocolate Digestives 400g",
        "Freshly Baked Sourdough",
        "Semi Skimmed Milk 2L",
        "Free Range Eggs x6",
        "Vine Tomatoes 400g",
    ]


def test_navigates_to_the_given_url(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _page_with_section(tiles=[_tile(aria_label="Anything")])
    _wire(monkeypatch, page)

    sainsburys.products_we_love(url="https://example.invalid/groceries")

    assert page.goto_calls == ["https://example.invalid/groceries"]
    assert page.load_states == ["networkidle"]


def test_honours_a_smaller_count(monkeypatch: pytest.MonkeyPatch) -> None:
    tiles = [_tile(aria_label="A"), _tile(aria_label="B"), _tile(aria_label="C")]
    page = _page_with_section(tiles=tiles)
    _wire(monkeypatch, page)

    assert sainsburys.products_we_love(count=2) == ["A", "B"]


def test_dismisses_a_cookie_banner_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _page_with_section(tiles=[_tile(aria_label="Anything")], cookie_banner=True)
    _wire(monkeypatch, page)

    sainsburys.products_we_love()

    assert page.cookie_button.clicked


def test_falls_back_to_text_search_when_no_heading_role_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchors = FakeLocator(items=[_tile(aria_label="Fallback Find")])
    container = FakeLocator(child=anchors)
    page = FakePage(
        cookie_button=FakeLocator(count_=0),
        heading_role=FakeLocator(count_=0),
        heading_text=FakeLocator(count_=1, child=container),
    )
    _wire(monkeypatch, page)

    assert sainsburys.products_we_love() == ["Fallback Find"]


def test_raises_when_no_heading_is_found(monkeypatch: pytest.MonkeyPatch) -> None:
    page = FakePage(
        cookie_button=FakeLocator(count_=0),
        heading_role=FakeLocator(count_=0),
        heading_text=FakeLocator(count_=0),
    )
    _wire(monkeypatch, page)

    with pytest.raises(RuntimeError, match=r"No .* heading found"):
        sainsburys.products_we_love()


def test_raises_when_the_heading_has_no_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_container = FakeLocator(count_=0)
    page = FakePage(
        cookie_button=FakeLocator(count_=0),
        heading_role=FakeLocator(count_=1, child=empty_container),
        heading_text=FakeLocator(count_=0),
    )
    _wire(monkeypatch, page)

    with pytest.raises(RuntimeError, match="no container beneath it"):
        sainsburys.products_we_love()


def test_skips_a_tile_with_no_usable_name(monkeypatch: pytest.MonkeyPatch) -> None:
    tiles = [_tile(), _tile(aria_label="Real Product")]
    page = _page_with_section(tiles=tiles)
    _wire(monkeypatch, page)

    assert sainsburys.products_we_love() == ["Real Product"]


def test_closes_the_browser_after_a_successful_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _page_with_section(tiles=[_tile(aria_label="Anything")])
    browser = _wire(monkeypatch, page)

    sainsburys.products_we_love()

    assert browser.closed


def test_closes_the_browser_even_when_the_section_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage(
        cookie_button=FakeLocator(count_=0),
        heading_role=FakeLocator(count_=0),
        heading_text=FakeLocator(count_=0),
    )
    browser = _wire(monkeypatch, page)

    with pytest.raises(RuntimeError):
        sainsburys.products_we_love()

    assert browser.closed
