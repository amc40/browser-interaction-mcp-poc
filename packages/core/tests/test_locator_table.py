"""Tests for the locator table's row types and `resolve`."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import cast

import pytest

from browser_mcp_core.locator_table import ByCss, ByRole, ByTestId, LocatorSpec, resolve


@dataclass
class _Recorder:
    """A page or locator that records how it was asked to find something."""

    calls: list[tuple[object, ...]] = field(default_factory=list)
    narrowed: bool = False

    def get_by_role(self, role: str, *, name: object = None) -> _Recorder:
        self.calls.append(("role", role, name))
        return self

    def get_by_test_id(self, test_id: str) -> _Recorder:
        self.calls.append(("test_id", test_id))
        return self

    def locator(self, selector: str) -> _Recorder:
        self.calls.append(("css", selector))
        return self

    @property
    def first(self) -> _Recorder:
        return _Recorder(calls=self.calls, narrowed=True)


def _resolve(spec: LocatorSpec) -> _Recorder:
    return cast("_Recorder", resolve(_Recorder(), spec))  # type: ignore[arg-type]


def test_resolves_a_role_with_its_accessible_name() -> None:
    name = re.compile("^Search", re.IGNORECASE)

    result = _resolve(LocatorSpec(ByRole("combobox", name=name), "search"))

    assert result.calls == [("role", "combobox", name)]


def test_resolves_a_role_with_no_name() -> None:
    assert _resolve(LocatorSpec(ByRole("heading"), "any heading")).calls == [
        ("role", "heading", None)
    ]


def test_resolves_a_test_id() -> None:
    assert _resolve(LocatorSpec(ByTestId("username"), "user")).calls == [
        ("test_id", "username")
    ]


def test_resolves_a_css_selector() -> None:
    assert _resolve(LocatorSpec(ByCss("img"), "image")).calls == [("css", "img")]


def test_narrows_to_the_first_match_only_when_asked() -> None:
    assert _resolve(LocatorSpec(ByCss("img"), "image", first=True)).narrowed
    assert not _resolve(LocatorSpec(ByCss("img"), "image")).narrowed


@pytest.mark.parametrize("description", ["", "   "])
def test_refuses_a_row_without_a_description(description: str) -> None:
    with pytest.raises(ValueError, match="needs a description"):
        LocatorSpec(ByTestId("username"), description)
