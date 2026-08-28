#!/usr/bin/env python3
"""CLI wrapper for `browser_interaction_mcp.sainsburys.order_history`.

Run this to validate - and correct - the order-history action for real, once
a session exists. The logic itself lives in
`src/browser_interaction_mcp/sainsburys.py`, registered as the
`sainsburys_order_history` MCP tool, so this script exercises exactly what
the server would run.

Unlike the other CLI wrappers here, this one is expected to need fixing up:
`order_history`'s selectors are a best-effort guess, not yet confirmed by a
real Playwright recording the way search and add-to-basket's were. If this
fails or returns nothing, use Playwright codegen against the real,
authenticated order-history page and update the `_ORDER_*`/`_ITEM_*`
constants in `sainsburys.py` to match what it actually renders.

Needs a captured session first - run `scripts/sainsburys_login.py` (with your
own credentials, never in an automated environment) and pass its output path
here.

Usage:
    uv run scripts/sainsburys_order_history.py <storage-state-path> [max-orders]
"""

from __future__ import annotations

import sys
from pathlib import Path

from browser_interaction_mcp.sainsburys import order_history


def main(argv: list[str]) -> int:
    if not argv:
        print(
            "Usage: sainsburys_order_history.py <storage-state-path> [max-orders]",
            file=sys.stderr,
        )
        return 2

    storage_state_path = Path(argv[0])
    max_orders = int(argv[1]) if len(argv) > 1 else 5

    try:
        items = order_history(
            storage_state_path=storage_state_path, max_orders=max_orders
        )
    except Exception as exc:  # noqa: BLE001 - top-level script, report and exit
        print(f"Failed: {exc}", file=sys.stderr)
        return 1

    if not items:
        print("No order history line items found.", file=sys.stderr)
        return 1

    for item in items:
        date = item.order_date or "unknown date"
        price = item.price_paid or "unknown price"
        quantity = item.quantity if item.quantity is not None else "?"
        print(f"{date}  {item.name}  x{quantity}  {price}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
