# browser-interaction-mcp-poc

A proof of concept for building an unofficial MCP for a service that I interact with personally.

## Requirements
- As this is unofficial and any services requiring authentication will be using my credentials, it will need to be only me who is able to authenticate (possibly based on a GH app)
- MCP tools should allow only pre-approved, deterministic, in-code browser actions
- Tool calls should be rate limited by default

## Status

The [FastMCP](https://gofastmcp.com) server is scaffolded and runnable, with
GitHub authentication, the rate limiting and the in-code tool registration in
place. No browser is driven yet — see [Not done yet](#not-done-yet).

## Getting started

Requires [uv](https://docs.astral.sh/uv/).

```sh
make install          # sync the environment and install the git hooks
make run              # serve on stdio (unauthenticated - see below)
make check            # run every quality gate
```

To point an MCP client at it:

```json
{
  "mcpServers": {
    "browser-interaction": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/browser-interaction-mcp-poc", "browser-interaction-mcp"]
    }
  }
}
```

## Authentication

Every tool is restricted to one GitHub account. That is applied as middleware,
so a tool is covered the moment it is registered — there is no per-tool
decorator to forget — and an unauthorised caller is not even shown the tool
list.

Authentication is [FastMCP's `GitHubProvider`](https://gofastmcp.com/servers/auth/oauth-proxy):
clients run a normal GitHub OAuth flow, and the resulting token is verified
against the GitHub API on every request. Authorisation is a one-function check
comparing the token's `login` claim to `BROWSER_MCP_GITHUB_LOGIN`, which
defaults to the repository owner.

**This only works over the http transport.** MCP carries credentials in HTTP
headers, and stdio has no headers — it is a pipe to a subprocess, so its trust
boundary is local shell access and nothing more. FastMCP skips its auth checks
there, and this server does not override that. **On stdio, anyone with local
shell access can call every tool.** The reasoning, and the alternative that was
built and rejected, are in [SDR 0001](docs/sdr/0001-github-authentication.md).

To serve authenticated, [register a GitHub OAuth
app](https://github.com/settings/developers) with the callback URL
`http://127.0.0.1:8000/auth/callback`, then set:

```sh
BROWSER_MCP_TRANSPORT=http
BROWSER_MCP_GITHUB_CLIENT_ID=Ov23li...
BROWSER_MCP_GITHUB_CLIENT_SECRET=...
```

Starting on http without both credentials is a startup error rather than an
unauthenticated server. Point a client at `http://127.0.0.1:8000/mcp` and it
will prompt for the OAuth flow on first connect.

## Layout

| Path | Purpose |
| --- | --- |
| `src/browser_interaction_mcp/server.py` | Builds the server: middleware, instructions, tool registration |
| `src/browser_interaction_mcp/tools.py` | Every exposed tool. New browser actions go here |
| `src/browser_interaction_mcp/auth.py` | Who may use the server: the OAuth provider and the login check |
| `src/browser_interaction_mcp/middleware.py` | Tool-call rate limiting |
| `src/browser_interaction_mcp/settings.py` | Configuration, from `BROWSER_MCP_*` env vars or `.env` |
| `src/browser_interaction_mcp/__main__.py` | The `browser-interaction-mcp` console script |
| `docs/sdr/` | Design records for decisions worth their own argument |

### Adding a browser action

Add a function to `tools.py` and decorate it with `@mcp.tool`. Keep it
deterministic and give it no parameter that a caller could use to reach a page,
selector or script that you have not approved in code — that constraint is the
whole point of the design, and nothing enforces it automatically.

## Configuration

All settings are read from `BROWSER_MCP_`-prefixed environment variables or a
local `.env` file; see [`.env.example`](.env.example) for the full list and the
defaults. Unknown or malformed values fail at startup rather than being ignored.

## Quality gates

Every gate below runs in CI on pushes and pull requests, and again daily on a
schedule so that newly published advisories surface without a push. `make check`
runs the same set locally, and `pre-commit` runs the fast ones on commit and the
slow ones on push.

| Gate | Tool | What it enforces |
| --- | --- | --- |
| Formatting | `ruff format` | One canonical formatting, checked not applied |
| Linting | `ruff check` | Every ruff rule (`select = ["ALL"]`), minus four that contradict the formatter or each other. Includes flake8-bandit security rules, docstring rules and complexity limits |
| Types | `mypy --strict` | Strict mode plus `warn_unreachable` and six extra error codes; `src` and `tests` both checked |
| Tests | `pytest` | Warnings are errors, strict markers and config, strict `xfail` |
| Coverage | `coverage` | 100% line *and* branch coverage, enforced by `fail_under` |
| Dependencies | `deptry` | No unused, missing or transitive-but-imported dependencies |
| Lockfile | `uv lock --check` | `uv.lock` is in sync with `pyproject.toml` |
| Vulnerabilities | `pip-audit` | No known advisories against any locked package, dev tooling included |
| Secrets | `gitleaks` | No credentials committed — the main risk here, since this server runs as me |
| Workflows | `zizmor` | Static analysis of the GitHub Actions workflows themselves |
| Code scanning | CodeQL | `security-extended` query suite, on PRs and weekly |
| Packaging | `uv build` + `twine check` | The distribution builds and its metadata is valid |
| Updates | Dependabot | Weekly PRs for dependencies and actions |

Adjusting a gate means editing `[tool.*]` in `pyproject.toml` — the same
configuration drives local runs, pre-commit and CI, so they cannot disagree.
Refresh the pinned pre-commit hooks with `uv run pre-commit autoupdate`.

## Not done yet

- **Authentication on stdio**, which cannot carry credentials at all. The http
  transport binds to loopback by default for the same reason. See
  [SDR 0001](docs/sdr/0001-github-authentication.md).
- **Browser automation.** No browser is driven yet — `tools.py` has a single
  `server_info` tool and the extension point for real actions.
- **Persistent credential storage** for the service being automated.
