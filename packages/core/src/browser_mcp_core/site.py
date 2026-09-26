"""What core needs to know about a site in order to serve it.

One app is core plus one :class:`Site`. Everything site-specific reaches the
server through this object, which is what makes ``build_server`` a function of
a site rather than a module that imports one - and what lets a second site be a
sibling directory rather than a fork.

The type parameter is the site's own settings class. It is not ceremony: a
site's ``register_tools`` needs its own ``CoreSettings`` subclass, and without
the parameter the only way to type that is to widen it to ``CoreSettings`` and
cast it back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from browser_mcp_core.settings import CoreSettings

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from fastmcp import FastMCP
    from pydantic import SecretStr


@dataclass(frozen=True)
class LoginCredentials:
    """The two things the out-of-band login page needs before it can be served."""

    username: SecretStr
    storage_state_path: Path


@dataclass(frozen=True)
class LoginPage[SettingsT: CoreSettings]:
    """A site's out-of-band browser login, if it has one.

    Attributes:
        path: Where the page is served, e.g. ``/sainsburys-login``. It is also
            the prefix the login session cookie is scoped to, and half of the
            GitHub OAuth redirect URI. docs/scaling-plan.md D10 argues for
            standardising this on ``/login`` for every app; doing so changes a
            registered redirect URI, so it belongs with the deployment work
            rather than with the restructure.
        worker_module: Importable module that runs one login attempt as a
            subprocess, spawned as ``python -m <worker_module>``. A module name
            rather than a callable because the point of the worker is that it
            is a *separate process* - one that a wedged Chromium can be killed
            with.
        credentials: Reads the site's own settings for the username and session
            path the page needs. Returning ``None`` means the page cannot be
            served yet, which is not an error: a site is perfectly usable for
            its public, unauthenticated actions before anyone has logged in.
    """

    path: str
    worker_module: str
    credentials: Callable[[SettingsT], LoginCredentials | None]


@dataclass(frozen=True)
class Site[SettingsT: CoreSettings]:
    """One automated site, as core sees it.

    Attributes:
        slug: Short machine name, e.g. ``sainsburys``. The fleet's per-app
            accounts, units and state directories are named after it (D3), and
            docs/scaling-plan.md R5 prefixes healing fingerprints with it
            because two sites will both have a ``login.username``.
        display_name: The site's name as a person would write it, used in the
            login page's copy.
        distribution: Installed distribution name to report as the server's
            version, e.g. ``browser-mcp-sainsburys``.
        server_name: Name the MCP server advertises to clients.
        register_tools: Registers every pre-approved tool on the server. This
            is the approval surface, so it stays wholly in the site's package -
            core never adds a tool of its own.
        login_page: The out-of-band login, or ``None`` for a site that needs no
            session at all.
    """

    slug: str
    display_name: str
    distribution: str
    server_name: str
    register_tools: Callable[[FastMCP, SettingsT, str], None]
    login_page: LoginPage[SettingsT] | None = None
