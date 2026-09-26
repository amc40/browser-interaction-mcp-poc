"""Subprocess that performs one real Sainsbury's login.

All of the mechanism - stdin credentials, the status file, the OTP poll, the
generic failure handling - is :mod:`browser_mcp_core.login_worker`. This module
exists because the worker has to be a *separate process* (so a wedged Chromium
can be killed outright), and a separate process needs something importable to
start from. It is named by this site's
:class:`~browser_mcp_core.site.LoginPage`.
"""

from __future__ import annotations

from browser_mcp_core.login_worker import run
from browser_mcp_sainsburys.site import SainsburysLoginSteps


def main(argv: list[str] | None = None) -> int:
    """Run one login attempt end to end; return 0 on success.

    Args:
        argv: Command line to parse. Read from ``sys.argv`` when omitted.

    Returns:
        0 if the session was captured, 1 otherwise.
    """
    return run(SainsburysLoginSteps(), argv)


if __name__ == "__main__":  # pragma: no cover - exercised by running it as a subprocess
    raise SystemExit(main())
