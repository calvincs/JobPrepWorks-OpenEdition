"""The single local profile row: your name and display preference, plus the
one destructive operation — wiping everything the app has stored about you.
"""

import logging

from app.db import get_conn

log = logging.getLogger(__name__)

VALID_THEMES = ("system", "light", "dark")

# Cleared top-down by reset_data(). Each entry cascades its own children
# (jobs → requirements/analyses/events/follow-ups, sessions → answers/
# assessments, facts → sources), so only the top-level tables are listed.
# tests/test_account.py checks this list against the schema, so a new
# user-scoped table that gets missed here fails loudly there.
_USER_TABLES = (
    "pulse_requests",       # per-request research ledger
    "interview_sessions",   # → session_answers, assessments
    "questions",            # global/mixer questions have no job to cascade from
    "jobs",                 # → requirements, fit_analyses, events, follow_ups,
                            #   pitches, resumes
    "study_guides",         # global guides have job_id NULL
    "insights",
    "insight_runs",
    "fact_parses",
    "profile_facts",        # → fact_sources
    "documents",
    "awards",
    "llm_requests",
    "llm_usage_daily",
)


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


def reset_data(user_id: int) -> bool:
    """Irreversibly delete every job, document, fact, session, and generated
    artifact, plus the uploaded files behind them. One transaction — the data
    is either fully gone or untouched; files are unlinked only after the commit
    succeeds, so a failed transaction can't orphan them.

    The profile row itself survives (name, theme) and so does the shared
    company_pulses research cache, which is about employers rather than you.
    If you want a truly clean slate, stop the app and delete the database file.
    """
    conn = get_conn()
    try:
        paths = [
            r["path"]
            for r in conn.execute(
                "SELECT path FROM documents WHERE user_id = ?", (user_id,)
            ).fetchall()
        ]
        for table in _USER_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        # The global focus plan's status lives in app_state, keyed per user.
        from app.services.study import global_error_key, global_status_key

        conn.execute(
            "DELETE FROM app_state WHERE key IN (?, ?)",
            (global_status_key(user_id), global_error_key(user_id)),
        )
        conn.commit()
    finally:
        conn.close()

    from app.services.storage import get_storage

    storage = get_storage()
    for p in paths:
        storage.delete(p)
    log.info("reset data for user %s (%s files removed)", user_id, len(paths))
    return True
