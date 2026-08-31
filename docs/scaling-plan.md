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

Three repositories and one inventory.

```
browser-mcp-core         library: server, auth, browser, login framework,
  (this repo, minus         failure capture, deploy webhook, the CI workflow
   Sainsbury's)             every site repo calls

<slug>-mcp               one per site, generated from a template:
  sainsburys-mcp            locators.py, site.py, tools.py, pyproject.toml
  ocado-mcp                 pinning core by tag, a 12-line ci.yml
  …

browser-mcp-fleet        Ansible. Provisions the host once, then loops over
                            apps.yml to create per-app users, dirs, units,
                            env files, webhooks and tunnel ingress rules
```

`apps.yml` is the single source of truth:

```yaml
apps:
  sainsburys:
    repo: https://github.com/amc40/sainsburys-mcp.git
    version: main
    hostname: sainsburys.mcp.example.com
    port: 8001
    webhook_port: 8801
    headed: true          # this site blocks headless Chromium
    memory_high: 900M
```

Adding a site is: generate the repo, add six lines here, run the playbook.

### D1 — Repo per site, not a monorepo

| Option | Verdict |
| --- | --- |
| **Repo per site + a shared library** | **Chosen.** Matches R1 and R2 directly. A per-app deploy credential is scoped to one repo; a heal PR touches one site; a bad commit ships one app |
| Monorepo, one package per site | Rejected. Every app's checkout would contain every app's code, so the per-app deploy account either sees everything (undoing R2 at the deploy layer) or needs sparse checkouts and per-path CI to fake the separation. It also makes the self-healing diff-surface check and the per-repo heal token much harder to scope |
| One process, sites as plugins | Rejected outright. It fails R2 at the first line: one process means one `storage_state` directory, one memory space, one compromise |

The cost is real and worth naming: **N repos means N sets of CI, N Dependabot
streams, N template drifts.** That cost is what D2's reusable workflow and the
`copier` template exist to pay down; if it is not paid down, this decision is the
wrong one. See "What would change my mind".

**Make new site repos private by default.** `browser-interaction-mcp-poc` is
public, and the self-healing plan spends its longest section on what that costs —
every committed fixture is a permanent world-readable copy of a page from a
logged-in account. A private site repo removes that constraint entirely: the bar
drops from *am I publishing this* back to *would I show a colleague*, fixtures
can be pages rather than hand-cut subtrees, and the plan's "private-repository
fallback" becomes the default rather than the retreat. The core library stays
public — it holds no site content at all.

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
- The **reusable GitHub Actions workflow** (`workflow_call`), so a site repo's
  `ci.yml` is a dozen lines and a gate change is one edit in one place

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

### D4 — Routing: one tunnel, one subdomain per app, wildcard DNS

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

Point a **wildcard DNS record** `*.mcp.example.com` at `<tunnel-id>.cfargotunnel.com`
once, and the per-app `cloudflared tunnel route dns` step — the one manual
post-task the playbook currently prints — disappears for every future app.

Ports are explicit in `apps.yml` rather than derived, with a playbook `assert`
that they are unique. Any local account can reach any app's loopback port, which
is why authentication is on every request and not on the network position; the
ports are defence in depth, not the gate. Unix sockets would remove even that,
but FastMCP does not expose a UDS bind today — worth revisiting if it does.

**One GitHub OAuth app per site.** A shared client secret means a leak from app A
compromises the front door of every other app, which is precisely what R2
forbids. The cost is one OAuth app registration per site — a two-minute manual
step, recorded in the template's checklist alongside the DNS record that wildcard
DNS just removed. Cloudflare Access in front of the whole `*.mcp` zone is worth
adding as an independent second gate; it does not replace this one, because the
claude.ai connector runs the OAuth flow itself.

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

### D7 — The Pi will run out of memory before it runs out of anything else

This is the constraint most likely to bite first, and it is a *fleet* problem
that does not exist with one app. A headed Chromium under Xvfb is 300–500MB. Five
apps that each launch one on a tool call is an OOM kill, and the process the
kernel picks will not be the one that caused it.

- A shared `browser-mcp.slice` with a global `MemoryMax`, and per-app `MemoryHigh`
  from `apps.yml`. Reclaim pressure before kills.
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
bmcp new-site <slug>            copier template → private repo, CI, skeleton
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

- **Private site repos remove the plan's hardest constraint.** Its longest section
  is about publishing fixtures from a logged-in account to a world-readable repo,
  and it names "run the heal loop in a private repository" as the fallback if
  fixtures cannot be cut down far enough. Per-site repos make that the default.
  Subtree-only fixtures and cropped CI images remain good practice — less to redact
  is less to get wrong — but they stop being the thing the whole design hangs on.
- **The stage 4 write credential gets properly scoped.** The plan flags a genuine
  widening: for the Pi to push a fixture branch itself it needs a write token, and
  its deploy credential is read-only today. Per app, that token is scoped to one
  repository and `claude/heal-*` on it — so a token leak from the machine holding
  app A's browser profile reaches app A's repo and nothing else. The plan's
  "defensible, but a genuine widening" becomes considerably more defensible.
- **Per-app quarantine is already per-app.** The plan's cap — one open PR per
  fingerprint, quarantine the tool on a repeat failure — now quarantines one app's
  tool without touching the rest of the fleet.

**What has to change:**

| Plan says | With a fleet |
| --- | --- |
| Stage 0's `locators.py` and the `claude/heal-*` diff-surface CI check live in this repo | Both live in **every** site repo. The check ships in the core library's reusable workflow, so it is one implementation, not N |
| Fingerprint = tool + locator id + failure class | Prefix it with the **app slug**. Two sites will have a `login.username` |
| One healing cloud environment, network `Custom`, package registries only | Still one environment — but the allowlist must exclude **every** target site, and it is now a list that grows silently as apps are added. Make "the new site's domain is not on the healing allowlist" a line on the new-site checklist |
| "Empty connectors" on the healing environment | **More load-bearing, not less.** The plan already notes this account has the POC's own MCP server connected, and that a healing session holding it can drive the real site with the network level still reading `None`. Every new app is another connector on the same account, so this rule now guards N routes instead of one |
| Bundles are fetched by hand over SSH from `/var/lib/browser-interaction-mcp` | Per app: `/var/lib/browser-mcp/<slug>/bundles`, owner-only to `bmcp-<slug>`. The manual hop still works and is still the thing enforcing a human redaction review before stage 4 |
| The baseline rule: a locator is healable only once its action has succeeded against the real site | **Now doing real work.** A site freshly imported from a codegen recording has *no* proven locators, so nothing in it is healable until a first real run promotes them. This is what stops the healer from generating confident guesses about automation that never worked — which is exactly the failure mode a fast new-site pipeline would otherwise mass-produce |
| Failures are noticed when a tool is called | D8's nightly smoke timer is the fleet's detector, and its output is a failure bundle in the plan's existing format |

Nothing above changes the three invariants (**I1** the healing agent never runs
against the real site, **I2** it never changes what the server executes, **I3**
failure evidence is as sensitive as the browser profile). The isolation work
strengthens all three: I2's "the Pi's credential is read-only" becomes per-app,
and I3's evidence now sits behind a `0700` directory owned by an account that is
not shared with any other app.

---

## Stages

Each is independently useful, independently reviewable, and leaves a working
deployment behind. Stage 0 of the self-healing plan is folded into stage 2 here,
because the locator table is a prerequisite for both plans and should be built
once.

### Stage 1 — Extract the core, in place, no deployment change

Split `src/` into `browser_mcp_core/` and `sainsburys_mcp/` **inside this
repository**. Same tests, same gates, same systemd unit, same Pi. Nothing about
the deployment learns anything happened.

This proves the seam is where I think it is before anything depends on it, and it
is cheap to undo. `settings.py` becomes `CoreSettings` with a subclass; `server.py`
takes the site as a parameter; the login flow takes a `LoginSteps` implementation.
The page-interaction helpers stay in `sainsburys_mcp/` per D2.

**Done when:** `make check` passes with 100% coverage and no behaviour change, and
`sainsburys_mcp` imports nothing from `browser_mcp_core` that mentions a grocer.

### Stage 2 — The locator table

Self-healing stage 0, done here because the codegen importer targets it too. A
`locators.py` table keyed by locator id, a `resolve()` function, no inline
locators anywhere in `site.py`, and the `claude/heal-*` diff-surface check in CI.
Timeouts, consent-cookie shapes, business-logic filters and URLs stay out of the
table, for the reasons the self-healing plan gives.

**Done when:** `make check` passes with no behaviour change, and a deliberately
out-of-surface commit on a test heal branch is rejected.

### Stage 3 — Split the repos

`browser-mcp-core` becomes its own public repository, tagged. `sainsburys-mcp`
becomes its own private repository depending on a core tag. The reusable CI
workflow moves to core; `sainsburys-mcp`'s `ci.yml` becomes a `workflow_call`
stub. Renovate or Dependabot watches the core pin.

The Pi keeps running the old unit throughout. Nothing is deployed differently yet.

**Done when:** `sainsburys-mcp` is green on its own CI, built from a pinned core,
and a core patch release produces an automatic bump PR on it.

### Stage 4 — The fleet repo, and cut Sainsbury's over

`browser-mcp-fleet` with `apps.yml` and the roles from `deploy/` made plural:
per-app users, template units, per-app env files, per-app webhook instances,
generated tunnel ingress, the shared slice with memory limits, wildcard DNS.

Then migrate the one existing app: new accounts, move
`/var/lib/browser-interaction-mcp` → `/var/lib/browser-mcp/sainsburys` with
`storage_state` intact, start `browser-mcp@sainsburys.service`, verify the
connector still works and the session still authenticates, retire the old unit.

The migration is the risky step in this whole plan, because a moved
`storage_state` that loses its permissions or its contents means a real
re-login on a site with MFA. Rehearse it with `--check --diff`; keep the old unit
installed-but-stopped until the new one has served a real tool call.

**Done when:** claude.ai calls `sainsburys_search` against the new unit, and
`ps -o user=` shows `bmcp-sainsburys`.

### Stage 5 — `bmcp new-site`, proven by a second site

The copier template, the `new-site` command, and the codegen importer. Then —
and this is the actual acceptance test — **automate a second, genuinely different
site end to end using only that pipeline.** Not a hypothetical second site; a real
one, chosen because you want it.

Site two is what tells you whether the library is right. Expect it to be wrong in
at least two places, and treat fixing those as part of this stage rather than as
a regression. In particular, expect the login framework's `LoginSteps` shape to be
Sainsbury's-shaped, and expect at least one of the "deliberately not extracted"
helpers to earn promotion.

**Done when:** site two is live on the Pi, under its own accounts, and the
*ceremony* — repo, accounts, units, routes — took minutes rather than a day.
Not the whole job: see "What it actually costs to add a site" below for why
that is a narrower claim than it sounds.

### Stage 6 — The fleet's ongoing life

Self-healing stages 1–4 from the existing plan, now built once in core and
inherited by every site. The nightly smoke timers. Backups of every app's
`storage_state` (they are the expensive thing to recreate — MFA, by hand, per
site). A one-page fleet status view: per app, last successful smoke run, current
core version, open heal PRs.

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
| **Removed** | Repo, CI config, gates, package skeleton | `bmcp new-site` from the template; CI becomes a `workflow_call` stub |
| **Removed** | Accounts, units, env files, webhook, tunnel ingress | Six lines in `apps.yml` and one playbook run |
| **Removed** | The DNS record | A wildcard `*.mcp` record means there is no per-site DNS step at all |
| **One-off** | A GitHub OAuth app per site | Registered by hand, and it needs **two** callback URLs, not one — the MCP endpoint's and the login page's, as `env.j2` already notes. A mismatch fails opaquely at first connect |
| **One-off** | Vault entries, then the connector in claude.ai | Client id and secret, webhook secret, the site username — one `ansible-vault` edit. Then adding the connector once |
| **Irreducible** | Does the site block headless Chromium? | Sainsbury's and Tesco both run Akamai Bot Manager, which blocks *headless* specifically, regardless of network origin or user agent. You find out by trying. It decides `headed: true`, which decides ~400MB, which decides whether the Pi has room for app N+1 at all |
| **Irreducible** | The consent banner's real shape | Which CMP, which cookies, which registrable domain. Pre-seeding beats clicking: the button text changed from "Required only" to "Continue without accepting" within weeks, and the backdrop silently intercepts clicks, so Playwright reports a timeout on something unrelated rather than "a banner is in the way" |
| **Irreducible** | What the login actually does | Not knowable from the outside. The Sainsbury's recording corrected four assumptions the login flow had *already made* from reading the public site: the login path, the consent copy, MFA living on a separate domain and not always appearing, and adding straight from the result tile |
| **Irreducible** | Whether MFA always appears | On Sainsbury's it doesn't. That single fact is why the login runs in a parked subprocess with a 300-second OTP wait rather than inside a request handler — a site with unconditional MFA would have been a much smaller design |
| **Irreducible** | Designing the tool surface | The real work, and it is design rather than transcription. `add_to_basket` went blind-first-result → exact name → name-or-id → ellipsis-prefix fallback, and needed `sainsburys_search` to exist at all, because an index into a result list goes stale between two calls |
| **Reduced** | Codegen output is the wrong shape | It emits `wait_until="load"`, unreliable when a background poller keeps the network busy, and unbounded list reads on grids whose non-product tiles block for Playwright's full 30-second default and take the whole call down — the two patterns [`site-automation-gotchas.md`](site-automation-gotchas.md) names first. The importer and the template's helpers carry the fixes; every one still gets reviewed |
| **Irreducible** | One real run against the live site | `deploy-branch.sh`, SSH, run it. Nothing counts before this, and it is also the gate that promotes the site's locators into the healable set |

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

- **It does not make the Pi redundant.** Single host, in-process rate limiting and
  an in-memory token cache — `inventory/hosts.yml` says one host deliberately, and
  that stays true per app. A second Pi would be a second fleet, not a cluster.
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

- **If the realistic count is two or three sites, not five-plus**, the repo-per-site
  overhead may not repay itself, and a monorepo with per-app deploy accounts on
  sparse checkouts becomes competitive. The isolation requirement (R2) is about the
  *runtime*, and that is satisfied either way.
- **If egress control becomes a requirement** — an app that must provably only be
  able to reach one domain — go to rootless Podman at that point rather than
  bolting nftables onto the user model.
- **If site two turns out to share almost nothing with site one**, the library is
  smaller than D2 assumes and the template is doing most of the work. That is a
  fine outcome; it just means R4 is served by `copier update` rather than by a
  version pin, and the library should stop growing.
- **If a second person ever gets merge rights or host access**, revisit the vault
  layout (D6), the self-healing plan's deploy-gate decision, and every place this
  document says "the operator" and means "one specific person".
