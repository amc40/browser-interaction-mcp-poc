"""Errors core raises that a site's code and the tool layer both recognise."""

from __future__ import annotations


class NotLoggedInError(RuntimeError):
    """Raised when an action needs a logged-in session and there isn't one.

    Deliberately shared rather than defined per site: the tool layer catches it
    to re-raise as a ``ToolError`` (so the message survives error masking), and
    the login worker catches it to report a failure the operator can act on.
    Both of those are generic, so the exception has to be too.
    """
