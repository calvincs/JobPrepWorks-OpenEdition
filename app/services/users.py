"""The single local profile row: the name and contact details a generated
résumé is headed with, and the display preference.

There is deliberately no "delete my account" or "wipe my data" operation. This
app is a local tool whose entire state is one SQLite file and one uploads
directory — deleting those IS the reset, it needs no code, and it can't half-
succeed. The Settings page prints both paths so you know what to remove.
"""

from app.db import get_conn

VALID_THEMES = ("system", "light", "dark")


def get_user(user_id: int):
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    finally:
        conn.close()


def set_profile(
    first_name: str,
    last_name: str = "",
    *,
    contact_email: str = "",
    contact_phone: str = "",
    user_id: int,
) -> bool:
    """Set the résumé header details. `name` is kept as the derived full display
    name, which is what a generated résumé is headed with. Contact fields are
    optional and stored verbatim. Returns False (no-op) when the first name is
    blank — a résumé with no name on it is not worth saving."""
    first = (first_name or "").strip()
    last = (last_name or "").strip()
    if not first:
        return False
    full = f"{first} {last}".strip()
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE users SET first_name = ?, last_name = ?, name = ?, "
            "contact_email = ?, contact_phone = ? WHERE id = ?",
            (first, last, full, (contact_email or "").strip(),
             (contact_phone or "").strip(), user_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


# Older name, kept so callers that only set a name keep working.
def set_name(first_name: str, last_name: str = "", *, user_id: int) -> bool:
    user = get_user(user_id)
    return set_profile(
        first_name, last_name,
        contact_email=user["contact_email"] if user else "",
        contact_phone=user["contact_phone"] if user else "",
        user_id=user_id,
    )


def set_theme(theme: str, *, user_id: int) -> bool:
    """Store the display preference ('system' | 'light' | 'dark'). The database
    is the source of truth; the browser's localStorage copy is only the
    anti-flash cache and is re-synced from this on every page load."""
    if theme not in VALID_THEMES:
        return False
    conn = get_conn()
    try:
        conn.execute("UPDATE users SET theme = ? WHERE id = ?", (theme, user_id))
        conn.commit()
        return True
    finally:
        conn.close()
