# browser-mcp-core

The site-agnostic half of a browser-interaction MCP server: the FastMCP server
and its middleware stack, GitHub-account authentication, the Playwright browser
lifecycle, the out-of-band login framework, secret redaction, and the deploy
webhook receiver.

It knows nothing about any particular site. A site package supplies a
[`Site`][browser_mcp_core.site.Site] — its tools, its settings, and optionally
its `LoginSteps` — and `build_server` does the rest.

See [`docs/scaling-plan.md`](../../docs/scaling-plan.md) for why this is a
package rather than a repository, and D2 for the rule about what earns a place
in here (extract on the second occurrence, not the first).
