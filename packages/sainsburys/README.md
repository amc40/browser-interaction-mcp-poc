# browser-mcp-sainsburys

Sainsbury's, as one site on top of [`browser-mcp-core`](../core/README.md):
the page interactions, the login steps, the settings the site needs, and the
pre-approved tools that are the approval surface.

The patterns in here — waiting for a page to settle, the bounded per-item grid
read, the ellipsis-prefix name match — are deliberately *not* in core. They were
learned from this one site, and [`docs/scaling-plan.md`](../../docs/scaling-plan.md)
D2 says to promote what a second site demonstrably shares, not to generalise
from a single example. Until then they live here and are written down in
[`docs/site-automation-gotchas.md`](../../docs/site-automation-gotchas.md).
