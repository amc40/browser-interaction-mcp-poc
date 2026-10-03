"""A site's locators as data: one row per element, keyed by a locator id.

Every element a site's browser actions touch is named in that site's
``locators.py`` - a mapping from a locator id (``"login.username"``) to a
``LocatorSpec`` saying *how* to address the element and, in one line, *what*
it is for. The site's code never builds a locator inline; it asks
:func:`resolve` for one by id.

That separation is what makes a locator change reviewable on its own. The
table is the one thing a self-healing branch (``claude/heal-*``) may edit, and
`heal_surface` fails CI when one edits anything else - see
docs/self-healing-plan.md stage 0 and docs/scaling-plan.md stage 2. So the table
holds *only* the addressing. Timeouts and poll intervals, cookie shapes, URLs
and business rules about what counts as a result stay in the site's code: a
"heal" that could widen a timeout would hide a real failure rather than fix it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import re

    from playwright._impl._api_structures import AriaRole
    from playwright.sync_api import Locator, Page


@dataclass(frozen=True)
class ByRole:
    """Address an element by its ARIA role and, optionally, accessible name.

    The first choice where it fits: it is how a person using assistive
    technology finds the element, so it changes least when the markup does.
    """

    role: AriaRole
    name: str | re.Pattern[str] | None = None


@dataclass(frozen=True)
class ByTestId:
    """Address an element by its ``data-testid``."""

    test_id: str


@dataclass(frozen=True)
class ByCss:
    """Address an element by CSS selector, when neither of the others fits."""

    selector: str


@dataclass(frozen=True)
class LocatorSpec:
    """One row of a site's locator table.

    Attributes:
        by: How to address the element.
        description: One line on what the element is and what the site's code
            uses it for - written for the reviewer of a locator change, and for
            whoever (or whatever) is proposing one.
        first: Narrow to the first match. For an element the page legitimately
            renders more than once, where the first is the one wanted.
    """

    by: ByRole | ByTestId | ByCss
    description: str
    first: bool = False

    def __post_init__(self) -> None:
        """Refuse a row with no description.

        Raises:
            ValueError: If ``description`` is blank.
        """
        if not self.description.strip():
            msg = "A locator needs a description: it is what a reviewer reads."
            raise ValueError(msg)


def resolve(scope: Page | Locator, spec: LocatorSpec) -> Locator:
    """Build the Playwright locator a table row describes.

    Args:
        scope: Where to search - the page, or a locator to search within (a
            result tile, for an element inside it).
        spec: The row to resolve.

    Returns:
        A locator. Like any Playwright locator it is lazy: nothing is looked up
        until it is acted on.
    """
    by = spec.by
    if isinstance(by, ByRole):
        locator = scope.get_by_role(by.role, name=by.name)
    elif isinstance(by, ByTestId):
        locator = scope.get_by_test_id(by.test_id)
    else:
        locator = scope.locator(by.selector)
    return locator.first if spec.first else locator
