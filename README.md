# browser-interaction-mcp-poc

A proof of concept for building an unofficial MCP for a service that I interact with personally.

## Requirements
- As this is unofficial and any services requiring authentication will be using my credentials, it will need to be only me who is able to authenticate (possibly based on a GH app)
- MCP tools should allow only pre-approved, deterministic, in-code browser actions
- Tool calls should be rate limited by default

## Status

The [FastMCP](https://gofastmcp.com) server is scaffolded and runnable, with
GitHub authentication, the rate limiting and the in-code tool registration in
place. One browser action exists —
`sainsburys_products_we_love`, reading Sainsbury's public groceries homepage —
verified against the real page from the deployment host. Getting there
surfaced a real constraint worth knowing before adding a second action:
Sainsbury's (and Tesco, tested for comparison) run Akamai Bot Manager, which
blocks *headless* Chromium specifically, regardless of network origin or user
agent. `browser_interaction_mcp.browser.browser_page(headless=False)` runs a
real, visible-mode Chromium under a short-lived Xvfb display instead, which
gets through cleanly - see that module and `sainsburys.py`'s docstrings.

A second action, `sainsburys_add_to_basket`, needs to act as a logged-in
account rather than reading a public page, so it takes a different approach
to authentication than the GitHub OAuth above: the *session context*
approach. The server never holds a Sainsbury's password. Instead
`scripts/sainsburys_login.py` is run locally, by hand, by the operator: it
opens a real browser, the operator logs in themself, and Playwright's
`storage_state` - the resulting cookies and local storage, not the
credentials that produced them - is captured to a file.
`browser_page(storage_state=...)` then seeds a fresh context from that file,
the same way restoring a saved browser profile would, so the tool call
replays an already-authenticated session instead of ever performing a login.
See `browser.py`'s and `sainsburys.py`'s docstrings, and
`Settings.sainsburys_storage_state_path`.

That script assumes the operator can run it: a real terminal, a real
browser window, to type real credentials into. That won't be true here -
the [documented deployment target](#status) is a Raspberry Pi behind a
tunnel, reached without routine shell access. For that case the server
serves a browser page at `/sainsburys-login`: the operator signs in with
GitHub (the same single-account allowlist as the MCP endpoint), then types
the password into a form there. It goes straight to the server over HTTPS
and never becomes part of the model's own context or conversation. The
login runs in a subprocess that parks if Sainsbury's asks for a
verification code, so the operator can come back to the same URL minutes
later with the code - no live session has to stay open. This is a real,
deliberate narrowing of "the server never sees a password" - accepted
because the alternative, without routine host access, is no way to refresh
a session at all. (It replaced an MCP-elicitation tool; Claude.ai's MCP
client supports tool calls only.) See `login_routes.py`,
`sainsburys_login_flow.py` and `sainsburys.refresh_session`'s docstring for
the full reasoning, and
[`deploy/inventory/group_vars/browser_mcp/local.yml.example`](deploy/inventory/group_vars/browser_mcp/local.yml.example)
for how the one supporting setting this needs
(`BROWSER_MCP_SAINSBURYS_USERNAME`) reaches the Pi without ever being
committed - the same problem `mcp_public_hostname` already solved there.

Its selectors aren't guesses: they were taken from a real Playwright codegen
recording of a manual login and search-and-add flow, which also corrected
several assumptions the login flow's own design had made from the public
groceries site alone - the login path (`/gol-ui/oauth/login`), a different
cookie-consent copy ("Required only"), MFA living on a separate domain
(`account.sainsburys.co.uk`) and not always appearing, and adding a product
straight from its search-result tile (`data-testid="add-button"`) rather than
needing to open the product's own page first. See `sainsburys.py`'s module
docstring for the detail.

**`sainsburys_add_to_basket` is still unverified end to end.** Its selectors
are drawn from a real recording rather than invented, but nobody has run it
against a real, authenticated session yet - that needs credentials this
project does not have and should not be given inside an automated session,
see `scripts/sainsburys_login.py`'s own docstring for why.
`scripts/sainsburys_add_to_basket.py` exists for whoever has those
credentials to run it for real and fix whatever still doesn't match. See
[Not done yet](#not-done-yet).

It runs as a claude.ai connector, on a Raspberry Pi behind a named Cloudflare
Tunnel. The target, the options rejected on the way to it, and what that
topology asks of the configuration are in
[`docs/pi-deployment.md`](docs/pi-deployment.md); the Ansible playbook that
provisions it is in [`deploy/`](deploy/README.md). A code-only change ships
without re-running that playbook: GitHub Actions signs a request to a small
webhook receiver on the Pi (`deploy_webhook.py`) once CI is green on `main`,
which pulls, syncs and restarts itself — see
[`docs/pi-deployment.md`](docs/pi-deployment.md#fast-path-webhook-triggered-code-deploys)
for the scope boundary against the playbook.

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
| `src/browser_interaction_mcp/browser.py` | Shared browser/page setup for every action, headless or headed |
| `src/browser_interaction_mcp/sainsburys.py` | Browser actions against Sainsbury's public groceries site |
| `src/browser_interaction_mcp/deploy_webhook.py` | Standalone webhook receiver that triggers a code-only redeploy on the Pi — not part of the running server |
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
| `scripts/sainsburys_products_we_love.py` | CLI wrapper to run `sainsburys_products_we_love` directly, for validating it against the real page |
| `scripts/sainsburys_login.py` | Run locally, by hand, with your own credentials: logs in to Sainsbury's for real and captures the session `add_to_basket` replays |
| `scripts/sainsburys_add_to_basket.py` | CLI wrapper to run `sainsburys_add_to_basket` directly, for validating it against the real page once a session exists |

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
- **Verifying `sainsburys_add_to_basket` and the `/sainsburys-login` flow
  against the real, logged-in site.** Both are written and tested against a
  fake page, and their selectors (login path, consent copy, MFA domain,
  login-form field ids, search box, result-tile add control) come from a
  real recording, so they're believed correct. But nobody has actually run
  either against the real site: not `scripts/sainsburys_login.py` with real
  credentials, not `/sainsburys-login` on the deployed Pi, and not
  `add_to_basket` against a session either of those produced. "Believed
  correct" is as far as this goes without that run.
- **Hardening `/sainsburys-login`.** It works as a POC but the branch that
  added it skipped the 100% coverage gate, has no `/security-review` on it,
  and hasn't been through an end-to-end run. The GitHub session cookie is
  the whole gate on a public endpoint that accepts a password; the concurrency
  is one in-process subprocess with a wall-clock kill.
- **Encrypting the captured session at rest.** `sainsburys_login.py` and the
  login subprocess both write the `storage_state` file with owner-only
  permissions, but it's still a plaintext file on disk holding a working
  login - see [`docs/deployment.md`](docs/deployment.md) §7, which already
  flags this as the actual thing worth stealing once a browser action drives
  one.
