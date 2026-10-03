# Provisioning the fleet

An Ansible playbook that provisions every app in [`apps.yml`](apps.yml) onto the
host it is assigned to, each behind that host's named Cloudflare Tunnel, with
every server bound to loopback. The architecture is
[`docs/scaling-plan.md`](../docs/scaling-plan.md); the single-host plan it grew
from, and the reasoning behind the topology, are in
[`docs/pi-deployment.md`](../docs/pi-deployment.md).

**Status:** one app (Sainsbury's) on one host (a Raspberry Pi 4). It is built
multi-host-shaped anyway, because placement is far cheaper to have from the start
than to retrofit (docs/scaling-plan.md D9). A host that ran the deployment from
before the fleet needs a one-off cut-over first, below. The first run against any
host should be `--check --diff`.

## The shape

| File | Says |
| --- | --- |
| [`apps.yml`](apps.yml) | Every app: the host it runs on, its package, ports, whether it is headed, its memory limit, any extra settings. The single source of truth |
| [`inventory/hosts.yml`](inventory/hosts.yml) | The hosts, and each one's memory budget |
| `inventory/group_vars/browser_mcp/local.yml` | Each app's public hostname, and any per-app setting tied to a real person (a site account's username). Gitignored; see [`local.yml.example`](inventory/group_vars/browser_mcp/local.yml.example) |
| `inventory/group_vars/browser_mcp/vault.yml` | The fleet's GitHub OAuth app, the tunnel's credentials, optionally a Cloudflare DNS token. Encrypted and gitignored; see [`vault.example.yml`](inventory/group_vars/browser_mcp/vault.example.yml). Nothing in it is per app |

**One playbook run covers the whole fleet.** The host-level roles run once per
host; every per-app step loops over the apps whose `host` is that host. Adding an
app is a diff to `apps.yml` and `local.yml`, not a new playbook.

On a host, each app gets:

| | |
| --- | --- |
| `bmcp-<app>` | Runs the app. Owns `/var/lib/browser-mcp/<app>` (`0700`): the site session, browser profile, caches and OAuth store. Reads its checkout, cannot write it |
| `bmcpd-<app>` | Owns `/opt/browser-mcp/<app>`: the checkout and its venv, uv's Python build, the app's own uv cache. May restart exactly this app's two units, through `/etc/sudoers.d/browser-mcp-<app>` |
| `/etc/browser-mcp/<app>/` | `env` (the app's settings), `webhook.env` and `webhook-secret` (its deploy webhook's), `deploy.env` (what its deploy needs). Read by systemd as root |
| `browser-mcp@<app>.service` | The app. An instance of one template unit; its command and `MemoryHigh` are in its drop-in, `browser-mcp@<app>.service.d/app.conf` |
| `browser-mcp-webhook@<app>.service` | Its deploy webhook receiver, as `bmcpd-<app>`, with its own secret |
| `browser-mcp-deploy@<app>.service` | The oneshot that receiver triggers: `fleet/deploy.sh` |

Two accounts per app, not one shared deploy account: a single account that could
write every checkout and restart every unit would be code execution as every app
at once, undoing the separation it appears to provide (D3). Shared by every app
on a host, and root-owned so no app's accounts can replace what another runs: the
Playwright browsers directory, the `browser-mcp.slice` that holds every app's
memory under the host's budget (D7), `cloudflared` and the uv binary.

## What you need first

On the control node (a laptop, not the Pi):

```sh
pipx install ansible-core          # or your package manager's ansible
cd fleet
ansible-galaxy collection install -r requirements.yml -p collections
```

On each host: a 64-bit Raspberry Pi OS install (trixie, current as of October
2025 — bookworm still works, but the apt package list in `roles/browser` assumes
trixie's names), SSH reachable with a key already installed, a sudo-capable
account, and the system `python3` that ships with it. The apps' own Python 3.13
is fetched by `uv` and is unrelated, even though trixie happens to ship the same
version system-wide.

Every `ansible-playbook` command below assumes key-based SSH and only prompts for
`--ask-become-pass` (sudo, asked *after* connecting) and `--ask-vault-pass`
(local, decrypts `vault.yml` — unrelated to the host at all). If a host still
needs a password to log in over SSH, add `--ask-pass` (`-k`) too.

Elsewhere, once for the fleet:

- **One GitHub OAuth app.** Every app needs two callbacks,
  `https://<app hostname>/auth/callback` and
  `https://<app hostname>/sainsburys-login/auth/callback`. Registering the fleet's
  domain once with wildcard matching enabled covers both for every app, present
  and future (docs/scaling-plan.md D10). Without it, register both URLs per app —
  GitHub scopes a redirect URI to sub-paths of the registered one, so the second
  is not implied by the first.
- **A named Cloudflare tunnel per host** — `cloudflared tunnel create <name>` on
  any machine logged in to Cloudflare. That command writes the credentials
  `vault_mcp_tunnel_credentials` needs to `~/.cloudflared/<tunnel-id>.json` — the
  home directory of whatever account ran it — not to `/etc/cloudflared/`, which
  doesn't exist until the `tunnel` role creates it.
- **Optionally, a Cloudflare API token** scoped to Zone → DNS → Edit on the one
  zone, vaulted as `vault_cloudflare_api_token` with `fleet_cloudflare_zone` in
  `local.yml`. With it, the playbook points each app's hostname at its host's
  tunnel itself; without it, the playbook prints the `cloudflared tunnel route dns`
  command for each.

## Running it

```sh
cd fleet
$EDITOR inventory/hosts.yml                            # each host's address and memory budget

cp inventory/group_vars/browser_mcp/local.yml.example \
   inventory/group_vars/browser_mcp/local.yml
$EDITOR inventory/group_vars/browser_mcp/local.yml     # each app's hostname

cp inventory/group_vars/browser_mcp/vault.example.yml \
   inventory/group_vars/browser_mcp/vault.yml
$EDITOR inventory/group_vars/browser_mcp/vault.yml     # then encrypt it:
ansible-vault encrypt inventory/group_vars/browser_mcp/vault.yml

ansible-playbook site.yml --ask-become-pass --ask-vault-pass --check --diff
ansible-playbook site.yml --ask-become-pass --ask-vault-pass
```

Before changing anything the playbook checks `apps.yml`: every entry complete,
every port unique across the fleet, every app on a host in the inventory with a
usable hostname, and the apps on each host within its `fleet_memory_budget_mb`.
CI runs the same checks (`make fleet`), so a bad entry fails its pull request.

`--check` is not a perfect dry run — the checkout, the environment sync and the
browser download all skip, so a first `--check` run on a fresh host reports less
than a first real run does. It is still the right way to read what is about to
change.

Afterwards, per app:

```sh
systemctl status 'browser-mcp@*' 'browser-mcp-webhook@*' cloudflared
journalctl -u browser-mcp@sainsburys -f
curl -sS http://127.0.0.1:8000/mcp     # 401 from the host itself is the good answer
```

Then add `https://<app hostname>/mcp` as a custom connector in claude.ai and run
the OAuth flow.

## Cutting over from the single-app deployment

A host provisioned before the fleet layer runs Sainsbury's as
`browser-interaction-mcp.service`, as `browsermcp`, from
`/opt/browser-interaction-mcp`, with its state in
`/var/lib/browser-interaction-mcp`. This is the one risky step in the scaling
plan: the state holds the logged-in site session, and losing it costs a real
login with MFA. So the playbook will not do it by surprise — until it has happened,
every run against that host stops at its pre-tasks unless given
`-e fleet_legacy_cutover=true`.

What the cut-over run does, in order, all in one playbook run:

1. Prepares `browser-mcp@sainsburys` completely — accounts, checkout, venv,
   configuration — while the old unit is still serving.
2. Stops and disables the old webhook receiver, any deploy it has in flight, and
   the old app.
3. Copies the old state directory, all of it, into
   `/var/lib/browser-mcp/sainsburys`, owned by `bmcp-sainsburys` with every mode
   kept. Copies, not moves: the original is left untouched.
4. Checks nothing in the copy is owned by anyone else, and that the site session
   file arrived byte-for-byte with its owner-only mode. If not, it stops there.
5. Records the cut-over in `/etc/browser-mcp/sainsburys/legacy-migrated`, so no
   later run does it again.
6. Starts `browser-mcp@sainsburys` and its webhook, on the same ports as before,
   so the tunnel's ingress does not change and the connector URL stays the same.

The new webhook receiver keeps the old one's secret, so CI's existing
`DEPLOY_WEBHOOK_SECRET` goes on working.

The steps:

1. **Move your local files across.** `vault.yml` and `local.yml` are gitignored,
   so they stayed in `deploy/` when the directory became `fleet/`:

   ```sh
   mv deploy/inventory/group_vars/browser_mcp/vault.yml \
      deploy/inventory/group_vars/browser_mcp/local.yml \
      fleet/inventory/group_vars/browser_mcp/
   ```

   The vault needs no edits; `vault_mcp_deploy_webhook_secret` is no longer read
   and can be deleted. In `local.yml`, `mcp_public_hostname` becomes
   `app_hostnames.sainsburys` and `mcp_sainsburys_username` becomes
   `app_settings.sainsburys.SAINSBURYS_USERNAME` — keep the hostname exactly as it
   was, or the connector in claude.ai needs re-adding. The playbook refuses to run
   with the old names. Then reinstall the collections in `fleet/`, as above.

2. **Rehearse it.** Read the diff: the old units stopping, the copy, the new
   units.

   ```sh
   ansible-playbook site.yml --ask-become-pass --ask-vault-pass --check --diff -e fleet_legacy_cutover=true
   ```

3. **Run it.**

   ```sh
   ansible-playbook site.yml --ask-become-pass --ask-vault-pass -e fleet_legacy_cutover=true
   ```

4. **Check it, against the stage's "done when".** Call `sainsburys_search` from
   claude.ai, and confirm the process serving it is the app's own account:

   ```sh
   ps -o user= -p "$(systemctl show -p MainPID --value browser-mcp@sainsburys)"   # bmcp-sainsburys
   ```

   Then `sainsburys_add_to_basket`, which needs the copied site session.

5. **Move CI's deploy secrets into the app's environment.** Each app's deploy job
   runs in a GitHub environment named after the app. Until that environment has
   secrets of its own it falls back to the repository's, which is what carried the
   cut-over. Create the `sainsburys` environment (Settings → Environments; limit
   its deployment branches to `main`), give it `DEPLOY_WEBHOOK_URL` (the same value
   as the repository secret) and `DEPLOY_WEBHOOK_SECRET`, without ever printing it:

   ```sh
   ssh <the-pi> sudo cat /etc/browser-mcp/sainsburys/webhook-secret \
     | gh secret set DEPLOY_WEBHOOK_SECRET --env sainsburys
   ```

   then delete the two repository-level secrets, so that a second app whose
   environment is missing them fails loudly rather than signing a request to
   Sainsbury's host.

6. **Retire what is left**, once the new unit has served real tool calls:

   ```sh
   ansible-playbook site.yml --ask-become-pass --ask-vault-pass --tags retire-legacy
   ```

   That removes the old units, sudoers rules and configuration, then lists what it
   deliberately leaves for you to remove by hand: the old state directory, browsers,
   checkout and uv directories, and the `browsermcp` and `deploy` accounts.

**Rolling back**, if the new unit does not serve: the old state is untouched and
the old units are only disabled, on the same ports, so

```sh
sudo systemctl disable --now browser-mcp@sainsburys browser-mcp-webhook@sainsburys
sudo systemctl enable --now browser-interaction-mcp deploy-webhook
```

restores the old deployment as it was before the cut-over. Anything the new unit
wrote meanwhile, such as a refreshed site session, stays in
`/var/lib/browser-mcp/sainsburys`. To try the cut-over again later, delete
`/etc/browser-mcp/sainsburys/legacy-migrated`; the next run with
`-e fleet_legacy_cutover=true` copies the old state over the new again.

**Order against merging.** Merge first, then cut over promptly, and merge nothing
else in between. The merge's own deploy still reaches the old unit, but the old
deploy unit runs `deploy/deploy.sh`, which no longer exists after that, so the old
deployment's code-only deploys stop working until the cut-over.

## Adding an app

1. The site package under `packages/<package>/`.
2. An entry in `apps.yml`: a host, ports no other app uses, `headed`, a
   `memory_high_mb` that fits the host's budget.
3. Its hostname in `local.yml`'s `app_hostnames` (or `fleet_domain`), and its
   personal settings, if any, in `app_settings`.
4. A playbook run.
5. Its GitHub environment, named after the app, with `DEPLOY_WEBHOOK_URL` and
   `DEPLOY_WEBHOOK_SECRET`, as in step 5 above.
6. The connector in claude.ai, and the site's first real login.

Nothing in the vault, and nothing in the OAuth app if it has wildcard matching on.
Make sure the new site's domain is **not** on the healing environment's network
allowlist (docs/scaling-plan.md R5).

## Moving an app to another host

Not a config edit: its state, and so its logged-in session, lives on the host it
is on. Stop it there, copy `/var/lib/browser-mcp/<app>` across with its
permissions intact, change its `host` in `apps.yml`, and run the playbook — which
creates the app on its new host and repoints its DNS record if the playbook
manages DNS. The playbook does not remove an app from a host it is no longer
assigned to: stop and disable its units there by hand. Decide placement
deliberately once rather than shuffling.

## Two phases: SD card first, SSD later

Storage doesn't have to arrive with the Pi. `storage_mount_ssd: false` (the
default) puts everything the apps write — every app's state and the shared
browsers — directly on the SD card, under `fleet_sd_state_root` and
`fleet_sd_browsers_dir`. Only `storage` and the two path variables it derives
care which phase you're in.

**Phase 2 (SSD attached).** Attach it, partition and `mkfs.ext4` it, then
`lsblk -f` on the Pi for its UUID. Set `storage_mount_ssd: true` and
`storage_ssd_uuid: <uuid>` in `vars.yml`, then:

```sh
ansible-playbook site.yml --tags migrate --ask-become-pass --ask-vault-pass
ansible-playbook site.yml --ask-become-pass --ask-vault-pass
```

The first command mounts the SSD and copies Phase 1's state across —
`roles/storage/tasks/migrate.yml` — stopping the host's apps first so nothing is
copied mid-write, and leaving the SD-card copy in place rather than deleting it.
It only runs when asked for with `--tags migrate` (it's tagged `never` as well),
and running it again once migrated is a safe no-op. The second command finishes
pointing everything at the SSD and restarts the apps.

If the whole system already boots from the SSD, skip both: `storage_mount_ssd:
false` is also correct there.

## Roles

| Role | Runs | Responsibility |
| --- | --- | --- |
| `base` | Once per host | apt upgrade, `unattended-upgrades`, key-only SSH, timezone, zram |
| `storage` | Once per host | The fleet's state root and the shared browsers directory; Phase 2's SSD mount and migration |
| `uv` | Once per host | The pinned `uv` binary |
| `browser` | Once per host | Chromium's apt libraries, Xvfb if any app on the host is headed, and — after every app is prepared — the browser build each app's Playwright needs |
| `app` | Per app | The shared template units and slice once, then per app: accounts, directories, checkout, `uv sync --package`, configuration, sudoers, drop-in, and finally starting it |
| `legacy` | Once per host | Detecting the single-app deployment, cutting it over, and — by name only — retiring it |
| `tunnel` | Once per host | `cloudflared`, the host's tunnel credentials, ingress generated from the host's apps, and their DNS records |

`site.yml` orders them so that nothing an app's traffic reaches changes until
everything it needs is ready. Each role is tagged with its own name, so
`--tags browser` re-runs just that part.

## Fast path: webhook-triggered code deploys

Re-running the playbook is the right tool for infra changes, but heavy for "one
Python file changed, ship it." Each app's webhook receiver
(`packages/core/src/browser_mcp_core/deploy_webhook.py`) lets GitHub Actions
trigger a code-only redeploy of that app — `git reset --hard` to `main`,
`uv sync --package`, a check that the browser it needs is installed, a restart —
without opening any inbound port.

After every push to `main` that passes CI, the `deploy targets` job works out
which apps it affects (`browser_mcp_core/deploy_targets.py`): a change to one
site's package redeploys that app, a change to core, the lockfile or
`fleet/deploy.sh` redeploys every app, and docs, tests, CI or `fleet/` redeploy
nothing. Then one `deploy` job per app signs a request with that app's
`DEPLOY_WEBHOOK_SECRET` and sends it to its `DEPLOY_WEBHOOK_URL`
(`https://<app hostname>/deploy-webhook`), both from the GitHub environment named
after the app.

Each app's secret is generated on its host by the playbook the first time and
kept in `/etc/browser-mcp/<app>/webhook-secret`; copy it into the app's
environment as shown in the cut-over's step 5.

**A Playwright upgrade that needs a new browser build is not a code-only
deploy.** The browsers directory is shared and root-owned, so no deploy can
write to it; `fleet/deploy.sh` notices the build is missing and fails before
restarting, and `ansible-playbook site.yml --tags browser` installs it.

## Manual path: publishing a branch over SSH

The webhook only ever ships `main`, and only once CI is green on it — that gate
is the point. To run something else on the real hardware first, publish a branch
to one app, as that app's deploy account:

```sh
ssh <your-admin-user>@<the-pi>
sudo -u bmcpd-sainsburys /opt/browser-mcp/sainsburys/checkout/fleet/deploy-branch.sh <branch>
```

It fetches and hard-resets that app's checkout to the branch, `uv sync`s and
restarts the app — the same steps `deploy.sh` runs for `main`, minus the CI gate
and the HMAC signature, which is why it refuses to run against `main` itself.
It is deliberately reachable only through a real SSH session and sudo, never a
webhook, MCP tool, cron job or anything unattended: the sudo is the authorisation
check.

## How the deployment mitigations land

Numbered against [`docs/deployment.md`](../docs/deployment.md).

| # | Mitigation | Where |
| --- | --- | --- |
| 1 | TLS, and the base URL FastMCP derives cookie security from | `BROWSER_MCP_GITHUB_OAUTH_BASE_URL` is each app's public https URL; `BROWSER_MCP_HOST` stays `127.0.0.1` and only `cloudflared` reaches it |
| 3 | Own the OAuth state | `XDG_DATA_HOME` puts each app's store in its own `0700` state directory, owned by its own service account |
| 4 | Rate limit at the edge | One process per app keeps its in-memory bucket honest; the unauthenticated half is Cloudflare's job and is **not** configured here |
| 5 | Handle the secrets as secrets | `ansible-vault` on the control node, root-owned `0600` environment files outside the git tree, `BROWSER_MCP_INCLUDE_ERROR_DETAILS=false` |
| 6 | Nobody gets a shell | Two `nologin`, locked accounts per app, the service account unable to write its own code, key-only SSH (`base_harden_ssh: true` — opt in once the key installed is somewhere durable). Each app's deploy account can restart only that app |
| 7 | Protect the browser profile at rest | Confined to the app's own `0700` state directory, owned by an account no other app shares — see the gap below |

## Gaps this playbook cannot close

Configuration cannot fix what the code does not expose. These are open:

- **§2, redirect URIs.** `allowed_client_redirect_uris` still defaults to
  `None`, which allows every URI. This needs a change in `auth.py`.
- **§3, `jwt_signing_key`.** Still derived from the client secret, so rotating
  that secret silently orphans every registered client and stored upstream
  token — and that secret is fleet-wide.
- **The login page's session key is the client secret too**, so it is shared by
  every app. docs/scaling-plan.md D10 recommends a per-app secret generated here,
  like the webhook's; it needs a setting in the code first.
- **§5, `LoadCredential`.** pydantic-settings reads the environment, not a
  credential file, so the units use `EnvironmentFile`.
- **§7, encryption at rest.** The profiles live on an unencrypted disk.
  Permissions are not encryption; anyone holding the disk holds the sessions.
- **§8, isolating the browser from the server.** Playwright starts inside each
  app's FastMCP process, so the process parsing untrusted page content holds the
  OAuth client secret, and the app unit cannot set `MemoryDenyWriteExecute`,
  `RestrictNamespaces` or `SystemCallFilter`, because Chromium needs all three.
- **§9, audit logging.** Nothing records which tool ran, when, or on whose
  authority.
- **The browser download runs an app's venv as root.** `playwright install`
  comes from each app's checkout, which that app's deploy account writes — the
  same trust the single-app deployment placed in its one deploy account.
- **No network egress control per app.** Per-user isolation covers files and
  processes; an app's compromised browser can still reach anywhere (D3).
- **The `uv` installer is fetched over HTTPS without a checksum.**

## Conventions worth keeping

- **`vault.yml` is never decrypted into the tree.** Use `ansible-vault edit`.
- **`local.yml` holds what is personal rather than secret**: hostnames, a site
  account's username. Gitignored, like `vault.yml`.
- **Nothing here templates a `.env`.** Settings read `.env` relative to the
  working directory and reject unknown `BROWSER_MCP_*` keys, which under systemd
  restart is a boot loop. Each app's `WorkingDirectory` is its state directory, and
  the playbook removes any `.env` it finds there or in the checkout.
- **`make fleet` before pushing**, which CI also runs: ansible-lint at the
  production profile, a syntax check, and [`tests/render.yml`](tests/render.yml),
  which renders every template against a two-host sample fleet and checks the
  cut-over is refused unless asked for.
- **CI never exercises arm64, systemd or the real accounts.** Those only ever
  meet on a host, which is why the first run against one is `--check --diff`.
