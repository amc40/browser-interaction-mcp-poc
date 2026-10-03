"""Tests that `site.py` and `locators.py` agree on which elements exist."""

from __future__ import annotations

import ast
import inspect

import pytest

from browser_mcp_sainsburys import site
from browser_mcp_sainsburys.locators import LOCATORS


def _ids_used_by_site() -> set[str]:
    """Every locator id `site.py` passes to `_locate`, read from its source."""
    ids: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(site))):
        match node:
            case ast.Call(
                func=ast.Name(id="_locate"), args=[_, ast.Constant(value=str(id_))]
            ):
                ids.add(id_)
    return ids


def test_every_id_the_site_uses_has_a_row() -> None:
    assert _ids_used_by_site() <= LOCATORS.keys()


def test_every_row_is_used() -> None:
    """A row nothing reads would be healed, reviewed and replayed for nothing."""
    assert LOCATORS.keys() <= _ids_used_by_site()


def test_the_site_never_builds_a_locator_inline() -> None:
    source = inspect.getsource(site)

    for call in ("get_by_role(", "get_by_test_id(", ".locator(", "get_by_text("):
        assert call not in source, f"site.py calls {call} - add a row instead"


@pytest.mark.parametrize("locator_id", sorted(LOCATORS))
def test_locate_resolves_the_row_by_its_id(
    locator_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, object]] = []

    def fake_resolve(scope: object, spec: object) -> str:
        calls.append((scope, spec))
        return "resolved"

    monkeypatch.setattr(site, "resolve", fake_resolve)
    scope = object()

    result: object = site._locate(scope, locator_id)  # type: ignore[arg-type]

    assert result == "resolved"
    assert calls == [(scope, LOCATORS[locator_id])]
