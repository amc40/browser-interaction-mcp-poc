"""Server middleware."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, override

from fastmcp.exceptions import AuthorizationError, ToolError
from fastmcp.exceptions import ValidationError as FastMCPValidationError
from fastmcp.server.middleware.middleware import Middleware
from fastmcp.server.middleware.rate_limiting import (
    RateLimitError,
    TokenBucketRateLimiter,
)
from mcp.shared.exceptions import McpError
from pydantic import ValidationError

from browser_mcp_core.errors import ClassifiedError, ErrorCategory, describe

if TYPE_CHECKING:
    import mcp.types as mt
    from fastmcp.server.middleware.middleware import CallNext, MiddlewareContext
    from fastmcp.tools.base import ToolResult

    from browser_mcp_core.redaction import SecretRedactor


class SecretRedactionMiddleware(Middleware):
    """Strips the server's own credentials out of failing tool calls.

    Registered outermost, so it sees whatever every inner layer produced. It
    consumes no budget and rejects nobody, so putting it ahead of authorisation
    does not weaken the ordering that keeps unauthorised callers from spending
    the rate limit.

    This covers the error path only. Tool *results* are Pydantic models built in
    `tools.py` from values chosen there, so nothing can reach a result that the
    tool did not put there deliberately. That stops being true for the first
    tool that returns page text, which is the point to extend this.
    """

    def __init__(self, redactor: SecretRedactor) -> None:
        """Initialise the middleware.

        Args:
            redactor: The redactor to apply to errors on their way out.
        """
        self._redactor = redactor

    @override
    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            return await call_next(context)
        except Exception as exc:
            # Rewriting `args` rather than raising a new exception keeps the
            # type, which the layers above use to decide how to report it.
            exc.args = tuple(
                self._redactor.redact(arg) if isinstance(arg, str) else arg
                for arg in exc.args
            )
            raise


_logger = logging.getLogger(__name__)


def _in_chain[E: BaseException](
    exc: BaseException, kind: type[E] | tuple[type[E], ...]
) -> E | None:
    """Return the first error of ``kind`` in ``exc``'s chain of causes, if any.

    FastMCP wraps whatever a tool raises in its own ``ToolError``, with the
    original as the cause, so the type a tool raised is only found by walking
    back from the one that arrives here.
    """
    visited: set[int] = set()
    seen: BaseException | None = exc
    # A cause can be its own cause (`raise error from error`).
    while seen is not None and id(seen) not in visited:
        if isinstance(seen, kind):
            return seen
        visited.add(id(seen))
        seen = seen.__cause__
    return None


class ErrorClassificationMiddleware(Middleware):
    """Reports every failed tool call as a category, a retry flag and a reason.

    Sits just inside the redaction layer, so it sees what authorisation and
    rate limiting raise as well as what a tool does, and so the text it builds
    is redacted like any other. It also does the logging, in place of FastMCP's
    stock error-handling layer, which re-raises with `raise error from error`
    and so overwrites the very cause this needs to find.

    Anything it cannot classify is `internal`, and its message is withheld
    unless the operator has opted in to error details: an unclassified
    exception's text is not written for a caller and can carry anything.
    """

    def __init__(self, *, include_details: bool) -> None:
        """Initialise the middleware.

        Args:
            include_details: Whether an unclassified failure's own message is
                shown to the caller.
        """
        self._include_details = include_details

    @override
    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            return await call_next(context)
        except Exception as exc:
            # A protocol-level error is already in the form the client expects;
            # the rate limiter's is one too, but is a failed call like any other.
            if isinstance(exc, McpError) and not isinstance(exc, RateLimitError):
                raise
            # `.log` rather than `.error`/`.exception`: a traceback is only
            # wanted when the operator has opted in to error details.
            _logger.log(
                logging.ERROR,
                "Error in tools/call %s: %s: %s",
                context.message.name,
                type(exc).__name__,
                exc,
                exc_info=exc if self._include_details else None,
            )
            raise ToolError(self._describe(exc)) from exc

    def _describe(self, exc: Exception) -> str:
        if (classified := _in_chain(exc, ClassifiedError)) is not None:
            return describe(classified)
        if _in_chain(exc, AuthorizationError) is not None:
            return describe(
                ClassifiedError(
                    str(exc),
                    ErrorCategory.CALLER_NOT_AUTHORISED,
                    retryable=False,
                )
            )
        if _in_chain(exc, RateLimitError) is not None:
            return describe(
                ClassifiedError(str(exc), ErrorCategory.RATE_LIMITED, retryable=True)
            )
        if (
            invalid := _in_chain(exc, (ValidationError, FastMCPValidationError))
        ) is not None:
            return describe(
                ClassifiedError(
                    str(invalid), ErrorCategory.INVALID_REQUEST, retryable=False
                )
            )
        # FastMCP's own masking wrapper is a `ToolError` with the tool's real
        # exception as its cause. One with no cause was raised by our own code
        # on purpose, so its message is meant to be read.
        written_for_the_caller = isinstance(exc, ToolError) and exc.__cause__ is None
        detail = (
            str(exc)
            if self._include_details or written_for_the_caller
            else "The tool failed for a reason the server does not report."
        )
        return describe(
            ClassifiedError(detail, ErrorCategory.INTERNAL, retryable=False)
        )


class ToolCallRateLimitingMiddleware(Middleware):
    """Token-bucket rate limiting applied to tool calls.

    FastMCP's stock rate limiter throttles every request, so listing tools or
    pinging the server eats into the same budget as the calls that actually
    drive a browser. This limits tool calls only, which are the requests with a
    side effect worth throttling.

    There is a single bucket for the whole server rather than one per client:
    the thing being protected is one browser session belonging to one operator,
    so every caller shares the same budget.
    """

    def __init__(self, max_calls_per_second: float, burst_capacity: int) -> None:
        """Initialise the limiter.

        Args:
            max_calls_per_second: Sustained tool-call rate to allow.
            burst_capacity: Calls allowed back-to-back before the sustained rate
                applies.
        """
        self._limiter = TokenBucketRateLimiter(
            capacity=burst_capacity,
            refill_rate=max_calls_per_second,
        )

    @override
    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        if not await self._limiter.consume():
            msg = f"Rate limit exceeded for tool {context.message.name!r}"
            raise RateLimitError(msg)
        return await call_next(context)
