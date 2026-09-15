"""A fake site for core's own tests.

Imported by name rather than injected as a fixture, because most of these
tests need it at module scope to build a server. ``pythonpath`` in the root
``pyproject.toml`` is what puts this directory on the path.

Core is exercised against this and never against ``packages/sainsburys``. That
is the point of the split: a core whose tests need a real site is not the
site-agnostic core this package claims to be, and nothing else would catch the
difference. It is also a worked example of the smallest thing a site package
has to provide.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 - pydantic resolves field types at runtime
from typing import TYPE_CHECKING

from pydantic import Field, SecretStr

from browser_mcp_core.settings import CoreSettings
from browser_mcp_core.site import LoginCredentials, LoginPage, Site

if TYPE_CHECKING:
    from fastmcp import FastMCP

#: Deliberately not ``/sainsburys-login``: if core ever hardcodes a path again,
#: these tests are what notices.
FAKE_LOGIN_PATH = "/fake-login"
FAKE_DISPLAY_NAME = "Fake Grocer"
FAKE_SERVER_NAME = "browser-mcp-fake"
FAKE_WORKER_MODULE = "browser_mcp_core_fake_worker"


class FakeSettings(CoreSettings):
    """Core's settings plus the two a login page needs, as a site would add them."""

    fake_username: SecretStr | None = Field(default=None, min_length=1)
    fake_storage_state_path: Path | None = None


def fake_credentials(settings: FakeSettings) -> LoginCredentials | None:
    """Return the fake site's login credentials, or ``None`` if unconfigured."""
    if settings.fake_username is None or settings.fake_storage_state_path is None:
        return None
    return LoginCredentials(
        username=settings.fake_username,
        storage_state_path=settings.fake_storage_state_path,
    )


def register_fake_tools(mcp: FastMCP, settings: FakeSettings, version: str) -> None:
    """Register one trivial tool, standing in for a site's browser actions."""
    del settings, version

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
    def fake_action(text: str = "hello") -> str:
        """Echo `text` back."""
        return text


FAKE_SITE: Site[FakeSettings] = Site(
    slug="fake",
    display_name=FAKE_DISPLAY_NAME,
    # Core's own distribution, so the version lookup finds something installed.
    distribution="browser-mcp-core",
    server_name=FAKE_SERVER_NAME,
    register_tools=register_fake_tools,
    login_page=LoginPage(
        path=FAKE_LOGIN_PATH,
        worker_module=FAKE_WORKER_MODULE,
        credentials=fake_credentials,
    ),
)

#: The same site with no login page at all - a site that only reads public
#: pages is a legitimate shape, and core must not assume otherwise.
FAKE_SITE_WITHOUT_LOGIN: Site[FakeSettings] = Site(
    slug="fake",
    display_name=FAKE_DISPLAY_NAME,
    distribution="browser-mcp-core",
    server_name=FAKE_SERVER_NAME,
    register_tools=register_fake_tools,
)
