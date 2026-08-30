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
  locator resolves to exactly one element, and the rendered outline shows *which*
  element. A gate that pauses a deploy adds nothing a reviewer can act on; a
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
  match outlined and uploads it, so review answers "is this *Archive* or
  *Delete*?" from a picture.

**Done when:** a wrong-but-plausible locator (points at the tile's price rather
than its add button) fails the job, and the outline image shows why.

### Stage 3 — heal by hand

- The agent runs **on the operator's laptop, on demand**, against a bundle
  fetched from the Pi — the design's "workable, not chosen" option, used here as
  the rehearsal. It writes the branch, the fixture and the PR; CI runs the
  diff-surface check, the replay and `make check` exactly as it would later.
- What this is for: the prompt, the bundle contents and the review artefact all
  get their first contact with a real failure while the trigger infrastructure
  does not yet exist. Everything learned here is cheap to change.

- Before merging: publish the branch to the Pi with `deploy/deploy-branch.sh`
  and run the failing tool once against the real site. This is the end-to-end
  check, in place of a deploy gate, and it is a habit worth forming here while
  the volume is one PR.

**Done when:** one genuine locator breakage has been healed this way, run
against the real site from the branch, reviewed and merged.

### Stage 4 — the automatic trigger

Gated on stage 3 having run once for real, and on each covered action meeting
the baseline rule above — which the login and search-and-add paths already do.

- Pi side: on a healable failure, upload the bundle and POST the manifest to a
  routine's API trigger. The HMAC-signed, size-capped, single-purpose shape of
  `deploy_webhook.py` is the precedent to copy — in the other direction.
- Session side: connector list **explicitly empty** (the design's sharpest
  point: connector traffic bypasses the network setting entirely), network
  `None` plus at most one allowlisted bundle host, branch `claude/heal-<fp>`.
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
  them on the Pi, which is the same trust boundary as the profile they came
  from. If the routine's fire text can carry a capped, gzipped manifest plus a
  single DOM snapshot, the storage question may never need answering.
- **How to notice the mechanism failing quietly** — a heal that merges, deploys
  and breaks something the snapshot could not show. With merge-to-deploy now
  automatic, this matters more than it did when the design was written, and
  `deployment.md` §8's per-call logging is still the raw material.
