#!/usr/bin/env bash
# Pulls origin/main and restarts one app. Run only by
# browser-mcp-deploy@<app>.service (triggered by that app's
# browser-mcp-webhook@<app>.service via `sudo systemctl restart --no-block`) -
# never by hand unless you mean to discard any local changes in the checkout,
# which the reset below does.
#
# Everything this script needs beyond the checkout itself - the uv binary, the
# app's caches and Python build, its package, its unit, the browsers directory -
# comes from the environment the calling unit loads from
# /etc/browser-mcp/<app>/deploy.env, not from anything hardcoded here: this
# file lives in the checkout and is replaced by the very reset below, so it has
# to keep working with whatever the playbook last wrote there. See
# fleet/roles/app/templates/deploy.env.j2.
#
# Scope is deliberately narrow: a code-only change (git pull, dependency sync,
# restart). New apt packages, systemd unit changes, tunnel config changes and
# new browser builds all need a real playbook run - see fleet/README.md.
set -euo pipefail

: "${UV:?}"
: "${APP_DISTRIBUTION:?}"
: "${APP_SYSTEMD_UNIT:?}"
: "${PLAYWRIGHT_BROWSERS_PATH:?}"

# FETCH_HEAD rather than origin/main: a single-branch clone of another branch
# (fleet_repo_version) has no refspec that would update origin/main.
git fetch --depth 1 origin main
git reset --hard FETCH_HEAD

"${UV}" sync --frozen --no-dev --package "${APP_DISTRIBUTION}"

# The browsers directory is shared and root-owned, so a deploy cannot download
# a new build into it (docs/scaling-plan.md D3). A Playwright upgrade that needs
# one fails here, loudly, rather than restarting onto a browser that is not
# there; `ansible-playbook site.yml --tags browser` installs it.
.venv/bin/python - <<'PY'
from pathlib import Path

from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    chromium = Path(playwright.chromium.executable_path)
if not chromium.exists():
    raise SystemExit(
        f"This Playwright needs {chromium}, which is not installed. Run "
        "`ansible-playbook site.yml --tags browser`, then deploy again."
    )
PY

sudo /usr/bin/systemctl restart "${APP_SYSTEMD_UNIT}"
