#!/usr/bin/env python3
"""CLI wrapper for `browser_interaction_mcp.sainsburys.products_we_love`.

Run this to validate the scraper for real - the logic itself now lives in
`src/browser_interaction_mcp/sainsburys.py`, registered as the
`sainsburys_products_we_love` MCP tool, so this script exercises exactly what
the server would run.

Usage:
    uv run scripts/sainsburys_products_we_love.py
"""

from __future__ import annotations

import sys

from browser_interaction_mcp.sainsburys import products_we_love


def main() -> int:
    try:
        names = products_we_love()
    except Exception as exc:  # noqa: BLE001 - top-level script, report and exit
        print(f"Failed: {exc}", file=sys.stderr)
        return 1

    if not names:
        print("No product names found.", file=sys.stderr)
        return 1

    for name in names:
        print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
