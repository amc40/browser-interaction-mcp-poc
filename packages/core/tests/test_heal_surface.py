"""Tests for the `claude/heal-*` diff-surface check.

The end-to-end cases run the check against a real, throwaway git repository:
what matters is what `git diff` reports for a branch, including renames, and a
fake would only restate what this module assumes about that.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from browser_mcp_core import heal_surface

if TYPE_CHECKING:
    from pathlib import Path

_TABLE = "packages/shop/src/browser_mcp_shop/locators.py"
_FIXTURE = "packages/shop/tests/fixtures/search/results.html"


@pytest.mark.parametrize(
    "paths",
    [
        [],
        [_TABLE],
        [_FIXTURE],
        [_TABLE, _FIXTURE, "packages/shop/tests/fixtures/login.html"],
    ],
)
def test_a_change_on_one_sites_surface_passes(paths: list[str]) -> None:
    assert heal_surface.surface_violations(paths) == []


@pytest.mark.parametrize(
    "path",
    [
        "packages/shop/src/browser_mcp_shop/site.py",
        "packages/shop/tests/test_site.py",
        "packages/shop/tests/fixtures",
        "packages/shop/locators.py",
        "packages/shop/src/browser_mcp_shop/nested/locators.py",
        # Core is shared by every site: never healable, even where it matches
        # the shape of a site's surface.
        "packages/core/src/browser_mcp_core/locators.py",
        "packages/core/tests/fixtures/page.html",
        ".github/workflows/ci.yml",
        "uv.lock",
    ],
)
def test_anything_else_is_outside_the_surface(path: str) -> None:
    assert heal_surface.surface_violations([_TABLE, path]) == [
        f"{path} is outside the heal surface"
    ]


def test_a_change_spanning_two_sites_fails() -> None:
    other = "packages/other/src/browser_mcp_other/locators.py"

    assert heal_surface.surface_violations([_TABLE, other]) == [
        "a heal branch changes one site, and this changes 2: other, shop"
    ]


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        ("claude/heal-shop-search-tile-timeout", True),
        ("claude/scaling-plan", False),
        ("heal/shop", False),
        ("main", False),
    ],
)
def test_only_heal_branches_are_checked(branch: str, *, expected: bool) -> None:
    assert heal_surface.is_heal_branch(branch) is expected


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, test-only
        [  # noqa: S607 - whichever git the test runner has
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> None:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "--message", message)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository with one site, on a fresh heal branch off ``main``."""
    _git(tmp_path, "init", "--quiet", "--initial-branch=main")
    _commit(
        tmp_path,
        {_TABLE: "LOCATORS = {}\n", "packages/shop/src/browser_mcp_shop/site.py": ""},
        "A site",
    )
    _git(tmp_path, "switch", "--quiet", "--create", "claude/heal-shop-tile")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _check(branch: str = "claude/heal-shop-tile") -> int:
    return heal_surface.main(["--branch", branch, "--base", "main"])


def test_accepts_a_heal_that_only_changes_the_table_and_fixtures(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(repo, {_TABLE: "LOCATORS = {'a': 1}\n", _FIXTURE: "<html>"}, "Heal")

    assert _check() == 0
    assert "stays within the heal surface" in capsys.readouterr().out


def test_rejects_a_heal_that_also_changes_site_code(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The stage's "done when": a deliberately out-of-surface commit fails."""
    _commit(repo, {_TABLE: "LOCATORS = {'a': 1}\n"}, "Heal")
    _commit(repo, {"packages/shop/src/browser_mcp_shop/site.py": "TIMEOUT = 1\n"}, "x")

    assert _check() == 1
    err = capsys.readouterr().err
    assert "packages/shop/src/browser_mcp_shop/site.py is outside" in err
    assert _TABLE not in err


def test_counts_both_sides_of_a_rename(repo: Path) -> None:
    """Moving a file *onto* the surface still removes it from where it was."""
    (repo / "packages/shop/tests/fixtures").mkdir(parents=True)
    _git(
        repo,
        "mv",
        "packages/shop/src/browser_mcp_shop/site.py",
        "packages/shop/tests/fixtures/site.py",
    )
    _git(repo, "commit", "--quiet", "--message", "Move")

    assert heal_surface.changed_paths("main") == [
        "packages/shop/src/browser_mcp_shop/site.py",
        "packages/shop/tests/fixtures/site.py",
    ]
    assert _check() == 1


def test_ignores_what_landed_on_the_base_branch_meanwhile(repo: Path) -> None:
    _commit(repo, {_TABLE: "LOCATORS = {'a': 1}\n"}, "Heal")
    _git(repo, "switch", "--quiet", "main")
    _commit(repo, {"README.md": "news\n"}, "Unrelated work on main")
    _git(repo, "switch", "--quiet", "claude/heal-shop-tile")

    assert _check() == 0


def test_passes_any_other_branch_without_looking(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(repo, {"packages/shop/src/browser_mcp_shop/site.py": "x = 1\n"}, "Work")

    assert _check("claude/scaling-plan") == 0
    assert "nothing to check" in capsys.readouterr().out


def test_needs_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("browser_mcp_core.heal_surface.shutil.which", lambda _: None)

    with pytest.raises(RuntimeError, match="git is not on the PATH"):
        heal_surface.changed_paths("main")
