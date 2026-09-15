"""The settings this site needs on top of the ones every app has."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 - pydantic resolves field types at runtime

from pydantic import Field, SecretStr

from browser_mcp_core.settings import CoreSettings


class SainsburysSettings(CoreSettings):
    """Core's settings, plus the two this site adds.

    Both keep the shared ``BROWSER_MCP_`` prefix, so a deployment that was
    configured before the workspace split reads exactly the same environment
    file afterwards.
    """

    sainsburys_storage_state_path: Path | None = Field(
        default=None,
        description="Path to a Playwright storage_state JSON file holding a "
        "logged-in Sainsbury's session - cookies and local storage, not a "
        "password. Written by scripts/sainsburys_login.py (run locally) or "
        "the login page (run remotely, e.g. on a host the operator doesn't "
        "have routine shell access to); actions that need to be logged in "
        "refuse to run without it. Never commit this file - treat it like the "
        "credentials it stands in for.",
    )
    sainsburys_username: SecretStr | None = Field(
        default=None,
        min_length=1,
        description="Sainsbury's account email/username. Required by the login "
        "page, which types it into the real login form; not required for "
        "anything that only reads sainsburys_storage_state_path. A SecretStr - "
        "not because a username grants access on its own, but so redaction.py "
        "covers it automatically like every other credential. Personal enough "
        "that it shouldn't go in a public git history even encrypted - see "
        "deploy/inventory/group_vars/browser_mcp/local.yml.example for how the "
        "deployment keeps it out.",
    )
