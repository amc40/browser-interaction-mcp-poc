"""The guard behind stage 1's "done when" criterion.

docs/scaling-plan.md stage 1 finishes when `packages/sainsburys/` imports
nothing from `packages/core/` that mentions a grocer. That is a property of the
source, it is easy to break by accident in a hurry, and a reviewer reading a
diff is otherwise the only thing that would catch it - so it is asserted here
instead.

**Code only: comments and docstrings are stripped first.** Naming the site that
taught core a lesson is how the reasoning stays legible - "not every site asks
for an MFA code every time; Sainsbury's does not" is the sentence that explains
why `otp_requested` is a question rather than an assumption. What must not
happen is core *behaving* differently for one site, and that is what is checked.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import browser_mcp_core

#: Names that must not appear in core's code. Not an exhaustive list of every
#: grocer - it is the site this repository actually has, which is the one that
#: could leak back in during a refactor.
_SITE_WORDS = ("sainsbury", "browser_mcp_sainsburys")

_CORE_SOURCES = sorted(Path(browser_mcp_core.__file__).parent.glob("*.py"))

_Scoped = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _code_only(source: Path) -> str:
    """Return the module's source with its comments and docstrings removed.

    Args:
        source: The module to read.

    Returns:
        The module unparsed from its AST, which drops comments, with every
        docstring dropped too.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, _Scoped) and node.body and _is_docstring(node.body[0]):
            node.body.pop(0)
            # A Protocol method whose whole body was its docstring still needs
            # one statement to be valid Python.
            if not node.body:
                node.body.append(ast.Pass())
    return ast.unparse(tree)


def test_there_are_core_sources_to_check() -> None:
    """Guards the guard: a bad glob would make every check below vacuous."""
    assert len(_CORE_SOURCES) > 10


def test_the_guard_would_notice_a_site_name() -> None:
    """Guards the guard again: docstring-stripping must not strip everything."""
    assert "sainsbury" in _code_only(Path(__file__).parent / "fixtures_site_name.py")


@pytest.mark.parametrize("source", _CORE_SOURCES, ids=lambda p: p.name)
def test_core_never_names_a_site(source: Path) -> None:
    code = _code_only(source).lower()

    for word in _SITE_WORDS:
        assert word not in code, (
            f"{source.name} names '{word}' in code. Core is shared by every app "
            f"in the fleet, so anything site-specific belongs in that site's "
            f"package - see docs/scaling-plan.md D2."
        )
