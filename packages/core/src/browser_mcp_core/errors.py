"""Errors core raises that a site's code and the tool layer both recognise.

Every failed tool call reaches the caller as one message shape:

    [<category> | retryable=<true|false>] <what went wrong>
    <what to do next>

A caller that is a model cannot be expected to guess from prose whether to try
again, so the first line says it outright. `ClassifiedError` is how a site says
which category a failure belongs to; anything else a tool raises is reported as
`internal`, and `describe` is the one place that turns an error into the text.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCategory(StrEnum):
    """What kind of failure a tool call hit; the first thing a caller reads."""

    #: The caller is not the account this server belongs to.
    CALLER_NOT_AUTHORISED = "caller_not_authorised"
    #: The arguments were wrong, whatever the site was doing.
    INVALID_REQUEST = "invalid_request"
    #: The operator's saved login for the site is missing or no longer accepted.
    SITE_LOGIN_REQUIRED = "site_login_required"
    #: What the call named is not there (no results, no such product).
    NOT_FOUND = "not_found"
    #: The thing exists but cannot be done right now (out of stock, say).
    UNAVAILABLE = "unavailable"
    #: The site's pages no longer look the way this server expects.
    SITE_CHANGED = "site_changed"
    #: The site or the browser was too slow; nothing says it is broken.
    TIMEOUT = "timeout"
    #: This server's own rate limit.
    RATE_LIMITED = "rate_limited"
    #: Anything unclassified, which is a bug in this server until shown otherwise.
    INTERNAL = "internal"


#: What to do next, by category. One line each, written for the caller.
_NEXT_STEP: dict[ErrorCategory, str] = {
    ErrorCategory.CALLER_NOT_AUTHORISED: (
        "Do not repeat the call: this server only answers its owner."
    ),
    ErrorCategory.INVALID_REQUEST: "Fix the arguments, then call again.",
    ErrorCategory.SITE_LOGIN_REQUIRED: (
        "Stop sending requests: every call to this site will fail the same way "
        "until the operator signs in again. Tell the operator."
    ),
    ErrorCategory.NOT_FOUND: (
        "Repeating the same call will not help; change the query or search again."
    ),
    ErrorCategory.UNAVAILABLE: (
        "Repeating the same call will not help; tell the person or choose "
        "something else."
    ),
    ErrorCategory.SITE_CHANGED: (
        "Do not repeat the call: the server needs a code fix. Tell the operator."
    ),
    ErrorCategory.TIMEOUT: (
        "The same call may work if repeated, once, after a short wait."
    ),
    ErrorCategory.RATE_LIMITED: "Wait a few seconds, then repeat the call.",
    ErrorCategory.INTERNAL: (
        "Do not repeat the call unchanged; the server's log has the detail. "
        "Tell the operator."
    ),
}


class ClassifiedError(RuntimeError):
    """A failure that knows its category and whether repeating the call can help.

    ``retryable`` means the *same* call may succeed if sent again later. It is
    false for a failure that needs a person (a lapsed login) or a different
    request (nothing found), however likely the next attempt is to work.
    """

    def __init__(
        self,
        message: str,
        category: ErrorCategory,
        *,
        retryable: bool,
    ) -> None:
        """Initialise the error.

        Args:
            message: What went wrong, in terms the caller can act on.
            category: Which kind of failure this is.
            retryable: Whether repeating the same call can succeed.
        """
        super().__init__(message)
        self.category = category
        self.retryable = retryable


class NotLoggedInError(ClassifiedError):
    """Raised when an action needs a logged-in session and there isn't one.

    Deliberately shared rather than defined per site: the tool layer reports it
    with the right category, and the login worker catches it to report a
    failure the operator can act on. Both of those are generic, so the
    exception has to be too.
    """

    def __init__(self, message: str) -> None:
        """Initialise the error.

        Args:
            message: What the operator should do about it.
        """
        super().__init__(message, ErrorCategory.SITE_LOGIN_REQUIRED, retryable=False)


def describe(error: ClassifiedError) -> str:
    """Render an error as the two lines a caller reads.

    Args:
        error: The classified failure.

    Returns:
        The header line (category, retryability, message) and the next step.
    """
    header = f"[{error.category} | retryable={str(error.retryable).lower()}] {error}"
    return f"{header}\n{_NEXT_STEP[error.category]}"
