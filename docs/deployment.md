# Mitigations required before this runs on a server

Everything built so far assumes the server runs on the operator's own machine:
bound to loopback, one user, one process, started and stopped by hand. Putting
it on a server breaks three of those assumptions at once — the network in front
of it is no longer trusted, the host is no longer only the operator's, and the
process outlives the session that started it.

**None of the following is implemented.** Until it is, http-on-loopback is the
only configuration this repository supports. The list is ordered roughly by how
badly each one bites.

Two items that were originally on this list have since been done, and are noted
here so the reasoning is not lost:

- **Identity is pinned to the GitHub numeric user ID**, not the login, because
  logins can be changed and a freed login can be registered by somebody else.
  `BROWSER_MCP_GITHUB_USER_ID` holds it, and settings validation rejects
  anything that is not digits so a login typed there fails at startup rather
  than locking the operator out silently.
- **Token verification is cached** for `BROWSER_MCP_GITHUB_TOKEN_CACHE_SECONDS`,
  five minutes by default. Without it every request cost two GitHub API calls.
  The cache is a revocation delay: a token revoked on GitHub keeps working until
  its entry expires, so shorten it — or set `0` — if revocation needs to be
  immediate. It is also per-process, so replicas do not share it.

## 1. Terminate TLS, and set the base URL to the https one

Bearer tokens travel in request headers. Over plaintext, anyone on the path can
lift one and replay it — and a replayed token is a session driving the
operator's browser.

Terminating TLS at a reverse proxy is necessary but **not sufficient**: FastMCP
decides whether to mark its OAuth cookies `Secure` purely by testing whether
`base_url` starts with `https://`, and it builds the advertised OAuth metadata
from the same value rather than from forwarded headers. If TLS stops at the load
balancer and `BROWSER_MCP_GITHUB_OAUTH_BASE_URL` is left as http, the proxy
issues non-secure cookies and logs the "deploy with HTTPS for production"
warning that currently appears on every http start.

So: set `BROWSER_MCP_GITHUB_OAUTH_BASE_URL` to the public https URL, update the
GitHub OAuth app's callback to match, and keep `BROWSER_MCP_HOST=127.0.0.1` so
the reverse proxy is the only thing actually listening.

## 2. Restrict the OAuth proxy's redirect URIs

`allowed_client_redirect_uris` defaults to `None`, which allows every URI. That
is reasonable when the only client is on the same machine; on a reachable
endpoint it widens the authorization-code interception surface for no benefit.
Pin it to the redirect URIs the clients actually use.

## 3. Take ownership of the OAuth state

With `client_storage=None`, FastMCP creates an encrypted store under
`~/.local/share/fastmcp/oauth-proxy/<fingerprint>`, where the fingerprint and
the encryption key are both derived from `jwt_signing_key` — which in turn
defaults to being derived from the GitHub client secret. Three consequences:

- **Rotating the client secret silently moves the directory.** Every registered
  client and stored upstream token is orphaned, and everything must re-auth.
  Rotation should be a deliberate act, not an accidental data loss.
- **More than one process means more than one store.** Tokens issued by one
  worker are not recognised by another, so multiple workers or replicas need a
  shared `client_storage` backend.
- **That directory holds upstream GitHub tokens at rest.** It needs deliberate
  file permissions, and a decision about whether it is backed up.

Set `jwt_signing_key` explicitly from a managed secret rather than letting it
follow the client secret around.

## 4. Rate limit at the edge as well

`ToolCallRateLimitingMiddleware` keeps its bucket in process memory, so two
workers give twice the configured rate. It also sits *behind* authentication by
design, which means it does nothing about unauthenticated floods — those have
to be limited at the reverse proxy, along with the OAuth endpoints themselves.

## 5. Handle the secrets as secrets

`github_client_secret` is a `SecretStr`, so it stays out of logs, reprs and
tracebacks — but a `.env` file is still a plaintext file on a disk somebody
else may be able to read. Prefer the platform's secret mechanism (systemd
`LoadCredential`, a secrets manager, whatever the host offers), and if `.env` is
used anyway, make sure its permissions are not world-readable.

`BROWSER_MCP_INCLUDE_ERROR_DETAILS` must stay off: it sends internal errors and
tracebacks, which by then will include browser state, to whoever is calling.

## 6. Nobody gets a shell

[SDR 0001](sdr/0001-github-authentication.md) accepts an unauthenticated stdio
transport on the grounds that local shell access is the operator's own. On a
server that stops being true, and it is the sharpest edge in this whole
document: anyone who can run a command on the box can start
`browser-interaction-mcp` on stdio and drive the browser as the operator,
without ever touching GitHub.

That makes shell access on the host exactly equivalent to full control of every
account the server automates. Run it as a dedicated unprivileged user with no
interactive login, do not co-locate it with anything else, and treat remote code
execution in *any* service on that host as a total compromise of the automated
accounts — not a partial one.

## 7. Protect the browser profile at rest

Now that `sainsburys.add_to_basket` drives a real, logged-in browser, the host
stores a live authenticated session for a third-party service in the file
`scripts/sainsburys_login.py` writes — the actual thing worth stealing, and
worth more than the GitHub token that guards it. Owner-only file permissions
(what the script does today) are not encryption at rest. Encrypt the file,
keep the session scoped as narrowly as Sainsbury's permits, and have a tested
way to revoke it quickly — signing out on Sainsbury's, or just deleting the
file and rerunning the login script.

## 8. Isolate the browser from the server

There is currently no boundary between the two. `tools.py` calls
`sainsburys.py`, which calls `browser_page()`, which starts Playwright **inside
the FastMCP process**: Chromium is a separate OS process, but the storage state,
the parsed page content and the automation code all live in the server. Same
user, same unit, same `EnvironmentFile` — so the process handling untrusted page
content is the process holding the OAuth client secret. The one existing process
split, `sainsburys_login_worker`, is a robustness boundary and says so in its own
docstring: it exists so a hung Chromium can be killed, it inherits the parent's
whole environment, and it runs as the same user.

The cost shows up in the unit file, where three sandboxing directives are
switched off with a comment explaining that Chromium needs them:
`MemoryDenyWriteExecute` (V8 JIT), `RestrictNamespaces` (the renderer sandbox)
and `SystemCallFilter` (same). **The browser's requirements set the sandbox
floor for the server as well**, which is the clearest statement of the problem:
one process is as weak as the weakest thing in it.

The proportionate fix is a second unit, not a container:

- A long-lived `browser-worker` running as its own user, talking to the server
  over a Unix socket with one message per pre-approved action — the same shape
  the tools already have, and the same pattern `sainsburys_login_worker` already
  proves, made long-lived and given its own identity.
- The server unit then gets the three strict directives back, because it no
  longer hosts a browser.
- The worker gets no `EnvironmentFile` beyond what it needs, `ReadWritePaths`
  limited to the profile directory, and its own state directory.
- The `storage_state` file becomes owned by the worker and **unreadable by the
  server**, so the thing §7 calls the most valuable asset on the box stops being
  reachable from the process that terminates OAuth and faces the network.

What that buys is a smaller blast radius, not immunity. A page that escapes
Chromium's own sandbox still lands next to the live session, which is the thing
worth stealing; what it no longer lands next to is the OAuth client secret, the
deploy checkout and the server's network position. Worth being clear that this
reduces the loss rather than preventing it — and that encryption at rest (§7)
does not help against a worker that must decrypt the session to use it.

A rootless container per action is stronger and is the next step up if this
turns out to be insufficient; on a Pi it costs memory and per-call startup for
a boundary the two-unit split already establishes. A separate machine is the
step after that, and is disproportionate for a proof of concept.

One consequence for [`self-healing.md`](self-healing.md): once the browser is
split out, failure captures and their redaction belong on the worker's side of
the socket. The raw trace is exactly the sensitive artefact this section is
about, and it should never cross into the server process — the server should
only ever see a redacted bundle, or a path to one.

## 9. Log who did what

Nothing currently records which tool ran, when, or on whose authority. On a
laptop the answer is always "the operator". Deployed, an authenticated `sub` and
tool name per call is the only way to answer that question afterwards — and the
only way to notice that the answer has become surprising.
