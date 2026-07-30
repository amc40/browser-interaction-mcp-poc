"""Server middleware."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from fastmcp.server.middleware.middleware import Middleware
from fastmcp.server.middleware.rate_limiting import (
    RateLimitError,
    TokenBucketRateLimiter,
)

if TYPE_CHECKING:
    import mcp.types as mt
    from fastmcp.server.middleware.middleware import CallNext, MiddlewareContext
    from fastmcp.tools.base import ToolResult


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
