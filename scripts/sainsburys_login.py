#!/usr/bin/env python3
"""Capture an authenticated Sainsbury's session for later, logged-in actions.

For `browser_interaction_mcp.sainsburys.add_to_basket` and anything else that
needs to act as a signed-in Sainsbury's account. Thin CLI wrapper around
`sainsburys.refresh_session` - the same function the `sainsburys_refresh_session`
MCP tool calls, for when there's no way to run a script like this one at all
(e.g. no routine shell access to wherever the server is deployed). See that
function's docstring, and `sainsburys.py`'s module docstring, for what it
means that this project's one login flow handles a password.

**Run this locally, by hand, with your own Sainsbury's credentials - never
inside the MCP server, never in an automated environment, and never in a
session that isn't yours.** The password is read with `getpass` (never
echoed, never in shell history), and typed by this script into a real,
visible Chromium window (headed, because Sainsbury's blocks headless Chromium
outright - see `browser.py` and `sainsburys.py`'s docstrings). If Sainsbury's
asks for a verification code - it doesn't always, seemingly depending on
whether the device/network is already trusted - you're prompted for that too.
Neither value is written anywhere by this script; only the resulting session
is, as Playwright's `storage_state` - cookies and local storage, not
credentials - to a JSON file.

That file is the entire output. It contains session tokens, not a password,
but a session token is enough to act as you until it expires or is revoked -
treat it exactly as sensitively as the login itself:

- Never commit it. It matches no repository path that ships, but keep it
  outside the repository entirely to be sure.
- Set restrictive file permissions on it (`refresh_session` does so on POSIX).
- Revoke it by signing out of that session on Sainsbury's, or by simply
  deleting the file and rerunning this script when it has expired.

Usage:
    uv run scripts/sainsburys_login.py [output-path]

`output-path` defaults to `sainsburys_storage_state.json` in the current
directory. Point `BROWSER_MCP_SAINSBURYS_STORAGE_STATE_PATH` at wherever you
save it.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

from browser_interaction_mcp.sainsburys import refresh_session

DEFAULT_OUTPUT = Path("sainsburys_storage_state.json")


def _prompt_for_otp() -> str | None:
    """Ask the operator for a verification code, if Sainsbury's wants one."""
    code = input(
        "Sainsbury's is asking for a verification code - check your email or "
        "phone and enter it (blank to abandon): "
    ).strip()
    return code or None


def main(argv: list[str]) -> int:
    output = Path(argv[0]) if argv else DEFAULT_OUTPUT

    username = input("Sainsbury's account email/username: ").strip()
    password = getpass.getpass("Sainsbury's account password (not echoed): ")

    try:
        refresh_session(
            username,
            password,
            storage_state_path=output,
            get_otp=_prompt_for_otp,
        )
    except Exception as exc:  # noqa: BLE001 - top-level script, report and exit
        print(f"Failed: {exc}", file=sys.stderr)
        return 1

    print(f"Session captured to {output}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
