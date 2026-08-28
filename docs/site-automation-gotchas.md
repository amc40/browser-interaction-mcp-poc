# Site automation gotchas

Concrete things that broke while driving the real Sainsbury's site, kept here
so the next site (or a generalisation of this POC) doesn't rediscover them the
hard way. Each is a pattern, not a one-off.

## Consent banners (OneTrust, and CMPs in general)

- **Injected asynchronously, *after* the `load` event.** Checking for the
  banner once, right after `goto`, finds nothing; it appears a beat later and
  its full-page backdrop (`.onetrust-pc-dark-filter`) then **silently
  intercepts every click** - Playwright reports a timeout on some unrelated
  element, not "a banner is in the way".
- **Pre-seed its cookies instead of clicking it.** Do a reject-all/minimal
  choice once in a real browser, copy the resulting cookies
  (`OptanonConsent`, `OptanonAlertBoxClosed` for OneTrust), and set them on the
  context *before the first navigation*. The banner then never renders. This is
  steadier than racing to click a button whose text keeps changing ("Required
  only" -> "Continue without accepting" / "Accept all cookies" within weeks).
- **Cookie domain: use the registrable domain** (`.sainsburys.co.uk`), not a
  host-specific one. OneTrust reads consent from the registrable domain on
  every subdomain; a `.www.sainsburys.co.uk` cookie was not even visible to
  `document.cookie` on `www.sainsburys.co.uk`. One `.sainsburys.co.uk` entry
  covers `www`, `account`, and the rest.
- **Find the cookie shape upfront**, don't reverse-engineer by clicking: the
  CMP-generated **cookie declaration table** on the site's cookie-policy page
  lists every cookie with its domain and expiry; DevTools -> Application ->
  Cookies after a manual visit is the definitive check; OneTrust documents the
  `OptanonConsent` value format (`groups=C000n:0/1`, ...).
- Stable hooks when you *do* have to touch it: `#onetrust-banner-sdk`
  (container), `#onetrust-accept-btn-handler` / `#onetrust-reject-all-handler`
  (buttons). These ids have been constant across OneTrust deployments for
  years; the button *text* has not.

## Login flows

- **The login page is often a redirect shell.**
  `www.sainsburys.co.uk/gol-ui/oauth/login` bounces to
  `account.sainsburys.co.uk/gol/login?login_challenge=...` (an Ory-style IdP).
  `goto(LOGIN_URL)` and then immediately filling fields races the redirect -
  wait for the actual form (`get_by_test_id("username")`) to be visible first.
- **Decide "logged in?" by an element, never by the URL.** These SPAs redirect
  *through* login-shaped URLs even on the **success** path - visiting an
  account page triggers a silent session check that passes through
  `/gol/login` before landing. A URL match ("am I on /gol/login?") gives false
  failures. Check whether the login *form* is actually on screen.
- **Same for MFA.** Detect the step by the verification-code field appearing,
  not by a `/mfa` URL fragment.
- **Wait after every form submit that navigates**, including the OTP submit -
  the submit kicks off the redirect that actually completes the login, and
  jumping to the next `goto` too early abandons it.

## Navigation / waiting

- **`wait_until="load"` is unreliable** on these sites: a background poller
  keeps the network busy, so `load` (and `networkidle`) eat the full timeout on
  every call. Use `wait_until="domcontentloaded"` and then wait for the
  specific element that matters. Where a `goto` is only a probe (e.g. "does
  visiting the account page bounce me to login?"), wrap it in
  `contextlib.suppress(PlaywrightTimeoutError)` and read the result off the
  page afterwards.

## Bot detection

- **Akamai Bot Manager blocks *headless* Chromium specifically.** Headed
  Chromium under a virtual display (Xvfb) with a real UA gets through with the
  identical navigation. This is per-site; assume any large retailer does it.

## storage_state + added cookies

- `browser.new_context(storage_state=...)` then `context.add_cookies(...)`:
  the added cookies win on a name+domain+path collision, but **domain format
  matters** - a leading-dot host cookie (`.www.example.com`) may land in the
  jar (`context.cookies()` shows it) yet not be visible to `document.cookie`
  on that host. Prefer the registrable domain.

## Debugging aids that touch credentials

- A failure screenshot can show the typed username or an on-screen OTP. Keep
  it owner-only (`0600`) in a `0700` dir, ideally under the service's
  `PrivateTmp`, and delete it on a short retention window.

## MCP client capabilities

- **Claude.ai's MCP client supports tool calls only** - no elicitation (form
  or URL mode), no sampling. Anything that needs a secret from the user has to
  go out of band (a browser page the tool returns a link to). See
  `login_routes.py`.
