"""Local LLM spend ledger and optional runaway brake.

You bring your own API key, so nothing here gates a feature: every part of the
app is always fully available. What this module does is keep an honest record
of how much model work you've asked for — one ``llm_requests`` row per
LLM-triggering action, tagged with the pipeline kind and counted per UTC day —
so the Account page can tell you what today has cost you in calls.

``LLM_DAILY_LIMIT`` (default 0 = unlimited) turns that record into a brake. It
exists for one failure mode: a loop, a stuck retry, or a mis-click on a large
batch quietly burning through a paid API key while you're not watching. Set it
to a number you'd be annoyed but not alarmed to spend in a day.

Company Pulse keeps its own ledger (``pulse_requests``) and is not
double-recorded here — web-search research is priced very differently from a
plain completion, so it gets its own allowance (``PULSE_DAILY_LIMIT``).

Call sites choose one of three shapes, always in the synchronous request path
BEFORE any row is created or task enqueued:
- ``spend()``     — the standard gate; raises QuotaExceeded (main.py renders it).
- ``check()`` + ``record()`` — around an atomic status claim, so a lost claim
  (a double-submit that changed nothing) isn't charged.
- ``try_spend()`` — flows that must degrade quietly instead of erroring
  (GET-triggered generation, batch side effects, saving a typed answer).

Dependency-light leaf module: safe to import from any service or router.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db import get_conn
from app.user_errors import USER_ERROR_QUOTA

log = logging.getLogger(__name__)

# Human names for the Account page's per-feature breakdown — the only place
# these kind slugs become words.
FEATURE_NAMES = {
    "document": "document uploads",
    "intake": "job intakes",
    "fit": "fit analyses",
    "fit_all": "bulk re-analyses",
    "questions": "mock interview sessions",
    "grade": "answer grades",
    "grade_followup": "follow-up grades",
    "assessment": "session assessments",
    "drill": "study drills",
    "study_guide": "study guide builds",
    "study_guide_global": "focus plan builds",
    "pitch": "pitch generations",
    "insights": "insight refreshes",
    "fact_parse": "profile additions",
    "resume": "tailored resumes",
}


class QuotaExceeded(Exception):
    """The local daily brake (LLM_DAILY_LIMIT) refused this action."""

    def __init__(self, message: str = USER_ERROR_QUOTA):
        super().__init__(message)


def _today_start() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d 00:00:00")


def _used_today(conn, user_id: int) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(units), 0) FROM llm_requests WHERE user_id = ? AND created_at >= ?",
        (user_id, _today_start()),
    ).fetchone()[0]


def used_today(user_id: int) -> int:
    conn = get_conn()
    try:
        return _used_today(conn, user_id)
    finally:
        conn.close()


def _check(conn, user_id: int, kind: str | None = None) -> None:
    limit = settings.llm_daily_limit
    if not limit:  # 0 = unlimited, the default
        return
    used = _used_today(conn, user_id)
    if used >= limit:
        log.info("daily LLM brake hit for user %s: %s/%s units (kind=%s)",
                 user_id, used, limit, kind)
        raise QuotaExceeded()


def _record(conn, user_id: int, kind: str, units: int) -> None:
    conn.execute(
        "INSERT INTO llm_requests (user_id, kind, units) VALUES (?, ?, ?)",
        (user_id, kind, max(1, units)),
    )
    conn.commit()


def check(user_id: int, kind: str | None = None) -> None:
    """Raise QuotaExceeded when the daily brake is configured and spent."""
    conn = get_conn()
    try:
        _check(conn, user_id, kind)
    finally:
        conn.close()


def record(user_id: int, kind: str, units: int = 1) -> None:
    """Ledger a spent action without a check — pair with check() around an
    atomic claim so a lost claim (double-submit no-op) isn't charged."""
    conn = get_conn()
    try:
        _record(conn, user_id, kind, units)
    finally:
        conn.close()


def spend(user_id: int, kind: str, units: int = 1) -> None:
    """check + record on one connection: the standard pre-enqueue gate."""
    conn = get_conn()
    try:
        _check(conn, user_id, kind)
        _record(conn, user_id, kind, units)
    finally:
        conn.close()


def try_spend(user_id: int, kind: str, units: int = 1) -> bool:
    """spend() for flows that must skip quietly instead of surfacing an error."""
    try:
        spend(user_id, kind, units)
        return True
    except QuotaExceeded:
        return False


def usage_summary(user_id: int) -> dict:
    """What the Account page's usage card shows: today's actions (with the
    brake, if you set one), the Company Pulse allowance, when the UTC day
    rolls over, and a per-feature breakdown of today. Mirrors enforcement
    exactly — same UTC-midnight boundary `_check` uses."""
    now = datetime.now(timezone.utc)
    next_reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    reset_minutes = max(1, int((next_reset - now).total_seconds() // 60))
    limit = settings.llm_daily_limit
    conn = get_conn()
    try:
        used = _used_today(conn, user_id)
        by_kind = [
            {"label": FEATURE_NAMES.get(r["kind"], r["kind"]), "units": r["units"]}
            for r in conn.execute(
                "SELECT kind, SUM(units) AS units FROM llm_requests "
                "WHERE user_id = ? AND created_at >= ? GROUP BY kind ORDER BY 2 DESC",
                (user_id, _today_start()),
            ).fetchall()
        ]
        pulse_used = conn.execute(
            "SELECT COUNT(*) FROM pulse_requests WHERE user_id = ? AND created_at >= ?",
            (user_id, _today_start()),
        ).fetchone()[0]
        lifetime = conn.execute(
            "SELECT COALESCE(SUM(units), 0) FROM llm_requests WHERE user_id = ?", (user_id,)
        ).fetchone()[0] + conn.execute(
            "SELECT COALESCE(SUM(units), 0) FROM llm_usage_daily WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
    finally:
        conn.close()

    pulse_limit = settings.pulse_daily_limit
    return {
        "used_today": used,
        "lifetime": lifetime,
        "daily_limit": limit,  # 0 = no brake configured
        "daily_left": max(0, limit - used) if limit else None,
        "daily_pct": min(100, round(used / limit * 100)) if limit else 0,
        "by_kind": by_kind,
        "pulse_used": pulse_used,
        "pulse_limit": pulse_limit,
        "pulse_left": max(0, pulse_limit - pulse_used) if pulse_limit else None,
        "pulse_pct": min(100, round(pulse_used / pulse_limit * 100)) if pulse_limit else 0,
        "reset_h": reset_minutes // 60,
        "reset_m": reset_minutes % 60,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Rollup + prune (the _usage_sweeper lifespan task; safe to call anywhere)
# ─────────────────────────────────────────────────────────────────────────────

def _rollup_batch(conn, cutoff: str, batch: int) -> int:
    """Aggregate then delete one bounded batch of aged rows, in a single
    transaction the caller commits. Aggregate-then-delete (rather than
    delete-then-aggregate) means a crash between the two leaves the raw rows
    intact and the batch simply runs again — the summary can never gain units
    that are still sitting in the ledger."""
    ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM llm_requests WHERE created_at < ? LIMIT ?", (cutoff, batch)
        ).fetchall()
    ]
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    conn.execute(
        f"""INSERT INTO llm_usage_daily (user_id, day, kind, units)
            SELECT user_id, substr(created_at, 1, 10), kind, SUM(units)
            FROM llm_requests WHERE id IN ({marks})
            GROUP BY user_id, substr(created_at, 1, 10), kind
            ON CONFLICT (user_id, day, kind)
            DO UPDATE SET units = llm_usage_daily.units + excluded.units""",
        tuple(ids),
    )
    conn.execute(f"DELETE FROM llm_requests WHERE id IN ({marks})", tuple(ids))
    return len(ids)


def rollup_and_prune(batch: int = 5_000) -> int:
    """Collapse llm_requests rows older than the retention window into
    llm_usage_daily (one row per day/kind) and delete them, in bounded batches.
    Returns the number of raw rows pruned. Retention is clamped to >= 1 day:
    the daily brake reads today's raw rows and must never lose them."""
    days = max(1, settings.llm_ledger_retention_days)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
    total = 0
    while True:
        conn = get_conn()
        try:
            pruned = _rollup_batch(conn, cutoff, batch)
            conn.commit()
        finally:
            conn.close()
        total += pruned
        if pruned < batch:
            break
    if total:
        log.info("usage ledger rollup: pruned %s rows older than %s", total, cutoff)

    # pulse_requests rides the same retention. It is only ever read through a
    # same-day window, so anything past the cutoff is dead weight — no rollup
    # table needed.
    conn = get_conn()
    try:
        pulses = conn.execute(
            "DELETE FROM pulse_requests WHERE created_at < ?", (cutoff,)
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    if pulses:
        log.info("usage ledger rollup: pruned %s aged pulse_requests rows", pulses)
    return total
