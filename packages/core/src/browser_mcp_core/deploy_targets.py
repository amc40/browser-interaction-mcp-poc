"""Which apps a push to ``main`` has to redeploy.

With one repository holding every site (docs/scaling-plan.md D1), a push is
not a deploy of everything. A change to one site's package redeploys that
site's app; a change to anything every app runs - core, the lockfile, the
workspace's own ``pyproject.toml``, the deploy script itself - redeploys all of
them; anything else - docs, tests, CI, the fleet's Ansible - redeploys
nothing, because none of it reaches a running app without a playbook run.

CI's deploy job runs this after every push to ``main`` that passed, and sends
one signed webhook per app it names. Stdlib only, and run by file path with the
runner's own ``python3``, like :mod:`browser_mcp_core.heal_surface`. The app
list is ``fleet/apps.yml``'s, each app mapped to its package, as JSON - the
standard library reads no YAML, so CI converts it with ``yq``::

    python3 deploy_targets.py --apps "$APPS_JSON" --base "$BEFORE" --head "$SHA"

It prints the apps as a JSON list, the shape a job matrix takes.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

# Paths whose change reaches every app.
_EVERY_APP = (
    re.compile(r"packages/core/src/.+"),
    re.compile(r"packages/core/pyproject\.toml"),
    re.compile(r"pyproject\.toml"),
    re.compile(r"uv\.lock"),
    re.compile(r"fleet/deploy\.sh"),
)

# What GitHub sends as the "before" commit of a push that created the branch.
_NO_COMMIT = re.compile(r"0+")


def affected_apps(paths: Iterable[str], apps: Mapping[str, str]) -> list[str]:
    """Return the apps a change to ``paths`` has to redeploy.

    Args:
        paths: Every path the push changed, relative to the repository root. A
            rename must appear as both its old and new path.
        apps: Each app's name, mapped to the package under ``packages/`` it
            runs - ``fleet/apps.yml``'s ``package`` key.

    Returns:
        The app names, sorted.
    """
    affected: set[str] = set()
    for path in paths:
        if any(pattern.fullmatch(path) for pattern in _EVERY_APP):
            return sorted(apps)
        affected.update(
            app
            for app, package in apps.items()
            if path.startswith(f"packages/{package}/src/")
            or path == f"packages/{package}/pyproject.toml"
        )
    return sorted(affected)


def changed_paths(base: str, head: str = "HEAD") -> list[str] | None:
    """Return every path changed between ``base`` and ``head``.

    Renames are split into a deletion and an addition, so moving a file out of
    a package still counts against that package.

    Args:
        base: The commit ``main`` pointed at before the push.
        head: The commit it points at now.

    Returns:
        The changed paths, relative to the repository root - or ``None`` when
        ``base`` is no commit this clone has: the push that created the branch,
        or a force-push that rewrote it away. The caller cannot tell what
        changed, so it should redeploy everything.

    Raises:
        RuntimeError: If ``git`` is not on the ``PATH``.
    """
    git = shutil.which("git")
    if git is None:
        msg = "git is not on the PATH"
        raise RuntimeError(msg)
    if _NO_COMMIT.fullmatch(base):
        return None
    known = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [git, "cat-file", "-e", f"{base}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    if known.returncode != 0:
        return None
    diff = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [git, "diff", "--name-only", "--no-renames", "-z", base, head],
        capture_output=True,
        check=True,
        text=True,
    )
    return [path for path in diff.stdout.split("\0") if path]


def _parse_apps(raw: str) -> dict[str, str] | None:
    """Return ``--apps`` as a mapping of app to package, or ``None``."""
    try:
        apps = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(apps, dict):
        return None
    if not all(isinstance(v, str) for v in apps.values()):
        return None
    return {str(app): package for app, package in apps.items()}


def main(argv: Sequence[str] | None = None) -> int:
    """Print the apps to redeploy, as a JSON list.

    Args:
        argv: Command-line arguments, without the program name.

    Returns:
        The exit status, ``0``. A malformed ``--apps`` exits with ``2``, as
        any other usage error does.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apps", required=True, help="each app's package, as a JSON object"
    )
    parser.add_argument("--base", required=True, help="main before the push")
    parser.add_argument("--head", default="HEAD", help="main after the push")
    args = parser.parse_args(argv)

    apps = _parse_apps(args.apps)
    if apps is None:
        parser.error("--apps must be a JSON object mapping each app to its package")

    paths = changed_paths(args.base, args.head)
    targets = sorted(apps) if paths is None else affected_apps(paths, apps)
    sys.stdout.write(json.dumps(targets) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    sys.exit(main())
