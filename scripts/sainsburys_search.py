#!/usr/bin/env python3
"""CLI wrapper for `browser_interaction_mcp.sainsburys.search_products`.

Run this to validate the search action for real, once a session exists - the
logic itself lives in `src/browser_interaction_mcp/sainsburys.py`, registered
as the `sainsburys_search` MCP tool, so this script exercises exactly what
the server would run. Nothing is added to the basket.

Needs a captured session first - run `scripts/sainsburys_login.py` (with your
own credentials, never in an automated environment) and pass its output path
here.

Usage:
    uv run scripts/sainsburys_search.py <storage-state-path> [search-query]
"""

from __future__ import annotations

import sys
from pathlib import Path

from browser_interaction_mcp.sainsburys import search_products


def main(argv: list[str]) -> int:
    if not argv:
        print(
            "Usage: sainsburys_search.py <storage-state-path> [search-query]",
            file=sys.stderr,
        )
        return 2

    storage_state_path = Path(argv[0])
    query_args = argv[1:2]

    try:
        matches = search_products(*query_args, storage_state_path=storage_state_path)
    except Exception as exc:  # noqa: BLE001 - top-level script, report and exit
        print(f"Failed: {exc}", file=sys.stderr)
        return 1

    if not matches:
        print("No search results found.", file=sys.stderr)
        return 1

    for match in matches:
        print(f"{match.name}  ({match.image_url or 'no image'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
