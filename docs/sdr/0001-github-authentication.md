# SDR 0001: GitHub authentication, and what stdio costs

- **Status:** accepted
- **Date:** 2026-07-30

## Context

This server drives a browser using the operator's own logged-in sessions. A
tool call here is an action taken *as that person*, against services that never
agreed to any of this. The project's founding requirement is therefore that
exactly one person can call it — [`README.md`](../../README.md) puts it as "it
will need to be only me who is able to authenticate".

Until now nothing enforced that. Anyone who could reach the process could call
every tool.

MCP's answer to authentication is OAuth 2.1 bearer tokens carried in HTTP
headers. That mechanism only exists on HTTP transports. The stdio transport is
a pipe to a subprocess: there are no headers, no request metadata, and nowhere
for a token to live. Its trust model is the operating system's — whoever can
start the process is the caller.

Two questions followed: which layer enforces the rule, and what happens on
stdio, which is the transport the README documents and `make run` starts.

## Decision

**Use FastMCP's authentication and authorisation machinery as it ships, rather
than writing our own.** Specifically:

- `GitHubProvider` for authentication. It runs the OAuth flow against a GitHub
  OAuth app and verifies the resulting opaque token by calling the GitHub API,
  putting the verified `login` on the token's claims. Configured whenever the
  transport is http; settings validation refuses an http server that has no
  OAuth app, so a port is never bound that callers cannot be authenticated on.
- `AuthMiddleware` for authorisation, with a one-function `AuthCheck` that
  compares that `login` to the configured one, case-insensitively. Middleware
  rather than per-tool decorators, so a browser action added to `tools.py` is
  covered the moment it is registered and there is no decorator to forget.
  It also filters `tools/list`, so an unauthorised caller is not even shown
  what exists.

**Accept that this leaves the stdio transport unauthenticated.**
`AuthMiddleware` deliberately skips its checks when the request came over
stdio. On stdio, anyone with local shell access can call every tool.

## Alternatives considered

**Enforce on stdio too, by writing our own middleware that does not waive the
check.** This was built first and worked: stdio has no token, so every call was
refused, and only an OAuth'd http caller could do anything. It was rejected
because the security-critical code — the part that decides who gets to act as
the operator — would then be ours to keep correct, in a repository whose whole
premise is that this is a proof of concept maintained by one person. FastMCP's
version is the one that gets reviewed, fuzzed and fixed upstream. Owning a
parallel implementation to close a gap on a transport that cannot be secured
anyway is a poor trade.

**Require a GitHub personal access token in the environment on stdio, verified
against the GitHub API at startup.** This authenticates the *process*, not the
caller, and the token sits in the environment of a process anyone with local
shell access can already read. It looks like authentication without adding any.

## Consequences

- Over http, only the configured GitHub account can list or call tools. That is
  the requirement, met.
- Over stdio, the local user is the caller and no GitHub identity is involved.
  `build_auth_provider` logs a warning at startup saying so, rather than letting
  it be discovered. The README says it too.
- The stdio waiver is upstream behaviour, so a FastMCP upgrade could change it
  in either direction. `test_stdio_callers_are_waved_through` asserts the
  current behaviour, so a change shows up as a failing test rather than as a
  silent shift in who can drive the operator's browser.
- The bind address stays on loopback by default, and `AuthMiddleware` is
  registered ahead of the rate limiter so that refused callers cannot spend a
  budget that is shared server-wide.
- Anyone the operator would not hand a shell to should not be given one. On
  stdio that is now the whole of the access control.
