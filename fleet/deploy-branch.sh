#!/usr/bin/env bash
# Publishes an arbitrary branch's code to one app and restarts it. Run BY HAND,
# over SSH, by an operator who already has sudo on the host, as that app's
# deploy account - e.g. for the sainsburys app:
#
#   sudo -u bmcpd-sainsburys /opt/browser-mcp/sainsburys/checkout/fleet/deploy-branch.sh my-branch
#
# There is deliberately no other way to reach this. The deploy webhook
# (packages/core/src/browser_mcp_core/deploy_webhook.py, triggered by CI once
# it's green on `main`) and this script solve different problems and are not
# meant to converge: the webhook only ever takes a commit that has already
# passed CI on `main`, gated by an HMAC signature GitHub Actions computes -
# nobody decides that by hand. This script takes whatever branch you name, with
# no CI gate and no signature, which is fine for a human sitting at a real SSH
# session deciding to try it, and not fine for anything automated. Never wire
# this to a webhook, an MCP tool, a cron job, or any other network-reachable or
# unattended trigger - the sudo an operator types by hand *is* the
# authorisation check.
#
# The app is the one whose checkout this script is in; everything else comes
# from that app's /etc/browser-mcp/<app>/deploy.env, the same settings its
# webhook-triggered deploy uses.
#
# Discards any local changes in the checkout, same as deploy.sh - and, because
# it can reset to any branch, discards whatever the checkout currently holds
# even if that was a *different* branch published this way earlier. There is no
# history of what has been published where; the app reflects whatever the last
# deploy-branch.sh or deploy.sh run left it at.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <branch>" >&2
  exit 1
fi
branch=$1

# main goes through deploy.sh (CI-gated, webhook-triggered) or a full playbook
# run, never this script - both skip that gate.
if [[ "${branch}" == "main" ]]; then
  echo "refusing to deploy 'main' with this script - it has no CI gate." >&2
  echo "main is deployed by the webhook (deploy.sh) once CI is green, or by" >&2
  echo "re-running the playbook. This script is for a branch that hasn't" >&2
  echo "gone through either yet." >&2
  exit 1
fi

checkout=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
app=$(basename "$(dirname "${checkout}")")
: "${FLEET_CONFIG_ROOT:=/etc/browser-mcp}"

set -a
# shellcheck source=/dev/null
. "${FLEET_CONFIG_ROOT}/${app}/deploy.env"
set +a

cd "${checkout}"

echo "fetching origin/${branch} for ${app}..."
git fetch --depth 1 origin "${branch}"
git reset --hard FETCH_HEAD

"${UV}" sync --frozen --no-dev --package "${APP_DISTRIBUTION}"

# Only this app's own unit: the app's deploy account may restart nothing else
# (fleet/roles/app/templates/sudoers.j2), and asks for no more here than
# deploy.sh already does.
sudo /usr/bin/systemctl restart "${APP_SYSTEMD_UNIT}"

echo "deployed ${branch} ($(git rev-parse --short HEAD)) to ${app} and restarted ${APP_SYSTEMD_UNIT}"
