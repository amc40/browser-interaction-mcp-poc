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
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from browser_interaction_mcp import sainsburys

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass
class FakeLocator:
    """A resolved Playwright locator: already narrowed, so `.first` is itself."""

    count_: int = 1
    text: str = ""
    clicked: bool = False
    click_count: int = 0
    raises_on_wait: bool = False
    filled: str | None = None
    pressed_keys: list[str] = field(default_factory=list)
    heading: FakeLocator | None = None
    add_button: FakeLocator | None = None

    @property
    def first(self) -> FakeLocator:
        """Return this locator, already treated as narrowed to one match."""
        return self

    def count(self) -> int:
        """Return how many elements this locator matched."""
        return self.count_

    def click(self, *, timeout: int | None = None) -> None:
        """Record that this locator was clicked, once per call."""
        del timeout
        self.clicked = True
        self.click_count += 1

    def fill(self, value: str) -> None:
        """Record what was typed into this locator, as a search box."""
        self.filled = value

    def press(self, key: str) -> None:
        """Record a key pressed on this locator, as a search box."""
        self.pressed_keys.append(key)

    def inner_text(self) -> str:
        """Return the pre-wired text content."""
        return self.text

    def wait_for(self, *, state: str, timeout: int | None = None) -> None:
        """No-op, unless wired to simulate a real Playwright timeout."""
        del state, timeout
        if self.raises_on_wait:
            msg = "Timeout waiting for locator"
            raise PlaywrightTimeoutError(msg)

    def get_by_role(self, role: str, *, name: object = None) -> FakeLocator:
        """Return this tile's product-name heading."""
        del name
        assert role == "heading", f"unexpected role {role!r}"
        return self.heading if self.heading is not None else FakeLocator(count_=0)

    def get_by_test_id(self, test_id: str) -> FakeLocator:
        """Return this tile's "add" control."""
        assert test_id == sainsburys._ADD_BUTTON_TEST_ID, f"unexpected id {test_id!r}"
        return self.add_button if self.add_button is not None else FakeLocator(count_=0)


@dataclass
class FakePage:
    """A page that only knows the lookups `sainsburys.py` performs."""

    headings: list[FakeLocator] = field(default_factory=list)
    cookie_button: FakeLocator = field(default_factory=lambda: FakeLocator(count_=0))
    search_box: FakeLocator = field(default_factory=FakeLocator)
    product_tile: FakeLocator = field(
        default_factory=lambda: FakeLocator(
            heading=FakeLocator(text="A Product"),
            add_button=FakeLocator(count_=1),
        )
    )
    url: str = "https://www.sainsburys.co.uk/gol-ui/MyAccount"
    goto_calls: list[str] = field(default_factory=list)

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        """Record the requested URL.

        Deliberately does *not* update `self.url`: a real page's `.url` can
        differ from what was requested after a redirect, which is exactly
        what the login-redirect tests need to simulate. `url` is set upfront
        by the test via the constructor instead.
        """
        del wait_until, timeout
        self.goto_calls.append(url)

    def get_by_role(
        self, role: str, *, name: object = None
    ) -> FakeLocator | _HeadingsLocator:
        """Return the matching button, combobox, or heading locator."""
        del name
        if role == "button":
            return self.cookie_button
        if role == "combobox":
            return self.search_box
        if role == "heading":
            return _HeadingsLocator(self.headings)
        msg = f"unexpected role {role!r}"
        raise AssertionError(msg)

    def locator(self, selector: str) -> FakeLocator:
        """Return the product tile, for the `product-tile-*` selector."""
        assert selector == sainsburys._PRODUCT_TILE_SELECTOR, (
            f"unexpected selector {selector!r}"
        )
        return self.product_tile


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


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    page: FakePage,
    *,
    storage_states: list[object] | None = None,
) -> None:
    """Monkeypatch `browser_page` to hand `page` straight to the caller.

    Args:
        monkeypatch: Standard pytest fixture.
        page: The fake page every call opens.
        storage_states: If given, every `storage_state` a caller passed is
            appended here, so a test can assert on it.
    """

    @contextlib.contextmanager
    def fake_browser_page(
        *, headless: bool, storage_state: object = None
    ) -> Iterator[FakePage]:
        assert headless is False, "browser actions here must ask for a headed session"
        if storage_states is not None:
            storage_states.append(storage_state)
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


# ---------------------------------------------------------------------------
# add_to_basket
# ---------------------------------------------------------------------------
def test_add_to_basket_searches_and_returns_the_first_results_product_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage(
        product_tile=FakeLocator(
            heading=FakeLocator(text=" Fairy Lemon Washing Up Liquid "),
            add_button=FakeLocator(count_=1),
        )
    )
    _wire(monkeypatch, page)

    result = sainsburys.add_to_basket(
        "washing up liquid", storage_state_path=Path("session.json")
    )

    assert result == "Fairy Lemon Washing Up Liquid"
    assert page.goto_calls == [sainsburys.MY_ACCOUNT_URL]
    assert page.search_box.filled == "washing up liquid"
    assert page.search_box.pressed_keys == ["Enter"]
    assert page.product_tile.add_button is not None
    assert page.product_tile.add_button.clicked


def test_add_to_basket_uses_the_default_query_when_none_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage()
    _wire(monkeypatch, page)

    sainsburys.add_to_basket(storage_state_path=Path("session.json"))

    assert page.search_box.filled == sainsburys.DEFAULT_SEARCH_QUERY


def test_add_to_basket_passes_the_storage_state_path_to_browser_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage()
    storage_states: list[object] = []
    _wire(monkeypatch, page, storage_states=storage_states)

    sainsburys.add_to_basket(storage_state_path=Path("session.json"))

    assert storage_states == [Path("session.json")]


def test_add_to_basket_clicks_add_once_per_unit_of_quantity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage()
    _wire(monkeypatch, page)

    sainsburys.add_to_basket(storage_state_path=Path("session.json"), quantity=3)

    assert page.product_tile.add_button is not None
    assert page.product_tile.add_button.click_count == 3


def test_add_to_basket_dismisses_a_cookie_banner_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage(cookie_button=FakeLocator(count_=1))
    _wire(monkeypatch, page)

    sainsburys.add_to_basket(storage_state_path=Path("session.json"))

    assert page.cookie_button.clicked


def test_add_to_basket_raises_when_redirected_to_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage(url="https://www.sainsburys.co.uk/gol-ui/oauth/login?returnUrl=%2F")
    _wire(monkeypatch, page)

    with pytest.raises(sainsburys.NotLoggedInError, match=r"sainsburys_login\.py"):
        sainsburys.add_to_basket(storage_state_path=Path("session.json"))

    assert not page.search_box.filled


def test_add_to_basket_raises_when_no_results_are_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage(product_tile=FakeLocator(count_=0, raises_on_wait=True))
    _wire(monkeypatch, page)

    with pytest.raises(RuntimeError, match="No search results"):
        sainsburys.add_to_basket(
            "a nonexistent product", storage_state_path=Path("session.json")
        )


def test_add_to_basket_raises_when_the_result_has_no_add_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage(
        product_tile=FakeLocator(
            heading=FakeLocator(text="A Product"),
            add_button=FakeLocator(count_=0),
        )
    )
    _wire(monkeypatch, page)

    with pytest.raises(RuntimeError, match='"add" control'):
        sainsburys.add_to_basket(storage_state_path=Path("session.json"))
