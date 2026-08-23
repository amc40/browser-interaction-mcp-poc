"""Tests for the Sainsbury's browser action, against a fake Playwright page.

Nothing here touches a real browser, Xvfb, or the real site: `browser_page`
(tested in its own right in test_browser.py) is monkeypatched out entirely, so
these only have to fake a `Page`, not the launch machinery beneath it. That
keeps these fast and offline, at the cost of not proving the real page still
looks like this - see scripts/sainsburys_products_we_love.py for validating
that for real.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from browser_interaction_mcp import sainsburys

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass
class FakeLocator:
    """A resolved Playwright locator: already narrowed, so `.first` is itself."""

    count_: int = 1
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

    def inner_text(self) -> str:
        """Return the pre-wired text content."""
        return self.text

    def wait_for(self, *, state: str, timeout: int | None = None) -> None:
        """No-op: the fake page is always "ready" as soon as it's built."""
        del state, timeout


@dataclass
class FakePage:
    """A page that only knows the two lookups `sainsburys.py` performs."""

    headings: list[FakeLocator]
    cookie_button: FakeLocator = field(default_factory=lambda: FakeLocator(count_=0))
    goto_calls: list[str] = field(default_factory=list)

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        """Record the navigated-to URL."""
        del wait_until, timeout
        self.goto_calls.append(url)

    def get_by_role(
        self, role: str, *, name: object = None
    ) -> FakeLocator | _HeadingsLocator:
        """Return the cookie button, or a locator over the matching heading."""
        del name
        if role == "button":
            return self.cookie_button
        if role == "heading":
            return _HeadingsLocator(self.headings)
        msg = f"unexpected role {role!r}"
        raise AssertionError(msg)


@dataclass
class _HeadingsLocator:
    """`page.get_by_role("heading")`: matches every heading, in order."""

    headings: list[FakeLocator]

    @property
    def first(self) -> FakeLocator:
        """Return the first heading whose text matches "Products we love"."""
        for heading in self.headings:
            if sainsburys._PRODUCTS_WE_LOVE_HEADING.search(heading.text):
                return heading
        return FakeLocator(count_=0)

    def all(self) -> list[FakeLocator]:
        """Return every heading, in document order."""
        return self.headings


def _heading(text: str) -> FakeLocator:
    return FakeLocator(text=text)


def _wire(monkeypatch: pytest.MonkeyPatch, page: FakePage) -> None:
    """Monkeypatch `browser_page` to hand `page` straight to the caller."""

    @contextlib.contextmanager
    def fake_browser_page(*, headless: bool) -> Iterator[FakePage]:
        assert headless is False, "products_we_love must ask for a headed session"
        yield page

    monkeypatch.setattr(sainsburys, "browser_page", fake_browser_page)


def test_returns_names_in_order_deduplicated_and_filtered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headings = [
        _heading("Welcome to Sainsbury's"),
        _heading("Carousel"),
        _heading("Products we love"),
        _heading("Chocolate Digestives 400g"),
        _heading("Freshly Baked Sourdough"),
        _heading("Chocolate Digestives 400g"),  # duplicate, skipped
        _heading("Semi Skimmed Milk 2L"),
        _heading("Free Range Eggs x6"),
        _heading("Vine Tomatoes 400g"),  # never reached: count already 5
    ]
    page = FakePage(headings=headings)
    _wire(monkeypatch, page)

    assert sainsburys.products_we_love() == [
        "Chocolate Digestives 400g",
        "Freshly Baked Sourdough",
        "Semi Skimmed Milk 2L",
        "Free Range Eggs x6",
        "Vine Tomatoes 400g",
    ]


def test_navigates_to_the_given_url(monkeypatch: pytest.MonkeyPatch) -> None:
    page = FakePage(headings=[_heading("Products we love"), _heading("A")])
    _wire(monkeypatch, page)

    sainsburys.products_we_love(url="https://example.invalid/groceries")

    assert page.goto_calls == ["https://example.invalid/groceries"]


def test_honours_a_smaller_count(monkeypatch: pytest.MonkeyPatch) -> None:
    headings = [
        _heading("Products we love"),
        _heading("A"),
        _heading("B"),
        _heading("C"),
    ]
    page = FakePage(headings=headings)
    _wire(monkeypatch, page)

    assert sainsburys.products_we_love(count=2) == ["A", "B"]


def test_dismisses_a_cookie_banner_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage(
        headings=[_heading("Products we love"), _heading("A")],
        cookie_button=FakeLocator(count_=1),
    )
    _wire(monkeypatch, page)

    sainsburys.products_we_love()

    assert page.cookie_button.clicked


def test_raises_when_no_heading_is_found(monkeypatch: pytest.MonkeyPatch) -> None:
    page = FakePage(headings=[_heading("Welcome to Sainsbury's")])
    _wire(monkeypatch, page)

    with pytest.raises(RuntimeError, match=r"No .* heading found"):
        sainsburys.products_we_love()


def test_skips_carousel_and_copyright_headings(monkeypatch: pytest.MonkeyPatch) -> None:
    headings = [
        _heading("Products we love"),
        _heading("Real Product"),
        _heading("Copyright terms"),
        _heading("Carousel"),
        _heading("Trending this week"),
    ]
    page = FakePage(headings=headings)
    _wire(monkeypatch, page)

    assert sainsburys.products_we_love() == ["Real Product", "Trending this week"]


def test_skips_a_heading_with_no_text(monkeypatch: pytest.MonkeyPatch) -> None:
    headings = [
        _heading("Products we love"),
        _heading(""),
        _heading("Real Product"),
    ]
    page = FakePage(headings=headings)
    _wire(monkeypatch, page)

    assert sainsburys.products_we_love() == ["Real Product"]
