"""Tests for this site's login-worker entry point.

The mechanism is core's and is tested there; all this module does is name the
steps, so that is all there is to check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from browser_mcp_sainsburys import login_worker
from browser_mcp_sainsburys.app import SITE
from browser_mcp_sainsburys.site import SainsburysLoginSteps

if TYPE_CHECKING:
    import pytest


def test_main_runs_core_with_this_site_s_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[Any, Any]] = []

    def fake_run(steps: Any, argv: Any) -> int:
        seen.append((steps, argv))
        return 0

    monkeypatch.setattr(login_worker, "run", fake_run)

    assert login_worker.main(["--ipc-dir", "ignored-by-the-fake"]) == 0

    (steps, argv) = seen[0]
    assert isinstance(steps, SainsburysLoginSteps)
    assert argv == ["--ipc-dir", "ignored-by-the-fake"]


def test_the_worker_module_is_the_one_the_site_names() -> None:
    """The flow spawns this by name, so a rename here has to be caught.

    `python -m <module>` is a string in `app.py`, which nothing else would
    check until a real login failed on the Pi.
    """
    assert SITE.login_page is not None
    assert SITE.login_page.worker_module == login_worker.__name__
