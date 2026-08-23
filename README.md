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

Running it as a claude.ai connector, on a Raspberry Pi behind a Cloudflare
Tunnel, is planned but not built. The target, the options rejected on the way to
it, and what that topology asks of the configuration are in
[`docs/pi-deployment.md`](docs/pi-deployment.md); the Ansible playbook that
would provision it is drafted in [`deploy/`](deploy/README.md), and has never
been run against real hardware.

Browser actions go stale as pages change.
[`docs/self-healing.md`](docs/self-healing.md) proposes a sandboxed agent that
reads a redacted capture of the failure and opens a PR against the selectors —
with no credentials for the automated service, no route to it, and no path to
what the server runs.

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
against the GitHub API. Authorisation is a one-function check comparing the
token's `sub` claim to `BROWSER_MCP_GITHUB_USER_ID`.

That setting is a numeric GitHub user ID rather than a login, because logins can
be changed and a login freed by a rename can be registered by somebody else. Find
one at `https://api.github.com/users/<login>`; it defaults to the repository
owner's. A value that is not digits is rejected at startup, so a login typed
there fails loudly instead of silently matching nothing.

Verified tokens are cached for `BROWSER_MCP_GITHUB_TOKEN_CACHE_SECONDS` (five
minutes by default) because each verification costs two GitHub API calls. The
cache is a revocation delay — a token revoked on GitHub keeps working until its
entry expires — so set `0` if that matters more than the API budget.

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

This is a loopback, single-process, single-user setup, and several of the
defaults it relies on are only safe because of that.
[`docs/deployment.md`](docs/deployment.md) lists the mitigations a real
deployment would need first — TLS and the base URL FastMCP derives cookie
security from, restricting the OAuth proxy's redirect URIs, owning the OAuth
state rather than inheriting a directory keyed off the client secret, and the
fact that shell access on the host bypasses all of this.

## Layout

| Path | Purpose |
| --- | --- |
| `src/browser_interaction_mcp/server.py` | Builds the server: middleware, instructions, tool registration |
| `src/browser_interaction_mcp/tools.py` | Every exposed tool. New browser actions go here |
| `src/browser_interaction_mcp/auth.py` | Who may use the server: the OAuth provider and the login check |
| `src/browser_interaction_mcp/middleware.py` | Tool-call rate limiting, and secret redaction on the error path |
| `src/browser_interaction_mcp/redaction.py` | Keeping the server's own credentials out of logs and errors |
| `src/browser_interaction_mcp/settings.py` | Configuration, from `BROWSER_MCP_*` env vars or `.env` |
| `src/browser_interaction_mcp/__main__.py` | The `browser-interaction-mcp` console script |
| `docs/sdr/` | Design records for decisions worth their own argument |
| `docs/deployment.md` | What would have to change before this runs on a server |
| `docs/pi-deployment.md` | Where it is planned to run, and how it would get there |
| `deploy/` | The Ansible playbook that provisions that host |
| `docs/self-healing.md` | Proposed mechanism for repairing stale selectors, and its limits |

### Adding a browser action

Add a function to `tools.py` and decorate it with `@mcp.tool`. Keep it
deterministic and give it no parameter that a caller could use to reach a page,
selector or script that you have not approved in code — that constraint is the
whole point of the design, and nothing enforces it automatically.

## Secret redaction

Every credential the server holds is a `SecretStr` field on `Settings`.
`SecretStr` keeps a value out of reprs and tracebacks, but that protection ends
at `get_secret_value()` — past that call it is an ordinary string that can reach
a log line or an error returned to a caller.

`redaction.py` closes that gap by replacing **known values**, matched exactly,
with a marker naming the setting they came from. It is not a secret *scanner*:
it cannot find a credential the server was never told about, and deliberately so
— for the values it does hold there are no false negatives and no regex to tune.

Two things make it work in practice rather than in principle:

- **Registration is automatic.** `build_redactor` walks `Settings` for
  `SecretStr` fields, so adding a credential registers it. There is no list to
  keep in sync, which is the only way this kind of layer normally fails.
- **Encodings are covered.** A secret in a URL is percent-encoded, in a `Basic`
  header it is base64, and in a JSON body it carries backslash escapes. Each is
  a different string, and matching only the raw bytes is the usual way a
  redactor is quietly wrong.

It is applied in two places: a logging filter on the root handlers, which is
what brings rendered tracebacks into scope, and middleware wrapping every tool
call, which rewrites a failing call's message while keeping its exception type.
Tool *results* are not covered — they are built in `tools.py` from values chosen
there — and that stops being true for the first tool that returns page text.

A value shorter than eight characters is refused rather than redacted, with a
warning: it would occur in ordinary output by coincidence, and replacing it
would corrupt unrelated text while advertising that something matched.

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
