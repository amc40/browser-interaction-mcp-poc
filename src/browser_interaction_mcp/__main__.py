"""Console entry point: ``browser-interaction-mcp``."""

from __future__ import annotations

import logging

from browser_interaction_mcp.server import build_server
from browser_interaction_mcp.settings import Settings


def main() -> None:
    """Build the server from the environment and serve until interrupted."""
    settings = Settings()
    logging.basicConfig(level=settings.log_level)

    server = build_server(settings)
    if settings.transport == "http":
        server.run(transport="http", host=settings.host, port=settings.port)
    else:
        server.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover - exercised via the console script
    main()
