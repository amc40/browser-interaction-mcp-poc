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
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    visible: bool = False
    filled: str | None = None
    pressed_keys: list[str] = field(default_factory=list)
    heading: FakeLocator | None = None
    add_button: FakeLocator | None = None
    image_src: str | None = "https://example.invalid/product.jpg"
    has_image: bool = True

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

    def is_visible(self) -> bool:
        """Return the pre-wired visibility."""
        return self.visible

    def get_by_role(self, role: str, *, name: object = None) -> FakeLocator:
        """Return this tile's product-name heading."""
        del name
        assert role == "heading", f"unexpected role {role!r}"
        return self.heading if self.heading is not None else FakeLocator(count_=0)

    def get_by_test_id(self, test_id: str) -> FakeLocator:
        """Return this tile's "add" control."""
        assert test_id == sainsburys._ADD_BUTTON_TEST_ID, f"unexpected id {test_id!r}"
        return self.add_button if self.add_button is not None else FakeLocator(count_=0)

    def locator(self, selector: str) -> FakeLocator:
        """Return this tile's image, as `<img>`."""
        assert selector == "img", f"unexpected selector {selector!r}"
        return self if self.has_image else FakeLocator(count_=0)

    def get_attribute(self, name: str) -> str | None:
        """Return this (image) locator's pre-wired `src`."""
        assert name == "src", f"unexpected attribute {name!r}"
        return self.image_src


@dataclass
class _ProductTilesLocator:
    """`page.locator(_PRODUCT_TILE_SELECTOR)`: every result tile, in order."""

    tiles: list[FakeLocator]

    @property
    def first(self) -> FakeLocator:
        """Return the first tile, or a locator that times out if there are none."""
        if self.tiles:
            return self.tiles[0]
        return FakeLocator(count_=0, raises_on_wait=True)

    def all(self) -> list[FakeLocator]:
        """Return every tile, in document order."""
        return self.tiles


@dataclass
class FakeBrowserContext:
    """Stands in for `page.context`: `refresh_session`'s only use of it."""

    storage_state_calls: list[object] = field(default_factory=list)

    def storage_state(self, *, path: Path) -> None:
        """Record where the session was asked to be saved, and write it.

        Actually creates the file - a placeholder, not a real storage_state
        payload - because `refresh_session` `chmod`s it immediately after,
        which needs a real file to exist.
        """
        self.storage_state_calls.append(path)
        path.write_text("{}", encoding="utf-8")


@dataclass
class FakePage:
    """A page that only knows the lookups `sainsburys.py` performs."""

    headings: list[FakeLocator] = field(default_factory=list)
    search_box: FakeLocator = field(default_factory=FakeLocator)
    product_tiles: list[FakeLocator] = field(
        default_factory=lambda: [
            FakeLocator(
                heading=FakeLocator(text="A Product"),
                add_button=FakeLocator(count_=1),
            )
        ]
    )
    username_field: FakeLocator = field(default_factory=FakeLocator)
    password_field: FakeLocator = field(default_factory=FakeLocator)
    log_in_button: FakeLocator = field(default_factory=FakeLocator)
    otp_field: FakeLocator = field(default_factory=FakeLocator)
    submit_code_button: FakeLocator = field(default_factory=FakeLocator)
    context: FakeBrowserContext = field(default_factory=FakeBrowserContext)
    url: str = "https://www.sainsburys.co.uk/gol-ui/MyAccount"
    goto_calls: list[str] = field(default_factory=list)
    screenshot_path: object = None

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        """Record the requested URL.

        Deliberately does *not* update `self.url`: a real page's `.url` can
        differ from what was requested after a redirect, which is exactly
        what the login-redirect tests need to simulate. `url` is set upfront
        by the test via the constructor instead.
        """
        del wait_until, timeout
        self.goto_calls.append(url)

    def wait_for_load_state(self, state: str, *, timeout: int | None = None) -> None:
        """No-op: the fake has no real navigation to settle."""
        del state, timeout

    def wait_for_timeout(self, timeout: int) -> None:
        """No-op: no real clock to wait on."""
        del timeout

    def screenshot(self, *, path: object) -> None:
        """Record that a debug screenshot was requested."""
        self.screenshot_path = path

    def get_by_role(
        self, role: str, *, name: object = None
    ) -> FakeLocator | _HeadingsLocator:
        """Return the matching combobox or heading locator."""
        del name
        if role == "combobox":
            return self.search_box
        if role == "heading":
            return _HeadingsLocator(self.headings)
        msg = f"unexpected role {role!r}"
        raise AssertionError(msg)

    @property
    def product_tile(self) -> FakeLocator:
        """Return the first result tile, for tests that only care about one."""
        return self.product_tiles[0]

    def locator(self, selector: str) -> _ProductTilesLocator:
        """Return the locator for a CSS selector `sainsburys.py` uses."""
        assert selector == sainsburys._PRODUCT_TILE_SELECTOR, (
            f"unexpected selector {selector!r}"
        )
        return _ProductTilesLocator(self.product_tiles)

    def get_by_test_id(self, test_id: str) -> FakeLocator:
        """Return the matching login-form field or button."""
        by_test_id = {
            sainsburys._USERNAME_TEST_ID: self.username_field,
            sainsburys._PASSWORD_TEST_ID: self.password_field,
            sainsburys._LOG_IN_TEST_ID: self.log_in_button,
            sainsburys._OTP_TEST_ID: self.otp_field,
            sainsburys._SUBMIT_CODE_TEST_ID: self.submit_code_button,
        }
        assert test_id in by_test_id, f"unexpected id {test_id!r}"
        return by_test_id[test_id]


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
    cookies_seen: list[Any] | None = None,
) -> None:
    """Monkeypatch `browser_page` to hand `page` straight to the caller.

    Args:
        monkeypatch: Standard pytest fixture.
        page: The fake page every call opens.
        storage_states: If given, every `storage_state` a caller passed is
            appended here, so a test can assert on it.
        cookies_seen: If given, every `cookies` list a caller passed is
            appended here.
    """

    @contextlib.contextmanager
    def fake_browser_page(
        *, headless: bool, storage_state: object = None, cookies: object = None
    ) -> Iterator[FakePage]:
        assert headless is False, "browser actions here must ask for a headed session"
        if storage_states is not None:
            storage_states.append(storage_state)
        if cookies_seen is not None:
            cookies_seen.append(cookies)
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


def test_seeds_minimal_consent_cookies_so_the_banner_never_renders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage(headings=[_heading("Products we love"), _heading("A")])
    cookies_seen: list[Any] = []
    _wire(monkeypatch, page, cookies_seen=cookies_seen)

    sainsburys.products_we_love()

    seeded = {c["name"]: c for c in cookies_seen[0]}
    assert "OptanonAlertBoxClosed" in seeded
    consent = seeded["OptanonConsent"]["value"]
    # strictly-necessary only: group 1 in, every optional group out.
    assert "groups=1%3A1%2C2%3A0%2C3%3A0%2C4%3A0" in consent
    # the registrable domain: one entry covers www, account and the rest.
    assert {c["domain"] for c in cookies_seen[0]} == {".sainsburys.co.uk"}


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
# search_products
# ---------------------------------------------------------------------------
def test_search_products_returns_names_and_image_urls_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage(
        product_tiles=[
            FakeLocator(
                heading=FakeLocator(text=" Fairy Lemon Washing Up Liquid "),
                image_src="https://example.invalid/fairy.jpg",
            ),
            FakeLocator(
                heading=FakeLocator(text="Ecover Washing Up Liquid"),
                image_src="https://example.invalid/ecover.jpg",
            ),
        ]
    )
    _wire(monkeypatch, page)

    results = sainsburys.search_products(
        "washing up liquid", storage_state_path=Path("session.json")
    )

    assert results == [
        sainsburys.ProductMatch(
            name="Fairy Lemon Washing Up Liquid",
            image_url="https://example.invalid/fairy.jpg",
        ),
        sainsburys.ProductMatch(
            name="Ecover Washing Up Liquid",
            image_url="https://example.invalid/ecover.jpg",
        ),
    ]
    assert page.search_box.filled == "washing up liquid"
    assert page.search_box.pressed_keys == ["Enter"]


def test_search_products_honours_a_smaller_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage(
        product_tiles=[
            FakeLocator(heading=FakeLocator(text="A")),
            FakeLocator(heading=FakeLocator(text="B")),
            FakeLocator(heading=FakeLocator(text="C")),
        ]
    )
    _wire(monkeypatch, page)

    results = sainsburys.search_products(
        storage_state_path=Path("session.json"), count=2
    )

    assert [match.name for match in results] == ["A", "B"]


def test_search_products_uses_the_default_query_when_none_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage()
    _wire(monkeypatch, page)

    sainsburys.search_products(storage_state_path=Path("session.json"))

    assert page.search_box.filled == sainsburys.DEFAULT_SEARCH_QUERY


def test_search_products_reports_no_image_when_the_tile_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage(
        product_tiles=[
            FakeLocator(heading=FakeLocator(text="No Photo Product"), has_image=False),
        ]
    )
    _wire(monkeypatch, page)

    results = sainsburys.search_products(storage_state_path=Path("session.json"))

    assert results == [sainsburys.ProductMatch(name="No Photo Product", image_url=None)]


def test_search_products_raises_when_redirected_to_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage(username_field=FakeLocator(visible=True))
    _wire(monkeypatch, page)

    with pytest.raises(sainsburys.NotLoggedInError, match=r"sainsburys_login\.py"):
        sainsburys.search_products(storage_state_path=Path("session.json"))

    assert not page.search_box.filled


def test_search_products_raises_when_no_results_are_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage(product_tiles=[])
    _wire(monkeypatch, page)

    with pytest.raises(RuntimeError, match="No search results"):
        sainsburys.search_products(
            "a nonexistent product", storage_state_path=Path("session.json")
        )


# ---------------------------------------------------------------------------
# add_to_basket
# ---------------------------------------------------------------------------
def test_add_to_basket_searches_and_adds_the_exact_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage(
        product_tiles=[
            FakeLocator(
                heading=FakeLocator(text=" Fairy Lemon Washing Up Liquid "),
                add_button=FakeLocator(count_=1),
            )
        ]
    )
    _wire(monkeypatch, page)

    result = sainsburys.add_to_basket(
        "Fairy Lemon Washing Up Liquid", storage_state_path=Path("session.json")
    )

    assert result == "Fairy Lemon Washing Up Liquid"
    assert page.goto_calls == [sainsburys.MY_ACCOUNT_URL]
    assert page.search_box.filled == "Fairy Lemon Washing Up Liquid"
    assert page.search_box.pressed_keys == ["Enter"]
    assert page.product_tile.add_button is not None
    assert page.product_tile.add_button.clicked


def test_add_to_basket_ignores_surrounding_whitespace_on_the_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage(
        product_tiles=[
            FakeLocator(
                heading=FakeLocator(text="Fairy Lemon Washing Up Liquid"),
                add_button=FakeLocator(count_=1),
            )
        ]
    )
    _wire(monkeypatch, page)

    result = sainsburys.add_to_basket(
        "  Fairy Lemon Washing Up Liquid  ", storage_state_path=Path("session.json")
    )

    assert result == "Fairy Lemon Washing Up Liquid"
    assert page.product_tile.add_button is not None
    assert page.product_tile.add_button.clicked


def test_add_to_basket_picks_the_matching_tile_among_several_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wanted_add_button = FakeLocator(count_=1)
    other_add_button = FakeLocator(count_=1)
    page = FakePage(
        product_tiles=[
            FakeLocator(
                heading=FakeLocator(text="Ecover Washing Up Liquid"),
                add_button=other_add_button,
            ),
            FakeLocator(
                heading=FakeLocator(text="Fairy Lemon Washing Up Liquid"),
                add_button=wanted_add_button,
            ),
        ]
    )
    _wire(monkeypatch, page)

    sainsburys.add_to_basket(
        "Fairy Lemon Washing Up Liquid", storage_state_path=Path("session.json")
    )

    assert wanted_add_button.clicked
    assert not other_add_button.clicked


def test_add_to_basket_raises_when_no_result_matches_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage(
        product_tiles=[
            FakeLocator(heading=FakeLocator(text="Ecover Washing Up Liquid"))
        ]
    )
    _wire(monkeypatch, page)

    with pytest.raises(RuntimeError, match="No search result exactly matches"):
        sainsburys.add_to_basket(
            "Fairy Lemon Washing Up Liquid", storage_state_path=Path("session.json")
        )


def test_add_to_basket_passes_the_storage_state_path_to_browser_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage()
    storage_states: list[object] = []
    _wire(monkeypatch, page, storage_states=storage_states)

    sainsburys.add_to_basket("A Product", storage_state_path=Path("session.json"))

    assert storage_states == [Path("session.json")]


def test_add_to_basket_clicks_add_once_per_unit_of_quantity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage()
    _wire(monkeypatch, page)

    sainsburys.add_to_basket(
        "A Product", storage_state_path=Path("session.json"), quantity=3
    )

    assert page.product_tile.add_button is not None
    assert page.product_tile.add_button.click_count == 3


def test_add_to_basket_seeds_consent_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage()
    cookies_seen: list[Any] = []
    _wire(monkeypatch, page, cookies_seen=cookies_seen)

    sainsburys.add_to_basket("A Product", storage_state_path=Path("session.json"))

    assert any(c["name"] == "OptanonConsent" for c in cookies_seen[0])


def test_add_to_basket_raises_when_redirected_to_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage(username_field=FakeLocator(visible=True))
    _wire(monkeypatch, page)

    with pytest.raises(sainsburys.NotLoggedInError, match=r"sainsburys_login\.py"):
        sainsburys.add_to_basket("A Product", storage_state_path=Path("session.json"))

    assert not page.search_box.filled


def test_add_to_basket_raises_when_no_results_are_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage(product_tiles=[])
    _wire(monkeypatch, page)

    with pytest.raises(RuntimeError, match="No search results"):
        sainsburys.add_to_basket(
            "a nonexistent product", storage_state_path=Path("session.json")
        )


def test_add_to_basket_raises_when_the_result_has_no_add_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage(
        product_tiles=[
            FakeLocator(
                heading=FakeLocator(text="A Product"),
                add_button=FakeLocator(count_=0),
            )
        ]
    )
    _wire(monkeypatch, page)

    with pytest.raises(RuntimeError, match='"add" control'):
        sainsburys.add_to_basket("A Product", storage_state_path=Path("session.json"))


# ---------------------------------------------------------------------------
# refresh_session
# ---------------------------------------------------------------------------
def _no_otp() -> str | None:
    return None


def _unreachable_otp() -> str | None:
    msg = "get_otp should not be called when MFA isn't required"
    raise AssertionError(msg)


def test_refresh_session_fills_and_submits_the_login_form(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    page = FakePage()
    _wire(monkeypatch, page)
    storage_state_path = tmp_path / "session.json"

    sainsburys.refresh_session(
        "alan@example.com",
        "hunter2",
        storage_state_path=storage_state_path,
        get_otp=_unreachable_otp,
    )

    assert page.goto_calls[0] == sainsburys.LOGIN_URL
    assert page.username_field.filled == "alan@example.com"
    assert page.password_field.filled == "hunter2"
    assert page.log_in_button.clicked
    assert page.context.storage_state_calls == [storage_state_path]


def test_refresh_session_restricts_permissions_on_the_saved_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    page = FakePage()
    _wire(monkeypatch, page)
    storage_state_path = tmp_path / "session.json"

    sainsburys.refresh_session(
        "alan@example.com",
        "hunter2",
        storage_state_path=storage_state_path,
        get_otp=_unreachable_otp,
    )

    mode = storage_state_path.stat().st_mode
    assert stat.S_IMODE(mode) == stat.S_IRUSR | stat.S_IWUSR


def test_refresh_session_does_not_ask_for_an_otp_when_not_redirected_to_mfa(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    page = FakePage()  # default url has no MFA path
    _wire(monkeypatch, page)

    sainsburys.refresh_session(
        "alan@example.com",
        "hunter2",
        storage_state_path=tmp_path / "session.json",
        get_otp=_unreachable_otp,
    )

    assert not page.otp_field.filled
    assert not page.submit_code_button.clicked


def test_refresh_session_submits_the_otp_when_redirected_to_mfa(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    page = FakePage(otp_field=FakeLocator(visible=True))
    _wire(monkeypatch, page)
    storage_state_path = tmp_path / "session.json"

    sainsburys.refresh_session(
        "alan@example.com",
        "hunter2",
        storage_state_path=storage_state_path,
        get_otp=lambda: "123456",
    )

    assert page.otp_field.filled == "123456"
    assert page.submit_code_button.clicked
    assert page.context.storage_state_calls == [storage_state_path]


def test_refresh_session_raises_when_mfa_is_required_and_no_otp_is_given(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    page = FakePage(otp_field=FakeLocator(visible=True))
    _wire(monkeypatch, page)

    with pytest.raises(sainsburys.NotLoggedInError, match="verification code"):
        sainsburys.refresh_session(
            "alan@example.com",
            "hunter2",
            storage_state_path=tmp_path / "session.json",
            get_otp=_no_otp,
        )

    assert not page.otp_field.filled
    assert page.context.storage_state_calls == []


def test_refresh_session_raises_when_still_not_logged_in_afterwards(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    page = FakePage(username_field=FakeLocator(visible=True))
    _wire(monkeypatch, page)
    shot = tmp_path / "failure.png"

    with pytest.raises(sainsburys.NotLoggedInError, match="check the password"):
        sainsburys.refresh_session(
            "alan@example.com",
            "wrong-password",
            storage_state_path=tmp_path / "session.json",
            get_otp=_no_otp,
            failure_screenshot_path=shot,
        )

    assert page.context.storage_state_calls == []
    assert page.screenshot_path == shot  # captured for debugging


def test_refresh_session_seeds_consent_cookies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    page = FakePage()
    cookies_seen: list[Any] = []
    _wire(monkeypatch, page, cookies_seen=cookies_seen)

    sainsburys.refresh_session(
        "alan@example.com",
        "hunter2",
        storage_state_path=tmp_path / "session.json",
        get_otp=_unreachable_otp,
    )

    assert any(c["name"] == "OptanonConsent" for c in cookies_seen[0])


def test_refresh_session_opens_a_headed_session_with_no_prior_storage_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    page = FakePage()
    storage_states: list[object] = []
    _wire(monkeypatch, page, storage_states=storage_states)

    sainsburys.refresh_session(
        "alan@example.com",
        "hunter2",
        storage_state_path=tmp_path / "session.json",
        get_otp=_unreachable_otp,
    )

    assert storage_states == [None]
