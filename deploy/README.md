# Provisioning the Pi

An Ansible playbook implementing the plan in
[`docs/pi-deployment.md`](../docs/pi-deployment.md): a Raspberry Pi 4 serving
`browser-interaction-mcp` to claude.ai through a named Cloudflare Tunnel, with
the server itself bound to loopback.

**Status: drafted, never run against real hardware.** It is written to be read
before it is trusted, and the first run should be `--check --diff`.

The justification for automating a single host is recovery: when the SD card
fails, provisioned means one command rather than an evening rediscovering which
libraries Chromium needed.

## What you need first

On the control node (a laptop, not the Pi):

```sh
pipx install ansible-core          # or your package manager's ansible
ansible-galaxy collection install -r requirements.yml -p collections
```

On the Pi: a 64-bit Raspberry Pi OS install (trixie, current as of October
2025 — bookworm still works, but the apt package list in `roles/browser`
assumes trixie's names), SSH reachable with a key already installed, a
sudo-capable account, and the system `python3` that ships with it. The
application's own Python 3.13 is fetched by `uv` and is unrelated, even though
trixie happens to ship the same version system-wide.

Elsewhere, once:

- A **GitHub OAuth app** with the callback `https://<your-host>/auth/callback`.
- A **named Cloudflare tunnel** — `cloudflared tunnel create browser-interaction-mcp`
  on any machine logged in to Cloudflare — and its DNS route:
  `cloudflared tunnel route dns <tunnel-id> <your-host>`. Neither is automated
  here: both are one-off account-level operations that want a human and a
  browser login, not a playbook. That command writes the credentials
  `vault_mcp_tunnel_credentials` needs to `~/.cloudflared/<tunnel-id>.json` —
  the *home directory of whatever account ran it* (`/root/.cloudflared/` if
  that was via sudo) — not `/etc/cloudflared/`, which doesn't exist until the
  `tunnel` role creates it. See `vault.example.yml` for the exact fields.

## Two phases: SD card first, SSD later

Storage doesn't have to arrive with the Pi. `storage_mount_ssd: false` (the
default) puts everything a running server writes — browser profile, cache,
OAuth token store, downloaded Chromium — directly on the SD card, at
`mcp_sd_state_dir` / `mcp_sd_browsers_dir`. Everything else in this playbook
works identically either way; only `storage` and the two path variables it
derives care which phase you're in.

**Phase 1 (SD card only).** Nothing to configure beyond what's below —
`storage_mount_ssd: false` is already the default. Run the playbook.

**Phase 2 (SSD attached).** Once you have one: attach it, partition and
`mkfs.ext4` it, then `lsblk -f` on the Pi for its UUID. Set
`storage_mount_ssd: true` and `storage_ssd_uuid: <uuid>` in `vars.yml`, then:

```sh
ansible-playbook site.yml --tags migrate --ask-become-pass --ask-vault-pass
ansible-playbook site.yml --ask-become-pass --ask-vault-pass
```

The first command mounts the SSD and copies Phase 1's state across —
`roles/storage/tasks/migrate.yml` — stopping the service first so nothing is
copied mid-write, and leaving the SD-card copy in place rather than deleting
it: confirm the server actually works from the SSD before removing that by
hand. It only runs when asked for `--tags migrate` explicitly (it's tagged
`never` as well), so an ordinary run can never trigger it by accident, and
running it again once already migrated is a safe no-op — it checks first and
says so. The second command is a normal run: it finishes pointing everything
at the SSD and restarts the service, since the migrate-only pass deliberately
leaves that to it.

If the whole system already boots from the SSD (no separate mount to manage),
skip both: `storage_mount_ssd: false` is also correct there, and Phase 1's
default paths just happen to already be wherever the SD-card equivalent would
have been — no migration needed.

## Running it

```sh
cd deploy
$EDITOR inventory/hosts.yml                            # the Pi's address
$EDITOR inventory/group_vars/browser_mcp/vars.yml      # SSD UUID (Phase 2), user ID

cp inventory/group_vars/browser_mcp/local.yml.example \
   inventory/group_vars/browser_mcp/local.yml
$EDITOR inventory/group_vars/browser_mcp/local.yml     # your tunnel's hostname

cp inventory/group_vars/browser_mcp/vault.example.yml \
   inventory/group_vars/browser_mcp/vault.yml
$EDITOR inventory/group_vars/browser_mcp/vault.yml     # then encrypt it:
ansible-vault encrypt inventory/group_vars/browser_mcp/vault.yml

ansible-playbook site.yml --ask-become-pass --ask-vault-pass --check --diff
ansible-playbook site.yml --ask-become-pass --ask-vault-pass
```

Already installed `cloudflared` and created the tunnel by hand before running
this? That's fine — every task in the `tunnel` role uses ordinary idempotent
Ansible modules (`apt`, `group`, `user`, `template`, `copy`, `systemd_service`)
rather than one-shot commands, so re-running it converges rather than erroring
or duplicating anything. The one thing it actively fixes rather than just
tolerates: Cloudflare's own manual-install docs write an old one-line-format
apt source that would otherwise sit alongside the one this role manages, so
the role removes it first. It's still on you to make sure `vault.yml` holds
the *real* tunnel ID and credentials from what you already created, not the
placeholders from `vault.example.yml` — `site.yml` asserts that before
touching anything, specifically because this role writes those straight over
whatever's already at `/etc/cloudflared`.

`--check` is not a perfect dry run — the environment sync, the browser download
and the Playwright detection all skip, so a first `--check` run reports less
than a first real run does. It is still the right way to read what is about to
change.

Afterwards:

```sh
systemctl status browser-interaction-mcp cloudflared
journalctl -u browser-interaction-mcp -f
curl -sS http://127.0.0.1:8000/mcp     # 401 from the Pi itself is the good answer
```

Then add `https://<your-host>/mcp` as a custom connector in claude.ai and run
the OAuth flow. Verify that flow before building anything else on top: claude.ai
requires OAuth 2.1 with PKCE `S256`, and it decides whether the whole approach
is viable.

## Roles

| Role | Responsibility |
| --- | --- |
| `base` | apt upgrade, `unattended-upgrades`, key-only SSH, timezone, zram, and the service account |
| `storage` | Phase 1: SD-card state paths. Phase 2: mount the SSD, migrate onto it |
| `uv` | Install `uv`, check out the application, `uv sync --frozen --no-dev` |
| `browser` | An explicit apt library list, then `playwright install chromium` |
| `app` | systemd unit, `EnvironmentFile`, the service it runs as |
| `tunnel` | `cloudflared` from Cloudflare's apt repository, named-tunnel credentials, its own unit |

Two departures from the table in `docs/pi-deployment.md`, both because of
ordering: the service account is created in `base` rather than `app`, since
`storage` has to chown paths to it first; and the git checkout lives in `uv`,
since it is the thing being synced.

Each role is tagged with its own name, so `--tags browser` re-runs just that
part. Always include `app` when using tags: the `uv` and `browser` roles notify
its restart handler. `migrate` is the exception: it's not a role but a
one-shot task inside `storage`, tagged `never` as well, so it only ever runs
when named explicitly — see "Two phases" above.

## How the deployment mitigations land

Numbered against [`docs/deployment.md`](../docs/deployment.md).

| # | Mitigation | Where |
| --- | --- | --- |
| 1 | TLS, and the base URL FastMCP derives cookie security from | `BROWSER_MCP_GITHUB_OAUTH_BASE_URL` is the public https URL; `BROWSER_MCP_HOST` stays `127.0.0.1` and only `cloudflared` reaches it |
| 3 | Own the OAuth state | `XDG_DATA_HOME` puts the store on the SSD at mode `0700`, owned by the service account |
| 4 | Rate limit at the edge | One process keeps the in-memory bucket honest; the unauthenticated half is Cloudflare's job and is **not** configured here |
| 5 | Handle the secrets as secrets | `ansible-vault` on the control node, `EnvironmentFile` at `0600` outside the git tree, `BROWSER_MCP_INCLUDE_ERROR_DETAILS=false` |
| 6 | Nobody gets a shell | Dedicated system account, `nologin`, locked password, root-owned checkout it cannot rewrite, key-only SSH, nothing else on the host |
| 7 | Protect the browser profile at rest | Confined to the service account's `0700` state directory — see the gap below |

## Gaps this playbook cannot close

Configuration cannot fix what the code does not expose. These are open, and
naming them here is cheaper than rediscovering them later:

- **§2, redirect URIs.** `allowed_client_redirect_uris` still defaults to
  `None`, which allows every URI. There is no setting to pin it, so this needs a
  change in `auth.py` before a playbook can configure it.
- **§3, `jwt_signing_key`.** Still derived from the client secret, so rotating
  that secret silently orphans every registered client and stored upstream
  token. `settings.py` has no field for it, and `extra="forbid"` means the
  environment file cannot smuggle one in.
- **§5, `LoadCredential`.** The better home for the client secret, but
  pydantic-settings reads the environment, not a credential file. The unit uses
  `EnvironmentFile` until the code can read one.
- **§7, encryption at rest.** The profile lives on an unencrypted ext4 SSD.
  Permissions are not encryption; anyone holding the disk holds the sessions.
- **§8, audit logging.** Nothing records which tool ran, when, or on whose
  authority.
- **Playwright is not a dependency yet.** `tools.py` exposes only `server_info`,
  so the `browser` role installs the apt libraries and then skips the browser
  download with a message. It becomes live the moment Playwright is added to
  `pyproject.toml`.
- **The `uv` installer is fetched over HTTPS without a checksum.** Pinning a
  known digest per `uv_version` would close it.

## Conventions worth keeping

- **`vault.yml` is never decrypted into the tree.** Use `ansible-vault edit`,
  which decrypts to a temporary file. The repository's gitleaks gate is a
  backstop, not the plan.
- **`local.yml` holds the one non-secret that's still personal**: the tunnel's
  public hostname. It doesn't need vault-grade encryption — it isn't a
  credential — but it's a real domain tied to one person, so it's gitignored
  the same way `vault.yml` is rather than living in tracked `vars.yml`.
- **Nothing here templates a `.env`.** `settings.py` reads `.env` relative to
  the working directory and rejects unknown `BROWSER_MCP_*` keys, which under
  systemd restart is a boot loop. The unit's `WorkingDirectory` is the state
  directory, and the `app` role removes any `.env` it finds in either that or
  the checkout.
- **Lint before pushing:** `ansible-lint` from this directory, configured by
  `.ansible-lint`. `deploy/` falls outside every gate in the README's quality
  table — mypy is scoped to `src` and `tests`, ruff only inspects Python — so
  wiring `ansible-lint` into `make check` is the way to keep that standard
  consistent.
- **CI never exercises arm64.** Browser-launch breakage will only ever appear on
  the Pi.
