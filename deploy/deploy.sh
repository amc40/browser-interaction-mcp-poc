#!/usr/bin/env bash
# Pulls origin/main and restarts the service. Run only by
# deploy-browser-interaction-mcp.service (triggered by deploy-webhook.service
# via `sudo systemctl start ... --no-block`) - never by hand unless you mean
# to discard any local changes in the checkout, which the reset below does.
#
# Every path this script needs beyond the checkout itself - the uv binary,
# its cache/Python install dirs, the Playwright browsers dir, which unit to
# restart - comes from the environment the calling systemd unit sets, not
# from anything hardcoded here: this file lives in the checkout and is
# updated by the very `git reset` below, so it has to keep working across
# whatever Ansible variables were current when the *previous* deploy ran.
# See deploy/roles/deploy_webhook/templates/deploy-browser-interaction-mcp.service.j2
# for what sets them.
#
# Scope is deliberately narrow: a code-only change (git pull, dependency
# sync, browser download, restart). New apt packages, systemd unit changes
# or tunnel config changes still need a real Ansible run - see
# docs/pi-deployment.md.
set -euo pipefail

: "${UV:?}"
: "${MCP_SYSTEMD_UNIT:?}"

git fetch --depth 1 origin main
git reset --hard origin/main

"${UV}" sync --frozen --no-dev
.venv/bin/playwright install chromium

sudo /usr/bin/systemctl restart "${MCP_SYSTEMD_UNIT}.service"
