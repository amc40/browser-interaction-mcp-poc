"""Console entry point: ``browser-interaction-mcp``."""

from __future__ import annotations

import logging

from browser_mcp_core.redaction import build_redactor, install_log_redaction
from browser_mcp_core.server import build_server, http_middleware
from browser_mcp_sainsburys.app import SITE
from browser_mcp_sainsburys.settings import SainsburysSettings


def main() -> None:
    """Build the server from the environment and serve until interrupted."""
    settings = SainsburysSettings()
    logging.basicConfig(level=settings.log_level)
    # After basicConfig, which is what creates the handler this attaches to.
    install_log_redaction(build_redactor(settings))

    server = build_server(SITE, settings)
    if settings.transport == "http":
        server.run(
            transport="http",
            host=settings.host,
            port=settings.port,
            middleware=http_middleware(SITE, settings),
        )
    else:
        server.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover - exercised via the console script
    main()
