"""Every site's `locators.py` is declarations, and nothing else.

`heal_surface` lets a `claude/heal-*` branch change a site's `locators.py` and
nothing more. That only keeps a heal to re-addressing elements if the file
cannot hold behaviour: a function, a loop or an arbitrary call added there
would run in the server, and the file check alone would wave it through. A heal
branch cannot edit this test, so it holds on every heal.

Checked by walking the syntax tree against an allowlist, rather than for known
bad shapes, so something new is refused until it is deliberately allowed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import browser_mcp_core

_PACKAGES = Path(browser_mcp_core.__file__).parents[3]

_TABLES = sorted(
    table
    for table in _PACKAGES.glob("*/src/*/locators.py")
    if table.parts[-4] != "core"
)

_ALLOWED_IMPORTS = {
    "__future__",
    "browser_mcp_core.locator_table",
    "collections.abc",
    "re",
    "typing",
}

#: The only calls a table may make: its own row types, and compiling a pattern.
_ROW_TYPES = {"LocatorSpec", "ByRole", "ByTestId", "ByCss"}


def _is_import(node: ast.stmt) -> bool:
    """Return whether a statement imports only what a table needs, unrenamed.

    No aliases at all: `from ... import resolve as LocatorSpec` would otherwise
    pass a call to anything off as a row.
    """
    if not isinstance(node, ast.Import | ast.ImportFrom) or any(
        alias.asname for alias in node.names
    ):
        return False
    if isinstance(node, ast.Import):
        return all(alias.name in _ALLOWED_IMPORTS for alias in node.names)
    if node.module == "browser_mcp_core.locator_table":
        return all(alias.name in _ROW_TYPES for alias in node.names)
    return node.module in _ALLOWED_IMPORTS


def _is_re(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "re"


def _is_leaf(node: ast.expr | None) -> bool:
    """Return whether an expression is a literal or a flag such as `re.IGNORECASE`."""
    if isinstance(node, ast.Attribute):
        return _is_re(node.value) and node.attr.isupper()
    return isinstance(node, ast.Constant)


def _is_data(node: ast.expr | None) -> bool:
    """Return whether an expression is a literal, a row, or a compiled pattern."""
    match node:
        case ast.Dict(keys=keys, values=values):
            return all(map(_is_data, keys)) and all(map(_is_data, values))
        case ast.Tuple(elts=elts) | ast.List(elts=elts):
            return all(map(_is_data, elts))
        case ast.BinOp(left=left, op=ast.BitOr(), right=right):
            return _is_data(left) and _is_data(right)
        case ast.Call(func=func, args=args, keywords=keywords):
            is_row = isinstance(func, ast.Name) and func.id in _ROW_TYPES
            is_pattern = (
                isinstance(func, ast.Attribute)
                and _is_re(func.value)
                and func.attr == "compile"
            )
            return (
                (is_row or is_pattern)
                and all(map(_is_data, args))
                and all(_is_data(keyword.value) for keyword in keywords)
            )
    return _is_leaf(node)


def _is_allowed(node: ast.stmt) -> bool:
    match node:
        case ast.Expr(value=ast.Constant(value=str())):
            return True  # the docstring
        case ast.If(test=ast.Name(id="TYPE_CHECKING"), body=body, orelse=[]):
            return all(map(_is_import, body))
        case ast.Assign(value=value) | ast.AnnAssign(value=value):
            # An annotation is never evaluated (`from __future__ import
            # annotations`), so only the value is checked.
            return _is_data(value)
    return _is_import(node)


def _disallowed_lines(source: str) -> list[int]:
    return [node.lineno for node in ast.parse(source).body if not _is_allowed(node)]


def test_there_are_tables_to_check() -> None:
    """Guards the guard: a bad glob would make every check below vacuous."""
    assert _TABLES


@pytest.mark.parametrize(
    "source",
    [
        "def f() -> None: ...",
        "import os",
        "import re as os",
        "from browser_mcp_core.locator_table import resolve",
        "from browser_mcp_core.locator_table import resolve as LocatorSpec",
        "from re import sub as ByCss",
        "from browser_mcp_core import browser",
        "X = open('/etc/passwd')",
        "X = {'a': LocatorSpec(ByCss(__import__('os').name), 'd')}",
        "X = [i for i in range(3)]",
        "X = lambda: 1",
        "X = re.escape('a')",
        "X = os.SEP",
        "if TYPE_CHECKING:\n    X = 1",
        "if True:\n    import re",
        "for _ in []: pass",
    ],
)
def test_the_guard_refuses_behaviour(source: str) -> None:
    """Guards the guard again: each of these must be refused."""
    assert _disallowed_lines(source) == [1]


def test_the_guard_accepts_a_table() -> None:
    source = '''
"""A table."""
from __future__ import annotations
import re
from typing import TYPE_CHECKING
from browser_mcp_core.locator_table import ByRole, LocatorSpec
if TYPE_CHECKING:
    from collections.abc import Mapping
LOCATORS: Mapping[str, LocatorSpec] = {
    "a": LocatorSpec(ByRole("heading", name=re.compile("x", re.I | re.M)), "d"),
    "b": LocatorSpec(ByRole("heading"), "d", first=True),
}
'''
    assert _disallowed_lines(source) == []


@pytest.mark.parametrize("table", _TABLES, ids=lambda p: p.parts[-4])
def test_a_sites_locator_table_is_data_only(table: Path) -> None:
    lines = _disallowed_lines(table.read_text(encoding="utf-8"))

    assert not lines, (
        f"{table} has something other than declarations at line(s) {lines}. A "
        f"heal branch may change this file on its own, so it must not be able "
        f"to hold behaviour - see browser_mcp_core.heal_surface."
    )
