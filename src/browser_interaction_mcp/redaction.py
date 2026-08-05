"""Redaction of the credentials this server holds, from anything it emits.

The server knows a small number of secrets: they arrive through
:class:`~browser_interaction_mcp.settings.Settings` as ``SecretStr`` fields.
``SecretStr`` keeps them out of reprs and tracebacks, but that protection ends
at ``get_secret_value()`` - past that call the value is an ordinary string that
can reach a log line, an error returned to a caller, or, once tools drive a
browser, a page.

Redaction here is by **exact match against known values**, not by pattern. That
is deliberately narrow. It cannot find a credential the server was never told
about - a token the automated site happens to put in its own markup is out of
scope - but for the ones it does hold there are no false negatives and no regex
to keep tuned.

The convention that makes it complete is that a credential is a ``SecretStr``
field on ``Settings``. :func:`build_redactor` walks those fields, so adding one
registers it; there is no second place to remember.
"""

from __future__ import annotations

import base64
import json
import logging
import traceback
import urllib.parse
from typing import TYPE_CHECKING, override

from pydantic import SecretStr

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from browser_interaction_mcp.settings import Settings

logger = logging.getLogger(__name__)

MIN_SECRET_LENGTH = 8
"""Shortest value worth redacting.

A short secret turns up in ordinary text by coincidence, so redacting one would
scribble over unrelated output *and* advertise that something matched. Values
below this length are refused rather than half-handled, loudly enough that the
refusal is not discovered later.
"""


def _encodings(value: str) -> Iterator[str]:
    """Yield the forms a secret takes on its way out of the process.

    Matching only the raw bytes is the usual way a redactor is quietly wrong: a
    secret in a URL is percent-encoded, in a ``Basic`` header it is base64, and
    in a JSON body it may carry backslash escapes. Each is a distinct string
    that has to be searched for in its own right.

    Args:
        value: The secret, as held in configuration.

    Yields:
        The secret as written, then each encoding of it. Duplicates are
        possible - an alphanumeric secret percent-encodes to itself - and are
        collapsed by the caller.
    """
    encoded = value.encode()
    yield value
    yield urllib.parse.quote(value, safe="")
    yield urllib.parse.quote_plus(value)
    yield base64.b64encode(encoded).decode()
    yield base64.urlsafe_b64encode(encoded).decode()
    yield json.dumps(value)[1:-1]


class SecretRedactor:
    """Replaces known secret values, in any encoding, with a labelled marker."""

    def __init__(self, secrets: Mapping[str, str]) -> None:
        """Compile the replacement table.

        Args:
            secrets: The values to redact, keyed by the name they are labelled
                with. Names reach callers in place of the value, so they should
                identify the setting rather than describe the credential.
        """
        replacements: dict[str, str] = {}
        for label, value in secrets.items():
            if len(value) < MIN_SECRET_LENGTH:
                logger.warning(
                    "Not redacting %s: shorter than %d characters, so redacting "
                    "it would corrupt unrelated output. Treat its value as "
                    "public, or lengthen it.",
                    label,
                    MIN_SECRET_LENGTH,
                )
                continue
            for encoded in _encodings(value):
                replacements.setdefault(encoded, f"[redacted: {label}]")
        # Longest first, so that a secret containing another is replaced whole
        # rather than being cut in half by the shorter one.
        self._replacements = tuple(
            sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True),
        )

    def redact(self, text: str) -> str:
        """Return ``text`` with every known secret replaced.

        Args:
            text: Text that may contain a secret.

        Returns:
            The text, with each match replaced by a marker naming the setting
            it came from.
        """
        for needle, placeholder in self._replacements:
            text = text.replace(needle, placeholder)
        return text


def build_redactor(settings: Settings) -> SecretRedactor:
    """Build a redactor covering every secret on ``settings``.

    Args:
        settings: Configuration to take the secrets from. Every ``SecretStr``
            field is registered under its own field name.

    Returns:
        The redactor.
    """
    secrets = {
        name: value.get_secret_value()
        for name in type(settings).model_fields
        if isinstance(value := getattr(settings, name), SecretStr)
    }
    return SecretRedactor(secrets)


class SecretRedactingFilter(logging.Filter):
    """Redacts known secrets from log records before they are formatted.

    Attached to handlers rather than loggers: a filter on a logger only sees
    records logged directly to it, so one on the root logger would miss
    everything propagated up from the libraries this server is built on.
    """

    def __init__(self, redactor: SecretRedactor) -> None:
        """Initialise the filter.

        Args:
            redactor: The redactor to apply to each record.
        """
        super().__init__()
        self._redactor = redactor

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        """Redact a record in place.

        Args:
            record: The record about to be emitted.

        Returns:
            ``True`` always: this filter rewrites records rather than dropping
            them.
        """
        record.msg = self._redactor.redact(str(record.msg))
        if isinstance(record.args, tuple):
            record.args = tuple(self._redact_value(arg) for arg in record.args)
        elif record.args is not None:
            record.args = {
                key: self._redact_value(value) for key, value in record.args.items()
            }
        self._redact_traceback(record)
        return True

    def _redact_value(self, value: object) -> object:
        """Redact one interpolation argument.

        Args:
            value: A value about to be interpolated into the message.

        Returns:
            The value, redacted if it is a string and left alone otherwise -
            an object's ``__str__`` runs at formatting time, which
            :meth:`filter` is too early to intercept.
        """
        return self._redactor.redact(value) if isinstance(value, str) else value

    def _redact_traceback(self, record: logging.LogRecord) -> None:
        """Render and redact the record's traceback, if it has one.

        A traceback is normally rendered by the formatter, after every filter
        has run, so it cannot be reached through ``record.msg``. Rendering it
        here and storing the result in ``exc_text`` - which the formatter
        prefers when it is already set - is what brings it into scope.

        Args:
            record: The record about to be emitted.
        """
        if record.exc_info is None:
            return
        _, exc, _ = record.exc_info
        if exc is None:  # pragma: no cover - logging's (None, None, None) form
            return
        record.exc_text = self._redactor.redact(
            "".join(traceback.format_exception(exc)).rstrip("\n"),
        )


def install_log_redaction(redactor: SecretRedactor) -> None:
    """Redact secrets from every log record the root handlers emit.

    Call this after logging is configured; handlers added afterwards are not
    covered.

    Args:
        redactor: The redactor to apply.
    """
    log_filter = SecretRedactingFilter(redactor)
    for handler in logging.getLogger().handlers:
        handler.addFilter(log_filter)
