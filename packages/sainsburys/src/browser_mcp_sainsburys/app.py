"""This site, as core sees it: the one object that wires the two together."""

from __future__ import annotations

from browser_mcp_core.site import LoginCredentials, LoginPage, Site

# Not in a TYPE_CHECKING block: SITE subscripts Site[] with it at runtime.
from browser_mcp_sainsburys.settings import SainsburysSettings  # noqa: TC001
from browser_mcp_sainsburys.tools import register_tools


def _login_credentials(settings: SainsburysSettings) -> LoginCredentials | None:
    """Return what the login page needs, or ``None`` if it cannot be served.

    Args:
        settings: Runtime configuration.

    Returns:
        The username and session path, or ``None`` before they are configured -
        which is not an error, because the public actions work without them.
    """
    if settings.sainsburys_username is None or (
        settings.sainsburys_storage_state_path is None
    ):
        return None
    return LoginCredentials(
        username=settings.sainsburys_username,
        storage_state_path=settings.sainsburys_storage_state_path,
    )


SITE: Site[SainsburysSettings] = Site(
    slug="sainsburys",
    display_name="Sainsbury's",
    distribution="browser-mcp-sainsburys",
    # The name claude.ai already has this connector under. Renaming it per app
    # belongs with the per-app deployment in stage 3 of docs/scaling-plan.md,
    # not with a restructure the deployment is supposed not to notice.
    server_name="browser-interaction-mcp",
    register_tools=register_tools,
    login_page=LoginPage(
        # Site-named, and docs/scaling-plan.md D10 argues for `/login` on every
        # app instead. That change rewrites a registered GitHub redirect URI,
        # so it travels with the deployment work rather than with this.
        path="/sainsburys-login",
        worker_module="browser_mcp_sainsburys.login_worker",
        credentials=_login_credentials,
    ),
)
