#!/usr/bin/env python3
"""CLI wrapper for `browser_interaction_mcp.sainsburys.add_to_basket`.

Run this to validate the action for real, once a session exists - the logic
itself lives in `src/browser_interaction_mcp/sainsburys.py`, registered as
the `sainsburys_add_to_basket` MCP tool, so this script exercises exactly
what the server would run.

Needs a captured session first - run `scripts/sainsburys_login.py` (with your
own credentials, never in an automated environment) and pass its output path
here. Run `scripts/sainsburys_search.py` first to find a result to pass -
either its `id` (preferred, if it has one), or its exact product name.

Usage:
    uv run scripts/sainsburys_add_to_basket.py <storage-state-path> <product-name>
    uv run scripts/sainsburys_add_to_basket.py <storage-state-path> <name> <product-id>
"""

from __future__ import annotations

import sys
from pathlib import Path

from browser_interaction_mcp.sainsburys import add_to_basket


def main(argv: list[str]) -> int:
    if len(argv) < 2:  # noqa: PLR2004 - <storage-state-path> and <product-name>
        print(
            "Usage: sainsburys_add_to_basket.py <storage-state-path> "
            "<product-name> [product-id]",
            file=sys.stderr,
        )
        return 2

    storage_state_path = Path(argv[0])
    product_name = argv[1]
    product_id = argv[2] if len(argv) > 2 else None  # noqa: PLR2004

    try:
        product = add_to_basket(
            product_name, product_id=product_id, storage_state_path=storage_state_path
        )
    except Exception as exc:  # noqa: BLE001 - top-level script, report and exit
        print(f"Failed: {exc}", file=sys.stderr)
        return 1

    print(f"Added to basket: {product}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
