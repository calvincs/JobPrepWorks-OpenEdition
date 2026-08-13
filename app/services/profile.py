import logging
import threading

from app.db import get_conn
from app.llm.base import LLMError, get_provider
from app.llm.prompts import (
    FACT_PARSE_SYSTEM,
    PROFILE_EXTRACTION_SYSTEM,
    PROFILE_RECONCILIATION_SYSTEM,
    fact_parse_prompt,
    profile_extraction_prompt,
    profile_reconciliation_prompt,
)
from app.models.extraction import FactParse, ProfileExtraction, ProfileReconciliation
from app.services import insights
from app.services.documents import parse_document, recompute_orphaned
from app.text import canonical
from app.user_errors import USER_ERROR_GENERIC

log = logging.getLogger(__name__)

# The read-existing → dedup → write apply step must not interleave with a
# concurrent fact write — two writers each checking "does this fact exist?"
# before either has committed would both decide "no" and create a duplicate.
# The background extraction pipelines run in a threadpool inside this one
# process, so an in-process lock is the whole story. It is released when the
# connection that took it closes, so no early return or exception can leak it.
# Keep it OUT of any path that makes an LLM call — see _reconcile_plan.
_fact_write_lock = threading.RLock()


def _take_fact_lock(conn, user_id: int) -> None:
    """Serialize the fact read-check-write for the lifetime of `conn`."""
    _fact_write_lock.acquire()
    conn.add_close_hook(_fact_write_lock.release)


def _link_source(conn, fact_id: int, document_id: int, evidence_text: str | None) -> None:
    conn.execute(
        """INSERT INTO fact_sources (fact_id, document_id, evidence_text)
           VALUES (?, ?, ?)
           ON CONFLICT(fact_id, document_id) DO UPDATE SET evidence_text = excluded.evidence_text""",
        (fact_id, document_id, evidence_text),
    )


def _match_key(kind, name, organization):
    """Dedup key. For roles the employer is part of identity — otherwise two
    'Staff Engineer' roles at different companies would wrongly merge. Other
    kinds key on name alone (an employer/issuer is often inconsistently filled)."""
    base = (kind, canonical(name))
    if kind == "role":
        return base + (canonical(organization or ""),)
    return base


def _enrich_fields(conn, existing_row, new_fact) -> None:
    """Fill EMPTY fields on a machine fact from a duplicate. Never touches
    user_edited facts, never overwrites a value that is already set."""
    if existing_row["user_edited"]:
        return
    updates = {}
    for col, val in (
        ("organization", new_fact.organization),
        ("detail", new_fact.detail),
        ("proficiency", new_fact.proficiency),
        ("start_date", new_fact.start_date),
        ("end_date", new_fact.end_date),
        ("evidence_text", new_fact.evidence_text),
    ):
        if val and not existing_row[col]:
            updates[col] = val
    if updates:
        assignments = ", ".join(f"{c} = ?" for c in updates)
        conn.execute(
            f"UPDATE profile_facts SET {assignments} WHERE id = ?",
            (*updates.values(), existing_row["id"]),
        )


def _create_fact(conn, new_fact, document_id: int, user_id: int) -> int:
    cur = conn.execute(
        """INSERT INTO profile_facts
           (user_id, document_id, kind, name, organization, detail, proficiency,
            start_date, end_date, evidence_text)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
        (
            user_id,
            document_id,
            new_fact.kind,
            new_fact.name,
            new_fact.organization,
            new_fact.detail,
            new_fact.proficiency,
            new_fact.start_date,
            new_fact.end_date,
            new_fact.evidence_text,
        ),
    )
    fact_id = cur.fetchone()[0]
    _link_source(conn, fact_id, document_id, new_fact.evidence_text)
    return fact_id


def _fact_line(prefix, kind, name, organization, detail, start, end, evidence) -> str:
    # Surface organization + dates + the evidence quote so the reconciler can
    # tell same-titled facts apart (e.g. the same role title at two employers).
    line = f"{prefix} | {kind} | {name}"
    if organization:
        line += f" @ {organization}"
    if detail:
        line += f" — {detail}"
    if start or end:
        line += f" [{start or '?'}..{end or 'present'}]"
    if evidence:
        line += f' | evidence: "{evidence}"'
    return line


def _render_existing(rows, kinds) -> str:
    return "\n".join(
        _fact_line(r["id"], r["kind"], r["name"], r["organization"], r["detail"],
                   r["start_date"], r["end_date"], r["evidence_text"])
        for r in rows
        if r["kind"] in kinds
    )


def _render_new(unmatched) -> str:
    return "\n".join(
        _fact_line(i, f.kind, f.name, f.organization, f.detail, f.start_date, f.end_date, f.evidence_text)
        for i, f in enumerate(unmatched)
    )


def _reconcile(unmatched, existing) -> dict[int, int]:
    """Ask the LLM which unmatched new facts are fuzzy duplicates of existing
    facts. Every returned mapping is validated (real id, in-range index, same
    kind); anything invalid or an LLMError degrades to 'no match'."""
    existing_ids = {r["id"] for r in existing}
    kind_by_id = {r["id"]: r["kind"] for r in existing}
    kinds = {f.kind for f in unmatched}
    try:
        res = get_provider().extract(
            system=PROFILE_RECONCILIATION_SYSTEM,
            prompt=profile_reconciliation_prompt(_render_existing(existing, kinds), _render_new(unmatched)),
            schema=ProfileReconciliation,
        )
    except LLMError:
        return {}
    out: dict[int, int] = {}
    for rc in res.reconciliations:
        if rc.match is None:
            continue
        if not (0 <= rc.new_index < len(unmatched)):
            continue
        if rc.match not in existing_ids:
            continue
        if kind_by_id[rc.match] != unmatched[rc.new_index].kind:
            continue
        out.setdefault(rc.new_index, rc.match)
    return out


def _reconcile_plan(facts, user_id: int) -> dict[int, int]:
    """Run the AI reconciliation pass and return matches keyed by index into
    `facts` (facts-index -> existing fact id). This makes an LLM call, so the
    caller MUST run it BEFORE taking _FACT_APPLY_LOCK — holding the advisory
    lock (and an open transaction) across a minutes-long provider call would
    stall every fact write app-wide (A06 Insecure Design). The snapshot read
    happens on its own connection, closed before the LLM call, so no pool slot
    is pinned for the provider round-trip either. The map is re-validated
    against freshly-read rows in _apply_extraction, so reconciling against
    this pre-lock snapshot only ever degrades to 'no match'."""
    conn = get_conn()
    try:
        existing = conn.execute(
            "SELECT * FROM profile_facts WHERE user_id = ?", (user_id,)
        ).fetchall()
    finally:
        conn.close()
    index: dict[tuple, object] = {}
    for r in existing:
        index.setdefault(_match_key(r["kind"], r["name"], r["organization"]), r)
    unmatched, positions = [], []  # positions[j] = index into `facts` of unmatched[j]
    for i, f in enumerate(facts):
        if index.get(_match_key(f.kind, f.name, f.organization)) is None:
            unmatched.append(f)
            positions.append(i)
    if not (unmatched and existing):
        return {}
    matches = _reconcile(unmatched, existing)  # unmatched-index -> existing id
    return {positions[j]: fid for j, fid in matches.items()}


def _apply_extraction(conn, document_id: int, facts, user_id: int, reconcile_map: dict[int, int]) -> None:
    """De-dup + enrich the extracted facts against the whole profile, linking
    each to this document as a source. `reconcile_map` (facts-index -> existing
    id) is the pre-computed AI reconciliation result; it is re-validated against
    the freshly-read rows here (target still exists, same kind), so a stale
    snapshot is safe. Runs under _FACT_APPLY_LOCK; makes NO LLM call."""
    existing = conn.execute(
        "SELECT * FROM profile_facts WHERE user_id = ?", (user_id,)
    ).fetchall()
    index: dict[tuple, object] = {}  # _match_key -> fact row
    by_id = {}
    for r in existing:
        index.setdefault(_match_key(r["kind"], r["name"], r["organization"]), r)
        by_id[r["id"]] = r

    for i, f in enumerate(facts):
        # 1. Deterministic pass: exact/normalized match enriches + links in place.
        hit = index.get(_match_key(f.kind, f.name, f.organization))
        if hit is not None:
            _enrich_fields(conn, hit, f)
            _link_source(conn, hit["id"], document_id, f.evidence_text)
            continue
        # 2. AI reconciliation match, re-validated against the current rows.
        target = reconcile_map.get(i)
        if target is not None and target in by_id and by_id[target]["kind"] == f.kind:
            _enrich_fields(conn, by_id[target], f)
            _link_source(conn, target, document_id, f.evidence_text)
            continue
        # 3. A duplicate created earlier in THIS batch, else a brand-new fact.
        key = _match_key(f.kind, f.name, f.organization)
        stub = index.get(key)
        if stub is not None:
            _enrich_fields(conn, stub, f)
            _link_source(conn, stub["id"], document_id, f.evidence_text)
        else:
            new_id = _create_fact(conn, f, document_id, user_id)
            index[key] = conn.execute(
                "SELECT * FROM profile_facts WHERE id = ?", (new_id,)
            ).fetchone()


def _fail_document(document_id: int, message: str) -> None:
    """Terminal-error a document row on its own connection — the pipeline no
    longer holds one open across the provider calls (pool slots are scarce)."""
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE documents SET status = 'error', error = ? WHERE id = ?",
            (message, document_id),
        )
        conn.commit()
    finally:
        conn.close()


def process_document(document_id: int) -> None:
    """Background pipeline for a profile upload: parse -> extract facts ->
    de-dup/enrich against the existing profile -> ready. Errors land on the
    document row (status 'error') for the UI to surface.
    """
    try:
        text = parse_document(document_id)
    except Exception:
        return  # parse_document already recorded the error

    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT filename, user_id FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        if row is None:
            return
        was_empty = not has_profile_facts(row["user_id"])
        conn.execute(
            "UPDATE documents SET status = 'extracting', busy_since = datetime('now') "
            "WHERE id = ?",
            (document_id,),
        )
        conn.commit()
    finally:
        conn.close()

    # The provider round-trip can take minutes — no pooled connection is held.
    try:
        result = get_provider().extract(
            system=PROFILE_EXTRACTION_SYSTEM,
            prompt=profile_extraction_prompt(row["filename"], text),
            schema=ProfileExtraction,
        )
    except LLMError as exc:
        log.warning("document %s extraction failed: %s", document_id, exc)
        _fail_document(document_id, str(exc))  # LLMError copy is curated at the provider
        return

    # AI reconciliation runs OUTSIDE the lock (it makes an LLM call) and
    # borrows/returns its own connection around that call.
    try:
        reconcile_map = _reconcile_plan(result.facts, row["user_id"])
    except Exception:
        # Any non-LLM failure (DB blip, bug) must NOT leave the row stuck in
        # 'extracting' forever — the UI would poll it indefinitely (A10).
        log.exception("document %s extraction apply failed", document_id)
        _fail_document(document_id, USER_ERROR_GENERIC)
        return

    conn = get_conn()
    try:
        # Facts + status flip in one write transaction: the polling UI sees
        # either the pre-apply state or the finished 'ready' state, never a
        # half-relinked one. The advisory lock serializes the dedup read
        # against a concurrent apply (another worker), which waits and then
        # sees committed facts — held only across fast local writes now.
        _take_fact_lock(conn, row["user_id"])
        # Re-extraction: drop this doc's provenance, then re-derive from scratch.
        conn.execute("DELETE FROM fact_sources WHERE document_id = ?", (document_id,))
        _apply_extraction(conn, document_id, result.facts, row["user_id"], reconcile_map)
        recompute_orphaned(conn, row["user_id"])
        conn.execute("UPDATE documents SET status = 'ready' WHERE id = ?", (document_id,))
        conn.commit()
    except Exception:
        # Any non-LLM failure (DB blip, bug) must NOT leave the row stuck in
        # 'extracting' forever — the UI would poll it indefinitely (A10).
        log.exception("document %s extraction apply failed", document_id)
        conn.rollback()
        conn.execute(
            "UPDATE documents SET status = 'error', error = ? WHERE id = ?",
            (USER_ERROR_GENERIC, document_id),
        )
        conn.commit()
        return
    finally:
        conn.close()

    # First facts to land on an empty profile — score the jobs that were waiting.
    if was_empty and has_profile_facts(row["user_id"]):
        from app.services import analysis

        analysis.reanalyze_all_jobs(row["user_id"])
    insights.mark_stale(row["user_id"])  # profile evidence feeds the insights matrix


def _fact_with_sources(conn, fact_id: int, user_id: int):
    """A fact row as a dict, annotated with its source documents."""
    f = conn.execute(
        "SELECT * FROM profile_facts WHERE id = ? AND user_id = ?", (fact_id, user_id)
    ).fetchone()
    if f is None:  # missing or not owned by this user
        return None
    src = conn.execute(
        """SELECT s.document_id, d.filename FROM fact_sources s
           JOIN documents d ON d.id = s.document_id
           WHERE s.fact_id = ? ORDER BY d.filename""",
        (fact_id,),
    ).fetchall()
    d = dict(f)
    d["sources"] = [{"document_id": r["document_id"], "filename": r["filename"]} for r in src]
    d["source_filenames"] = ", ".join(r["filename"] for r in src)
    d["source_count"] = len(src)
    return d


def fact_with_sources(fact_id: int, user_id: int):
    conn = get_conn()
    try:
        return _fact_with_sources(conn, fact_id, user_id)
    finally:
        conn.close()


def facts_grouped(user_id: int) -> dict[str, list]:
    conn = get_conn()
    try:
        facts = conn.execute(
            "SELECT * FROM profile_facts WHERE user_id = ? ORDER BY kind, name",
            (user_id,),
        ).fetchall()
        src_rows = conn.execute(
            """SELECT s.fact_id, s.document_id, d.filename
               FROM fact_sources s
               JOIN documents d ON d.id = s.document_id
               JOIN profile_facts f ON f.id = s.fact_id
               WHERE f.user_id = ?
               ORDER BY s.fact_id, d.filename""",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    sources: dict[int, list] = {}
    for r in src_rows:
        sources.setdefault(r["fact_id"], []).append(
            {"document_id": r["document_id"], "filename": r["filename"]}
        )
    groups: dict[str, list] = {}
    for f in facts:
        d = dict(f)
        d["sources"] = sources.get(f["id"], [])
        d["source_filenames"] = ", ".join(s["filename"] for s in d["sources"])
        d["source_count"] = len(d["sources"])
        groups.setdefault(f["kind"], []).append(d)
    return groups


def remove_unsourced_facts(user_id: int) -> int:
    """Delete machine facts that have no remaining source. Keeps user_edited
    orphans (the user invested in them) for individual handling."""
    conn = get_conn()
    try:
        cur = conn.execute(
            """DELETE FROM profile_facts
               WHERE user_id = ? AND user_edited = 0 AND orphaned = 1
                 AND id NOT IN (SELECT fact_id FROM fact_sources)""",
            (user_id,),
        )
        conn.commit()
        removed = cur.rowcount
    finally:
        conn.close()
    if removed:
        insights.mark_stale(user_id)
    return removed


def has_profile_facts(user_id: int) -> bool:
    """True if there's at least one usable (non-orphaned) evidence fact — i.e.
    the profile block fed to analyses/questions won't be empty. Direction
    facts don't count: they're stated preferences, not evidence, and a
    direction-only profile must not unlock the evidence-based pipelines."""
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT 1 FROM profile_facts
               WHERE user_id = ? AND orphaned = 0 AND kind <> 'direction' LIMIT 1""",
            (user_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def profile_block_for_prompt(user_id: int) -> str:
    """Compact plaintext rendering of non-orphaned facts, for LLM prompts.
    Direction facts are excluded — they're the user's opinions, not evidence,
    and reach prompts only as the separate labeled direction block."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT kind, name, organization, detail, proficiency, start_date, end_date, evidence_text
               FROM profile_facts WHERE user_id = ? AND orphaned = 0 AND kind <> 'direction'
               ORDER BY kind, name""",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    lines = []
    for r in rows:
        parts = [f"[{r['kind']}] {r['name']}"]
        if r["organization"]:
            parts.append(f"@ {r['organization']}")
        if r["proficiency"]:
            parts.append(f"({r['proficiency']})")
        if r["detail"]:
            parts.append(f"- {r['detail']}")
        if r["start_date"] or r["end_date"]:
            parts.append(f"[{r['start_date'] or '?'} to {r['end_date'] or 'present'}]")
        if r["evidence_text"]:
            parts.append(f'| evidence: "{r["evidence_text"]}"')
        lines.append(" ".join(parts))
    return "\n".join(lines)


VALID_KINDS = ("skill", "role", "education", "cert", "project", "direction")


# Career-direction wizard steps: (fact name / step key, question, placeholder).
# The single source of truth for step order, the fact rows each step writes,
# and the copy shown in the wizard — templates and routes derive the step
# count from len(); adding a step here is the whole change.
DIRECTION_STEPS: tuple[tuple[str, str, str], ...] = (
    ("Career trajectory", "Where are you headed?",
     "e.g. Moving from senior IC toward staff/platform work…"),
    ("Wants more of", "What do you want more of in your next role?",
     "e.g. Ownership, greenfield work, mentoring…"),
    ("Wants less of", "What do you want less of?",
     "e.g. On-call churn, legacy maintenance, constant context-switching…"),
    ("Dealbreakers and constraints", "Any dealbreakers or hard constraints?",
     "e.g. Must be remote, no relocation, no defense sector…"),
    ("Sectors and problem spaces",
     "Which industries or problem spaces draw you in — or put you off?",
     "e.g. Drawn to climate and healthcare; want to avoid adtech…"),
    ("Work environment and style", "What working environment suits you best?",
     "e.g. Small team, calm pace, remote-first, deep-work culture…"),
)

_STEP_ORDER = {name: i for i, (name, _, _) in enumerate(DIRECTION_STEPS)}


def direction_facts(user_id: int) -> dict[str, dict]:
    """The user's direction facts keyed by name (= wizard step key for
    wizard-written rows; manual adds can carry any name)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM profile_facts WHERE user_id = ? AND kind = 'direction'",
            (user_id,),
        ).fetchall()
        return {r["name"]: dict(r) for r in rows}
    finally:
        conn.close()


def has_direction_facts(user_id: int) -> bool:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM profile_facts WHERE user_id = ? AND kind = 'direction' LIMIT 1",
            (user_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def save_direction_answer(user_id: int, step_name: str, answer: str) -> None:
    """Upsert one wizard step's answer as a direction fact, verbatim — no LLM,
    no dedup merge (each step is its own row, keyed by name). A blank answer
    is a no-op; clearing an answer is deleting the fact via the facts list."""
    answer = answer.strip()
    if not answer:
        return
    conn = get_conn()
    try:
        _take_fact_lock(conn, user_id)  # guards the update-then-insert race
        cur = conn.execute(
            """UPDATE profile_facts SET detail = ?, user_edited = 1, orphaned = 0
               WHERE user_id = ? AND kind = 'direction' AND name = ?""",
            (answer, user_id, step_name),
        )
        if cur.rowcount == 0:
            conn.execute(
                """INSERT INTO profile_facts
                   (user_id, document_id, kind, name, detail, user_edited, orphaned)
                   VALUES (?, NULL, 'direction', ?, ?, 1, 0)""",
                (user_id, step_name, answer),
            )
        conn.commit()
    finally:
        conn.close()


def direction_block_for_prompt(user_id: int) -> str:
    """Plaintext rendering of the user's stated career direction, for the
    prompts that weigh it (fit alignment, pitch). Empty string when unset."""
    facts = direction_facts(user_id)
    ordered = sorted(
        facts.values(),
        key=lambda r: (_STEP_ORDER.get(r["name"], len(_STEP_ORDER)), r["name"]),
    )
    return "\n".join(f"- {r['name']}: {r['detail']}" for r in ordered if r["detail"])


def create_manual_fact(
    *,
    user_id: int,
    kind: str,
    name: str,
    organization: str | None = None,
    detail: str | None = None,
    proficiency: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[int, str]:
    """Add a fact by hand — no document behind it. It's marked user_edited (so
    the orphan sweep and unsourced banner leave it alone) with no source link.

    Reuses the extraction dedup key: if an identically-identified fact already
    exists we adopt/enrich it (the user's non-empty fields win) rather than
    create a duplicate. Returns (fact_id, 'created' | 'updated')."""
    conn = get_conn()
    try:
        # Hold the advisory lock across the read-check-write so a concurrent fact
        # write (any worker) can't create the same fact before we commit.
        _take_fact_lock(conn, user_id)
        result = _apply_manual_fact(
            conn, user_id=user_id, kind=kind, name=name, organization=organization,
            detail=detail, proficiency=proficiency, start_date=start_date, end_date=end_date,
        )
        conn.commit()
    finally:
        conn.close()
    insights.mark_stale(user_id)  # profile evidence feeds the insights matrix
    return result


def _apply_manual_fact(
    conn,
    *,
    user_id: int,
    kind: str,
    name: str,
    organization: str | None = None,
    detail: str | None = None,
    proficiency: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[int, str]:
    """The dedup + write step of create_manual_fact on the caller's connection.
    Assumes the per-user fact-apply lock is held; does not commit — a batch
    caller (process_fact_parse) applies many facts in one lock/transaction."""
    name = name.strip()
    fields = {
        "organization": (organization or "").strip() or None,
        "detail": (detail or "").strip() or None,
        "proficiency": (proficiency or "").strip() or None,
        "start_date": (start_date or "").strip() or None,
        "end_date": (end_date or "").strip() or None,
    }
    existing = conn.execute(
        "SELECT * FROM profile_facts WHERE user_id = ?", (user_id,)
    ).fetchall()
    index: dict[tuple, object] = {}
    for r in existing:
        index.setdefault(_match_key(r["kind"], r["name"], r["organization"]), r)
    hit = index.get(_match_key(kind, name, fields["organization"]))

    if hit is not None:
        # Keep the existing (canonically-equal) name; the user is adding
        # detail, not renaming. Their non-empty fields win over blanks.
        updates = {"user_edited": 1, "orphaned": 0}
        updates.update({c: v for c, v in fields.items() if v is not None})
        assignments = ", ".join(f"{c} = ?" for c in updates)
        conn.execute(
            f"UPDATE profile_facts SET {assignments} WHERE id = ?",
            (*updates.values(), hit["id"]),
        )
        return (hit["id"], "updated")
    cur = conn.execute(
        """INSERT INTO profile_facts
           (user_id, document_id, kind, name, organization, detail, proficiency,
            start_date, end_date, user_edited, orphaned)
           VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, 1, 0) RETURNING id""",
        (
            user_id,
            kind,
            name,
            fields["organization"],
            fields["detail"],
            fields["proficiency"],
            fields["start_date"],
            fields["end_date"],
        ),
    )
    return (cur.fetchone()[0], "created")


def create_fact_parse(text: str, user_id: int) -> int:
    """Record a freeform fact submission (status 'running') for the background
    parse pipeline; the facts-section banner polls it."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO fact_parses (user_id, raw_text) VALUES (?, ?) RETURNING id",
            (user_id, text.strip()),
        )
        parse_id = cur.fetchone()[0]
        conn.commit()
        return parse_id
    finally:
        conn.close()


def _finish_fact_parse(parse_id: int, status: str, *, error: str | None = None,
                       summary: str | None = None) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE fact_parses SET status = ?, error = ?, summary = ? WHERE id = ?",
            (status, error, summary, parse_id),
        )
        conn.commit()
    finally:
        conn.close()


def process_fact_parse(parse_id: int) -> None:
    """Background pipeline for a freeform fact submission: parse the text into
    structured facts, then run each through the manual-fact dedup path. Errors
    land on the parse row (status 'error') for the banner to surface."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM fact_parses WHERE id = ?", (parse_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return

    try:
        result = get_provider().extract(
            system=FACT_PARSE_SYSTEM,
            prompt=fact_parse_prompt(row["raw_text"]),
            schema=FactParse,
        )
    except LLMError as exc:
        _finish_fact_parse(parse_id, "error", error=str(exc))
        return

    facts = [f for f in result.facts if f.name.strip()]
    if not facts:
        _finish_fact_parse(
            parse_id, "error",
            error="Couldn't find a career fact in that text — try rephrasing, or enter it manually.",
        )
        return

    was_empty = not has_profile_facts(row["user_id"])
    added, merged = [], []
    conn = get_conn()
    try:
        # One connection, one lock, one transaction for the whole batch (the
        # old per-fact create_manual_fact loop re-acquired the lock and
        # re-scanned the profile for every fact). The parse row flips 'ready'
        # in the same commit, so the banner never sees a half-applied batch.
        _take_fact_lock(conn, row["user_id"])
        for f in facts:
            _fact_id, action = _apply_manual_fact(
                conn, user_id=row["user_id"], kind=f.kind, name=f.name,
                organization=f.organization, detail=f.detail, proficiency=f.proficiency,
                start_date=f.start_date, end_date=f.end_date,
            )
            (added if action == "created" else merged).append(f.name.strip())
        parts = []
        if added:
            parts.append("Added " + ", ".join(added) + ".")
        if merged:
            parts.append("Merged " + ", ".join(merged) + " into existing facts.")
        conn.execute(
            "UPDATE fact_parses SET status = 'ready', error = NULL, summary = ? WHERE id = ?",
            (" ".join(parts), parse_id),
        )
        conn.commit()
    except Exception:
        # Any non-LLM failure must not strand the parse row 'running' (A10).
        log.exception("fact parse %s apply failed", parse_id)
        conn.rollback()
        _finish_fact_parse(parse_id, "error", error=USER_ERROR_GENERIC)
        return
    finally:
        conn.close()
    insights.mark_stale(row["user_id"])  # profile evidence feeds the insights matrix

    # First facts to land on an empty profile — score the jobs that were waiting.
    if was_empty and has_profile_facts(row["user_id"]):
        from app.services import analysis

        analysis.reanalyze_all_jobs(row["user_id"])


def fact_parse(parse_id: int, user_id: int):
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM fact_parses WHERE id = ? AND user_id = ?", (parse_id, user_id)
        ).fetchone()
    finally:
        conn.close()


def fact_parses_pending(user_id: int):
    """Parses the facts section should surface: 'running' (self-polling banner)
    and 'error' (dismissible banner). 'ready' rows are deleted on delivery."""
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM fact_parses WHERE user_id = ? AND status IN ('running', 'error') "
            "ORDER BY id",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()


def delete_fact_parse(parse_id: int, user_id: int) -> None:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM fact_parses WHERE id = ? AND user_id = ?", (parse_id, user_id))
        conn.commit()
    finally:
        conn.close()


def update_fact(
    fact_id: int, *, user_id: int, name: str, organization: str | None,
    detail: str | None, proficiency: str | None, kind: str | None = None,
    start_date: str | None = None, end_date: str | None = None,
) -> None:
    """Persist an edited fact. kind/start_date/end_date are optional so older
    callers (name/org/detail/proficiency only) keep working; when kind is None
    the type column is left untouched."""
    sets = [
        "name = ?", "organization = ?", "detail = ?", "proficiency = ?",
        "start_date = ?", "end_date = ?", "user_edited = 1", "orphaned = 0",
    ]
    params: list = [
        name.strip(), (organization or "").strip() or None, (detail or "").strip() or None,
        (proficiency or "").strip() or None, (start_date or "").strip() or None,
        (end_date or "").strip() or None,
    ]
    if kind is not None and kind in VALID_KINDS:
        sets.insert(0, "kind = ?")
        params.insert(0, kind)
    conn = get_conn()
    try:
        conn.execute(
            f"UPDATE profile_facts SET {', '.join(sets)} WHERE id = ? AND user_id = ?",
            (*params, fact_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()
    insights.mark_stale(user_id)


def delete_fact(fact_id: int, user_id: int) -> None:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM profile_facts WHERE id = ? AND user_id = ?", (fact_id, user_id))
        conn.commit()
    finally:
        conn.close()
    insights.mark_stale(user_id)
