"""Tests for the Sainsbury's tools, as the assembled server exposes them.

These are wiring tests: that a tool call reaches the right function in
`site.py` and wraps what it returns. `test_site.py` covers the page
interactions themselves.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 - used at runtime to build paths in tests
from typing import TYPE_CHECKING

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from browser_mcp_core.errors import NotLoggedInError
from browser_mcp_core.server import build_server
from browser_mcp_sainsburys import site
from browser_mcp_sainsburys.app import SITE
from browser_mcp_sainsburys.settings import SainsburysSettings

if TYPE_CHECKING:
    from tests_support import Authenticate


async def test_sainsburys_products_we_love_wires_to_the_browser_action(
    monkeypatch: pytest.MonkeyPatch,
    authenticate: Authenticate,
) -> None:
    """Only checks the wiring - test_site.py covers the scraping itself.

    Calling the tool should reach `site.products_we_love` and wrap
    whatever it returns.
    """
    authenticate()
    monkeypatch.setattr(
        site,
        "products_we_love",
        lambda: ["Chocolate Digestives 400g", "Semi Skimmed Milk 2L"],
    )

    async with Client(build_server(SITE, SainsburysSettings())) as client:
        result = await client.call_tool("sainsburys_products_we_love")

    assert result.data.products == [
        "Chocolate Digestives 400g",
        "Semi Skimmed Milk 2L",
    ]


async def test_sainsburys_search_wires_to_the_browser_action(
    monkeypatch: pytest.MonkeyPatch,
    authenticate: Authenticate,
    tmp_path: Path,
) -> None:
    """Only checks the wiring - test_site.py covers the search itself."""
    authenticate()
    storage_state_path = tmp_path / "session.json"
    storage_state_path.write_text("{}", encoding="utf-8")
    settings = SainsburysSettings(sainsburys_storage_state_path=storage_state_path)
    seen_calls: list[tuple[str, Path]] = []

    def fake_search_products(
        query: str, *, storage_state_path: Path
    ) -> list[site.ProductMatch]:
        seen_calls.append((query, storage_state_path))
        return [
            site.ProductMatch(
                name="Chocolate Digestives 400g",
                id="1234567",
                image_url="https://example.invalid/digestives.jpg",
            ),
            site.ProductMatch(
                name="Semi Skimmed Milk 2L", id="7654321", image_url=None
            ),
        ]

    monkeypatch.setattr(site, "search_products", fake_search_products)

    async with Client(build_server(SITE, settings)) as client:
        result = await client.call_tool("sainsburys_search", {"query": "chocolate"})

    assert [(r.name, r.id, r.image_url) for r in result.data.results] == [
        (
            "Chocolate Digestives 400g",
            "1234567",
            "https://example.invalid/digestives.jpg",
        ),
        ("Semi Skimmed Milk 2L", "7654321", None),
    ]
    assert seen_calls == [("chocolate", storage_state_path)]


async def test_sainsburys_search_refuses_without_a_saved_session(
    authenticate: Authenticate,
) -> None:
    authenticate()
    settings = SainsburysSettings(
        sainsburys_storage_state_path=None, include_error_details=True
    )

    async with Client(build_server(SITE, settings)) as client:
        with pytest.raises(ToolError, match=r"sainsburys_login\.py"):
            await client.call_tool("sainsburys_search")


async def test_sainsburys_search_reports_a_stale_session_through_error_masking(
    monkeypatch: pytest.MonkeyPatch,
    authenticate: Authenticate,
    tmp_path: Path,
) -> None:
    """A stale session's message reaches the caller even with masking on."""
    authenticate()
    storage_state_path = tmp_path / "session.json"
    storage_state_path.write_text("{}", encoding="utf-8")
    settings = SainsburysSettings(sainsburys_storage_state_path=storage_state_path)

    def fake_search_products(
        query: str, *, storage_state_path: Path
    ) -> list[site.ProductMatch]:
        del query, storage_state_path
        msg = "Session expired - re-authenticate at /sainsburys-login."
        raise NotLoggedInError(msg)

    monkeypatch.setattr(site, "search_products", fake_search_products)

    async with Client(build_server(SITE, settings)) as client:
        with pytest.raises(ToolError, match="/sainsburys-login"):
            await client.call_tool("sainsburys_search", {"query": "milk"})


async def test_sainsburys_add_to_basket_wires_to_the_browser_action(
    monkeypatch: pytest.MonkeyPatch,
    authenticate: Authenticate,
    tmp_path: Path,
) -> None:
    """Only checks the wiring - test_site.py covers the action itself."""
    authenticate()
    storage_state_path = tmp_path / "session.json"
    storage_state_path.write_text("{}", encoding="utf-8")
    settings = SainsburysSettings(sainsburys_storage_state_path=storage_state_path)
    seen_calls: list[tuple[str, str | None, Path]] = []

    def fake_add_to_basket(
        product_name: str,
        *,
        storage_state_path: Path,
        product_id: str | None = None,
    ) -> str:
        seen_calls.append((product_name, product_id, storage_state_path))
        return "Chocolate Digestives 400g"

    monkeypatch.setattr(site, "add_to_basket", fake_add_to_basket)

    async with Client(build_server(SITE, settings)) as client:
        result = await client.call_tool(
            "sainsburys_add_to_basket", {"product_name": "Chocolate Digestives 400g"}
        )

    assert result.data.product == "Chocolate Digestives 400g"
    assert seen_calls == [("Chocolate Digestives 400g", None, storage_state_path)]


async def test_sainsburys_add_to_basket_passes_product_id_through(
    monkeypatch: pytest.MonkeyPatch,
    authenticate: Authenticate,
    tmp_path: Path,
) -> None:
    """An optional `product_id` reaches `site.add_to_basket` untouched."""
    authenticate()
    storage_state_path = tmp_path / "session.json"
    storage_state_path.write_text("{}", encoding="utf-8")
    settings = SainsburysSettings(sainsburys_storage_state_path=storage_state_path)
    seen_calls: list[tuple[str, str | None, Path]] = []

    def fake_add_to_basket(
        product_name: str,
        *,
        storage_state_path: Path,
        product_id: str | None = None,
    ) -> str:
        seen_calls.append((product_name, product_id, storage_state_path))
        return "Chocolate Digestives 400g"

    monkeypatch.setattr(site, "add_to_basket", fake_add_to_basket)

    async with Client(build_server(SITE, settings)) as client:
        await client.call_tool(
            "sainsburys_add_to_basket",
            {"product_name": "Chocolate Digestives", "product_id": "1234567"},
        )

    assert seen_calls == [("Chocolate Digestives", "1234567", storage_state_path)]


async def test_sainsburys_add_to_basket_refuses_without_a_saved_session(
    authenticate: Authenticate,
) -> None:
    authenticate()
    settings = SainsburysSettings(
        sainsburys_storage_state_path=None, include_error_details=True
    )

    async with Client(build_server(SITE, settings)) as client:
        with pytest.raises(ToolError, match=r"sainsburys_login\.py"):
            await client.call_tool(
                "sainsburys_add_to_basket", {"product_name": "Anything"}
            )


async def test_sainsburys_add_to_basket_refuses_when_the_session_file_is_missing(
    authenticate: Authenticate,
    tmp_path: Path,
) -> None:
    """A configured-but-absent path is a friendly error, not a raw FileNotFoundError."""
    authenticate()
    settings = SainsburysSettings(
        sainsburys_storage_state_path=tmp_path / "gone.json",
        include_error_details=True,
    )

    async with Client(build_server(SITE, settings)) as client:
        with pytest.raises(ToolError, match="No saved Sainsbury's session"):
            await client.call_tool(
                "sainsburys_add_to_basket", {"product_name": "Anything"}
            )
