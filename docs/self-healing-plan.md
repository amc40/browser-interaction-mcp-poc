# Self-healing: implementation plan

[`self-healing.md`](self-healing.md) fixed the *shape* of the mechanism while
nothing was built. Browser actions now exist, run on the Pi, and ship there
automatically. This document is the plan for building the thing, in the order
the pieces actually depend on each other — and it starts by recording where the
design doc's assumptions have been overtaken by the infrastructure.

The invariants are unchanged and are not re-argued here: **I1** the healing
agent never runs anything against the real site, **I2** it never changes what
the MCP server executes, **I3** the failure evidence is as sensitive as the
browser profile.

## The security model at a glance

![The self-healing security model: the evidence path from the live page through
capture, redaction, bundling, the cloud session and CI, what each invariant
rests on, and what is left over](img/security-model.svg)

Read the diagram as three claims. The top row is the path a page takes to
become a pull request, with the gate that each step *is* — every one of them an
absence (no durable copy, no credential, no route) rather than a rule someone
remembers. The middle bands say what holds each invariant up and, in italics,
what that invariant does **not** cover; those lines are the honest part and are
worth more than the rest. The bottom row is what no layer addresses at all.

The sections below are the same model at working depth.

## What has changed since the design was written

| Design assumed | Repository now | Consequence |
| --- | --- | --- |
| "The Pi pulls from `main` at deploy time only, driven by hand… no webhook, no polling" — the load-bearing half of I2 | `ci.yml`'s `deploy` job HMAC-signs a request to `deploy_webhook.py` on every green push to `main`. `main` is a protected branch, so who may merge is still settled | **Merge is deploy.** A merged heal PR reaches the live browser in seconds; there is no "merged but not yet live" state. Protection answers *who* merges, not what happens the instant they do. See below — the gap is real, and deliberately left open for now |
| Locators would be split from actions "when browser actions are first written" | They were not. Test ids, the search-box name, the tile selector and the heading patterns are module constants in `sainsburys.py`, read directly by control flow (`_raise_if_not_logged_in` decides login state from `_USERNAME_TEST_ID`) | The narrow diff surface — the thing that makes the review tractable and the CI check possible — is a prerequisite, not a detail |
| A trace is captured on failure | `browser_page` starts no tracing. The only artefact on any failure path is the login screenshot in `_raise_if_not_logged_in` | Nothing to bundle. Capture is the first new code |
| `redaction.py` covers the credentials | It does, and only those: `SecretRedactor` replaces known secret *values* in log records. There is no DOM redactor | The bundle builder reuses it, as planned, but the attribute allowlist and text-node rule are all still to write |
| Snapshot replay verifies a proposed locator | The suite fakes `Page` outright (`tests/test_sainsburys.py`); no test has ever loaded HTML into a real browser | Replay introduces a real Chromium to CI — a new job, not a new assertion in an existing one |
| Heal branches are `heal/**` | The cloud-session push rules accept `claude/`-prefixed branches | Use `claude/heal-<fingerprint>`. A `heal/**` branch would be refused by the platform mechanic the design relies on |
| The automation is known-good and goes stale | It is: signing in at `/sainsburys-login` on the Pi and then searching and adding to the basket has been done end to end against the live site | The baseline the mechanism needs exists, for both the login locators and the search-and-add ones. See below for what that does and does not license |

### The baseline rule, now satisfied

Self-healing repairs automation that *worked* and stopped working. Pointed at
automation that has never worked, it is a machine for generating confident
guesses about why: a failure is then evidence the selectors were wrong from the
start, and the right response is a person reading the page, not a heal.

A completed real-site run — sign in on the Pi, search, add — settles that for
every locator it touched: the login form's fields, the search box, the result
tile and its add button are all confirmed against the live page, so a future
timeout on one of them really is the page having moved. That is exactly the
signal the mechanism is built to act on, and it removes what would otherwise
have been the blocker on stage 4.

The rule itself stands as a standing condition rather than a one-off gate: **a
locator enters the healable set only once the action using it has succeeded
against the real site.** Newly written actions start outside it and are promoted
by a real run, which also keeps the fixture corpus honest — every healable
locator has at least one snapshot of the page as it looked when it worked.

### The deploy gate: recorded, not built

The design's I2 rests on "a human merges *and* a human deploys"; the webhook has
since collapsed those into one click. Written down because it is a real change
to a load-bearing assumption — not because it is being acted on yet.

**Decision: no additional deploy gate for now.** Two things carry the weight
instead, and the reasoning is that they are strictly better uses of the same
human attention:

- `main` is protected, and the healing session can only push `claude/**` and
  cannot merge at all. (Worth confirming *"do not allow bypassing the above
  settings"* is on, or the protection binds everyone except the person actually
  merging.)
- The heal PR's review is made substantive rather than ceremonial by stage 2's
  artefacts: the diff touches only the locator table, the replay proves the new
  locator resolves to exactly one element, and the cropped outline render shows
  *which* element. A gate that pauses a deploy adds nothing a reviewer can act on; a
  picture of the matched element does.

Where an end-to-end check is wanted before a heal goes live, the existing manual
path is the better instrument and needs no new machinery: publish the heal
branch to the Pi with `deploy/deploy-branch.sh` and run the failing tool once
against the real site *before* merging. That tests the thing snapshot replay
structurally cannot — the design is explicit that only the operator running it
for real establishes end-to-end behaviour — for the same effort as clicking an
approval.

The options, kept for the record:

| Option | Note |
| --- | --- |
| **Nothing extra; protection plus a substantive review** | **Current choice.** Cheapest, and it puts the effort where it changes an outcome |
| Split the `deploy` job: skip the webhook when the merged commit touches the locator table, require an explicit `workflow_dispatch` or Environment approval for those | The gate to build *if* one is wanted. Path-conditioned, so it holds whoever authored the change, and it leaves the fast path alone for ordinary commits |
| Drop the webhook, go back to manual deploys | Rejected. Punishes every ordinary change for a mechanism that fires rarely |

**What would change the decision:** a heal that merges, deploys and breaks
something the snapshot could not show; a second person with merge rights; or
auto-merge appearing on any branch. The first of those is also the last open
question at the end of this document, and the two should be revisited together.

### Secrets in the evidence

This is the risk worth spending the most effort on, because it is the only one
in this document that is **irreversible**: the repository is public, and a
commit that carries a token is world-readable, forked, cached and permanent. No
later cleanup makes it un-leaked. Everything below is therefore arranged so that
the sensitive form of the evidence is never in reach of the thing that publishes
it, rather than so that a step remembers to clean it.

What can actually be in a capture of a logged-in page:

| Class | Where it hides | Handling |
| --- | --- | --- |
| Session cookies, `Authorization` / `Cookie` / `Set-Cookie` | Trace network entries, storage state | **Never captured.** Not redacted afterwards — the bundle builder has no code path that reads them |
| Tokens in the markup | Hydration blobs (`window.__INITIAL_STATE__`), CSRF `<meta>` tags, hidden inputs, `data-*` attributes, `?token=`/`?sid=` in link URLs | **Drop `<script>`, `<template>`, inline `on*` handlers and every `<meta>` but charset, wholesale**, strip query strings and fragments from every URL, and keep only allowlisted attributes. None of that is needed to resolve a locator, so dropping it costs nothing |
| The operator's own credentials | Anywhere | `redaction.build_redactor(settings)`, which already covers every `SecretStr` in every encoding. **Note the gap:** a password typed into `/sainsburys-login` never reaches `Settings`, so the redactor does not know it. The "scrub every input `value`, `textarea` and `contenteditable`" rule is what covers it, and it has to be unconditional for that reason |
| One-time URLs | `?login_challenge=`, password-reset and magic links, `?token=` — this site's own login already carries a `login_challenge` | Query strings and fragments stripped from every URL, and `href` reduced to its path |
| An on-screen OTP | The MFA step, in pixels and in the input's `value` | Input values always scrubbed; failure PNGs never leave the Pi. `sainsburys.py` already flags this hazard on its debug screenshot |
| Tokens an app logs about itself | `console.log`, and response bodies summarised in `network.jsonl` | Keep console entries only for the failing step, drop message bodies over a size cap, and record method/path/status only — never bodies or query strings |
| A secret inside a traceback | `error.txt`, where a frame's locals can render a password | Run `SecretRedactor` over `error.txt` too, not just over log records. This is the file most likely to be forgotten, because it looks like just an exception |
| Whatever the agent quotes | The PR title, body and commit messages it writes — all public | The agent may name the locator id and its new value, and must not paste fixture content into PR prose. Worth stating in the routine's prompt, and cheap to check at review |
| Personal data — name, address, orders, basket, Nectar number | Text nodes, `alt`/`title`/`aria-label` | The subtree rule, plus committing the failing action's subtree rather than the page. Not a secret, still not publishable |

Four structural controls, in the order they act:

1. **Never durably store the raw capture.** The trace is written under the
   unit's `PrivateTmp` (already set, and destroyed on restart), the redactor
   consumes it in place, and it is deleted in the same operation that produces
   the bundle. Two directories with two meanings: `traces/` is ephemeral and read
   by nothing but the redactor, `bundles/` is the only thing the operator ever
   fetches. This is the control that matters; the rest are nets under it, and it
   needs no process split — see the note below.
2. **Allowlist everywhere, blocklist nowhere.** Attributes, element types and
   URL parts are all opt-in. A blocklist would have to know that
   `data-account-email` exists before it could drop it.
3. **A publish gate that fails closed.** `gitleaks` is already a pre-commit hook
   and a CI job; point it at the bundle on the Pi as part of building it, and add
   a fixture check to CI that rejects a snapshot containing `<script`, a
   `Set-Cookie`/`Bearer` string, a URL query string, or one over a size cap. Be
   honest about its strength: gitleaks matches known credential shapes, not a
   bespoke session cookie, so it catches carelessness, not everything.
4. **A human reads the fixture before it is pushed** — which is only realistic
   because the fixture is a subtree rather than a page. "Too big to read" and
   "safe to publish" are not compatible states, and if a fixture cannot be cut
   down to something readable, that is the signal to use the private-repository
   fallback rather than to skim it.

**This does not need the browser split out of the server.**
[`deployment.md` §8](deployment.md) proposes that split, and an earlier draft of
this section leaned on it — wrongly. The split addresses a different threat (a
page escaping Chromium's sandbox landing next to the OAuth secret and the live
session), and it buys almost nothing here: the redactor and the bundle builder
would sit on the *same* side of the socket as the raw trace either way, and the
path a leak actually travels — bundle, SSH fetch, commit, push — is identical
with or without it. What keeps the raw capture out of that path is the directory
discipline above, which one process does just as well as two. §8 stands on its
own merits and is **not a prerequisite** for any of this.

Two properties worth naming because they are the reason this is tractable:

- **The published picture is derived from the redacted fixture, never from the
  live page.** Redaction happens once, on text, in one place; the render can only
  show what survived it. That is why a screenshot needs no second redaction pass
  of its own — pixels cannot leak what the DOM no longer contains.
- **The blast radius of a leaked Sainsbury's session is recoverable**, unlike
  most secrets: signing out and rerunning the login invalidates it. Cheap
  rotation is a real mitigation, and worth keeping cheap. Plan for it as the
  response rather than assuming detection: if a bundle turns out to have carried
  a token, rotate first, then work out how it got through.

**Host secrets are a different matter, and stay out by construction.** The
GitHub OAuth client secret, the deploy webhook HMAC secret, the tunnel
credentials and the `storage_state` file are never *in* a page capture — the
risk with them is a bundle builder that helpfully includes environment or
configuration. It must not: the bundle is assembled from the trace and the
manifest's fixed field list, never from `os.environ`, and
[`self-healing.md`](self-healing.md) already forbids passing bundles through
environment variables in the other direction. Note also that git history keeps
a fixture that a later commit trims, so "fix it in the next commit" is not a
remedy on a public repository.

**The residual, stated plainly:** the model sees the fixture. The Anthropic API
is an outbound channel by construction — the design says so — and redaction
governs what travels with the prompt, nothing else. A capture reduced to the
subtree of one control, with scripts and metadata gone, is a small enough thing
to be comfortable about; a full-page trace never would be.

### Screenshots, and the fact that this repository is public

"Screenshot" covers three different artefacts with three different audiences,
and conflating them is how a picture of a logged-in account ends up somewhere
permanent. Kept apart:

| Artefact | Who needs it | Where it lives |
| --- | --- | --- |
| `screenshot/pre.png`, `screenshot/fail.png` — the real page as it actually was | The operator, deciding whether this is even a locator problem | **On the Pi only.** Never uploaded, never committed, never attached to a PR. Read over the existing SSH path from the laptop |
| A render of the committed snapshot | The agent, writing the locator | **Inside the session**, generated on demand from the fixture with the preinstalled Chromium. Never committed — it is derived data, reproducible from the fixture at any time |
| The outlined-match image | The reviewer, on a phone | **Posted to the PR by CI**, which re-renders it from the fixture. Cropped to the matched element plus a margin — never the full page |

The load-bearing point is that the fix is written against the *snapshot*, not
against the live page, so a render of the snapshot is the more relevant picture
anyway. If the two ever disagree, that disagreement is itself the finding: the
fix is being written against something other than what broke, and the capture is
wrong.

**`amc40/browser-interaction-mcp-poc` is a public repository.** That is not a
detail — it means every committed fixture and every image CI attaches to a PR is
world-readable, permanently, at a URL that needs no credentials. The design's
"committed snapshot fixtures are redacted copies, reviewed like any other file"
was written without that in view. The bar is therefore not *would I show this to
a colleague* but *am I publishing this*, and three things follow:

- **Commit the subtree, not the page.** The fixture only has to be a valid
  replay target for one locator: the failing action's ancestor chain and its
  subtree — which is already the boundary the design's text-node rule draws. A
  results grid with a handful of tiles replays exactly as well as the whole
  logged-in page, is far less to redact, and publishes far less.
- **Crop what CI posts**, for the same reason and one other: an outline box on a
  full-page render is nearly invisible on a phone. Cropping serves the review
  and the exposure at once.
- Prefer fixtures from unauthenticated pages wherever a locator appears on one —
  the groceries homepage is public, and the search box appears there.

**If that proves too restrictive** — if useful fixtures cannot be cut down far
enough to publish — the fallback is to run the heal loop in a private repository
and bring only the locator diff across. Heavier, and it costs the phone-friendly
review; worth naming now so it is a decision rather than a discovery.

## Stages

Each stage is independently useful and independently reviewable. Nothing in
stages 0–2 involves a model at all.

### Stage 0 — split locators from actions

*Prerequisite for everything. Worth doing on its own merit.*

- New `locators.py`: a table keyed by **locator id** (`sainsburys.search_box`,
  `sainsburys.product_tile`, `sainsburys.add_button`, `login.username`,
  `login.password`, `login.submit`, `login.otp`, `login.submit_code`,
  `groceries.products_we_love_heading`), each row a small frozen dataclass
  saying *how* to address the element — role + accessible name, test id, or CSS
  — plus a one-line description of *what* it is for the reviewer and the model.
- A resolver, `resolve(page_or_locator, "sainsburys.add_button") -> Locator`,
  so `sainsburys.py` never constructs a locator inline.
- What stays out of the table, deliberately: timeouts and poll intervals (a
  "heal" that widens a timeout hides a real failure), the consent-cookie shape
  (it is a protocol with OneTrust, not a way of naming an element), the
  non-product heading filter (business logic about what counts as a product),
  and every URL.
- `_raise_if_not_logged_in` keeps its logic; it takes its two locators from the
  table.
- CI gains a **diff-surface check**: on a branch named `claude/heal-*`, fail if
  the diff touches anything but `locators.py` and `tests/fixtures/`.

**Done when:** `make check` passes with no behaviour change, and the check
rejects a deliberately out-of-surface commit on a test heal branch.

### Stage 1 — capture and redact, on the Pi, going nowhere

- Tracing in `browser_page`: `context.tracing.start(screenshots=True,
  snapshots=True)`, discarded on success, stopped to a temp path on exception.
  Cost on a Pi is real; measure it, and if it bites, gate it behind a setting
  defaulting to on.
- `failure_capture.py`: classify → fingerprint → build bundle. Classification
  has concrete signals here already: `NotLoggedInError` is **never** healable
  (it is the "healer fixes an auth failure by editing something correct" case
  the design warns about), `RuntimeError("no results")` is a site or query
  problem, and a `PlaywrightTimeoutError` from a `wait_for`/`click` on a table
  locator is the one healable class. Fingerprint = tool + locator id + failure
  class.
- `dom_redaction.py`: the attribute allowlist, the never-present list, the
  always-scrubbed list, and the text-node subtree rule — all as written in the
  design, composed with `redaction.build_redactor(settings)` for the operator's
  own credentials.
- **Capture, redact and delete in one operation**, with the raw trace under
  `PrivateTmp` and only the finished bundle written to durable storage. If the
  browser is later split out of the server ([`deployment.md`
  §8](deployment.md)), this whole step travels with it unchanged — but it does
  not wait for that.
- **The DOM snapshot must be self-contained.** The healing session runs with no
  network, so a snapshot that references external stylesheets renders as
  unstyled markup there — and an unstyled render is useless as review evidence
  and misleading to reason about layout from. Inline the CSS at capture time, on
  the Pi, where it is still reachable; drop images but keep their boxes
  (`width`/`height` on the `img`), so the shape of the page survives without
  shipping its pictures. This is a capture-time requirement precisely because
  the Pi is the last place with a route to those resources.
- The bundle is written to an owner-only directory on the Pi and **is not
  uploaded anywhere**. The operator fetches one over the existing SSH path when
  they want to look at it.

**Done when:** a real failure (force one by breaking a locator locally) yields a
bundle whose `manifest.json` is enough to triage from, and reading the bundle by
hand answers the design's largest open question — *is redacted text good enough
to write a locator from?* — against a real Sainsbury's page rather than in
advance. If the answer is no, the text-node heuristic changes here, before
anything depends on it.

### Stage 2 — replay harness and fixture regression tests

- `tests/locators/` : a real Chromium loads a committed snapshot from
  `file://`, and each locator-table row asserts it resolves to **exactly one**
  element on the fixture pages that should contain it. This is the check a
  proposed heal has to pass, and the permanent regression test the design
  values most.
- New CI job (`playwright install chromium`), separate from `test` so the fast
  browserless suite stays fast. Watch the coverage gate: browser tests should
  not become the reason `--cov` passes.
- **Fixture hygiene is an I3 matter.** Prefer fixtures from unauthenticated
  pages (the groceries homepage is public). A logged-in results page carries the
  operator's name, store and basket; a committed one is a permanent copy of it,
  and gitleaks catches credentials, not personal data. Every fixture is a
  redactor output that a human has read, never a raw save.
- Reviewer artefact: the job renders the fixture with the proposed locator's
  match outlined, crops to that element plus a margin, and posts it to the PR,
  so review answers "is this *Archive* or *Delete*?" from a picture — legibly on
  a phone, and without republishing the whole page.

**Done when:** a wrong-but-plausible locator (points at the tile's price rather
than its add button) fails the job, and the outline image shows why.

### Stage 3 — heal by hand, in a cloud session

The design's options table lists "operator's laptop, on demand" as workable but
not chosen, because it is just the operator debugging. A **cloud session**
(`claude --cloud`, resumable from claude.ai/code on a phone) is the better
rehearsal on both counts: it is the same off-host, credential-free environment
stage 4 will fire automatically, so what is learned here transfers instead of
being thrown away — and the iteration loop is reachable from a phone, which is
the point of doing it this way.

It also *strengthens* I1 rather than bending it. The laptop is the machine that
holds SSH access to the Pi and a working route to the real site; the cloud
session has neither, and can be configured so it never can.

- **Environment, built once and reused by stage 4 — and it does not exist yet.**
  Network access is a property of the *environment*, chosen when that
  environment is created, not of the `--cloud` flag. This account currently has
  two: "Default — trusted network access" and "Danger Zone". **Both have
  network access; neither is usable here.** A dedicated no-network environment
  has to be created and then verified from inside a session (try to reach
  `sainsburys.co.uk` and confirm it fails) before any heal runs in it. Treating
  `None` as the default is the mistake this bullet exists to prevent.
- **Connectors are the hole that the network setting cannot show**, and on this
  account it is not hypothetical: **this project's own deployed MCP server is
  connected**, exposing `sainsburys_search` and `sainsburys_add_to_basket`.
  Connector traffic goes through Anthropic's servers rather than the session's
  network, so a healing session with that connector enabled can drive the real
  site — I1 violated outright — with the network policy still reading `None`.
  The connector list must be explicitly empty, and re-checked whenever
  connectors are added to the account for unrelated reasons.
- **One new hazard the laptop framing hid:** these environments ship Chromium
  preinstalled. A session with a browser *and* network access is one navigation
  away from the live site, which is exactly what I1 forbids. Network `None` is
  what makes "it could not reach the site if it tried" true rather than
  intended — so it is a requirement here, not a hardening option.
- **That same Chromium is what makes the session useful:** the agent can run the
  snapshot replay itself, against `file://`, and iterate before pushing anything
  — the design's "agent with read-only tools over the snapshot" level, reached
  early and for free, with CI still re-running the same checks on the PR.
- **Nothing of substance travels in the prompt.** There is no good way to hand a
  cloud session a DOM snapshot and a pair of PNGs at launch, and trying is the
  wrong shape anyway — the evidence is large, binary, and needs to end up in the
  PR regardless. So the repository is the transport: the operator fetches the
  bundle from the Pi over SSH, reads the redacted snapshot, and **pushes** it to
  a `claude/heal-*` branch (pushed, not just committed — the session starts from
  the remote). The prompt is then one line: the branch, the fixture path, the
  locator id and the failure class, all of which fit in a phone message.
  Everything else the agent needs, it reads from the checkout.
- **Screenshots stay out of git.** The DOM snapshot earns its place in the
  repository because it becomes the permanent regression fixture. The failure
  PNGs do not: they are review evidence, they are binary, and git history keeps
  them for good — a poor place for pictures of a logged-in account. The session
  has Chromium, so it *re-renders* what it needs from the committed snapshot
  instead, which is why stage 1 requires that snapshot to be self-contained. The
  original PNGs stay on the Pi for the operator to look at directly.
- This costs one laptop hop per heal and buys four things: no bundle store to
  design yet, no allowlist entry, no sensitive binaries in git history, and the
  redaction review the design insists on — by a human, before anything leaves
  the Pi — as a step nobody can skip. Every turn after that hop is
  phone-workable.
- **Review from a phone raises the stakes on the artefacts.** A selector diff on
  a small screen is barely reviewable; the outlined-match screenshot is. CI —
  which does have a network and already re-renders from the fixture — posts it
  to the PR, so the picture arrives where the review happens rather than as a
  file someone has to go and find. Stage 2 is therefore a hard prerequisite for
  stage 3, not a nice-to-have alongside it.
- Before merging: publish the branch to the Pi with `deploy/deploy-branch.sh`
  and run the failing tool once against the real site. This is the end-to-end
  check, in place of a deploy gate, and it is a habit worth forming here while
  the volume is one PR. It needs the laptop, and it is the one step that should.

**Done when:** one genuine locator breakage has been healed this way — bundle
committed from the laptop, the fix iterated from a phone, run against the real
site from the branch, reviewed and merged.

### Stage 4 — the automatic trigger

Gated on stage 3 having run once for real, and on each covered action meeting
the baseline rule above — which the login and search-and-add paths already do.

- Pi side: on a healable failure, upload the bundle and POST the manifest to a
  routine's API trigger. The HMAC-signed, size-capped, single-purpose shape of
  `deploy_webhook.py` is the precedent to copy — in the other direction.
- Session side: the stage 3 environment unchanged — empty connectors, network
  `None` — plus at most one allowlisted bundle host if the bundle cannot ride in
  the fire text, and branch `claude/heal-<fp>`. The only genuinely new pieces
  are the trigger and whatever replaces the operator's laptop hop.
- **The repo-as-transport trick does not generalise for free.** For the Pi to
  push the fixture branch itself, it needs a write credential — and the Pi's
  deploy credential is read-only today, which is one of the absences I2 rests
  on. A ref-scoped token limited to `claude/heal-*` on one repository is
  defensible (branch protection still keeps it off `main`, and a human still
  merges), but it is a genuine widening on the machine that holds the browser
  profile. The alternative is the bundle store and its one allowlist entry.
  Decide this when stage 4 is actually built, with a real bundle's size in hand;
  both options are live, and the manual hop is what makes it safe to defer.
- Caps: one open PR per fingerprint, superseding rather than stacking; a second
  failure on a fingerprint whose heal already merged stops healing and pages the
  operator; the tool is quarantined meanwhile so a retrying client gets a clear
  `ToolError` instead of burning the shared rate-limit budget.
- The heal workflow runs on `pull_request`, never `pull_request_target`, and
  holds no secrets. `zizmor` already flags the alternative.

**Done when:** a forced breakage on the Pi produces a reviewable PR with no
human in the loop before review — and the quarantine holds when the same tool is
called again.

## What is not planned here

- **Anything that gives the healing job a route to the Pi or the site.** The
  bundle is a static capture; there is no re-capture request path, by design.
- **Auto-merge on a heal branch**, at any stage.
- **Healing `NotLoggedInError`.** It is explicitly excluded at triage.
- **Proposing changes to the target site** ("add a test id"), which is the
  version of this that makes the automation permanently less fragile. Still out
  of scope, still the better fix where the operator controls the page.

## Open, still

The design doc's open questions survive, minus the one stage 1 is built to
answer. Two now have sharper edges:

- **Where bundles live** only becomes a question at stage 4. Stages 1–3 keep
  them on the Pi and move one by hand, which is the same trust boundary as the
  profile they came from. If the routine's fire text can carry a capped,
  gzipped manifest plus a single DOM snapshot, the storage question may never
  need answering — and the human redaction review that the manual hop enforces
  will need replacing with something deliberate when it goes.
- **How to notice the mechanism failing quietly** — a heal that merges, deploys
  and breaks something the snapshot could not show. With merge-to-deploy now
  automatic, this matters more than it did when the design was written, and
  `deployment.md` §9's per-call logging is still the raw material.
