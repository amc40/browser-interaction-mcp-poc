"""Tests for the webhook receiver that triggers a code-only redeploy.

`Handler` is exercised over a real `http.server.HTTPServer` on an ephemeral
loopback port, driven by real HTTP requests — the module's whole job is a
few lines of stdlib glue around `verify_signature`/`should_deploy`, and that
glue is what is worth exercising for real rather than hand-building a fake
request object. `subprocess.run` is the one thing faked: nothing here should
ever really invoke sudo/systemctl.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import http.client
import logging
import socket
import subprocess
import threading
from dataclasses import dataclass, field
from http.server import HTTPServer
from typing import TYPE_CHECKING

import pytest

from browser_interaction_mcp import deploy_webhook

if TYPE_CHECKING:
    from collections.abc import Iterator

SECRET = b"test-shared-secret"


def _sign(body: bytes, *, secret: bytes = SECRET) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# verify_signature
# ---------------------------------------------------------------------------
def test_accepts_a_correctly_signed_body() -> None:
    body = b'{"ref": "refs/heads/main"}'
    assert deploy_webhook.verify_signature(SECRET, body, _sign(body))


def test_rejects_a_signature_made_with_the_wrong_secret() -> None:
    body = b'{"ref": "refs/heads/main"}'
    signature = _sign(body, secret=b"a-different-secret")
    assert not deploy_webhook.verify_signature(SECRET, body, signature)


def test_rejects_a_signature_for_a_tampered_body() -> None:
    signature = _sign(b'{"ref": "refs/heads/main"}')
    assert not deploy_webhook.verify_signature(
        SECRET, b'{"ref": "refs/heads/evil"}', signature
    )


def test_rejects_a_missing_header() -> None:
    assert not deploy_webhook.verify_signature(SECRET, b"{}", None)


def test_rejects_a_header_without_the_sha256_prefix() -> None:
    body = b"{}"
    bare_hex = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    assert not deploy_webhook.verify_signature(SECRET, body, bare_hex)


# ---------------------------------------------------------------------------
# should_deploy
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"ref": "refs/heads/main"}, True),
        ({"ref": "refs/heads/some-feature"}, False),
        ({}, False),
        ("not a dict", False),
        (None, False),
    ],
)
def test_should_deploy(payload: object, *, expected: bool) -> None:
    assert deploy_webhook.should_deploy(payload) is expected


# ---------------------------------------------------------------------------
# Handler, over a real HTTP request
# ---------------------------------------------------------------------------
@dataclass
class RecordingRun:
    """Fake for subprocess.run: records calls instead of shelling out."""

    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, args: list[str], *, check: bool = False) -> None:
        """Record the call instead of running it."""
        del check
        self.calls.append(list(args))


@contextlib.contextmanager
def _serving() -> Iterator[str]:
    """Run a real HTTPServer against Handler on an ephemeral loopback port.

    Shared by every test that needs to drive `Handler.do_POST` over a real
    request rather than call it directly - callers set whatever
    `Handler`/`subprocess.run` state they need via monkeypatch first.
    """
    server = HTTPServer(("127.0.0.1", 0), deploy_webhook.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.fixture
def deploy(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, RecordingRun]]:
    monkeypatch.setattr(deploy_webhook.Handler, "secret", SECRET)
    recording = RecordingRun()
    monkeypatch.setattr(subprocess, "run", recording)

    with _serving() as address:
        yield address, recording


def _post(address: str, body: bytes, *, signature: str | None) -> int:
    connection = http.client.HTTPConnection(address, timeout=5)
    try:
        headers = {}
        if signature is not None:
            headers["X-Deploy-Signature"] = signature
        connection.request("POST", "/", body=body, headers=headers)
        return connection.getresponse().status
    finally:
        connection.close()


def _post_with_raw_headers(address: str, extra_headers: str) -> int:
    # http.client always adds a Content-Length itself once a body is passed
    # to request(), even an empty one - a raw socket is the only way to send
    # a POST with no Content-Length at all, or a malformed one, which is what
    # _content_length() exists to reject.
    host, _, port = address.partition(":")
    request = (
        f"POST / HTTP/1.1\r\nHost: {host}\r\n{extra_headers}Connection: close\r\n\r\n"
    )
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        sock.sendall(request.encode())
        response = http.client.HTTPResponse(sock)
        response.begin()
        return response.status


def test_starts_the_deploy_unit_for_a_correctly_signed_main_push(
    deploy: tuple[str, RecordingRun],
) -> None:
    address, recording = deploy
    body = b'{"sha": "abc123", "ref": "refs/heads/main"}'

    status = _post(address, body, signature=_sign(body))

    assert status == 202
    assert recording.calls == [
        [
            "/usr/bin/sudo",
            "/usr/bin/systemctl",
            "restart",
            "--no-block",
            "deploy-browser-interaction-mcp.service",
        ]
    ]


def test_rejects_a_bad_signature_without_deploying(
    deploy: tuple[str, RecordingRun],
) -> None:
    address, recording = deploy
    body = b'{"sha": "abc123", "ref": "refs/heads/main"}'

    status = _post(address, body, signature=_sign(body, secret=b"wrong"))

    assert status == 403
    assert recording.calls == []


def test_ignores_a_correctly_signed_push_to_another_branch(
    deploy: tuple[str, RecordingRun],
) -> None:
    address, recording = deploy
    body = b'{"sha": "abc123", "ref": "refs/heads/some-feature"}'

    status = _post(address, body, signature=_sign(body))

    assert status == 204
    assert recording.calls == []


def test_rejects_a_body_over_the_size_cap(deploy: tuple[str, RecordingRun]) -> None:
    address, recording = deploy
    padding = b"x" * deploy_webhook._MAX_BODY_BYTES
    oversized = b'{"ref": "refs/heads/main", "pad": "' + padding + b'"}'

    status = _post(address, oversized, signature=_sign(oversized))

    assert status == 400
    assert recording.calls == []


def test_rejects_a_missing_content_length(deploy: tuple[str, RecordingRun]) -> None:
    address, recording = deploy

    status = _post_with_raw_headers(
        address, "X-Deploy-Signature: sha256=irrelevant\r\n"
    )

    assert status == 400
    assert recording.calls == []


def test_rejects_a_non_numeric_content_length(deploy: tuple[str, RecordingRun]) -> None:
    address, recording = deploy

    status = _post_with_raw_headers(address, "Content-Length: not-a-number\r\n")

    assert status == 400
    assert recording.calls == []


def test_rejects_a_negative_content_length(deploy: tuple[str, RecordingRun]) -> None:
    # A negative value would otherwise reach `self.rfile.read(negative)`,
    # which reads until EOF rather than erroring - on a live connection that
    # never comes, hanging the handler thread indefinitely.
    address, recording = deploy

    status = _post_with_raw_headers(address, "Content-Length: -1\r\n")

    assert status == 400
    assert recording.calls == []


def test_rejects_invalid_json(deploy: tuple[str, RecordingRun]) -> None:
    address, recording = deploy
    body = b"not json"

    status = _post(address, body, signature=_sign(body))

    assert status == 400
    assert recording.calls == []


def test_ignores_a_correctly_signed_non_dict_payload_without_crashing(
    deploy: tuple[str, RecordingRun],
) -> None:
    # Valid JSON, but not an object - `should_deploy` correctly says no, and
    # the "why" logging must not assume a dict either.
    address, recording = deploy
    body = b"123"

    status = _post(address, body, signature=_sign(body))

    assert status == 204
    assert recording.calls == []


@dataclass
class RaisingRun:
    """Fake for subprocess.run: always raises, to exercise the failure path."""

    def __call__(self, args: list[str], *, check: bool = False) -> None:
        """Simulate sudo/systemctl failing instead of running anything."""
        del args, check
        raise subprocess.CalledProcessError(returncode=1, cmd="systemctl")


def test_returns_502_when_the_restart_command_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deploy_webhook.Handler, "secret", SECRET)
    monkeypatch.setattr(subprocess, "run", RaisingRun())
    body = b'{"sha": "abc123", "ref": "refs/heads/main"}'

    with _serving() as address:
        status = _post(address, body, signature=_sign(body))

    assert status == 502


def test_restarts_a_custom_deploy_unit_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deploy_webhook.Handler, "secret", SECRET)
    monkeypatch.setattr(deploy_webhook.Handler, "deploy_unit", "custom.service")
    recording = RecordingRun()
    monkeypatch.setattr(subprocess, "run", recording)
    body = b'{"sha": "abc123", "ref": "refs/heads/main"}'

    with _serving() as address:
        status = _post(address, body, signature=_sign(body))

    assert status == 202
    assert recording.calls == [
        [
            "/usr/bin/sudo",
            "/usr/bin/systemctl",
            "restart",
            "--no-block",
            "custom.service",
        ]
    ]


def test_logs_an_escaped_ref_rather_than_a_raw_newline(
    deploy: tuple[str, RecordingRun],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A verified-but-not-main ref is logged - repr(), not a raw %s/%r
    # substitution, is what stops an attacker-chosen newline in it from
    # forging a second log line (CodeQL py/log-injection).
    address, recording = deploy
    caplog.set_level(logging.INFO, logger=deploy_webhook.__name__)
    body = b'{"ref": "refs/heads/evil\\ninjected line"}'

    status = _post(address, body, signature=_sign(body))

    assert status == 204
    assert recording.calls == []
    messages = [record.getMessage() for record in caplog.records]
    assert not any("\n" in message for message in messages)
    assert any("injected line" in message for message in messages)


def test_logs_an_escaped_sha_rather_than_a_raw_newline(
    deploy: tuple[str, RecordingRun],
    caplog: pytest.LogCaptureFixture,
) -> None:
    address, recording = deploy
    caplog.set_level(logging.INFO, logger=deploy_webhook.__name__)
    body = b'{"sha": "abc\\ndef", "ref": "refs/heads/main"}'

    status = _post(address, body, signature=_sign(body))

    assert status == 202
    assert recording.calls
    messages = [record.getMessage() for record in caplog.records]
    assert not any("\n" in message for message in messages)
    assert any("abc\\ndef" in message for message in messages)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
@dataclass
class FakeThreadingHTTPServer:
    """Stands in for the real server: records what it was built with."""

    address: tuple[str, int]
    handler_class: type
    served: bool = False

    def serve_forever(self) -> None:
        """Record that serving started, instead of actually blocking."""
        self.served = True


def test_main_reads_env_and_serves_on_loopback_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEPLOY_WEBHOOK_SECRET", "env-supplied-secret")
    monkeypatch.setenv("DEPLOY_WEBHOOK_PORT", "9876")
    monkeypatch.setenv("DEPLOY_ONESHOT_UNIT", "custom-deploy.service")
    created: list[FakeThreadingHTTPServer] = []

    def fake_server_class(
        address: tuple[str, int], handler_class: type
    ) -> FakeThreadingHTTPServer:
        server = FakeThreadingHTTPServer(address, handler_class)
        created.append(server)
        return server

    monkeypatch.setattr(deploy_webhook, "ThreadingHTTPServer", fake_server_class)

    deploy_webhook.main()

    assert created == [
        FakeThreadingHTTPServer(
            ("127.0.0.1", 9876), deploy_webhook.Handler, served=True
        )
    ]
    assert deploy_webhook.Handler.secret == b"env-supplied-secret"
    assert deploy_webhook.Handler.deploy_unit == "custom-deploy.service"


def test_main_defaults_the_port_and_deploy_unit_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEPLOY_WEBHOOK_SECRET", "env-supplied-secret")
    monkeypatch.delenv("DEPLOY_WEBHOOK_PORT", raising=False)
    monkeypatch.delenv("DEPLOY_ONESHOT_UNIT", raising=False)
    created: list[FakeThreadingHTTPServer] = []

    def fake_server_class(
        address: tuple[str, int], handler_class: type
    ) -> FakeThreadingHTTPServer:
        server = FakeThreadingHTTPServer(address, handler_class)
        created.append(server)
        return server

    monkeypatch.setattr(deploy_webhook, "ThreadingHTTPServer", fake_server_class)

    deploy_webhook.main()

    assert created[0].address == ("127.0.0.1", deploy_webhook._DEFAULT_PORT)
    assert deploy_webhook.Handler.deploy_unit == deploy_webhook._DEFAULT_DEPLOY_UNIT
