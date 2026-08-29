# Self-healing browser actions

**Status: proposed, nothing built.** Written before any browser was driven, so
nothing here had been validated against a real failure. This document exists to
fix the shape of the mechanism — and, more importantly, its boundaries — before
any of it is written. The staged implementation plan that follows from it, and
the points where the infrastructure built since has overtaken its assumptions,
are in [`self-healing-plan.md`](self-healing-plan.md).

## The problem

Playwright actions break for reasons that have nothing to do with the action
being wrong: a class name changes, an element moves inside a new wrapper, a
consent dialog appears, a list gains a header row. The action is still the right
action; only the way it names the page has gone stale. That failure mode is
frequent, mechanical, and almost always fixable by looking at the page and
picking a different locator — which is to say, it is the kind of work an agent
can do and a human resents doing.

The risk is what such an agent has to be trusted with to do it. The thing it is
repairing drives a browser holding the operator's own logged-in sessions, and
the repair lands in code that runs with those sessions. A self-healer with the
access it "needs" to verify its own work is a machine that edits the automation
*and* runs it against live accounts. That is strictly worse than a broken
selector.

## Invariants

Two are the operator's, and are the reason this document is separate from the
mechanism it describes:

- **I1 — the healing agent can never run a script against the real site.** Not
  the failing script, not its proposed replacement, not a probe.
- **I2 — the healing agent can never change what the MCP server executes.** Its
  only output is a proposal that a human merges and a human deploys.

A third follows from the project's premise rather than from the agent:

- **I3 — the failure evidence is as sensitive as the browser profile.** Whatever
  the agent is shown was captured from a page the operator was logged in to.
  [`deployment.md` §7](deployment.md) already calls the live sessions "the actual
  thing worth stealing"; a captured page is a copy of one. This constrains the
  design as tightly as I1 and I2 do, and is the invariant most likely to be
  broken by accident, because breaking it looks like helpfulness — a screenshot
  pasted into a PR body, a trace committed as a fixture.

Everything below is chosen to make these three structural — enforced by what the
agent has no credentials or route to reach — rather than behavioural.

## Do the cheap thing first

The framing was "fragile even when you have full control over the underlying
site". If that control is real, most of this problem is not an AI problem:

- Address elements by a **stable contract** — `data-testid`, or ARIA roles and
  accessible names, which break far less often than DOM structure. Playwright's
  `get_by_role` and `get_by_test_id` exist for exactly this.
- Make the contract **enforceable in the site's own repository**: a check that
  fails when a test id the automation depends on disappears turns a silent
  breakage discovered days later into a red build on the commit that caused it.
  Publishing the list of ids this server relies on is enough to make that check
  writable.

A healing agent that runs constantly is a signal that this step was skipped. The
mechanism below is for the residual — third-party pages, contracts that were not
kept, and changes nobody thought to coordinate — and it should be rare enough
that each PR is worth reading properly.

## Options

### Where the agent runs

| Option | Verdict | Reason |
| --- | --- | --- |
| On the Pi, in a container | Rejected | I1 becomes a policy, not a fact. The host has the browser, the profile and a working route to the target site; the only thing standing between a container and a live login is configuration nobody re-checks. Same objection as [`deployment.md` §6](deployment.md) makes about shell access |
| Operator's laptop, on demand | Workable, not chosen | Safe, and a reasonable first implementation, but it is just "the operator debugs it", which is the work being automated away |
| **Ephemeral CI job, off-host** | **Chosen** | The runner has no browser profile, no credentials for the target service, and no route to the Pi. I1 holds because there is nothing to run against and nothing to authenticate with — the agent could not reach the site if it tried |

Egress from that job is allowlisted to the model API and GitHub. Note what this
does and does not buy: the model API is itself an outbound channel carrying page
content, which is inherent to asking a model about a page. The allowlist stops
the agent talking to the *target site*; only redaction (below) limits what
leaves with the prompt.

### What the agent is shown

| Option | Verdict | Reason |
| --- | --- | --- |
| Exception and traceback only | Rejected | "Timeout 30000ms exceeded waiting for locator" without the page is a guessing game, and a guessing agent produces plausible diffs that cannot be checked |
| Screenshot at failure | Insufficient alone | Good for review, useless for writing a locator — there is no DOM in a PNG |
| **Playwright trace, redacted** | **Chosen** | `tracing.start(screenshots=True, snapshots=True)` records a DOM snapshot per action, plus console and network. It contains the page *before* the failing step, which is the state a working locator has to match — the failure screenshot alone shows the aftermath |
| Live re-navigation to capture fresh state | Rejected | Violates I1 outright |

The trace is captured on the Pi and is not safe as recorded: assume it holds
everything the page saw, including request headers carrying session cookies,
response bodies, and whatever personal data was on screen. Playwright's own
documentation warns that traces may contain sensitive information. So the bundle
is built by a redaction step that runs on the Pi, before anything is uploaded:

- drop cookies, `Authorization` headers, and storage state outright;
- keep only the snapshots for the page the failing action targeted;
- keep DOM structure, attributes and accessible names — the raw material of a
  locator — and decide deliberately about text nodes.

That last one is a genuine trade-off with no clean answer. Text is often exactly
what a good locator uses (`get_by_role("button", name="Archive")`), and it is
also where the personal data is. Redacting all text yields safer bundles and
worse fixes. The workable middle is to redact text in nodes outside the failing
action's subtree and its ancestors, which keeps the labels a locator needs and
drops the inbox contents around them — but it is a heuristic, and it should be
reviewed against real bundles rather than trusted in advance.

The bundle never enters the repository and is never inlined into a PR. It goes
to short-retention storage; the PR carries an identifier.

#### Bundle contents

Precisely this, and nothing else:

| Path | What it is | Why the agent needs it |
| --- | --- | --- |
| `manifest.json` | Failure class, fingerprint, tool name, step index, the **locator id** that failed and its deployed value, server git SHA, package version, Playwright and browser versions, viewport, timestamp, attempt number, redaction-profile version | Triage and dedupe happen from this file alone, without opening the rest |
| `error.txt` | Exception type, message, traceback | Playwright's timeout text names the resolved selector and the wait chain |
| `dom/pre.html` | DOM snapshot from the action *before* the failing one | The state a working locator has to match — the single most load-bearing file |
| `dom/fail.html` | DOM at failure | Distinguishes "the element moved" from "a dialog is covering it" |
| `screenshot/pre.png`, `screenshot/fail.png` | Full-page, redacted | Reviewer evidence, and the model reads rendered UI well enough to use them |
| `console.log` | Browser console for the step | Catches the app throwing before it ever rendered the target |
| `network.jsonl` | Method, path, status, resource type, timing | Catches "the XHR 500'd, so the list never populated" |

`manifest.json` carries the **locator id** — the identity of a row in the locator
table — not a selector supplied by a caller. That id is what the fix edits.

`network.jsonl` earns its place mostly by *preventing* heals: a 500 or a 401
among the step's requests is the triage signal that the locator was never the
problem.

The redactor works by allowlist, not blocklist, because a blocklist cannot
anticipate what a given site puts in its own attributes:

- **Attributes kept:** `id`, `class`, `role`, `aria-*`, `data-testid`, `name`,
  `type`, `alt`, `title`, `placeholder`, and `href` reduced to its path.
  Everything else is dropped, including app-specific `data-*` — a blocklist
  would have to know that `data-account-email` exists before it could remove it.
- **Never present:** cookies, `localStorage`, `sessionStorage`, IndexedDB,
  `Authorization` / `Cookie` / `Set-Cookie` headers, request and response
  bodies, and URL query strings and fragments — session tokens ride in both.
- **Always scrubbed regardless of subtree:** `value` on inputs, `textarea`
  contents, and `contenteditable` text.
- **Text nodes** survive only inside the failing action's ancestor chain and its
  subtree — the heuristic discussed above, and the part to check against real
  bundles rather than trust in advance.
- **The operator's own credentials** are removed by
  [`redaction.py`](../src/browser_interaction_mcp/redaction.py), which the
  bundle builder reuses rather than reimplements. That covers values the server
  holds, exactly and in every encoding. It does **not** cover a token the
  automated site puts in its own markup — that is the residual, and it is what
  the attribute allowlist and the text-node rule above are actually for.

A trace of any size is far too large to pass inline, so the bundle is uploaded
and only `manifest.json` plus the bundle id travel with the trigger. That split
is useful rather than merely necessary: triage reads the manifest and can decline
to fetch the bundle at all.

### How a proposal is verified

This is the hard part, because I1 removes the obvious answer. An agent that
cannot run the script against the site cannot know its fix works.

| Option | Verdict | Reason |
| --- | --- | --- |
| No verification — propose and let review decide | Rejected as the whole design | Puts the entire burden on a human reading a selector diff, which is the review most likely to be rubber-stamped |
| Static gates only (`make check`) | Necessary, not sufficient | Proves the patch compiles, types and passes the existing suite. Says nothing about whether the new locator matches anything |
| **Replay against the frozen snapshot** | **Chosen** | The captured DOM is served locally and the proposed locator is resolved against it. Offline, deterministic, and it answers the actual question |
| Replay against a staging copy of the site | Rejected | A staging site is a running site; the moment one exists the agent has somewhere to point a browser, and I1 depends on there being nowhere |

Snapshot replay is `page.route_from_har()` — or simply serving the snapshot from
`file://` — followed by asserting the proposed locator resolves to exactly one
element. It is worth being precise about what that proves: **the locator matches
the page as captured.** It does not prove the flow still works end to end. Only
the operator, running it for real after merge, establishes that. The mechanism
converts "is this selector plausible?" into "does this selector match?", which is
the part a machine can settle; the rest stays human.

Two things fall out of this, both worth more than the verification itself:

- **Each heal ships a regression test.** The redacted snapshot is committed as a
  fixture alongside the fix, so the page that broke the automation becomes a case
  that permanently guards it. Over time the suite accumulates exactly the shapes
  the real site has taken. This is the most valuable output of the whole
  mechanism, and it is a side effect.
- **The review gets a picture.** CI renders the snapshot with the proposed
  locator's match outlined and attaches it. A reviewer can then see that the fix
  points at *Archive* and not at *Delete* — which is the failure mode that makes
  self-healing selectors dangerous, and the one a diff hides best.

The replay harness is not in the agent's writable surface (see below), so a fix
cannot pass by rewriting its own check.

### How much agency

| Option | Verdict | Reason |
| --- | --- | --- |
| Single model call: bundle in, diff out | Viable, smaller surface | Cheapest, most auditable, and probably right for a first version |
| **Agent with read-only tools over the snapshot** | **Chosen eventually** | Querying the DOM and re-running the replay beats one-shot guessing on anything non-trivial, and every tool it has is offline and read-only |
| Agent with shell on the checkout | Rejected | Needless. The two useful capabilities are "look at this snapshot" and "try this locator against it"; a shell adds only ways to be surprised |

Whatever the level, the agent is reading page content pulled off the internet,
which means the DOM it is shown is untrusted input and may contain text written
to be read as instructions. This is contained rather than solved: the agent has
no credentials to escalate with, no route to the site, and no path to `main` — so
the worst outcome of a successful injection is a bad PR, which is the same
outcome as a bad guess. That containment is a consequence of I1 and I2, and it
is the main reason to keep them structural.

## How the session is started

The ephemeral off-host job is not hypothetical infrastructure — Claude Code
cloud sessions can be triggered programmatically, and their configuration
surface maps onto the invariants closely enough to be worth designing against.

**A routine with an API trigger.** A routine is a saved prompt plus repositories,
a cloud environment, and a connector list; an API trigger gives it an endpoint
the Pi can POST to with a bearer token, which starts a session and returns its
id. That is the whole integration on the Pi's side: upload the bundle, POST the
manifest. (Routines are a research preview, and the endpoint ships behind a
dated beta header, so treat the exact shape as liable to change.)

Three properties of that surface do real work here:

- **Fire text is already treated as untrusted.** The text posted with the
  trigger arrives wrapped and labelled as untrusted data, and the routine's saved
  prompt has to opt into acting on it. This is exactly the posture the DOM
  content needs — the platform's default matches the threat model rather than
  having to be argued into it — and it means a leaked trigger token yields
  labelled data, not instructions.
- **Network access is a first-class environment setting**, with `None` as an
  available level. `None` is the honest expression of I1: no outbound
  connections through the session's network at all. Two documented carve-outs
  survive it, and both happen to be wanted — GitHub goes through a separate
  proxy, and the Anthropic API stays reachable, which is the same inherent
  exfiltration channel noted above.
- **GitHub credentials never enter the VM.** A proxy swaps a scoped credential
  for the real token on the way out, `git push` is restricted to the session's
  current working branch, and API access is scoped to repositories attached to
  the session. For a routine, pushes to `claude/`-prefixed branches are accepted
  and pushes elsewhere are refused if the branch is protected or carries someone
  else's commits.

That last point moves a chunk of I2 from a rule into platform mechanics: the
agent cannot push `main` because it can only push its own working branch, and
cannot reach an unattached repository at all. The branch ruleset and the
manual-deploy discipline remain the parts that are still ours to hold.

**The trap is connectors, not the network.** Every connector on the account is
included in a routine by default, and a session can use every tool a connector
exposes — writes included — without prompting. Connector traffic goes through
Anthropic's servers rather than the session's network, so it **does not appear
in the allowlist and is not blocked by `None`**. A Slack or Notion connector left
enabled is an egress path for the bundle that the network setting will not show.
The healing routine's connector list must be explicitly empty, and that is worth
re-checking when connectors are added to the account for unrelated reasons.

One tension to resolve rather than paper over: `None` means the session cannot
fetch the bundle from a store either. Either the bundle store becomes the single
entry in a `Custom` allowlist — one auditable host, not the target site — or the
bundle is small enough to ride in the fire text. The former is more likely, and
is still a far narrower grant than `Trusted`, whose default list includes most
of the package-registry internet.

**The alternative, if this outgrows a subscription-side routine**, is the Managed
Agents API: agents and sessions as persisted API objects, per-session containers,
`limited` networking with an explicit host allowlist, files mounted as session
resources — which handles the bundle natively instead of needing a fetch — and
vault credentials that are substituted at egress and never visible in the
sandbox. It is the better-fitting platform and the heavier one. For a proof of
concept driving one person's browser, a routine is the proportionate choice; the
design above does not depend on which is used.

## Recommended design

```
Pi                                 tool call fails
  browser, real sessions              │
  real route to the site              │ classify: locator failure?
                                      │ redact, bundle, upload
                                      ▼
                            bundle store (short retention, not the repo)
                                      │  id only
                                      ▼
CI                          ephemeral job: no profile, no site credentials,
  no browser profile        no route to the Pi, egress allowlisted
  no site credentials               │
                            healing agent ──> diff, selectors file only
                                      │
                            replay against frozen snapshot + make check
                                      │
                                      ▼
                            PR on heal/<fingerprint>, + snapshot fixture
                                      │
                                   human review
                                      ▼
                                    main ──manual deploy──> Pi
```

**Triage before anything else.** Only locator-shaped failures are worth healing:
element not found, timeout waiting for a selector, strict-mode violation from an
ambiguous match. An expired login, a rate-limit response, a 500 from the site or
a network error must not reach the agent — there is nothing to fix in the script,
and a healer that "fixes" an auth failure will do so by editing something that
was correct. Everything else pages the operator instead.

**Narrow the diff surface.** Split the parts that go stale away from the parts
that must not change: locators live in one module or data file, control flow and
the approved-actions list stay in `tools.py`. The agent may only touch the
locator file, and CI enforces that by rejecting a heal branch whose diff touches
anything else. This is what makes the review tractable — the question becomes
"is this the right element?" rather than "does this code now do something
different?" — and it keeps the founding constraint intact: *which* actions exist
is still decided in code by a human, and only *how a step names an element* is
open to proposal. The locator file is part of the deployed, reviewed artifact.
Nothing is fetched at runtime.

**Cap it.** Fingerprint the failure and open at most one PR per fingerprint,
superseding rather than stacking. If a tool fails again after a heal for the same
fingerprint was merged, stop healing it and escalate — a fix that did not hold is
evidence the model is wrong about the page, and the second guess is worth less
than the first. Quarantine the failing tool in the meantime so a client retrying
in a loop gets a clear error instead of burning the shared rate-limit budget.

## Enforcement

The point of the table is that each invariant rests on an absence, not a rule.

| Invariant | Enforced by | What would break it |
| --- | --- | --- |
| I1 | The healing job holds no credentials for the target service, and its egress allowlist excludes it. Its only browser target is a local file | Adding a staging environment, or "just letting it check" with a read-only login. There is no read-only login to a session |
| I1 | Bundles are static captures. There is no code path from the healing job to the Pi | A debugging convenience that lets the job ask the Pi to re-capture |
| I2 | The agent's token can push `heal/**` and open PRs. It cannot push `main`, cannot merge, cannot approve | Widening the token, or auto-merge on a heal branch |
| I2 | The Pi pulls from `main` at deploy time only, driven by hand from the operator's laptop. No webhook, no polling, no agent-triggered deploy | Any pull-on-push deployment. It would turn a merged PR into an immediate change to what runs against live accounts |
| I2 | The Pi's deploy credential is read-only, and the server loads no code from a writable path | A runtime plugin directory, or the locator file being fetched rather than deployed |
| I1/I3 | The healing session's connector list is empty | Any connector left enabled. Connector traffic bypasses the network allowlist entirely, so this hole is invisible in the network setting |
| I3 | Redaction runs on the Pi, before upload. Cookies and auth headers never leave | Uploading a raw trace "temporarily" to debug the redactor |
| I3 | The bundle is not passed through environment variables | Cloud environments have no secrets store, and their variables are readable by anyone using the environment |
| I3 | Bundles go to short-retention storage, never the repo, never a PR body | Committing a trace as a fixture without redacting it. The gitleaks gate catches credentials, not personal data |
| I3 | Committed snapshot fixtures are redacted copies, reviewed like any other file | Treating fixtures as generated data nobody reads |

One CI detail is load-bearing enough to name: the workflow that builds and tests
a heal branch must run on `pull_request`, without secrets, and never on
`pull_request_target`. The latter runs with the base repository's secrets against
the PR's code — which would hand the agent's own output the credentials this
design spends its length keeping away from it. The repository already runs
`zizmor` over its workflows, which flags exactly this.

## What this asks of the repository

| Change | Where | Note |
| --- | --- | --- |
| Locators split from actions | `tools.py` plus a new locator module | Prerequisite for the narrow diff surface. Worth doing when browser actions are first written, not retrofitted |
| Tracing on, saved on failure | Wherever the browser context is built | `screenshots=True, snapshots=True`; discard on success |
| Failure triage and fingerprinting | New | Decides what is worth healing and what is escalated |
| Redaction | New, runs on the Pi | The piece with the most room to be wrong, and the one to test hardest |
| Snapshot replay harness | `tests/` | Also the shape of the committed regression tests |
| Heal workflow | `.github/workflows/` | Diff-surface check, replay, `make check`, PR creation |
| Branch ruleset and a scoped app token | Repository settings | Not code, and the load-bearing half of I2 |

Two existing constraints carry over unchanged: `deploy/` would sit outside every
quality gate unless linted deliberately, and CI never exercises arm64, so
anything that only breaks on the Pi still only surfaces on the Pi.

## Open questions

- **Whether redacted text is good enough to write locators from.** The whole
  verification story is worthless if the bundle is too scrubbed to fix anything.
  This needs real bundles from real failures, which means it cannot be settled
  until browser actions exist and have broken at least once.
- **Where bundles live.** They are short-lived, sensitive, and need to be
  readable by a CI job and nothing else. Encrypting them to a key only the
  healing job holds would make the storage choice much less interesting.
- **Whether the operator wants a PR at all**, versus a draft PR or an issue with
  a patch attached. A PR is the most convenient and the most auto-mergeable, and
  convenience is the direction this design is most likely to erode from.
- **How to notice the mechanism failing quietly** — heals that merge, deploy, and
  break something the snapshot could not show. Some of [`deployment.md`
  §8](deployment.md)'s per-call logging would be the raw material.
- **Whether the healing agent should propose changes to the site instead.** Where
  the operator controls the page, "add a test id" is the better fix, and it is a
  PR to a different repository. Out of scope here, but it is the version of this
  mechanism that makes the automation permanently less fragile rather than
  repeatedly repaired.

## References

- [Playwright tracing](https://playwright.dev/python/docs/trace-viewer) — DOM
  snapshots per action, and the warning that traces may contain sensitive data
- [`page.route_from_har()`](https://playwright.dev/python/docs/mock#mocking-with-har-files)
  — replaying a capture offline
- [Playwright locators](https://playwright.dev/python/docs/locators) — role and
  test-id addressing, and strict-mode ambiguity
- [`deployment.md`](deployment.md) — the mitigations this design assumes,
  particularly §6 (shell access), §7 (the profile at rest) and §8 (logging)
- [SDR 0001](sdr/0001-github-authentication.md) — why the trust boundary is where
  it is
