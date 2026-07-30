# Deployment plan

**Status: planned, not implemented.** Nothing in this document is built yet.
It records the target deployment, the options rejected on the way to it, and
the work each step implies.

## Goal

Reach this server from Claude chat (claude.ai), while keeping the two
properties the project is built around:

- **Only I can call it.** Currently enforced by binding to loopback and by
  needing local shell access — neither of which survives being reachable from
  the internet.
- **Only pre-approved, deterministic actions run**, at a throttled rate.

## Decision summary

| Option | Verdict | Reason |
| --- | --- | --- |
| Serverless function | Rejected | Destroys the two invariants the design depends on — see [below](#why-not-serverless) |
| Anthropic MCP tunnels | Not applicable | Outbound-only and otherwise ideal, but [explicitly unavailable as claude.ai connectors](https://platform.claude.com/docs/en/agents-and-tools/mcp-tunnels/overview); serves Managed Agents and the Messages API only. Also research preview |
| Cloud VM | Workable, not chosen | Solves nothing the Pi does not, and puts a personal logged-in browser session on a datacentre IP |
| **Raspberry Pi 4 + Cloudflare Tunnel** | **Chosen** | Keeps browser state warm, keeps the rate limiter honest, keeps traffic on a residential IP, needs no inbound firewall rule |

### Why not serverless

Two things break, both structural rather than fixable by configuration:

1. **The rate limiter stops being a rate limit.** The token bucket in
   `middleware.py:41` lives in process memory, and its docstring states the
   invariant: *"a single bucket for the whole server … the thing being
   protected is one browser session belonging to one operator."* Horizontal
   scaling gives N instances N independent buckets, silently.
2. **No browser survives between calls.** Each invocation would relaunch
   Chromium (5–15s cold start, ~1GB RAM), and multi-step flows spanning
   several tool calls become impossible because the state lives in a process
   that no longer exists.

Add to that a personal account's session cookies living in cloud storage, and
logins arriving from a datacentre IP, which consumer services treat as account
compromise.

### A public URL is not the same as an open network

claude.ai connects to connectors from Anthropic's own infrastructure, so a
publicly resolvable HTTPS endpoint is required — there is no localhost path.
That does **not** require exposing the home network. `cloudflared` runs on the
Pi and opens an *outbound* connection to Cloudflare's edge; no port forwarding,
no inbound firewall rule, and the home IP never appears in DNS.

## Architecture

```
claude.ai  ──HTTPS──>  Cloudflare edge  ──outbound tunnel──>  cloudflared (Pi 4)
                                                                    │
                                                              127.0.0.1:8000
                                                                    │
                                                          browser-interaction-mcp
                                                                    │
                                                             Chromium (headless)
```

The loopback default at `settings.py:36` therefore stays correct — `cloudflared`
dials localhost, and the server never binds a routable interface.

Use a **named tunnel on a domain you own**, not a `trycloudflare.com` quick
tunnel: quick-tunnel hostnames are ephemeral, and claude.ai stores the connector
URL, so it would break on every restart.

## Prerequisite: authentication

This is the blocking item, and the reason the rest of the plan cannot ship
first. Today the entire access-control model is "loopback plus local shell".
A connector URL is addressable by anyone who finds it.

### Phase 1 — static bearer token

claude.ai's connector configuration accepts custom request headers, storing the
value and sending it on every request. A single shared secret verified by a
FastMCP token verifier, wired into the `FastMCP(...)` construction at
`server.py:47`, is proportionate for a single-user tool and is the smallest
change that closes the hole.

### Phase 2 — GitHub OAuth

The requirement recorded in the README. FastMCP ships `GitHubProvider` in
`fastmcp.server.auth.providers.github`, built on its `OAuthProxy`, which exists
to bridge identity providers that lack dynamic client registration — the exact
situation claude.ai creates.

Two caveats before committing to this path:

- claude.ai requires **OAuth 2.1 with PKCE `S256`** and rejects plain or missing
  PKCE. Verify against a real connector rather than assuming.
- **GitHub OAuth authenticates every GitHub user, not me.** Satisfying "only I
  can authenticate" needs an explicit allowlist check on the authenticated
  login, as separate middleware. This is the easy thing to omit and the whole
  point of the requirement.

## Hardware preparation (Raspberry Pi 4)

| Check | Command | Required |
| --- | --- | --- |
| 64-bit OS | `uname -m` | `aarch64` — `armv7l` means a reinstall, as Playwright ships no armhf browsers |
| Bootloader | `sudo rpi-eeprom-update -a` | Needed before booting from USB |
| Power | `vcgencmd get_throttled` | `0x0` — under-voltage on an always-on service surfaces as random unexplained failures |

Pi 4 installs are frequently older than the switch to 64-bit as the default, so
the first check is worth doing before any other work.

Further notes specific to this board:

- **Storage.** The Pi 4 has no PCIe connector, so the SSD goes in a USB 3.0
  port rather than on an NVMe HAT. This matters more than it sounds: an
  always-on Chromium writes to its profile and cache constantly, and that is
  precisely what kills SD cards. Relocate the browser profile and cache to the
  SSD at minimum. Prefer a powered enclosure, or use the official 3A PSU.
- **Cooling.** The Cortex-A72 throttles around 80 °C and the Pi 4 reaches it
  under sustained load in a closed case. Chromium rendering is exactly that
  load pattern. Throttling presents as intermittently slow page loads, which is
  easily misread as flaky selectors. Heatsink minimum; fan preferred.
- **Memory.** Chromium wants roughly 1GB per context. 8GB is comfortable, 4GB
  is fine for one browser at a time, 2GB is tight — enable **zram** rather than
  a swap *file*, since swapping to the SD card is both slow and destructive.
- **Speed.** The A72 at 1.5–1.8GHz is roughly 2–3x slower than a Pi 5. Playwright
  timeout defaults assume a laptop; budget accordingly.

## Provisioning with Ansible

Agentless, so the control node is a laptop and the Pi needs only SSH and a
system `python3`. Raspberry Pi OS Bookworm ships 3.11, which satisfies
Ansible's target-side requirement; this is unrelated to the 3.13 the
application needs, which `uv` fetches as its own standalone arm64 build.

The justification for automating a single host is recovery: when the SD card
fails, provisioned means one command rather than an evening rediscovering which
libraries Chromium needed.

### Roles

| Role | Responsibility |
| --- | --- |
| `base` | apt upgrade, `unattended-upgrades`, key-only SSH, timezone |
| `storage` | Mount the SSD; relocate browser profile and cache off the SD card |
| `uv` | Install `uv`; `uv sync --frozen --no-dev` |
| `browser` | Explicit apt library list, then `playwright install chromium` |
| `app` | systemd unit, `EnvironmentFile`, service account |
| `tunnel` | `cloudflared` (arm64), named-tunnel credentials, its own unit |

### Implementation notes

- **Do not rely on `playwright install-deps`.** It keys off the `ID` field in
  `/etc/os-release` and has known failures on Debian-derived distributions it
  does not recognise; the common workaround is
  `install-deps || echo "install manually"`. Declaring the libraries as an
  explicit `ansible.builtin.apt` list is pinned, idempotent and immune to
  upstream changing its distro detection.
- **`playwright install chromium` is not idempotent** and will report changed
  on every run. Guard it with `creates:` on the browser path, or an explicit
  `changed_when`.
- **Do not template a `.env` file into the checkout.** `settings.py:24` reads
  `.env` relative to the working directory, and `extra="forbid"` at
  `settings.py:22` means one unrecognised `BROWSER_MCP_*` key fails at startup —
  which, under systemd restart, is a boot loop. Use a root-owned systemd
  `EnvironmentFile` at mode `0600`, outside the git tree.
- **Secrets via `ansible-vault`**, never written into the checkout even
  transiently. The repository has a gitleaks gate precisely because this server
  runs with personal credentials.
- **`uv sync --frozen`** respects `uv.lock` and fails rather than silently
  drifting. `--no-dev` omits ruff, mypy and the audit tooling, which have no
  purpose on the Pi.
- Develop the playbook with `--check --diff` against the live host before the
  first real run.

## Repository changes required

| Change | Location | Note |
| --- | --- | --- |
| Serve over HTTP | `BROWSER_MCP_TRANSPORT=http` | `settings.py:32` defaults to stdio; the `run` target in the `Makefile` is stdio-only |
| Auth provider | `server.py:47` | Passed to `FastMCP(...)`; see [above](#prerequisite-authentication) |
| Auth settings | `settings.py` | New fields, plus matching entries in `.env.example` |
| Browser actions | `tools.py` | Currently only `server_info`; the deployment is untested against real browser work until these exist |
| Playbook | `deploy/` | One app, one host — splitting it into a second repository buys nothing at this size |

Two gates worth noting:

- `mypy` is scoped to `files = ["src", "tests"]` and ruff only inspects Python,
  so `deploy/` falls outside every gate in the README's quality table. Adding
  `ansible-lint` to `make check` would keep that standard consistent.
- CI runs on x86 runners, so **arm64 is never exercised by CI**. Browser-launch
  breakage will only ever appear on the Pi. A smoke-test target runnable there
  is worth having once real actions exist.

## Known constraints

- **`playwright install chrome` fails on Linux arm64** — Chrome for Testing has
  no arm64 Linux build. Use bundled Chromium and never set `channel="chrome"`.
  Relevant only if a target site needs proprietary codecs or sniffs the brand.
- **Uptime becomes an operational concern.** Connector calls fail whenever the
  Pi is off or offline.
- **Headless detection.** If a target site blocks headless Chromium, the usual
  escape hatch is `xvfb-run` with a headed browser.
- **Single instance only.** The rate limiter's correctness depends on exactly
  one process. Do not scale this horizontally.

## Open questions

- Which Pi 4 memory variant. 1GB would warrant revisiting the approach.
- Whether Phase 2 OAuth is worth the work over a bearer token for a
  single-user tool, given the allowlist middleware it additionally requires.
- Where the automated service's own credentials for the target site are stored,
  and how they are refreshed when the session expires.

## References

- [Custom connectors via remote MCP servers](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
- [Anthropic MCP tunnels](https://platform.claude.com/docs/en/agents-and-tools/mcp-tunnels/overview)
- [Cloudflare Tunnel](https://developers.cloudflare.com/agents/model-context-protocol/guides/remote-mcp-server/)
- [Playwright supported platforms](https://playwright.dev/python/docs/browsers)
- [FastMCP authentication](https://gofastmcp.com/servers/auth/authentication)
