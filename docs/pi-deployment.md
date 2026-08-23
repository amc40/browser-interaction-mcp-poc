# Deploying to a Raspberry Pi as a claude.ai connector

**Status: running.** The provisioning playbook this document specifies is in
[`deploy/`](../deploy/README.md) and provisions the real host serving
`browser-interaction-mcp` to claude.ai today.

[`deployment.md`](deployment.md) lists *what must change* before this runs on a
server, independently of where that server is. This document picks the where,
records the options rejected on the way, and works out what the chosen topology
implies. Every mitigation in that list still applies; the sections below only
add what is specific to a Pi behind a tunnel.

## Goal

Reach this server from Claude chat (claude.ai) while keeping the properties the
project is built around: exactly one person can call it, only pre-approved
deterministic actions run, and tool calls stay rate limited.

## Decision summary

| Option | Verdict | Reason |
| --- | --- | --- |
| Serverless function | Rejected | Breaks two invariants the design depends on — see [below](#why-not-serverless) |
| Anthropic MCP tunnels | Not applicable | Outbound-only and otherwise ideal, but [explicitly unavailable as claude.ai connectors](https://platform.claude.com/docs/en/agents-and-tools/mcp-tunnels/overview); they serve Managed Agents and the Messages API only. Also research preview |
| Cloud VM | Workable, not chosen | Solves nothing the Pi does not, and puts a personal logged-in browser session on a datacentre IP |
| **Raspberry Pi 4 + Cloudflare Tunnel** | **Chosen** | Keeps browser state warm, keeps the rate limiter honest, keeps traffic on a residential IP, needs no inbound firewall rule |

### Why not serverless

Two things break, both structural rather than fixable by configuration:

1. **The rate limiter stops being a rate limit.** The token bucket in
   `middleware.py:40` lives in process memory, and its docstring states the
   invariant: *"a single bucket for the whole server … the thing being
   protected is one browser session belonging to one operator."* Horizontal
   scaling silently gives N instances N independent buckets — the same point
   [`deployment.md`](deployment.md) makes about running two workers.
2. **No browser survives between calls.** Each invocation would relaunch
   Chromium (5–15s cold start, ~1GB RAM), so multi-step flows spanning several
   tool calls become impossible: the state lives in a process that no longer
   exists.

Add to that a personal account's session cookies living in cloud storage, and
logins arriving from a datacentre IP, which consumer services treat as account
compromise.

### A public URL is not the same as an open network

claude.ai connects to connectors from Anthropic's own infrastructure, so a
publicly resolvable HTTPS endpoint is required — there is no localhost path.
That does **not** mean exposing the home network. `cloudflared` runs on the Pi
and opens an *outbound* connection to Cloudflare's edge: no port forwarding, no
inbound firewall rule, and the home IP never appears in DNS.

## Architecture

```
claude.ai  ──HTTPS──>  Cloudflare edge  ──outbound tunnel──>  cloudflared (Pi 4)
                                                                    │
                                                            http, 127.0.0.1:8000
                                                                    │
                                                          browser-interaction-mcp
                                                                    │
                                                             Chromium (headless)
```

The loopback default at `settings.py:37` stays correct — `cloudflared` dials
localhost, so the server never binds a routable interface. That is the same
arrangement [`deployment.md`](deployment.md) asks for when it says to keep
`BROWSER_MCP_HOST=127.0.0.1` so the reverse proxy is the only listener.

Use a **named tunnel on a domain you own**, not a `trycloudflare.com` quick
tunnel: quick-tunnel hostnames are ephemeral and claude.ai stores the connector
URL, so it would break on every restart.

## What the tunnel changes about authentication

Authentication is already implemented — `GitHubProvider`, authorisation pinned
to the numeric GitHub user ID, verification cached. See
[SDR 0001](sdr/0001-github-authentication.md). What remains is configuring it
correctly for this topology, and one assumption to verify before committing.

**TLS terminates at Cloudflare's edge, not on the Pi.** The tunnel carries
plain http over the last hop, which is precisely the case
[`deployment.md` §1](deployment.md) warns about: FastMCP decides whether to mark
its OAuth cookies `Secure` by testing whether `base_url` starts with `https://`,
and builds its advertised OAuth metadata from the same value rather than from
forwarded headers. The `oauth_base_url` property at `settings.py:106` defaults
to `http://{host}:{port}`, which here would be `http://127.0.0.1:8000` — wrong
on both counts. So:

| Setting | Value |
| --- | --- |
| `BROWSER_MCP_GITHUB_OAUTH_BASE_URL` | `https://<tunnel-host>` — the public URL, not the loopback one |
| GitHub OAuth app callback | `https://<tunnel-host>/auth/callback`, replacing the loopback callback the README documents |
| `BROWSER_MCP_HOST` | `127.0.0.1`, unchanged |
| claude.ai connector URL | `https://<tunnel-host>/mcp` |

**Verify the OAuth flow before building anything else.** claude.ai requires
OAuth 2.1 with PKCE `S256` and rejects plain or missing PKCE. `GitHubProvider`
is built on FastMCP's `OAuthProxy`, which exists to bridge providers lacking
dynamic client registration — the situation claude.ai creates — so this should
work, but it decides whether the whole approach is viable and is cheap to test
against a quick tunnel first.

Three further items from [`deployment.md`](deployment.md) resolve favourably on
a single Pi, and are worth recording so they are not re-litigated:

- **§3, OAuth state.** One process means no shared-store problem. Still set
  `jwt_signing_key` explicitly from a managed secret rather than letting it
  follow the client secret, and put the store on the SSD with tight
  permissions — it holds upstream GitHub tokens at rest.
- **§4, rate limiting.** One process keeps the in-memory bucket accurate. It
  sits behind authentication by design, so unauthenticated floods should be
  limited at the Cloudflare edge, which is well placed to do it.
- **§6, shell access.** The sharpest edge, and it does not improve on a Pi:
  anyone who can run a command on the box can start the server on stdio and
  drive the browser without touching GitHub. The Ansible `app` role must
  therefore run it as a dedicated unprivileged user with no interactive login,
  and nothing else should be co-located on the host.

## Hardware preparation (Raspberry Pi 4)

| Check | Command | Required |
| --- | --- | --- |
| 64-bit OS | `uname -m` | `aarch64` — `armv7l` means a reinstall, as Playwright ships no armhf browsers |
| Bootloader | `sudo rpi-eeprom-update -a` | Needed before booting from USB |
| Power | `vcgencmd get_throttled` | `0x0` — under-voltage on an always-on service surfaces as random unexplained failures |

Pi 4 installs are frequently older than the switch to 64-bit as the default, so
the first check is worth doing before any other work.

Further notes specific to this board:

- **Storage.** The Pi 4 has no PCIe connector, so the SSD goes in a USB 3.0 port
  rather than on an NVMe HAT. An always-on Chromium writes to its profile and
  cache constantly, which is what kills SD cards, so getting off it eventually
  matters — but it doesn't have to happen before the server can run. The
  playbook in `deploy/` starts on the SD card by default and migrates onto an
  SSD once one exists; see [`deploy/README.md`](../deploy/README.md#two-phases-sd-card-first-ssd-later).
  Prefer a powered enclosure, or the official 3A PSU, whenever the SSD arrives.
- **Cooling.** The Cortex-A72 throttles around 80 °C and the Pi 4 reaches it
  under sustained load in a closed case. Chromium rendering is exactly that load
  pattern, and throttling presents as intermittently slow page loads — easily
  misread as flaky selectors. Heatsink minimum; fan preferred.
- **Memory.** Chromium wants roughly 1GB per context. 8GB is comfortable, 4GB is
  fine for one browser at a time, 2GB is tight — enable **zram** rather than a
  swap *file*, since swapping to the SD card is both slow and destructive.
- **Speed.** The A72 at 1.5–1.8GHz is roughly 2–3x slower than a Pi 5.
  Playwright's timeout defaults assume a laptop; budget accordingly.

## Provisioning with Ansible

The playbook implementing this section is in [`deploy/`](../deploy/README.md),
which also records the two ordering-driven departures from the role table below
and the mitigations no playbook can apply until the code exposes them.

Agentless, so the control node is a laptop and the Pi needs only SSH and a
system `python3`. Current Raspberry Pi OS images are rebased on Debian 13
(trixie, since October 2025) and ship Python 3.13, which comfortably satisfies
Ansible's target-side requirement; that is coincidentally the same version the
application needs, but unrelated in practice, since `uv` fetches its own
standalone arm64 build rather than using the system interpreter. An older
bookworm image (Python 3.11) still satisfies Ansible's requirement too, but see
[`deploy/roles/browser/vars/main.yml`](../deploy/roles/browser/vars/main.yml)
for the apt package names that change between the two.

The justification for automating a single host is recovery: when the SD card
fails, provisioned means one command rather than an evening rediscovering which
libraries Chromium needed.

### Roles

| Role | Responsibility |
| --- | --- |
| `base` | apt upgrade, `unattended-upgrades`, key-only SSH, timezone |
| `storage` | Mount the SSD; relocate the browser profile, cache and OAuth store off the SD card |
| `uv` | Install `uv`; `uv sync --frozen --no-dev` |
| `browser` | Explicit apt library list, then `playwright install chromium` |
| `app` | systemd unit, `EnvironmentFile`, dedicated unprivileged service account with no interactive login |
| `tunnel` | `cloudflared` (arm64), named-tunnel credentials, its own unit |
| `deploy_webhook` | The fast code-deploy path — see below |

### Implementation notes

- **Do not rely on `playwright install-deps`.** It keys off the `ID` field in
  `/etc/os-release` and has known failures on Debian-derived distributions it
  does not recognise; the common workaround is
  `install-deps || echo "install manually"`. Declaring the libraries as an
  explicit `ansible.builtin.apt` list is pinned, idempotent and immune to
  upstream changing its distro detection.
- **`playwright install chromium` is not idempotent** and will report changed on
  every run. Guard it with `creates:` on the browser path, or an explicit
  `changed_when`.
- **Do not template a `.env` file into the checkout.** `settings.py:27` reads
  `.env` relative to the working directory, and `extra="forbid"` at
  `settings.py:29` means one unrecognised `BROWSER_MCP_*` key fails at startup —
  which, under systemd restart, is a boot loop. Use a systemd `EnvironmentFile`
  owned by the service account at mode `0600`, outside the git tree. This is
  also what [`deployment.md` §5](deployment.md) asks for, and systemd
  `LoadCredential` is the better option again if the client secret can move to
  it.
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
| Serve over HTTP | `BROWSER_MCP_TRANSPORT=http` | `settings.py:33` defaults to stdio; the `run` target in the `Makefile` is stdio-only |
| Public OAuth base URL | `BROWSER_MCP_GITHUB_OAUTH_BASE_URL` | Already supported; see [above](#what-the-tunnel-changes-about-authentication) |
| Browser actions | `tools.py` | Currently only `server_info`; the deployment is untested against real browser work until these exist |
| Playbook | `deploy/` | Done: one app, one host — a second repository buys nothing at this size |

Authentication needs no code change: it landed with SDR 0001.

Two gates worth noting:

- `mypy` is scoped to `files = ["src", "tests"]` and ruff only inspects Python,
  so `deploy/` would fall outside every gate in the README's quality table.
  Adding `ansible-lint` to `make check` would keep that standard consistent.
- CI runs on x86 runners, so **arm64 is never exercised by CI**. Browser-launch
  breakage will only ever appear on the Pi. A smoke-test target runnable there
  is worth having once real actions exist.

## Fast path: webhook-triggered code deploys

Re-running the whole playbook is the right tool for infra changes — new apt
packages, systemd unit changes, tunnel config — but heavy for "one Python
file changed, ship it," and it needs someone's laptop, the vault password and
the become password every time. `deploy_webhook.py`
(`src/browser_interaction_mcp/`) is a small, dependency-free receiver the
`deploy_webhook` role installs as its own long-lived systemd service: GitHub
Actions HMAC-signs a request naming the commit that just passed CI on `main`
and POSTs it to `https://<host>/deploy-webhook` — a second, path-scoped
ingress rule on the *same* tunnel and hostname, so no new DNS record or
inbound port is involved either. On a valid signature for `refs/heads/main`
it starts a second, oneshot unit that does the actual
`git reset --hard origin/main` → `uv sync --frozen --no-dev` →
`playwright install chromium` → restart sequence (`deploy/deploy.sh`), running
as a dedicated `deploy` account that owns the checkout outright rather than
via any elevation.

Deliberately narrow: this path only ever does what a code-only change needs.
It cannot install a new apt package, change a systemd unit, or touch the
tunnel config — those still need this playbook, run by hand, exactly as
today. The two mechanisms are not merged, and are not meant to be.

## Known constraints

- **`playwright install chrome` fails on Linux arm64** — Chrome for Testing has
  no arm64 Linux build. Use bundled Chromium and never set `channel="chrome"`.
  Relevant only if a target site needs proprietary codecs or sniffs the brand.
- **Uptime becomes an operational concern.** Connector calls fail whenever the
  Pi is off or offline.
- **Headless detection.** If a target site blocks headless Chromium, the usual
  escape hatch is `xvfb-run` with a headed browser.
- **Single instance only.** Both the rate limiter and the token-verification
  cache are per-process. Do not scale this horizontally.

## Open questions

- Which Pi 4 memory variant. 1GB would warrant revisiting the approach.
- Whether `BROWSER_MCP_GITHUB_TOKEN_CACHE_SECONDS` should be shortened once the
  server is reachable from the internet, trading GitHub API calls for a smaller
  revocation window.
- Where the automated service's own credentials are stored and how they are
  refreshed when the session expires — still the largest unanswered piece, and
  the subject of [`deployment.md` §7](deployment.md).

## References

- [Custom connectors via remote MCP servers](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
- [Anthropic MCP tunnels](https://platform.claude.com/docs/en/agents-and-tools/mcp-tunnels/overview)
- [Cloudflare Tunnel](https://developers.cloudflare.com/agents/model-context-protocol/guides/remote-mcp-server/)
- [Playwright supported platforms](https://playwright.dev/python/docs/browsers)
- [FastMCP OAuth proxy](https://gofastmcp.com/servers/auth/oauth-proxy)
