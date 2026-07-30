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

Once `tools.py` drives a real browser, the host stores live authenticated
sessions for third-party services — the actual thing worth stealing, and worth
more than the GitHub token that guards it. Encrypt the profile at rest, keep the
sessions scoped as narrowly as each service permits, and have a tested way to
revoke them all quickly.

## 8. Log who did what

Nothing currently records which tool ran, when, or on whose authority. On a
laptop the answer is always "the operator". Deployed, an authenticated `sub` and
tool name per call is the only way to answer that question afterwards — and the
only way to notice that the answer has become surprising.
