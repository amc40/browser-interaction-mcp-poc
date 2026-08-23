"""Webhook receiver that triggers a code-only redeploy on the Pi.

This is the fast path for "one Python file changed, ship it": GitHub Actions
signs a small JSON body and POSTs it here after CI passes on `main`; this
receiver checks the signature and asks systemd to run the actual
git-pull/uv-sync/restart sequence (`deploy/deploy.sh`, via
`deploy-browser-interaction-mcp.service`) — it never runs that sequence
itself. Anything infra-shaped (new apt packages, systemd unit changes, tunnel
config) is still an Ansible job; see docs/pi-deployment.md.

Deliberately dependency-free, and deliberately never imported as part of the
`browser_interaction_mcp` package: on the Pi it is invoked directly by file
path with the system interpreter —

    /usr/bin/python3 <checkout>/src/browser_interaction_mcp/deploy_webhook.py

— never via `-m browser_interaction_mcp.deploy_webhook` and never as a
`[project.scripts]` entry point through the venv. Both of those would import
`browser_interaction_mcp/__init__.py` (which pulls in FastMCP, pydantic, …)
or depend on `uv sync` having already succeeded, and a broken `uv sync` is
exactly the kind of bad commit this mechanism needs to survive redeploying
past. Keep this module's imports stdlib-only and free of any import from a
sibling module in this package — running a file directly by path gives it no
package context to resolve a relative import against, and ruff's
`ban-relative-imports = "all"` already forbids one anyway.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar, Final

logger = logging.getLogger(__name__)

_SIGNATURE_PREFIX: Final = "sha256="
_MAX_BODY_BYTES: Final = 16 * 1024
_EXPECTED_REF: Final = "refs/heads/main"
_DEFAULT_PORT: Final = 8787
# Only a fallback for standalone/local runs: on the Pi, main() always
# overrides this from DEPLOY_ONESHOT_UNIT, which env.j2 sets from the same
# Ansible variable (deploy_oneshot_systemd_unit) that actually names the
# installed unit and its sudoers rule — so the two can never drift the way a
# second hardcoded literal here would.
_DEFAULT_DEPLOY_UNIT: Final = "deploy-browser-interaction-mcp.service"


def verify_signature(secret: bytes, body: bytes, header: str | None) -> bool:
    """Check an `X-Deploy-Signature: sha256=<hex>` header against `body`.

    Constant-time by construction: `hmac.compare_digest`, not `==`, is what
    keeps a wrong-but-close guess from being distinguishable by timing.
    """
    if header is None or not header.startswith(_SIGNATURE_PREFIX):
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[len(_SIGNATURE_PREFIX) :], expected)


def should_deploy(payload: object) -> bool:
    """Whether a verified payload names the one branch this receiver deploys."""
    return isinstance(payload, dict) and payload.get("ref") == _EXPECTED_REF


class Handler(BaseHTTPRequestHandler):
    """Handles exactly one route: a signed POST / body.

    `secret` and `deploy_unit` are class attributes, not instance ones,
    because `ThreadingHTTPServer` constructs a fresh `Handler` per request —
    `main` sets them once before `serve_forever`.
    """

    secret: ClassVar[bytes] = b""
    deploy_unit: ClassVar[str] = _DEFAULT_DEPLOY_UNIT

    def do_POST(self) -> None:
        """Verify the signature, then hand off to the deploy unit."""
        content_length = self._content_length()
        if content_length is None or content_length > _MAX_BODY_BYTES:
            self._respond(400)
            return

        body = self.rfile.read(content_length)
        if not verify_signature(
            self.secret, body, self.headers.get("X-Deploy-Signature")
        ):
            logger.warning("Rejected deploy webhook: missing or invalid signature")
            self._respond(403)
            return

        try:
            payload = json.loads(body)
        except ValueError:
            logger.warning("Rejected deploy webhook: body was not valid JSON")
            self._respond(400)
            return

        if not should_deploy(payload):
            ref = payload.get("ref") if isinstance(payload, dict) else None
            # repr(), called here rather than via a %r format spec, so it's
            # an explicit transformation in the log call rather than
            # something deferred into logging's own lazy %-formatting - a
            # verified but untrusted ref could otherwise carry a newline and
            # forge extra log lines.
            logger.info("Ignoring verified webhook for ref=%s", repr(ref))
            self._respond(204)
            return

        sha = payload.get("sha") if isinstance(payload, dict) else None
        logger.info(
            "Verified webhook for %s; restarting %s", repr(sha), self.deploy_unit
        )
        try:
            # restart, not start: a oneshot unit that's still running an
            # earlier trigger merges a plain `start` into that in-flight job
            # rather than queuing a fresh one, so a second push landing while
            # the first is still deploying would otherwise be silently
            # dropped. restart supersedes it with a fresh run against
            # whatever is on main *now* - deploy.sh's git reset --hard and
            # uv sync --frozen are both fully reproducible, so a run
            # interrupted this way is repaired by the one that supersedes it.
            subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
                [
                    "/usr/bin/sudo",
                    "/usr/bin/systemctl",
                    "restart",
                    "--no-block",
                    self.deploy_unit,
                ],
                check=True,
            )
        except (OSError, subprocess.CalledProcessError):
            logger.exception("Failed to restart %s", self.deploy_unit)
            self._respond(502)
            return
        self._respond(202)

    def _content_length(self) -> int | None:
        raw = self.headers.get("Content-Length")
        if raw is None:
            return None
        try:
            value = int(raw)
        except ValueError:
            return None
        return value if value >= 0 else None

    def _respond(self, status: int) -> None:
        self.send_response(status)
        self.end_headers()


def main() -> None:
    """Serve the receiver on loopback until killed.

    Never binds anything but 127.0.0.1: the tunnel role's path-scoped
    ingress rule is what makes this reachable at all, the same way the app
    server itself is only ever reached through cloudflared.
    """
    logging.basicConfig(level=logging.INFO)
    Handler.secret = os.environ["DEPLOY_WEBHOOK_SECRET"].encode()
    Handler.deploy_unit = os.environ.get("DEPLOY_ONESHOT_UNIT", _DEFAULT_DEPLOY_UNIT)
    port = int(os.environ.get("DEPLOY_WEBHOOK_PORT", str(_DEFAULT_PORT)))

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    logger.info("Serving the deploy webhook on 127.0.0.1:%d", port)
    server.serve_forever()


if __name__ == "__main__":  # pragma: no cover - exercised by running it on the Pi
    main()
