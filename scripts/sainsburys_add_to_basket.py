#!/usr/bin/env python3
"""CLI wrapper for `browser_interaction_mcp.sainsburys.add_to_basket`.

Run this to validate the action for real, once a session exists - the logic
itself lives in `src/browser_interaction_mcp/sainsburys.py`, registered as
the `sainsburys_add_to_basket` MCP tool, so this script exercises exactly
what the server would run. It is unverified against the real page: expect to
fix locators in `sainsburys.py` after running this, the way
`sainsburys_products_we_love.py` was used for `products_we_love`.

Needs a captured session first - run `scripts/sainsburys_login.py` (with your
own credentials, never in an automated environment) and pass its output path
here.

Usage:
    uv run scripts/sainsburys_add_to_basket.py <storage-state-path> [product-url]
"""

from __future__ import annotations

import sys
from pathlib import Path

from browser_interaction_mcp.sainsburys import add_to_basket


def main(argv: list[str]) -> int:
    if not argv:
        print(
            "Usage: sainsburys_add_to_basket.py <storage-state-path> [product-url]",
            file=sys.stderr,
        )
        return 2

    storage_state_path = Path(argv[0])
    url_args = argv[1:2]

    try:
        product = add_to_basket(*url_args, storage_state_path=storage_state_path)
    except Exception as exc:  # noqa: BLE001 - top-level script, report and exit
        print(f"Failed: {exc}", file=sys.stderr)
        return 1

    print(f"Added to basket: {product}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
