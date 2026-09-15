"""Test scaffolding shared by every package's tests.

A module rather than a ``conftest.py`` because the tests import a *type* from
it, not just a fixture. ``pythonpath = ["."]`` in the root ``pyproject.toml``
is what puts it on the path.
"""

from __future__ import annotations

from typing import Any, Protocol


class Authenticate(Protocol):
    """Presents a verified token to the server, as an authenticated request does."""

    def __call__(
        self,
        user_id: Any = ...,
        login: Any = ...,
        **extra_claims: Any,
    ) -> None:
        """Authenticate subsequent tool calls as that GitHub account.

        Args:
            user_id: Numeric GitHub ID to claim, as the ``sub`` claim. ``None``
                omits it entirely, standing in for a token issued by something
                other than GitHub; a non-string stands in for a malformed one.
            login: GitHub login to claim. Only ever used in error messages.
            extra_claims: Further claims to put on the token.
        """
