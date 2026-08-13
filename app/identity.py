"""Who the acting user is.

Open Edition has no accounts and no sign-in: the app is a single-user tool you
run on your own machine, and everything belongs to the one seeded row
(``config.DEFAULT_USER_ID``). This module exists so that identity stays an
explicit, single-sourced value rather than a literal ``1`` sprinkled through
the routers — services still take ``user_id`` as a required argument and every
query stays owner-scoped.

If you ever add multi-user support, this is the seam: resolve a real user here
and everything downstream already honors it.
"""

from fastapi import Request

from app.config import DEFAULT_USER_ID


def current_user_id(request: Request | None = None) -> int:
    """The acting user's id. Takes the request for signature compatibility with
    a future session-backed implementation; it is unused today."""
    return DEFAULT_USER_ID
