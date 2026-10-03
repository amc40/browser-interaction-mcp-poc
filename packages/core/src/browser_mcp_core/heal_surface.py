"""CI's diff-surface check for self-healing branches.

A ``claude/heal-*`` branch exists to change how a site addresses its elements,
and nothing else - see docs/self-healing-plan.md stage 0. That is what makes a
heal reviewable from a phone: the diff is a locator-table row and the page
snapshot that justifies it. This enforces it, so the claim holds whoever (or
whatever) wrote the branch rather than relying on a reviewer to notice.

A heal branch may touch, for **one** site only:

- ``packages/<slug>/src/<module>/locators.py`` - the site's locator table;
- ``packages/<slug>/tests/fixtures/`` - the snapshots it is replayed against.

Anything else - site code, tests, core, CI, the lockfile - fails the check,
as does a branch spanning two sites. Core is never a site: its code is shared
by every app in the fleet, which is exactly what a heal must not reach.

What the table itself may contain is a separate check (a test, so it holds on
every branch): a site's ``locators.py`` must be declarations only, or editing
"only the table" could still add behaviour.

Run by CI on every pull request; anything not on a heal branch passes at once,
so the check can be required without special-casing ordinary work::

    python -m browser_mcp_core.heal_surface --branch "$HEAD_REF" --base "$BASE_SHA"
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

HEAL_BRANCH_PREFIX = "claude/heal-"

_CORE_SLUG = "core"

_SURFACE = (
    re.compile(r"packages/(?P<slug>[^/]+)/src/[^/]+/locators\.py"),
    re.compile(r"packages/(?P<slug>[^/]+)/tests/fixtures/.+"),
)


def is_heal_branch(branch: str) -> bool:
    """Return whether ``branch`` is one this check applies to."""
    return branch.startswith(HEAL_BRANCH_PREFIX)


def _site_of(path: str) -> str | None:
    """Return the site a path is on the heal surface of, or ``None``."""
    for pattern in _SURFACE:
        match = pattern.fullmatch(path)
        if match is not None and match["slug"] != _CORE_SLUG:
            return match["slug"]
    return None


def surface_violations(paths: Iterable[str]) -> list[str]:
    """Explain every way a heal branch's changed paths leave the surface.

    Args:
        paths: Every path the branch changes, relative to the repository root.
            A rename must appear as both its old and new path, or a file could
            be moved off the surface unnoticed.

    Returns:
        One line per problem, empty if the change is within the surface.
    """
    problems: list[str] = []
    sites: set[str] = set()
    for path in paths:
        site = _site_of(path)
        if site is None:
            problems.append(f"{path} is outside the heal surface")
        else:
            sites.add(site)
    if len(sites) > 1:
        problems.append(
            f"a heal branch changes one site, and this changes {len(sites)}: "
            f"{', '.join(sorted(sites))}"
        )
    return problems


def changed_paths(base: str, head: str = "HEAD") -> list[str]:
    """Return every path changed between ``base`` and ``head``.

    Compared from their merge base, as a pull request's diff is, so commits
    that landed on the base branch meanwhile are not counted against the
    branch. Renames are split into a deletion and an addition.

    Args:
        base: The commit the branch is compared against.
        head: The branch's tip.

    Returns:
        The changed paths, relative to the repository root.

    Raises:
        RuntimeError: If ``git`` is not on the ``PATH``.
    """
    git = shutil.which("git")
    if git is None:
        msg = "git is not on the PATH"
        raise RuntimeError(msg)
    diff = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [git, "diff", "--name-only", "--no-renames", "-z", f"{base}...{head}"],
        capture_output=True,
        check=True,
        text=True,
    )
    return [path for path in diff.stdout.split("\0") if path]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the check, printing any violations.

    Args:
        argv: Command-line arguments, without the program name.

    Returns:
        The exit status: ``0`` if the branch is not a heal branch or stays on
        the surface, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--branch", required=True, help="the pull request's branch")
    parser.add_argument("--base", required=True, help="the commit to compare with")
    parser.add_argument("--head", default="HEAD", help="the branch's tip")
    args = parser.parse_args(argv)

    if not is_heal_branch(args.branch):
        sys.stdout.write(
            f"{args.branch} is not a {HEAL_BRANCH_PREFIX}* branch: nothing to check\n"
        )
        return 0

    problems = surface_violations(changed_paths(args.base, args.head))
    if not problems:
        sys.stdout.write(f"{args.branch} stays within the heal surface\n")
        return 0
    sys.stderr.write(f"{args.branch} changes more than a heal may:\n")
    sys.stderr.writelines(f"  - {problem}\n" for problem in problems)
    return 1


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    sys.exit(main())
