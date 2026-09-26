"""A module that names a site in code, so the guard can be shown to notice.

Not imported by anything: `test_core_is_site_agnostic.py` reads it as text.
"""

from __future__ import annotations

SAINSBURYS_LOGIN_PATH = "/sainsburys-login"
"""Exactly the kind of constant core must never grow."""
