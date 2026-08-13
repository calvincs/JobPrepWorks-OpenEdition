"""Cross-job insights: compute the skill matrix in SQL, let the LLM turn
it into a handful of sharp, numbers-grounded findings.

Freshness model: the current insights are the non-dismissed rows of the user's
latest 'ready' insight_run. Anything that changes an input bumps
users.insights_seq (mark_stale); the UI compares that against the ready run's
seen_seq and invites a refresh. Only job intake/delete and the refresh button
actually regenerate: the INSERT of a 'running' run row is the cross-worker
claim (partial unique index idx_insight_runs_one_running), and a seq re-check
after each run coalesces bursts into at most one extra generation. Dismissed
rows are never deleted — they are the permanent "don't resurface" list, fed to
the prompt and enforced with an exact (kind, canonical title) guard on insert.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from app import db as dberr

from app.db import get_conn
from app.llm.base import LLMError, get_provider
from app.llm.prompts import INSIGHTS_SYSTEM, insights_prompt
from app.models.extraction import InsightsResult
from app.text import canonical
from app.user_errors import USER_ERROR_GENERIC

log = logging.getLogger(__name__)

# A 'running' run older than this is presumed dead (worker crash mid-run) and
# is expired at the next claim; the stale-pipeline reaper (services/reaper.py)
# covers rows nobody re-claims.
_RUN_EXPIRY_MINUTES = 10
# Runs kept per user for history; pruning spares dismissed rows (run_id is
# ON DELETE SET NULL).
_KEEP_RUNS = 10
# Bound on the coalescing loop — each extra pass requires a fresh mark_stale
# to have landed mid-generation, so this is a safety net, not a limiter.
_MAX_PASSES = 3

_KIND_ORDER = "CASE kind WHEN 'gap' THEN 0 WHEN 'strength' THEN 1 ELSE 2 END"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _skill_evidence(conn, user_id: int):
    """Word-boundary matcher over the user's fact names — 'java' must not
    match 'JavaScript', but 'python' still matches 'Python programming'."""
    names = [
        canonical(r["name"])
        for r in conn.execute(
            "SELECT name FROM profile_facts WHERE user_id = ? AND orphaned = 0",
            (user_id,),
        ).fetchall()
    ]

    def has_evidence(skill: str) -> bool:
        pat = re.compile(r"(?<!\w)" + re.escape(canonical(skill)) + r"(?!\w)")
        return any(pat.search(name) for name in names)

    return has_evidence


def _skill_matrix(conn, user_id: int) -> str:
    rows = conn.execute(
        """SELECT r.skill,
                  MAX(r.skill_display) AS display,
                  COUNT(DISTINCT r.job_id) AS jobs_requiring,
                  SUM(CASE WHEN r.kind = 'must' THEN 1 ELSE 0 END) AS must_count,
                  -- follow-up is final: COALESCE(followup_score, score)
                  (SELECT ROUND(AVG(COALESCE(a.followup_score, a.score)), 1) FROM session_answers a
                   JOIN questions q ON q.id = a.question_id
                   WHERE q.skill = r.skill AND q.user_id = ? AND a.score IS NOT NULL) AS avg_score,
                  (SELECT COUNT(*) FROM session_answers a
                   JOIN questions q ON q.id = a.question_id
                   WHERE q.skill = r.skill AND q.user_id = ? AND a.score IS NOT NULL) AS n_answers
           FROM job_requirements r
           JOIN jobs j ON j.id = r.job_id
           WHERE j.user_id = ?
           GROUP BY r.skill
           ORDER BY jobs_requiring DESC, must_count DESC""",
        (user_id, user_id, user_id),
    ).fetchall()
    total_jobs = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE user_id = ? AND extract_status = 'ready'",
        (user_id,),
    ).fetchone()[0]
    has_evidence = _skill_evidence(conn, user_id)
    lines = [f"(total jobs analyzed: {total_jobs})"]
    for r in rows:
        perf = (
            f"avg {r['avg_score']}/5 over {r['n_answers']} answers"
            if r["avg_score"] is not None
            else "never practiced"
        )
        lines.append(
            f"- {r['display']}: required by {r['jobs_requiring']} job(s) "
            f"({r['must_count']} as must-have) | profile evidence: "
            f"{'yes' if has_evidence(r['skill']) else 'NO'} | interview: {perf}"
        )
    return "\n".join(lines) if rows else ""


def _sector_block(conn, user_id: int) -> str:
    rows = conn.execute(
        """SELECT j.title, j.company, j.sector, j.status,
                  (SELECT score FROM fit_analyses f
                   WHERE f.job_id = j.id ORDER BY version DESC LIMIT 1) AS fit_score,
                  (SELECT band FROM fit_analyses f
                   WHERE f.job_id = j.id ORDER BY version DESC LIMIT 1) AS fit_band
           FROM jobs j WHERE j.user_id = ? AND j.extract_status = 'ready'
           ORDER BY j.created_at""",
        (user_id,),
    ).fetchall()
    return "\n".join(
        f"- {r['title'] or '?'} ({r['sector'] or 'unknown sector'}, {r['status']}): "
        f"fit {r['fit_score'] if r['fit_score'] is not None else 'n/a'}"
        f"{' (' + r['fit_band'] + ')' if r['fit_band'] else ''}"
        for r in rows
    )


def _feedback_block(conn, user_id: int) -> str:
    rows = conn.execute(
        """SELECT e.payload_json, j.title FROM application_events e
           JOIN jobs j ON j.id = e.job_id
           WHERE e.kind = 'feedback' AND j.user_id = ?
           ORDER BY e.occurred_at DESC LIMIT 10""",
        (user_id,),
    ).fetchall()
    lines = []
    for r in rows:
        payload = json.loads(r["payload_json"])
        lines.append(f"- {r['title'] or 'job'}: {payload.get('text', '')}")
    return "\n".join(lines)


def mark_stale(user_id: int) -> None:
    """Record that an insights input changed (own transaction; safe anywhere)."""
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE users SET insights_seq = insights_seq + 1 WHERE id = ?", (user_id,)
        )
        conn.commit()
    finally:
        conn.close()


def claim_run(user_id: int) -> int | None:
    """Try to become the one generator for this user. The partial unique index
    makes the INSERT the claim; losing it means a run is already in flight
    (that run's post-pass seq check, or the stale banner, covers our caller).
    Expires presumed-dead running rows first so a crashed worker can't wedge
    refresh forever."""
    conn = get_conn()
    try:
        cutoff = (_utcnow() - timedelta(minutes=_RUN_EXPIRY_MINUTES)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn.execute(
            """UPDATE insight_runs SET status = 'error', error = 'interrupted',
                      finished_at = datetime('now')
               WHERE user_id = ? AND status = 'running' AND started_at < ?""",
            (user_id, cutoff),
        )
        conn.commit()
        try:
            cur = conn.execute(
                "INSERT INTO insight_runs (user_id) VALUES (?) RETURNING id", (user_id,)
            )
            run_id = cur.fetchone()[0]
            conn.commit()
            return run_id
        except dberr.UniqueViolation:
            conn.rollback()
            return None
    finally:
        conn.close()


def request_refresh(user_id: int) -> None:
    """Bump the staleness seq, then regenerate unless someone already is.
    The entry point for the auto triggers (job intake/delete)."""
    mark_stale(user_id)
    run_id = claim_run(user_id)
    if run_id is not None:
        run_claimed(user_id, run_id)


def run_claimed(user_id: int, run_id: int) -> None:
    """Generate against an already-claimed run row (routers claim synchronously
    so the returned partial polls, then hand generation to a background task).
    Loops while data changed mid-run and we can re-claim — that coalescing is
    the debounce for bursts of job adds."""
    for _ in range(_MAX_PASSES):
        try:
            run_id = _generate_pass(user_id, run_id)
        except Exception:  # never leak a 'running' row — or the raw error text
            log.exception("insight run %s failed", run_id)
            _finish_error(run_id, USER_ERROR_GENERIC)
            raise
        if run_id is None:
            return
    # Pass limit hit with a freshly claimed run in hand (data kept changing).
    # Don't leave it 'running': surface as stale-refresh instead.
    _finish_error(run_id, "data kept changing during analysis — refresh to retry")


def _generate_pass(user_id: int, run_id: int) -> int | None:
    """One build → LLM → write cycle. Returns the next claimed run id if the
    seq advanced mid-pass, else None (done). No transaction or lock is held
    across the LLM call."""
    conn = get_conn()
    try:
        # The seq snapshot is the data snapshot point: bumps that land after
        # this read (but before we finish) trigger another pass or the banner.
        seen_seq = conn.execute(
            "SELECT insights_seq FROM users WHERE id = ?", (user_id,)
        ).fetchone()[0]
        matrix = _skill_matrix(conn, user_id)
        sector = _sector_block(conn, user_id)
        feedback = _feedback_block(conn, user_id)
        dismissed = conn.execute(
            "SELECT kind, title, canonical_title FROM insights WHERE user_id = ? AND dismissed = 1",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    items = []
    if matrix:  # empty matrix still finishes the run: 'ready' with zero rows
        dismissed_block = "\n".join(f"- [{d['kind']}] {d['title']}" for d in dismissed)
        try:
            result: InsightsResult = get_provider().extract(
                system=INSIGHTS_SYSTEM,
                prompt=insights_prompt(matrix, sector, feedback, dismissed_block),
                schema=InsightsResult,
            )
        except LLMError as exc:
            # Error runs never loop, even if the seq advanced — the stale
            # banner covers it instead of hammering a failing provider.
            log.warning("insight run %s LLM failure: %s", run_id, exc)
            _finish_error(run_id, str(exc))  # LLMError copy is curated at the provider
            return None
        suppressed = {(d["kind"], d["canonical_title"]) for d in dismissed}
        items = [i for i in result.insights if (i.kind, canonical(i.title)) not in suppressed]

    return _finish_ready(user_id, run_id, seen_seq, items)


def _finish_ready(user_id: int, run_id: int, seen_seq: int, items) -> int | None:
    conn = get_conn()
    try:
        # Carry created_at forward for insights that persist across runs (same
        # kind + canonical title) so cards can show a stable "since <date>".
        prev = conn.execute(
            """SELECT kind, canonical_title, MIN(created_at) AS created_at FROM insights
               WHERE user_id = ? AND run_id = (SELECT MAX(id) FROM insight_runs
                                               WHERE user_id = ? AND status = 'ready')
               GROUP BY kind, canonical_title""",
            (user_id, user_id),
        ).fetchall()
        first_seen = {(r["kind"], r["canonical_title"]): r["created_at"] for r in prev}
        for item in items:
            ctitle = canonical(item.title)
            conn.execute(
                """INSERT INTO insights (user_id, run_id, kind, title, canonical_title,
                                         body, evidence_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))""",
                (
                    user_id,
                    run_id,
                    item.kind,
                    item.title,
                    ctitle,
                    item.body,
                    json.dumps({"skills": item.evidence_skills}),
                    first_seen.get((item.kind, ctitle)),
                ),
            )
        conn.execute(
            """UPDATE insight_runs SET status = 'ready', seen_seq = ?,
                      finished_at = datetime('now')
               WHERE id = ?""",
            (seen_seq, run_id),
        )
        # Prune to the newest _KEEP_RUNS runs. Non-dismissed rows of pruned
        # runs go too; dismissed rows survive (run_id → NULL) as suppressions.
        old = [
            r["id"]
            for r in conn.execute(
                # SQLite requires a LIMIT before OFFSET; -1 means "no limit".
                "SELECT id FROM insight_runs WHERE user_id = ? "
                "ORDER BY id DESC LIMIT -1 OFFSET ?",
                (user_id, _KEEP_RUNS),
            ).fetchall()
        ]
        for old_id in old:
            conn.execute(
                "DELETE FROM insights WHERE run_id = ? AND dismissed = 0", (old_id,)
            )
            conn.execute("DELETE FROM insight_runs WHERE id = ?", (old_id,))
        conn.commit()

        cur_seq = conn.execute(
            "SELECT insights_seq FROM users WHERE id = ?", (user_id,)
        ).fetchone()[0]
    finally:
        conn.close()

    if cur_seq > seen_seq:
        # Data changed while we were generating. Claim again (a separate
        # transaction — the finish above must never roll back with it) and
        # let the caller loop; if someone else claims first, they've got it.
        return claim_run(user_id)
    return None


def _finish_error(run_id: int, error: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE insight_runs SET status = 'error', error = ?,
                      finished_at = datetime('now')
               WHERE id = ? AND status = 'running'""",
            (error, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def page_status(user_id: int) -> dict:
    """Everything the insights page/dashboard need: latest run status/error,
    when the current analysis finished, and whether data changed since."""
    conn = get_conn()
    try:
        latest = conn.execute(
            "SELECT status, error FROM insight_runs WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        ready = conn.execute(
            """SELECT seen_seq, finished_at FROM insight_runs
               WHERE user_id = ? AND status = 'ready' ORDER BY id DESC LIMIT 1""",
            (user_id,),
        ).fetchone()
        seq_row = conn.execute(
            "SELECT insights_seq FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    seq = seq_row[0] if seq_row else 0
    return {
        "status": latest["status"] if latest else "none",
        "error": latest["error"] if latest else None,
        "analyzed_at": ready["finished_at"] if ready else None,
        "stale": bool(ready) and seq > ready["seen_seq"],
    }


def list_insights(
    user_id: int,
    include_dismissed: bool = False,
    limit: int | None = None,
):
    """The current set: rows of the user's latest ready run."""
    conn = get_conn()
    try:
        where = "" if include_dismissed else "AND dismissed = 0"
        sql = f"""SELECT * FROM insights
                  WHERE user_id = ? {where}
                    AND run_id = (SELECT MAX(id) FROM insight_runs
                                  WHERE user_id = ? AND status = 'ready')
                  ORDER BY {_KIND_ORDER}, id"""
        if limit:
            sql += f" LIMIT {int(limit)}"
        return conn.execute(sql, (user_id, user_id)).fetchall()
    finally:
        conn.close()


def dismiss(insight_id: int, user_id: int) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE insights SET dismissed = 1 WHERE id = ? AND user_id = ?", (insight_id, user_id)
        )
        conn.commit()
    finally:
        conn.close()
