"""Tests for redaction of the server's own credentials."""

from __future__ import annotations

import base64
import logging
import urllib.parse
from typing import TYPE_CHECKING

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from browser_interaction_mcp.middleware import SecretRedactionMiddleware
from browser_interaction_mcp.redaction import (
    MIN_SECRET_LENGTH,
    SecretRedactingFilter,
    SecretRedactor,
    build_redactor,
    install_log_redaction,
)
from browser_interaction_mcp.server import build_server
from browser_interaction_mcp.settings import Settings

if TYPE_CHECKING:
    from tests.conftest import Authenticate

SECRET = "s3cret-client-secret-value"
PLACEHOLDER = "[redacted: github_client_secret]"


@pytest.fixture
def redactor() -> SecretRedactor:
    """Return a redactor holding one secret."""
    return SecretRedactor({"github_client_secret": SECRET})


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------
def test_replaces_the_raw_value(redactor: SecretRedactor) -> None:
    assert redactor.redact(f"token={SECRET}!") == f"token={PLACEHOLDER}!"


def test_leaves_unrelated_text_alone(redactor: SecretRedactor) -> None:
    assert redactor.redact("nothing to see") == "nothing to see"


def test_replaces_every_occurrence(redactor: SecretRedactor) -> None:
    assert redactor.redact(f"{SECRET} and {SECRET}").count("[redacted") == 2


@pytest.mark.parametrize(
    "encode",
    [
        pytest.param(lambda v: urllib.parse.quote(v, safe=""), id="percent"),
        pytest.param(urllib.parse.quote_plus, id="form"),
        pytest.param(lambda v: base64.b64encode(v.encode()).decode(), id="base64"),
        pytest.param(
            lambda v: base64.urlsafe_b64encode(v.encode()).decode(),
            id="base64-urlsafe",
        ),
    ],
)
def test_replaces_encoded_forms(encode: object) -> None:
    """A secret is redacted however it was encoded on its way out."""
    secret = "pa/ss wo+rd?=value"
    redactor = SecretRedactor({"credential": secret})
    assert callable(encode)
    assert redactor.redact(f"x{encode(secret)}y") == "x[redacted: credential]y"


def test_replaces_json_escaped_form() -> None:
    """Backslash escapes in a JSON body are a distinct string to match."""
    secret = 'quote"and\\backslash'
    redactor = SecretRedactor({"credential": secret})
    assert redactor.redact(r'{"k": "quote\"and\\backslash"}') == (
        '{"k": "[redacted: credential]"}'
    )


def test_replaces_the_longer_of_two_overlapping_secrets() -> None:
    """A secret containing another is replaced whole, not cut in half."""
    redactor = SecretRedactor({"short": "aaaaaaaaaa", "long": "aaaaaaaaaa-and-a-tail"})
    assert redactor.redact("aaaaaaaaaa-and-a-tail") == "[redacted: long]"


def test_refuses_a_secret_too_short_to_redact_safely(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Redacting a short value would corrupt unrelated text, so it is refused."""
    short = "a" * (MIN_SECRET_LENGTH - 1)
    with caplog.at_level(logging.WARNING):
        redactor = SecretRedactor({"tiny": short})

    assert redactor.redact(f"{short} appears in ordinary words") == (
        f"{short} appears in ordinary words"
    )
    assert "tiny" in caplog.text


# --------------------------------------------------------------------------
# Registration from settings
# --------------------------------------------------------------------------
def test_build_redactor_covers_every_secret_field() -> None:
    """Any SecretStr on Settings is registered, with no second place to add it."""
    settings = Settings(
        transport="http",
        github_client_id="Ov23li",
        github_client_secret=SECRET,  # type: ignore[arg-type]
    )
    assert build_redactor(settings).redact(SECRET) == PLACEHOLDER


def test_build_redactor_tolerates_settings_with_no_secrets() -> None:
    assert build_redactor(Settings()).redact(SECRET) == SECRET


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
def _record(**kwargs: object) -> logging.LogRecord:
    """Build a log record, overriding any of its fields."""
    defaults: dict[str, object] = {
        "name": "test",
        "level": logging.ERROR,
        "pathname": __file__,
        "lineno": 1,
        "msg": "no secret here",
        "args": None,
        "exc_info": None,
    }
    return logging.LogRecord(**(defaults | kwargs))  # type: ignore[arg-type]


def test_log_filter_redacts_the_message(redactor: SecretRedactor) -> None:
    record = _record(msg=f"failed with {SECRET}")
    assert SecretRedactingFilter(redactor).filter(record)
    assert SECRET not in record.getMessage()


def test_log_filter_redacts_positional_arguments(redactor: SecretRedactor) -> None:
    record = _record(msg="failed with %s and %d", args=(SECRET, 1))
    SecretRedactingFilter(redactor).filter(record)
    assert SECRET not in record.getMessage()
    assert record.args == (PLACEHOLDER, 1)


def test_log_filter_redacts_mapping_arguments(redactor: SecretRedactor) -> None:
    # A mapping is passed as a one-tuple and unwrapped by LogRecord, the same
    # way `logger.error("%(k)s", {"k": ...})` does it.
    record = _record(msg="failed with %(k)s", args=({"k": SECRET},))
    SecretRedactingFilter(redactor).filter(record)
    assert SECRET not in record.getMessage()


def test_log_filter_redacts_the_traceback(redactor: SecretRedactor) -> None:
    """A traceback is rendered by the formatter, far too late for `msg`."""
    msg = f"upstream rejected {SECRET}"
    try:
        raise RuntimeError(msg)  # noqa: TRY301 - the traceback is the point
    except RuntimeError as exc:
        record = _record(
            msg="call failed",
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    SecretRedactingFilter(redactor).filter(record)

    assert record.exc_text is not None
    assert SECRET not in record.exc_text
    assert PLACEHOLDER in record.exc_text


def test_log_filter_leaves_a_record_without_a_traceback_alone(
    redactor: SecretRedactor,
) -> None:
    record = _record()
    SecretRedactingFilter(redactor).filter(record)
    assert record.exc_text is None


def test_install_log_redaction_covers_propagated_records(
    redactor: SecretRedactor,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The filter goes on handlers, so records from any logger are covered."""
    handler = logging.StreamHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        install_log_redaction(redactor)
        assert any(isinstance(f, SecretRedactingFilter) for f in handler.filters)

        with caplog.at_level(logging.ERROR):
            caplog.handler.addFilter(SecretRedactingFilter(redactor))
            logging.getLogger("some.library").error("leaked %s", SECRET)
        assert SECRET not in caplog.text
    finally:
        root.removeHandler(handler)


# --------------------------------------------------------------------------
# Middleware
# --------------------------------------------------------------------------
async def _call_through_middleware(exc: Exception | None) -> BaseException | None:
    """Run one tool call through the middleware, returning whatever escaped."""
    middleware = SecretRedactionMiddleware(
        SecretRedactor({"github_client_secret": SECRET}),
    )

    async def call_next(_: object) -> str:
        if exc is not None:
            raise exc
        return "fine"

    try:
        await middleware.on_call_tool(None, call_next)  # type: ignore[arg-type]
    except Exception as escaped:  # noqa: BLE001 - the object under test
        return escaped
    return None


async def test_middleware_passes_a_successful_call_through() -> None:
    assert await _call_through_middleware(None) is None


async def test_middleware_redacts_a_failing_call() -> None:
    escaped = await _call_through_middleware(
        RuntimeError(f"upstream rejected {SECRET}"),
    )
    assert str(escaped) == f"upstream rejected {PLACEHOLDER}"


async def test_middleware_keeps_the_exception_type() -> None:
    assert isinstance(await _call_through_middleware(ValueError(SECRET)), ValueError)


async def test_middleware_leaves_non_string_arguments_alone() -> None:
    escaped = await _call_through_middleware(RuntimeError(SECRET, 42))
    assert escaped is not None
    assert escaped.args == (PLACEHOLDER, 42)


async def test_tool_errors_reach_the_client_redacted(
    authenticate: Authenticate,
) -> None:
    """The wiring holds: a secret in a tool's error never leaves the server."""
    settings = Settings(
        include_error_details=True,
        github_client_secret=SECRET,  # type: ignore[arg-type]
    )
    server = build_server(settings)

    @server.tool
    def explode() -> str:
        """Fail in a way that quotes the operator's own client secret."""
        msg = f"upstream rejected {SECRET}"
        raise RuntimeError(msg)

    authenticate()
    async with Client(server) as client:
        with pytest.raises(ToolError) as raised:
            await client.call_tool("explode")

    assert SECRET not in str(raised.value)
    assert PLACEHOLDER in str(raised.value)
