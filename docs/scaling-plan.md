# Scaling to many sites: architecture and plan

The POC has answered the questions it existed to answer. One site is automated,
it runs on the Pi behind a named tunnel, it authenticates as exactly one GitHub
account, it holds a real logged-in session without ever holding a password, and
a green `main` deploys itself. What it has never had to answer is *what happens
when there are five of these*.

This document is the plan for that. The requirements, as stated:

- **R1 — Spin out a new site fast.** New site to be automated → new codebase,
  new deployment, minimal ceremony.
- **R2 — Blast-radius isolation.** An exposure in one application must not leak
  the credentials, sessions or tokens of another. Files and process owned by a
  separate user per app.
- **R3 — Playwright codegen as the input.** Record an interaction by hand,
  upload it, get a tool.
- **R4 — Maintenance stays cheap.** A fix made once should reach every app.
  Common functionality pulled out — a shared library, or something better.
- **R5 — Self-healing generalises.** The mechanism in
  [`self-healing.md`](self-healing.md) and
  [`self-healing-plan.md`](self-healing-plan.md) has to work across the fleet,
  not be rebuilt per site.

The invariants that survive unchanged, and are not re-argued: only pre-approved,
in-code browser actions are exposed; no free-form URL, selector or script ever
reaches a tool parameter; every tool call is authenticated as one GitHub account;
the server never holds a third-party password except on the deliberate,
documented `/…-login` path.

## What "one of everything" currently costs

The POC has exactly one of each thing. Before deciding what to change, it is
worth being precise about which of those are genuinely site-specific and which
are one-of-a-kind only because there has only ever been one site.

| Component | Site-specific? | Notes |
| --- | --- | --- |
| `server.py`, `auth.py`, `middleware.py`, `redaction.py`, `settings.py`, `__main__.py` | **No** — none of it mentions Sainsbury's | ~700 lines that every future app needs verbatim. `redaction.build_redactor` walks `Settings` for `SecretStr` fields, so a site's own settings must subclass a core `Settings` to stay covered |
| `browser.py` | **No** | Xvfb lifecycle, headed/headless choice, `storage_state` seeding, consent-cookie injection. Entirely generic already |
| `login_routes.py`, `login_oauth.py`, `sainsburys_login_flow.py`, `sainsburys_login_worker.py` | **Mostly no** | ~670 lines of state machine, OTP parking, subprocess supervision and GitHub-gated browser page. The site-specific part is the handful of form steps the worker performs |
| `deploy_webhook.py` | **No** | HMAC verification, size cap, unit trigger. The unit name already comes from the environment |
| `sainsburys.py` | **Partly** | 815 lines. The locators, URLs, login steps and "am I logged in" probe are Sainsbury's. `_wait_for_page_to_settle`, `_readable_matches`' bounded-per-item read, the ellipsis-prefix name match and `_authenticated_page`'s shape are patterns, not facts about Sainsbury's — and [`site-automation-gotchas.md`](site-automation-gotchas.md) already says so |
| `tools.py` | **Yes**, by design | The approval surface. One function per approved action |
| `deploy/roles/{base,storage,uv,browser,tunnel}` | **No** | Provision a host, not an app |
| `deploy/roles/{app,deploy_account,deploy_webhook}` | **Singular, not specific** | Every path, user, unit name and port is hardcoded to one app in `vars.yml`. This is the part that has to become plural |
| The GitHub OAuth app, the tunnel hostname, the vault | **Singular** | One client secret, one hostname, one `vault.yml` |

So: roughly 85% of `src/` is generic, and the deployment is generic-but-singular.
Nothing here needs rewriting. It needs **splitting along seams that already
exist** and then **parameterising over a list**.

---

## The shape

One repository, a `uv` workspace, and one inventory file inside it.

```
browser-mcp/                 one repo, private from the first committed fixture
  pyproject.toml               [tool.uv.workspace] members = ["packages/*"]
  uv.lock                      one lockfile: one pip-audit, one dependency story
  packages/
    core/                      server, auth, browser, login framework,
                                 redaction, failure capture, deploy webhook
    sainsburys/                locators.py, site.py, tools.py
    ocado/                     …one directory per site
  fleet/                       Ansible, and apps.yml
```

`fleet/apps.yml` is the single source of truth for the deployment:

```yaml
apps:
  sainsburys:
    package: sainsburys       # packages/<name>, not a repo URL
    hostname: sainsburys.mcp.example.com
    port: 8001
    webhook_port: 8801
    headed: true              # this site blocks headless Chromium
    memory_high: 900M
```

Adding a site is one pull request: a new directory under `packages/`, six lines
in `apps.yml`, then a playbook run. The code and the deployment that runs it
arrive together, atomically, which they cannot do when they live in different
repositories.

**One playbook run covers the whole fleet.** The host-level roles (`base`,
`storage`, `uv`, `browser`, `tunnel`) run once for the machine; the per-app roles
loop over `apps.yml`. There is no per-app playbook and no per-app invocation —
`ansible-playbook site.yml` provisions every app that file names, and adding one
is a diff to that file rather than a new deployment to stand up. Anything Ansible
can generate rather than be told, it should: the deploy webhook's HMAC secret is
a random string with no meaning outside the host, so it is generated and written
straight to the two places that need it, never typed into the vault.

### D1 — A monorepo workspace, not a repo per site

**This decision was made the other way first, and reversed.** The reasoning that
reversed it is worth keeping, because it is the same reasoning as D2's.

| Option | Verdict |
| --- | --- |
| **One `uv` workspace: core + every site + the fleet** | **Chosen.** One gate configuration, one lockfile, and — the deciding factor — promoting a helper from a site into core is a move plus an import change in a single commit |
| Repo per site + core as a pinned library | Rejected, but live. It buys one thing nothing else can: credentials scoped to one site's code. See the cost below |
| One process, sites as plugins | Rejected outright. It fails R2 at the first line: one process means one `storage_state` directory, one memory space, one compromise |

**R2 is unaffected either way, and that is the thing most easily got wrong.**
Isolation lives in the accounts, units and file modes on the host, not in where
the source is kept. Every app still gets `bmcp-<slug>`, `bmcpd-<slug>`, its own
checkout, its own venv (`uv sync --frozen --package <slug>`, which uv supports
natively) and its own `0700` state directory. Nothing in D3 changes.

**Why the reversal.** D2 says *extract on the second occurrence, not the first* —
which is a bet that you do not yet know where the seam goes, and that site two is
what will tell you. A version boundary between core and the sites is a commitment
to already knowing: with pinned releases, promoting a helper means add to core →
tag → release → bump site A → bump site B → delete both copies. That friction
falls hardest on exactly the refactoring R4 depends on, so the split world makes
this plan's own maintenance strategy expensive to carry out. Splitting before
site two exists locks in a guess behind a boundary designed to make guesses
costly to revise.

Three smaller things follow the same way:

- **One gate configuration.** The reusable workflow shares the CI *runner*, but
  Python has no clean way to share the *config*: `[tool.ruff]` with
  `select = ["ALL"]`, mypy strict plus six extra error codes, 100% line and
  branch coverage, `filterwarnings = ["error"]` — each repo would carry its own
  copy of that block, and they drift. Here it is written once.
- **One lockfile.** One `pip-audit`, one Dependabot stream, and no way for site A
  to sit on a vulnerable Playwright while site B is patched.
- **Reading across sites.** With five sites you genuinely want to compare how
  Ocado's consent banner was handled against Sainsbury's. One checkout, one grep
  — and that is how site N gets written, by reading sites 1 to N−1.

**What it costs, stated plainly.** GitHub has ref-scoped tokens, not path-scoped
ones. The self-healing plan calls the Pi's `claude/heal-*` write credential "a
genuine widening" on the machine that holds the browser profiles; in a monorepo
that widening covers every site's code rather than one. Branch protection still
keeps it off `main` and a human still merges, so the containment is unchanged —
but the reach is wider, and that is the price of this decision. It is the one
property repo-per-site has that nothing here replaces.

**Two new pieces of work the monorepo introduces**, neither large, both real:

- **The deploy path needs to know which apps a commit affects.** A commit under
  `packages/<slug>/` restarts that app; one under `packages/core/` restarts all
  of them. That is a path check in the CI job that signs the webhook, not new
  infrastructure — but it did not exist before.
- **Path-filtered CI needs a gate job.** A required check that gets skipped by a
  path filter never reports, and blocks the merge rather than passing it. The
  standard shape is one always-running job that depends on the filtered ones.

**Optional, and cheap: sparse per-app checkouts.** `git sparse-checkout set
packages/core packages/<slug>` gives each deploy account only the code it runs,
recovering most of what repo-per-site offered here. Verified to work with the
workspace: `uv sync --frozen --package <slug>` resolves happily with sibling
members absent from disk, because `--frozen` reads the committed lock rather
than re-resolving. Only `uv lock` needs the whole tree, and the host never locks
— `deploy.sh` has always run `--frozen`. Worth doing, though reading another
app's *source* is not a meaningful exposure: the secrets are in the per-app
environment file and `storage_state`, and those were never in the repository.

**The repository goes private before the first committed fixture.** The
self-healing plan spends its longest section on what a public repository costs —
every committed fixture is a permanent, world-readable copy of a page from a
logged-in account. That constraint does not bind until a fixture exists, which
is self-healing stage 2, so there is a precise trigger rather than a vague
intention: **public is fine until the first fixture lands, and not after.** From
then the bar drops back from *am I publishing this* to *would I show a
colleague*, and the plan's "private-repository fallback" is simply the default.

If publishing `core` as a library ever becomes a goal in its own right, it is a
`git filter-repo` away — and that is the moment to pay for the version boundary,
with the seam already known, rather than now.

### D2 — What goes in the library, and what deliberately does not yet

Extract now, because it contains no site-specific content and never will:

- `server.py` — becomes `build_server(site: SiteModule, settings: Settings)`
- `auth.py`, `middleware.py`, `redaction.py`, `__main__.py` — verbatim
- `settings.py` — becomes `CoreSettings`, which each site subclasses to add its
  own fields. The env prefix moves from `BROWSER_MCP_` to a per-app value so two
  apps' environment files can never collide
- `browser.py` — verbatim
- The login framework — `LoginFlow`, the worker supervisor, `login_routes`,
  `login_oauth`, with the site supplying a `LoginSteps` implementation (fill
  username, fill password, submit, is-OTP-showing, fill OTP, confirm)
- `deploy_webhook.py` — verbatim
- **Failure capture** (`failure_capture.py`, `dom_redaction.py`) — self-healing
  stage 1, written once here rather than five times
- Nothing needs a *reusable* CI workflow any more: there is one workflow and one
  `[tool.*]` block for the whole workspace, which is the same benefit without the
  indirection

Do **not** extract yet, even though it is tempting:

- The page-interaction helpers — consent-cookie seeding, `_wait_for_page_to_settle`,
  the bounded per-item grid read, the ellipsis-prefix match. These are patterns
  learned from *one* site. Generalising them from a single example produces an
  abstraction shaped like Sainsbury's with parameters bolted on, which is worse
  than a copy. Ship them in the template as a `_helpers.py` the generated repo
  owns outright, let site two either use them or diverge, and **promote to the
  library what two sites demonstrably share.**

That rule — *extract on the second occurrence, not the first* — is the main thing
protecting R4 from becoming its own maintenance burden. `site-automation-gotchas.md`
is currently doing exactly the right job for these: it is knowledge, written down,
with no premature API attached.

### D3 — Runtime isolation: two accounts per app

Per app, Ansible creates:

| Account | Owns | Can |
| --- | --- | --- |
| `bmcp-<slug>` (nologin, locked) | `/var/lib/browser-mcp/<slug>` (0700) — state, browser profile, `storage_state`, OAuth store | Run the service. **Read** its checkout; never write it |
| `bmcpd-<slug>` (nologin, locked) | `/opt/browser-mcp/<slug>` — the checkout, its `.venv`, its uv cache | Pull, sync, and `systemctl restart browser-mcp@<slug>.service` — one sudoers line, exact argv, no wildcards, as today |

**Two accounts, not one shared deploy account.** A single `deploy` user that can
write every checkout and restart every unit is arbitrary code execution as every
service account: it would undo R2 completely while looking like it satisfied it.
The extra account per app is nearly free — Ansible generates it — and it is what
makes the isolation claim true rather than decorative.

Everything else follows from the existing playbook, made plural:

- **systemd template units.** `browser-mcp@.service`, `browser-mcp-deploy@.service`
  and `browser-mcp-webhook@.service` — one file each, instantiated per slug.
  `User=bmcp-%i`, `EnvironmentFile=/etc/browser-mcp/%i/env`,
  `ReadWritePaths=/var/lib/browser-mcp/%i`, `WorkingDirectory=/var/lib/browser-mcp/%i`.
  The sandboxing block in `app.service.j2` carries over unchanged, including its
  comment about why `MemoryDenyWriteExecute`, `RestrictNamespaces` and
  `SystemCallFilter` are absent for Chromium's sake.
- **Shared, root-owned, read-only: the Playwright browsers directory.** ~400MB
  that must not be duplicated N times on a Pi, and which no service account may
  replace the binary inside. This is already how `roles/storage` sets it up.
- **Per-app uv cache**, not shared. A shared cache is writable by every deploy
  account, and poisoning it gets code executed in another app's next sync. Disk
  cost is small next to the browsers; the isolation is the point.
- **Encryption at rest for `storage_state` becomes less urgent, not more.**
  `deployment.md` §7 flags the plaintext session file as the thing actually worth
  stealing. The per-user split is what finally makes its `0700` mean something:
  today "owner-only" and "the one service account" are the same set, so the mode
  bits protect against nothing on this host.

**Considered and deferred: rootless Podman, one container per app.** Genuinely
stronger — per-app filesystem, trivial resource limits, and, uniquely, an easy
path to *per-app network egress restriction* (app A can reach only
`sainsburys.co.uk`), which the user model does poorly. Deferred because: the
existing playbook already delivers 80% of the user model; the shared browsers
directory is a real Pi constraint that images work against; and rootless
containers plus Xvfb plus Chromium's user-namespace sandbox on arm64 is a
yak-shave that buys nothing against the stated threat. Revisit if the app count
passes ~5 or if egress control becomes a requirement. A cheap middle path exists
if it does: nftables `meta skuid` rules, per-app, allowlisting outbound 443 —
coarse (IP, not domain) but honest and about ten lines.

### D4 — Routing: one tunnel per host, one subdomain per app

One named tunnel, ingress rules generated by looping `apps.yml`:

```yaml
ingress:
  - hostname: sainsburys.mcp.example.com
    path: ^/deploy-webhook$
    service: http://127.0.0.1:8801
  - hostname: sainsburys.mcp.example.com
    service: http://127.0.0.1:8001
  # … per app …
  - service: http_status:404
```

For a single host, a **wildcard DNS record** `*.mcp.example.com` pointed at
`<tunnel-id>.cfargotunnel.com` removes the per-app `cloudflared tunnel route dns`
step — the one manual post-task the playbook currently prints — for every future
app.

**That trick does not survive a second Pi** (D9): a wildcard record names one
tunnel, and each host runs its own. The replacement is better anyway, and the
domain is already on Cloudflare — have Ansible create each app's CNAME to
`<tunnel-id>.cfargotunnel.com` through the Cloudflare API
(`community.general.cloudflare_dns`) from `apps.yml`, so the record is generated
rather than typed and points at whichever host that app is assigned to. That
keeps each app's URL stable when it moves between hosts, which matters because
claude.ai stores the connector URL and re-adding one is manual. Do it this way
from the start; the wildcard is a shortcut that has to be unwound later.

Scope the API token to **Zone → DNS → Edit on that single zone**, and vault it
like any other credential. It is fleet-wide rather than per-app — it is the one
new secret this introduces, and the one credential in the whole design that can
affect a name outside the host it runs on, so it is worth the narrow scope.

Ports are explicit in `apps.yml` rather than derived, with a playbook `assert`
that they are unique. Any local account can reach any app's loopback port, which
is why authentication is on every request and not on the network position; the
ports are defence in depth, not the gate. Unix sockets would remove even that,
but FastMCP does not expose a UDS bind today — worth revisiting if it does.

**One GitHub OAuth app for the whole fleet, not one per site.** This was written
the other way first, on the assumption that an OAuth App takes a single callback
URL. It no longer does: as of
[14 August 2026](https://github.blog/changelog/2026-08-14-multiple-redirect-uris-and-token-refresh-for-oauth-apps/)
an OAuth App may register **up to ten redirect URIs**, and each one can
optionally enable **wildcard matching** across subdomains and subpaths.

Each app needs two callbacks — the MCP endpoint's `/auth/callback` and the login
page's — so ten URIs covers five apps with explicit, auditable URLs. That is the
recommended setting: **adding a site becomes editing one app's URI list rather
than registering a new app**, and the wildcard's "any subdomain or subpath of
this" property stays switched off, which matters here because a wildcard redirect
sitting behind a wildcard DNS record is a wider grant than it looks. Past five
apps, turn wildcard matching on for `https://*.mcp.example.com/` and the step
disappears entirely.

Note the changelog's own warning while doing it: **an app with only one redirect
URI has wildcard matching on by default** — legacy behaviour, now visible and
controllable. The existing single-callback app almost certainly has it enabled;
worth turning off.

What this costs is one client secret shared across every app, which the earlier
draft refused. Worth being precise about what that secret actually protects: it
authenticates the *server* to GitHub during the code exchange, and this server
additionally checks the token's `sub` against one numeric user ID and requests no
scopes. An attacker holding the secret still cannot mint a token this server
accepts without the operator completing a GitHub flow for them. It is a
phishing-grade exposure, not a direct-access one — a fair trade for deleting a
manual step from every future site. Cloudflare Access in front of the whole
`*.mcp` zone remains worth adding as an independent second gate; it does not
replace this one, because the claude.ai connector runs the OAuth flow itself.

### D5 — Deploy: one webhook instance per app

Today: one receiver, one secret, one hardcoded oneshot unit. Multiplied naively,
a single receiver would need sudo rights to restart every app — re-centralising
exactly the privilege D3 just split.

Instead: `browser-mcp-webhook@<slug>.service`, running as `bmcpd-<slug>`, with
its own HMAC secret and its own single sudoers line. Uniform with everything
else, no shared trust, and the code is unchanged — `deploy_webhook.py` already
takes its unit name and secret from the environment. Each is a ~15MB idle Python
process; if that stops being acceptable, systemd socket activation makes them
cost nothing until a push lands.

`deploy.sh` and `deploy-branch.sh` carry over per app, parameterised by slug.
`deploy-branch.sh`'s stance stands unchanged and gets more important with a
fleet: **never wire it to anything automated** — the interactive sudo is the
authorisation check.

### D6 — Secrets

Keep ansible-vault; restructure it per app. `inventory/host_vars/<slug>/vault.yml`
holds that app's OAuth client id and secret, its webhook secret, and any site
account username — the values `local.yml` currently keeps out of git for being
tied to a real person. The playbook asserts, per app, that its vault entries
exist and are not the example placeholders, exactly as `site.yml` does today for
the tunnel UUID.

This scales linearly and needs no new tooling. `sops` + `age` would be nicer for
per-app key separation and diffable encrypted files, but it is a migration for
its own sake until there is a second person with access.

### D7 — A Pi runs out of memory before it runs out of anything else

This is the constraint most likely to bite first, and it is a *fleet* problem
that does not exist with one app. A headed Chromium under Xvfb is 300–500MB. Five
apps that each launch one on a tool call is an OOM kill, and the process the
kernel picks will not be the one that caused it.

- A shared `browser-mcp.slice` with a global `MemoryMax`, and per-app `MemoryHigh`
  from `apps.yml`. Reclaim pressure before kills. These are also what D9's
  placement assertion sums: memory is the thing that decides how many apps a host
  can hold, and therefore when a second host is needed at all.
- `headed: true` per app in `apps.yml`, not a global default. `browser.py`'s
  docstring is already emphatic that headless is the cheaper default and headed
  is opted into per action; the fleet config should make that visible per app.
- Accept, for now, that concurrent cross-app browser use is rare: one operator,
  one conversation, rate-limited to 1 call/second server-wide. If that stops
  being true, the fix is a shared advisory lock — a single "one browser at a time
  on this host" semaphore in a shared directory — not more RAM.
- Sequence the nightly smoke tests (D8) rather than firing them together, for the
  same reason.

### D8 — A detector, so failures are noticed by the fleet and not by the operator

With one app, a stale selector is noticed the next time it is used. With five,
some app is always quietly broken. A per-app `browser-mcp-smoke@<slug>.timer`
runs that app's cheapest read-only action nightly, staggered, on the Pi — the only
place with a real session and a real route to the site. Its failure is the trigger
that feeds self-healing, which turns the heal loop from "wait until a human trips
over it" into "notice within a day".

---

### D9 — More than one Pi: shard, don't cluster

`deploy/inventory/hosts.yml` says one host, deliberately, because the tool-call
rate limiter and the GitHub token cache both live in process memory. That
reasoning is sound and frequently misread — including by an earlier draft of this
document, which concluded a second Pi would be "a second fleet". It would not.
The constraint is about **replicating one app**, not about **owning one host**:

- **No app runs on two hosts.** Two instances would double the effective rate
  limit against the target site, each would hold its own `storage_state`, and two
  browsers replaying the same session from different IPs is exactly the shape a
  site's own fraud detection is built to notice. This is a hard rule.
- **Different apps run on different hosts, freely.** Nothing is shared between
  two apps that has to be co-located — D3 spent its effort making sure of that —
  so the boundary that already separates two apps on one Pi separates them just
  as well across two.

So the fleet **shards**. `apps.yml` gains a `host:` field, the inventory grows a
second entry, and the per-app roles select their work with
`apps | dict2items | selectattr('value.host', 'eq', inventory_hostname)`.
Everything else about an app is unchanged by which machine it lands on.

**Build it this way at stage 3 even with one Pi.** A `host:` field on a
single-host fleet costs nothing and is read as `pi-01` everywhere; retrofitting
placement into an inventory, a tunnel config, a DNS strategy and a CI notify step
after the fact is a different and much worse afternoon. One host is just N=1.

What the three tiers hold:

| Tier | What lives there |
| --- | --- |
| **Fleet-wide** | The repository, the one OAuth app and its redirect URIs, CI, and `apps.yml` itself — none of which multiply with hosts |
| **Per host** | `cloudflared` with **its own named tunnel and credentials**, the shared Playwright browsers directory, the uv cache, and the `base`/`storage` roles. The vault grows a section per host for the tunnel credentials |
| **Per app** | Everything in D3 — the two accounts, the checkout, the venv, the `0700` state, the systemd units, the webhook instance — on exactly one host |

**One tunnel per host, not one tunnel with replicas.** Cloudflare does support
running a named tunnel on several machines, but it distributes requests across
the connectors, which is right for replicas of one origin and wrong here: an app
exists on one host, so a request landing on the other connector finds nothing.
Rejected for the same reason: a "front" Pi terminating one tunnel and reverse
proxying to the others over the LAN — it puts every app's traffic through one
shared component in plaintext, which gives back the isolation D3 just bought.

**Placement is decided by memory, and should be asserted rather than hoped.**
D7's headed-Chromium footprint is what actually fills a Pi. Give each host a
declared budget in the inventory and add a `pre_task` that sums `memory_high`
across the apps assigned to it and fails if the total exceeds it. That turns
"will this fit?" from something discovered at 3am into something the playbook
refuses to do.

**Moving an app between hosts is a real operation, not a config edit.** Its
`storage_state` lives on its current host, so a move is: stop, copy the session
across with its permissions intact, re-point DNS, start. The same care stage 5's
backups exist for, and the same failure if it goes wrong — an MFA re-login. Worth
deciding placement deliberately once rather than shuffling.

**CI fans out rather than changing shape.** It already computes which apps a
commit affects; it now maps each to that app's own webhook URL, which comes from
`apps.yml` like everything else. A change to `packages/core/` becomes N signed
requests across however many hosts, instead of one.

---

## R3 — The codegen intake pipeline

The honest framing first: **a codegen recording is an input to a code generator,
not a runtime artifact.** Nothing interprets a recording at runtime — that would
be the free-form-script escape hatch the whole design exists to refuse. The
recording is turned into reviewed, committed code, and the review is where the
approval boundary lives.

What codegen gives you: a flat script of real, verified locators against the real
page, captured from a session that actually worked. That is exactly the scarce
thing — `sainsburys.py`'s module docstring records that a codegen recording
corrected four wrong assumptions the login flow had made from reading the public
site.

What it does not give you: parameterisation, the login/action split, error
handling, or a single one of the patterns in `site-automation-gotchas.md`. Codegen
emits `wait_until="load"` waits and unbounded `.nth(1)` reads — the precise two
things that doc says take a call down.

The pipeline:

```
playwright codegen --save-storage=state.json https://site
        │  operator records login, then records the action
        ▼
bmcp new-site <slug>            scaffolds packages/<slug>/ in the workspace
        ▼
bmcp import-recording rec.py    parse the script → emit:
        │                         • locators.py   (a row per element touched)
        │                         • site.py       (draft steps, TODOs at each
        │                           point a value should become a parameter)
        │                         • tools.py      (one tool stub per recorded
        │                           flow, with the docstring skeleton)
        ▼
Claude Code session             fills the TODOs, applies the gotchas, writes
        │                         the tests. Same tooling as the heal loop
        ▼
PR → CI → real-site run → merge → deploy
```

Two things make this more than a wish:

- **The importer's output target is the locator table**, which self-healing
  stage 0 requires anyway. `locators.py` is a table keyed by locator id, each row
  saying how to address one element plus a one-line description. A codegen script
  is *already* a list of addressed elements — extracting it into that table is a
  mechanical transform, and it means authoring and healing produce diffs of the
  same shape, reviewed the same way, replayed by the same harness. Build the table
  once and both R3 and R5 are served by it.
- **Authoring and healing are the same problem** — "write a locator for this
  element on this page" — so they should share the primitives, the replay harness
  and the fixture format. The difference is only where the evidence comes from: a
  fresh recording, or a failure bundle.

The step that cannot be automated away is the parameterisation decision: *which
literal in this recording is a tool argument, and which is fixed?* That is the
approval boundary itself. A human decides it, at review, on a diff.

---

## R5 — Self-healing across a fleet

[`self-healing-plan.md`](self-healing-plan.md) is written for one repository, one
app and one public git history. Most of it transfers unchanged; the parts that do
not are listed here, so the plan is not silently invalidated by the split.

**What the split makes easier:**

- **Going private removes the plan's hardest constraint.** Its longest section is
  about publishing fixtures from a logged-in account to a world-readable repo, and
  it names "run the heal loop in a private repository" as the fallback if fixtures
  cannot be cut down far enough. D1's trigger — private before the first committed
  fixture — makes that the default. Subtree-only fixtures and cropped CI images
  remain good practice, because less to redact is less to get wrong, but they stop
  being the thing the whole design hangs on.
- **The stage 4 write credential does *not* get better, and this is the cost of
  D1.** The plan flags a genuine widening: for the host to push a fixture branch
  itself it needs a write token, and its deploy credential is read-only today. In
  a monorepo that token reaches every site's code, not one. Ref-scoping to
  `claude/heal-*` and branch protection still keep it off `main`, and a human
  still merges — the containment holds, the reach is wider. Accepted knowingly;
  see D1.
- **Per-app quarantine is already per-app.** The plan's cap — one open PR per
  fingerprint, quarantine the tool on a repeat failure — now quarantines one app's
  tool without touching the rest of the fleet.

**What has to change:**

| Plan says | With a fleet |
| --- | --- |
| Stage 0's `locators.py` and the `claude/heal-*` diff-surface CI check live in this repo | One `locators.py` per site package. The check becomes path-aware — a heal branch may touch `packages/<slug>/locators.py` and that site's fixtures, nothing else — and there is still only one implementation of it |
| Fingerprint = tool + locator id + failure class | Prefix it with the **app slug**. Two sites will have a `login.username` |
| One healing cloud environment, network `Custom`, package registries only | Still one environment — but the allowlist must exclude **every** target site, and it is now a list that grows silently as apps are added. Make "the new site's domain is not on the healing allowlist" a line on the new-site checklist |
| "Empty connectors" on the healing environment | **More load-bearing, not less.** The plan already notes this account has the POC's own MCP server connected, and that a healing session holding it can drive the real site with the network level still reading `None`. Every new app is another connector on the same account, so this rule now guards N routes instead of one |
| Bundles are fetched by hand over SSH from `/var/lib/browser-interaction-mcp` | Per app: `/var/lib/browser-mcp/<slug>/bundles`, owner-only to `bmcp-<slug>`. The manual hop still works and is still the thing enforcing a human redaction review before stage 4 |
| The baseline rule: a locator is healable only once its action has succeeded against the real site | **Now doing real work.** A site freshly imported from a codegen recording has *no* proven locators, so nothing in it is healable until a first real run promotes them. This is what stops the healer from generating confident guesses about automation that never worked — which is exactly the failure mode a fast new-site pipeline would otherwise mass-produce |
| Failures are noticed when a tool is called | D8's nightly smoke timer is the fleet's detector, and its output is a failure bundle in the plan's existing format |

Nothing above changes the three invariants (**I1** the healing agent never runs
against the real site, **I2** it never changes what the server executes, **I3**
failure evidence is as sensitive as the browser profile). The isolation work
leaves I1 and I2 exactly as they were, and strengthens I3: the evidence now sits
behind a `0700` directory owned by an account shared with no other app. The one
thing that moves the wrong way is the reach of the stage 4 write token, above.

---

## Stages

Each is independently useful, independently reviewable, and leaves a working
deployment behind. Stage 0 of the self-healing plan is folded into stage 2 here,
because the locator table is a prerequisite for both plans and should be built
once. There were six stages before D1 was reversed; dropping the repository split
removed one of them outright.

### Stage 1 — Restructure into a workspace, no deployment change

Move `src/browser_interaction_mcp/` into `packages/core/` and
`packages/sainsburys/`, with a workspace root `pyproject.toml` and one
`uv.lock`. Same tests, same gates, same systemd unit, same Pi. Nothing about the
deployment learns anything happened.

This proves the seam is where I think it is before anything depends on it, and it
is cheap to undo. `settings.py` becomes `CoreSettings` with a subclass;
`server.py` takes the site as a parameter; the login flow takes a `LoginSteps`
implementation. The page-interaction helpers stay in `packages/sainsburys/` per
D2. The unit's `ExecStart` moves to the venv that `uv sync --package sainsburys`
builds.

**Done when:** `make check` passes with 100% coverage and no behaviour change,
and `packages/sainsburys/` imports nothing from `packages/core/` that mentions a
grocer.

### Stage 2 — The locator table

Self-healing stage 0, done here because the codegen importer targets it too. A
`locators.py` table keyed by locator id, a `resolve()` function, no inline
locators anywhere in `site.py`, and the `claude/heal-*` diff-surface check in CI
— path-aware, so it names `packages/<slug>/locators.py` rather than a bare path.
Timeouts, consent-cookie shapes, business-logic filters and URLs stay out of the
table, for the reasons the self-healing plan gives.

**Done when:** `make check` passes with no behaviour change, and a deliberately
out-of-surface commit on a test heal branch is rejected.

### Stage 3 — The fleet layer, and cut Sainsbury's over

`deploy/` becomes `fleet/`, with `apps.yml` and the roles made plural: per-app
users, systemd template units, per-app env files, per-app webhook instances,
generated tunnel ingress, the shared slice with memory limits, and Ansible-created
per-app DNS records. The deploy path learns which package a commit touched, and
the CI gate job that path filtering requires goes in at the same time.

**Build it multi-host-shaped here** (D9), even though there is one Pi: `apps.yml`
carries a `host:` field, the per-app roles select on it, and the per-host tier —
tunnel, browsers, cache — is separated from the fleet-wide one. It costs nothing
at N=1 and is expensive to retrofit.

Then migrate the one existing app: new accounts, move
`/var/lib/browser-interaction-mcp` → `/var/lib/browser-mcp/sainsburys` with
`storage_state` intact, start `browser-mcp@sainsburys.service`, verify the
connector still works and the session still authenticates, retire the old unit.

The migration is the risky step in this whole plan, because a moved
`storage_state` that loses its permissions or its contents means a real re-login
on a site with MFA. Rehearse it with `--check --diff`; keep the old unit
installed-but-stopped until the new one has served a real tool call.

**Done when:** claude.ai calls `sainsburys_search` against the new unit, and
`ps -o user=` shows `bmcp-sainsburys`.

### Stage 4 — `bmcp new-site`, proven by a second site

The scaffold command and the codegen importer — a generator that writes a new
directory under `packages/`, not a repository template, which is most of why
this is smaller than it was. Then — and this is the actual acceptance test —
**automate a second, genuinely different site end to end using only that
pipeline.** Not a hypothetical second site; a real one, chosen because you want
it.

Site two is what tells you whether the library is right. Expect it to be wrong in
at least two places, and treat fixing those as part of this stage rather than as
a regression. In particular, expect the login framework's `LoginSteps` shape to
be Sainsbury's-shaped, and expect at least one of the "deliberately not
extracted" helpers to earn promotion — which is now a move and an import change
rather than a release cycle, and is the whole reason D1 went the way it did.

**Done when:** site two is live on the Pi, under its own accounts, and the
*ceremony* — package, accounts, units, routes — took minutes rather than a day.
Not the whole job: see "What it actually costs to add a site" below for why that
is a narrower claim than it sounds.

### Stage 5 — The fleet's ongoing life

Self-healing stages 1–4 from the existing plan, now built once in core and
inherited by every site. The nightly smoke timers. Backups of every app's
`storage_state` (they are the expensive thing to recreate — MFA, by hand, per
site). A one-page fleet status view: per app, last successful smoke run, the
commit it is running, and any open heal PRs.

---

## What it actually costs to add a site

Everything above attacks the ceremony: repos, accounts, units, routes, CI.
That work is real and worth removing, and it is **not where the time goes**.
Worth saying plainly, because a plan that only measures what it improves is
how "adding a site is six lines of YAML" turns into a promise nobody can keep.

Two tracks run in parallel for every new site. The architecture collapses the
first one and does nothing at all to the second, which is the critical path.

| | Friction | Why it bites |
| --- | --- | --- |
| **Removed** | Repo, CI config, gates, package skeleton | `bmcp new-site` writes a directory under `packages/`. There is no repository to create, no CI to configure and no gate config to copy — the workspace already has one of each |
| **Removed** | Accounts, units, env files, webhook, tunnel ingress | Six lines in `apps.yml` and one playbook run |
| **Removed** | The DNS record | A wildcard `*.mcp` record means there is no per-site DNS step at all |
| **One-off** | Two callback URLs added to the fleet's OAuth app | Editing one app's URI list, not registering a new app — see D4. A mismatch still fails opaquely at first connect |
| **One-off** | The connector in claude.ai | A UI action, once per site, and the one step with no automatable path at all. The vault barely features any more: the client id and secret are fleet-wide, the webhook secret is generated by Ansible rather than typed, so all that is left per site is the account username — and only for sites that log in |
| **Irreducible** | Does the site block headless Chromium? | Sainsbury's and Tesco both run Akamai Bot Manager, which blocks *headless* specifically, regardless of network origin or user agent. You find out by trying. It decides `headed: true`, which decides ~400MB, which decides whether the Pi has room for app N+1 at all |
| **Reduced** | The consent banner's real shape | The market is concentrated — OneTrust, Cookiebot, Didomi, Sourcepoint, Usercentrics, TrustArc — and many implement IAB TCF, which exposes a standard `window.__tcfapi`. So core carries a **CMP registry**: per CMP, a detection fingerprint, the cookies to pre-seed for reject-all, and the fallback selectors. A `bmcp probe <url>` command opens the page and reports which one a site uses, turning research into confirmation. What stays per-site is the *values* — OneTrust's `OptanonConsent` encodes group ids configured per site, and the registrable domain differs — read once from the site's own cookie-declaration table |
| **Irreducible** | What the login actually does | Not knowable from the outside. The Sainsbury's recording corrected four assumptions the login flow had *already made* from reading the public site: the login path, the consent copy, MFA living on a separate domain and not always appearing, and adding straight from the result tile |
| **Irreducible** | Whether MFA always appears | On Sainsbury's it doesn't. That single fact is why the login runs in a parked subprocess with a 300-second OTP wait rather than inside a request handler — a site with unconditional MFA would have been a much smaller design |
| **Irreducible** | Designing the tool surface | The real work, and it is design rather than transcription. `add_to_basket` went blind-first-result → exact name → name-or-id → ellipsis-prefix fallback, and needed `sainsburys_search` to exist at all, because an index into a result list goes stale between two calls |
| **Reduced** | Codegen output is the wrong shape | It emits `wait_until="load"`, unreliable when a background poller keeps the network busy, and unbounded list reads on grids whose non-product tiles block for Playwright's full 30-second default and take the whole call down — the two patterns [`site-automation-gotchas.md`](site-automation-gotchas.md) names first. Split in two: an AST script does the mechanical extraction, and a **skill** carries the judgment. See below |
| **Irreducible** | One real run against the live site | `deploy-branch.sh`, SSH, run it. Nothing counts before this, and it is also the gate that promotes the site's locators into the healable set |

### Shaping the recording: a script and a skill, not one importer

The importer was described above as a single tool. It should be two, split on
whether the work has a right answer:

- **An AST script does the mechanical half.** Codegen output is just Playwright
  calls, so extracting every addressed element into a `locators.py` row,
  hoisting the recording's literals into named constants and emitting the
  `site.py` / `tools.py` skeletons is a deterministic transform. A script does
  that better and more repeatably than a model.
- **A skill carries the judgment half** — which literal becomes a tool
  parameter, applying every pattern in
  [`site-automation-gotchas.md`](site-automation-gotchas.md), splitting the
  login prefix from the action, writing docstrings that actually teach a model
  how to call the tool, and the `Page`-faking tests that the coverage gate
  demands.

The useful observation is that **`site-automation-gotchas.md` is already written
as skill content** — concrete failure patterns, each with the fix — and simply
is not wired up as one. Making it `.claude/skills/site-automation/SKILL.md` turns
knowledge that has to be remembered into knowledge that gets applied, and it is
close to zero new writing.

**The self-healing agent loads the same skill.** That follows directly from R3
and R5 being the same problem: if authoring and healing both write locators
against the same table, they should be working from the same rules about what a
good locator on these sites looks like. One place to correct when a new pattern
is learned, and the correction reaches both arms at once.

### The cost nobody budgets for: sessions expire

Every friction point above is paid once. **Re-authentication is paid forever,
per site, by a person with a password and a phone** — and it is the one cost
that scales linearly with the number of apps whether or not anything is going
wrong. Five sites means five of these on a cadence you do not control and
cannot predict.

Two things follow, and both are in the plan for this reason rather than for
tidiness: the `/…-login` page has to be genuinely usable on a phone, because
that is where it will be used; and stage 6's `storage_state` backups matter
more than they look, because losing one costs a full MFA round-trip rather
than a file copy.

## What this plan does not do

- **It does not make any app redundant.** In-process rate limiting and an
  in-memory token cache mean no app may run on two hosts at once — see D9, which
  is about adding hosts to *hold more apps*, not about failover. If a Pi dies,
  the apps assigned to it are down until it is rebuilt or they are moved, and
  moving them needs their sessions (stage 5's backups).
- **It does not restrict what an app can reach on the network.** Per-user
  isolation covers files and processes; app A's compromised browser can still
  make outbound requests anywhere. See D3 for the two paths to fixing that when it
  matters.
- **It does not automate the two manual steps per site**: registering a GitHub
  OAuth app, and doing the first real login to capture a session. Both need a
  human, and both are on the template's checklist rather than hidden.
- **It does not add a deploy gate.** The self-healing plan's decision — protection
  plus a substantive review, not an approval click — stands, and stands per app.

## What would change my mind

- **If the fleet grows past roughly eight sites**, or a site arrives whose code
  genuinely must not sit beside the others, split that site out. The crossover is
  where one CI run and one head stop being able to hold the whole thing — and
  splitting one package out later is mechanical, which is exactly why starting
  here was safe.
- **If egress control becomes a requirement** — an app that must provably only be
  able to reach one domain — go to rootless Podman at that point rather than
  bolting nftables onto the user model.
- **If site two turns out to share almost nothing with site one**, the library is
  smaller than D2 assumes and the scaffold is doing most of the work. That is a
  fine outcome; it just means `packages/core/` should stop growing and the shared
  knowledge stays as knowledge, in
  [`site-automation-gotchas.md`](site-automation-gotchas.md), where it already
  is.
- **If a second person ever gets merge rights or host access**, revisit the vault
  layout (D6), the self-healing plan's deploy-gate decision, and every place this
  document says "the operator" and means "one specific person".
