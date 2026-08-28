#!/usr/bin/env bash
# Publishes an arbitrary branch's code to the Pi and restarts the service.
# Run BY HAND, over SSH, by an operator who already has sudo on the host -
# e.g.:
#
#   sudo -u deploy /opt/browser-interaction-mcp/deploy/deploy-branch.sh my-branch
#
# There is deliberately no other way to reach this. deploy_webhook.py
# (src/browser_interaction_mcp/, triggered by CI once it's green on `main`)
# and this script solve different problems and are not meant to converge:
# the webhook only ever takes a commit that has already passed CI on `main`,
# gated by an HMAC signature GitHub Actions computes - nobody decides that by
# hand. This script takes whatever branch you name, with no CI gate and no
# signature, which is fine for a human sitting at a real SSH session deciding
# to try it, and not fine for anything automated. Never wire this to a
# webhook, an MCP tool, a cron job, or any other network-reachable or
# unattended trigger - the sudo prompt an operator answers by hand *is* the
# authorisation check.
#
# Requires sudo (or being run as the deploy account directly): the deploy
# account is nologin (deploy/roles/deploy_account/tasks/main.yml), so this
# has to be reached with `sudo -u deploy`, same as `deploy.sh` is reached
# only via the oneshot systemd unit the webhook starts.
#
# Discards any local changes in the checkout, same as deploy.sh - and,
# because it can reset to any branch, discards whatever the checkout
# currently holds even if that was a *different* branch published this way
# earlier. There is no history of what has been published where; the Pi
# reflects whatever the last deploy-branch.sh or deploy.sh run left it at.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <branch>" >&2
  exit 1
fi
branch=$1

# main goes through deploy.sh (CI-gated, webhook-triggered) or a full
# ansible-playbook run, never this script - both skip that gate.
if [[ "${branch}" == "main" ]]; then
  echo "refusing to deploy 'main' with this script - it has no CI gate." >&2
  echo "main is deployed by the webhook (deploy.sh) once CI is green, or by" >&2
  echo "re-running the playbook. This script is for a branch that hasn't" >&2
  echo "gone through either yet." >&2
  exit 1
fi

# Same values Ansible templates into the oneshot deploy unit for the webhook
# path (roles/deploy_webhook/templates/deploy-browser-interaction-mcp.service.j2)
# and the paths roles/uv, roles/browser and roles/storage settle on for this
# host - overridable via the environment if a host is genuinely configured
# differently.
: "${MCP_CHECKOUT_DIR:=/opt/browser-interaction-mcp}"
: "${UV:=/usr/local/bin/uv}"
: "${UV_CACHE_DIR:=/var/cache/uv}"
: "${UV_PYTHON_INSTALL_DIR:=/opt/uv/python}"
: "${UV_PYTHON_DOWNLOADS:=automatic}"
: "${PLAYWRIGHT_BROWSERS_PATH:=/var/lib/browser-interaction-mcp-playwright}"
: "${MCP_SYSTEMD_UNIT:=browser-interaction-mcp}"
export UV_CACHE_DIR UV_PYTHON_INSTALL_DIR UV_PYTHON_DOWNLOADS PLAYWRIGHT_BROWSERS_PATH

cd "${MCP_CHECKOUT_DIR}"

echo "fetching origin/${branch}..."
git fetch --depth 1 origin "${branch}"
git reset --hard "origin/${branch}"

"${UV}" sync --frozen --no-dev
.venv/bin/playwright install chromium

# Same narrow NOPASSWD rule the webhook-triggered oneshot unit relies on
# (roles/deploy_account/templates/sudoers.j2): the deploy account can restart
# only this one unit, nothing else - this script asks for no more than
# deploy.sh already does.
sudo /usr/bin/systemctl restart "${MCP_SYSTEMD_UNIT}.service"

echo "deployed ${branch} ($(git rev-parse --short HEAD)) and restarted ${MCP_SYSTEMD_UNIT}.service"
