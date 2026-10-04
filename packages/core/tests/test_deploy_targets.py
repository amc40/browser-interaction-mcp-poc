"""Tests for working out which apps a push to ``main`` redeploys.

The end-to-end cases run against a real, throwaway git repository, as the
heal-surface tests do: what matters is what ``git diff`` reports for a push.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from browser_mcp_core import deploy_targets

if TYPE_CHECKING:
    from pathlib import Path

_APPS = {"shop": "shop", "grocer": "grocer"}


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        ([], []),
        (["packages/shop/src/browser_mcp_shop/site.py"], ["shop"]),
        (["packages/shop/src/browser_mcp_shop/locators.py"], ["shop"]),
        (["packages/shop/pyproject.toml"], ["shop"]),
        (
            [
                "packages/shop/src/browser_mcp_shop/site.py",
                "packages/grocer/src/browser_mcp_grocer/tools.py",
            ],
            ["grocer", "shop"],
        ),
        # Nothing a running app executes.
        (["packages/shop/tests/test_site.py"], []),
        (["packages/shop/tests/fixtures/search.html"], []),
        (["docs/scaling-plan.md", "README.md"], []),
        (["fleet/apps.yml", "fleet/roles/app/tasks/prepare.yml"], []),
        ([".github/workflows/ci.yml"], []),
        (["packages/core/tests/test_server.py"], []),
        # A package whose name only starts like an app's is not that app's.
        (["packages/shopfront/src/browser_mcp_shopfront/site.py"], []),
    ],
)
def test_a_site_change_redeploys_only_that_site(
    paths: list[str], expected: list[str]
) -> None:
    assert deploy_targets.affected_apps(paths, _APPS) == expected


@pytest.mark.parametrize(
    "path",
    [
        "packages/core/src/browser_mcp_core/server.py",
        "packages/core/src/browser_mcp_core/deploy_webhook.py",
        "packages/core/pyproject.toml",
        "pyproject.toml",
        "uv.lock",
        "fleet/deploy.sh",
    ],
)
def test_a_shared_change_redeploys_every_app(path: str) -> None:
    assert deploy_targets.affected_apps(["README.md", path], _APPS) == [
        "grocer",
        "shop",
    ]


def test_an_app_is_named_by_its_package() -> None:
    assert deploy_targets.affected_apps(
        ["packages/shop/src/browser_mcp_shop/site.py"], {"shop-uk": "shop"}
    ) == ["shop-uk"]


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


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "--message", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository with two sites and core, on ``main``."""
    _git(tmp_path, "init", "--quiet", "--initial-branch=main")
    _commit(
        tmp_path,
        {
            "packages/core/src/browser_mcp_core/server.py": "",
            "packages/shop/src/browser_mcp_shop/site.py": "",
            "packages/grocer/src/browser_mcp_grocer/site.py": "",
        },
        "Two sites",
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _targets(base: str, capsys: pytest.CaptureFixture[str]) -> list[str]:
    status = deploy_targets.main(["--apps", json.dumps(_APPS), "--base", base])
    assert status == 0
    targets = json.loads(capsys.readouterr().out)
    assert isinstance(targets, list)
    return targets


def test_redeploys_the_site_a_push_changed(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    _commit(repo, {"packages/shop/src/browser_mcp_shop/site.py": "x = 1\n"}, "Shop")
    _commit(repo, {"docs/notes.md": "notes\n"}, "Docs")

    assert _targets(before, capsys) == ["shop"]


def test_redeploys_every_app_for_a_core_change(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    _commit(repo, {"packages/core/src/browser_mcp_core/server.py": "x = 1\n"}, "Core")

    assert _targets(before, capsys) == ["grocer", "shop"]


def test_redeploys_nothing_for_a_docs_change(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    _commit(repo, {"README.md": "hello\n"}, "Docs")

    assert _targets(before, capsys) == []


def test_counts_a_file_moved_out_of_a_site(repo: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    (repo / "docs").mkdir()
    _git(repo, "mv", "packages/grocer/src/browser_mcp_grocer/site.py", "docs/site.py")
    _git(repo, "commit", "--quiet", "--message", "Move")

    assert deploy_targets.changed_paths(before) == [
        "docs/site.py",
        "packages/grocer/src/browser_mcp_grocer/site.py",
    ]


@pytest.mark.parametrize(
    "base",
    [
        # The push that created the branch.
        "0000000000000000000000000000000000000000",
        # A commit a force-push rewrote away, which the clone never fetched.
        "1234567890abcdef1234567890abcdef12345678",
    ],
)
def test_redeploys_everything_when_it_cannot_tell(
    base: str, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(repo, {"README.md": "hello\n"}, "Docs")

    assert deploy_targets.changed_paths(base) is None
    assert _targets(base, capsys) == ["grocer", "shop"]


@pytest.mark.parametrize("apps", ["not json", '["shop"]', '{"shop": 1}', '"shop"'])
def test_refuses_an_app_list_that_is_not_a_mapping(
    apps: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        deploy_targets.main(["--apps", apps, "--base", "main"])

    assert exited.value.code == 2
    assert "--apps must be a JSON object" in capsys.readouterr().err


def test_needs_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("browser_mcp_core.deploy_targets.shutil.which", lambda _: None)

    with pytest.raises(RuntimeError, match="git is not on the PATH"):
        deploy_targets.changed_paths("main")
