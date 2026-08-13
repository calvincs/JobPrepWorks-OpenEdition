"""The global search palette (⌘K).

One person's prep data is thousands of rows, not millions, so search is plain
substring matching rather than a full-text index — nothing to keep in sync, no
tokenizer to fight, and it finds partial words ("kubern") that a stemmed index
would miss. A query is split on whitespace and every term must appear somewhere
in the row's searchable text (AND, in any order).
"""

from fastapi import APIRouter, Request

from app.db import get_conn
from app.identity import current_user_id
from app.web import templates

router = APIRouter(prefix="/search")

# Per-category cap. The palette is a jump-to, not a report: past a screenful
# the useful move is a better query.
LIMIT = 20
# Terms past this add cost without narrowing anything a human typed on purpose.
MAX_TERMS = 6


def _terms(q: str) -> list[str]:
    """Query → LIKE patterns. Wildcards in user input are escaped so a stray
    % or _ searches for that character instead of matching everything."""
    out = []
    for raw in q.split():
        term = raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        if term:
            out.append(f"%{term}%")
    return out[:MAX_TERMS]


def _all_terms(columns: list[str], terms: list[str]) -> tuple[str, list]:
    """SQL + params for 'every term appears in at least one of these columns'.
    The columns are coalesced and concatenated once per term, so "python
    netflix" matches a row whose skill holds one word and whose evidence holds
    the other. `columns` is developer-supplied SQL, never user input."""
    blob = " || ' ' || ".join(f"COALESCE({c}, '')" for c in columns)
    clause = " AND ".join(f"{blob} LIKE ? ESCAPE '\\'" for _ in terms)
    return clause, list(terms)


def _run_search(q: str, user_id: int) -> tuple[dict, int]:
    results: dict = {"facts": [], "requirements": [], "questions": [], "answers": [], "jobs": []}
    terms = _terms(q)
    if not terms:
        return results, 0

    conn = get_conn()
    try:
        # Every query is scoped to the acting user. There is only one account in
        # Open Edition, but keeping the scope makes these queries correct by
        # construction if that ever changes.
        fact_where, fact_params = _all_terms(["f.name", "f.detail", "f.evidence_text"], terms)
        results["facts"] = conn.execute(
            f"""SELECT f.* FROM profile_facts f
                WHERE f.user_id = ? AND {fact_where}
                ORDER BY f.created_at DESC LIMIT ?""",
            (user_id, *fact_params, LIMIT),
        ).fetchall()

        req_where, req_params = _all_terms(["r.skill_display", "r.evidence_text"], terms)
        results["requirements"] = conn.execute(
            f"""SELECT r.*, j.public_id AS job_pid, j.title AS job_title,
                       j.company AS job_company
                FROM job_requirements r
                JOIN jobs j ON j.id = r.job_id
                WHERE j.user_id = ? AND {req_where}
                LIMIT ?""",
            (user_id, *req_params, LIMIT),
        ).fetchall()

        q_where, q_params = _all_terms(["qq.text", "qq.ideal_answer_criteria"], terms)
        results["questions"] = conn.execute(
            f"""SELECT qq.*, j.public_id AS job_pid, j.title AS job_title FROM questions qq
                LEFT JOIN jobs j ON j.id = qq.job_id
                WHERE qq.user_id = ? AND {q_where}
                ORDER BY qq.created_at DESC LIMIT ?""",
            (user_id, *q_params, LIMIT),
        ).fetchall()

        ans_where, ans_params = _all_terms(["a.answer_text", "a.feedback"], terms)
        results["answers"] = conn.execute(
            f"""SELECT a.id, s.public_id AS session_pid, a.answer_text, a.feedback, a.score,
                       qq.text AS question_text
                FROM session_answers a
                JOIN questions qq ON qq.id = a.question_id
                JOIN interview_sessions s ON s.id = a.session_id
                WHERE qq.user_id = ? AND {ans_where}
                ORDER BY a.answered_at DESC LIMIT ?""",
            (user_id, *ans_params, LIMIT),
        ).fetchall()

        job_where, job_params = _all_terms(["j.title", "j.company", "j.location"], terms)
        results["jobs"] = conn.execute(
            f"""SELECT j.id, j.public_id, j.title, j.company, j.status FROM jobs j
                WHERE j.user_id = ? AND {job_where}
                ORDER BY j.created_at DESC LIMIT ?""",
            (user_id, *job_params, LIMIT),
        ).fetchall()
    finally:
        conn.close()

    total = sum(len(v) for v in results.values())
    return results, total


@router.get("/results")
def search_results(request: Request, q: str = ""):
    """Partial for the search modal (live results as you type)."""
    q = q.strip()
    results, total = _run_search(q, current_user_id(request))
    return templates.TemplateResponse(
        request,
        "partials/search_results.html",
        {"q": q, "results": results, "total": total},
    )
