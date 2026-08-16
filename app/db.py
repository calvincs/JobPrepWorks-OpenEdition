"""SQLite access layer.

The whole database is one file (``JOBPREP_DB``, default ``data/jobprep.db``).
Nothing to install, nothing to run alongside the app, and a backup is a file
copy. Every service talks to it through ``get_conn()`` / ``conn.close()``.

Notes that matter when writing queries here:

* Statements use ``?`` placeholders and ``datetime('now')`` — both native.
  Timestamps are stored as TEXT in UTC ``'YYYY-MM-DD HH:MM:SS'`` and compared
  as strings, which sorts correctly because the format is fixed-width.
* Rows are subscriptable by name *and* index (``row["col"]``, ``row[0]``,
  ``dict(row)``) — see :class:`Row`.
* Constraint failures are re-raised as :class:`UniqueViolation` /
  :class:`ForeignKeyViolation` / :class:`CheckViolation` so callers can tell
  them apart; SQLite reports all three as one exception type.
* ``PRAGMA foreign_keys`` is ON for every connection, so the ``ON DELETE
  CASCADE`` rules in the schema actually fire.
* WAL mode is enabled once at startup: readers never block the background LLM
  pipelines' writes. SQLite still permits only one writer at a time, so a
  contended write waits up to ``DB_BUSY_TIMEOUT`` seconds before failing.
"""

import logging
import sqlite3
import threading
import uuid
from functools import lru_cache

from app.config import DB_PATH, settings

log = logging.getLogger(__name__)

# RETURNING (used for inserted ids) landed in SQLite 3.35. Every currently
# supported Python ships a newer one; check anyway so a truly ancient build
# fails with a sentence instead of a syntax error deep in a pipeline.
MIN_SQLITE = (3, 35, 0)


class IntegrityError(Exception):
    """A constraint rejected the write."""


class UniqueViolation(IntegrityError):
    """A UNIQUE constraint or unique index rejected the write."""


class ForeignKeyViolation(IntegrityError):
    """A foreign-key constraint rejected the write (referenced row is gone)."""


class CheckViolation(IntegrityError):
    """A CHECK constraint or NOT NULL rejected the write."""


def _map_integrity_error(exc: sqlite3.IntegrityError) -> IntegrityError:
    """SQLite reports every constraint failure as one exception type, with the
    kind spelled out in the message. Callers distinguish 'lost a versioning
    race' (unique) from 'the parent row was deleted under me' (foreign key), so
    translate it back into distinct classes."""
    text = str(exc).lower()
    if "unique" in text or "primary key" in text:
        return UniqueViolation(str(exc))
    if "foreign key" in text:
        return ForeignKeyViolation(str(exc))
    return CheckViolation(str(exc))


@lru_cache(maxsize=256)
def _colmap(names: tuple) -> dict:
    return {name: i for i, name in enumerate(names)}


class Row:
    """sqlite3.Row-like, plus ``.get()``: supports row["col"], row[0],
    dict(row), keys(), iteration and len()."""

    __slots__ = ("_map", "_vals")

    def __init__(self, cols, vals):
        self._map = cols if isinstance(cols, dict) else _colmap(tuple(cols))
        self._vals = vals

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._vals[self._map[key]]
        return self._vals[key]

    def keys(self):
        return list(self._map)

    def get(self, key, default=None):
        try:
            return self[key]
        except (KeyError, ValueError, IndexError):
            return default

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def __repr__(self):
        return f"Row({dict(zip(self._map, self._vals))!r})"


def _row_factory(cursor, values):
    return Row(_colmap(tuple(c[0] for c in cursor.description)), values)


class Cursor:
    """Thin cursor wrapper that translates constraint errors on the way out.
    Exposes the sqlite3 cursor surface the services use."""

    __slots__ = ("_cur",)

    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)

    @property
    def description(self):
        """None for statements that return no rows — callers use it to decide
        whether fetchone() is meaningful."""
        return self._cur.description

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def lastrowid(self):
        return self._cur.lastrowid


class Conn:
    """A checked-out SQLite connection with the surface the services use:
    execute()/executescript()/commit()/rollback()/close(). close() is
    idempotent and rolls back anything uncommitted."""

    __slots__ = ("_db", "_closed", "_close_hooks")

    def __init__(self, db: sqlite3.Connection):
        self._db = db
        self._closed = False
        self._close_hooks: list = []

    def add_close_hook(self, fn) -> None:
        """Run `fn` when this connection closes, whatever path got us there.
        Used to tie a held lock to the lifetime of the transaction that took it
        (services/profile.py), so no exit path can leak it."""
        self._close_hooks.append(fn)

    def execute(self, sql: str, params=()) -> Cursor:
        try:
            return Cursor(self._db.execute(sql, params))
        except sqlite3.IntegrityError as exc:
            raise _map_integrity_error(exc) from exc

    def executescript(self, script: str) -> None:
        # Used by the migration runner: many statements, no parameters.
        self._db.executescript(script)

    def commit(self) -> None:
        self._db.commit()

    def rollback(self) -> None:
        self._db.rollback()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._db.rollback()  # discard anything the caller didn't commit
        except Exception:
            pass
        try:
            self._db.close()
        except Exception:
            log.exception("failed to close a database connection")
        while self._close_hooks:
            hook = self._close_hooks.pop()
            try:
                hook()
            except Exception:
                log.exception("database close hook failed")


_init_lock = threading.Lock()
_initialized = False


def _prepare_file() -> None:
    """One-time per process: create the parent directory, verify the SQLite
    build, and switch the database into WAL mode."""
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        if sqlite3.sqlite_version_info < MIN_SQLITE:
            raise RuntimeError(
                f"SQLite {'.'.join(map(str, MIN_SQLITE))} or newer is required "
                f"(this Python is linked against {sqlite3.sqlite_version}). "
                "Upgrade Python or your system SQLite."
            )
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(DB_PATH, timeout=settings.db_busy_timeout_s)
        try:
            # WAL persists in the file itself, so this only has to take once —
            # but it is cheap and idempotent, and it makes a hand-copied or
            # restored database self-correcting.
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA synchronous = NORMAL")
        finally:
            db.close()
        _initialized = True


def get_conn() -> Conn:
    """Open a connection. SQLite connections are cheap to create, so each unit
    of work opens its own and closes it in a finally — no pool to size, and no
    connection is ever shared between threads."""
    _prepare_file()
    db = sqlite3.connect(
        DB_PATH,
        timeout=settings.db_busy_timeout_s,
        # The background LLM pipelines run in a threadpool; each gets its own
        # connection from this call, so cross-thread reuse never happens. The
        # flag only silences sqlite3's conservative same-thread assertion.
        check_same_thread=False,
    )
    db.row_factory = _row_factory
    db.execute("PRAGMA foreign_keys = ON")
    return Conn(db)


def close_pool() -> None:
    """Kept as the lifespan's teardown hook. There is no pool to drain with
    SQLite — connections are per-unit-of-work — so this only resets the
    one-time setup flag (which matters to tests that repoint DB_PATH)."""
    global _initialized
    with _init_lock:
        _initialized = False


# ── Public (opaque) id <-> internal integer id ──────────────────────────────
# URL-addressable tables carry a `public_id` uuid string. Routes resolve it to
# the internal integer key at the boundary; everything internal keeps using
# ints. There is one user here, so this is not an access-control boundary — it
# keeps URLs stable, unguessable, and safe to paste into a bug report.
_PUBLIC_ID_TABLES = frozenset(
    {"jobs", "interview_sessions", "documents", "profile_facts", "session_answers",
     "follow_ups", "insights", "fact_parses"}
)


def _as_uuid(value) -> str | None:
    """Normalize a public id, or None if it isn't a uuid at all. Normalizing
    (rather than comparing raw) means a differently-cased or braced uuid still
    matches the stored lowercase form."""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def resolve_id(table: str, public_id, *, where: str = "", params: tuple = ()) -> int | None:
    """Map a public_id to the internal integer id, or None for a malformed id or
    a missing/out-of-scope row. `where`/`params` add an ownership predicate,
    e.g. resolve_id("jobs", pid, where="AND user_id = ?", params=(uid,))."""
    if table not in _PUBLIC_ID_TABLES:  # guard against interpolating arbitrary names
        raise ValueError(f"unknown public-id table {table!r}")  # not assert: survives python -O
    pid = _as_uuid(public_id)
    if pid is None:
        return None
    conn = get_conn()
    try:
        row = conn.execute(
            f"SELECT id FROM {table} WHERE public_id = ? {where}", (pid, *params)
        ).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def public_id_of(table: str, internal_id: int) -> str | None:
    """The opaque public id for an internal row id (for building redirect URLs)."""
    if table not in _PUBLIC_ID_TABLES:
        raise ValueError(f"unknown public-id table {table!r}")  # not assert: survives python -O
    conn = get_conn()
    try:
        row = conn.execute(f"SELECT public_id FROM {table} WHERE id = ?", (internal_id,)).fetchone()
        return str(row["public_id"]) if row else None
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Schema. Append-only list; each entry runs once, tracked in schema_migrations.
# Entry 1 is the whole Open Edition schema as one consolidated script — NEVER
# edit an applied entry, always append a new one, or existing installs will
# silently skip your change.
#
# Conventions: timestamps are TEXT, UTC, 'YYYY-MM-DD HH:MM:SS'. Ids are
# INTEGER PRIMARY KEY (SQLite's rowid alias); URL-addressable tables also carry
# a random `public_id`. SQLite doesn't index foreign keys automatically, so
# every FK a query filters or cascades on gets an explicit index below.
# ─────────────────────────────────────────────────────────────────────────────

# A UUIDv4 as a DEFAULT expression, so public ids are the database's job and
# no insert has to remember to supply one. randomblob(16) is SQLite's CSPRNG;
# the literals pin the version (4) and variant (8/9/a/b) nibbles.
_UUID4 = (
    "(lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' || "
    "substr(lower(hex(randomblob(2))), 2) || '-' || "
    "substr('89ab', 1 + (abs(random()) % 4), 1) || "
    "substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6))))"
)
_TS_DEFAULT = "(datetime('now'))"

MIGRATIONS: list[str] = [
    f"""
    -- The single local user. Kept as a table (rather than folded away) because
    -- every row carries user_id and every service function is owner-scoped;
    -- Open Edition simply always passes config.DEFAULT_USER_ID.
    CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL DEFAULT '',
        first_name TEXT NOT NULL DEFAULT '',
        last_name TEXT NOT NULL DEFAULT '',
        -- Printed in the header of a generated résumé. Never sent to a model:
        -- the pipeline generates prose, these are injected at render time so
        -- your contact details cannot be hallucinated or altered.
        contact_email TEXT NOT NULL DEFAULT '',
        contact_phone TEXT NOT NULL DEFAULT '',
        theme TEXT NOT NULL DEFAULT 'system' CHECK (theme IN ('system', 'light', 'dark')),
        insights_seq INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT {_TS_DEFAULT}
    );

    CREATE TABLE documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT NOT NULL DEFAULT {_UUID4},
        user_id INTEGER NOT NULL REFERENCES users(id),
        purpose TEXT NOT NULL DEFAULT 'profile' CHECK (purpose IN ('profile', 'job')),
        filename TEXT NOT NULL,
        path TEXT NOT NULL,
        mime_type TEXT,
        parsed_text TEXT,
        status TEXT NOT NULL DEFAULT 'uploaded'
            CHECK (status IN ('uploaded', 'parsing', 'extracting', 'ready', 'error')),
        error TEXT,
        busy_since TEXT,
        uploaded_at TEXT NOT NULL DEFAULT {_TS_DEFAULT}
    );
    CREATE UNIQUE INDEX idx_documents_public_id ON documents(public_id);

    CREATE TABLE profile_facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT NOT NULL DEFAULT {_UUID4},
        user_id INTEGER NOT NULL REFERENCES users(id),
        document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
        kind TEXT NOT NULL CHECK (kind IN
            ('skill', 'role', 'education', 'cert', 'project', 'direction')),
        name TEXT NOT NULL,
        detail TEXT,
        proficiency TEXT,
        start_date TEXT,
        end_date TEXT,
        evidence_text TEXT,
        user_edited INTEGER NOT NULL DEFAULT 0,
        orphaned INTEGER NOT NULL DEFAULT 0,
        organization TEXT,
        created_at TEXT NOT NULL DEFAULT {_TS_DEFAULT}
    );
    CREATE UNIQUE INDEX idx_facts_public_id ON profile_facts(public_id);

    -- Provenance for extracted facts: one fact can be evidenced by several
    -- documents. profile_facts.document_id is the deprecated single-source
    -- column, kept only for legacy reads.
    CREATE TABLE fact_sources (
        fact_id       INTEGER NOT NULL REFERENCES profile_facts(id) ON DELETE CASCADE,
        document_id   INTEGER NOT NULL REFERENCES documents(id)     ON DELETE CASCADE,
        evidence_text TEXT,
        created_at    TEXT NOT NULL DEFAULT {_TS_DEFAULT},
        PRIMARY KEY (fact_id, document_id)
    );
    CREATE INDEX idx_fact_sources_document ON fact_sources(document_id);

    -- Free-text fact intake: a typed or dictated blob parsed into profile facts
    -- in the background. Rows are transient UI state.
    CREATE TABLE fact_parses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT NOT NULL DEFAULT {_UUID4},
        user_id INTEGER NOT NULL REFERENCES users(id),
        raw_text TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running'
            CHECK (status IN ('running', 'ready', 'error')),
        error TEXT,
        summary TEXT,
        created_at TEXT NOT NULL DEFAULT {_TS_DEFAULT}
    );
    CREATE UNIQUE INDEX idx_fact_parses_public_id ON fact_parses(public_id);

    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT NOT NULL DEFAULT {_UUID4},
        user_id INTEGER NOT NULL REFERENCES users(id),
        title TEXT,
        company TEXT,
        location TEXT,
        pay_min INTEGER,
        pay_max INTEGER,
        seniority TEXT,
        sector TEXT,
        raw_posting TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'pasted' CHECK (source IN ('pasted', 'file')),
        source_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
        url TEXT,
        responsibilities_json TEXT,
        benefits_json TEXT,
        status TEXT NOT NULL DEFAULT 'researching'
            CHECK (status IN ('researching', 'training', 'applied', 'interviewing',
                              'offer', 'rejected', 'withdrawn')),
        interest_level INTEGER CHECK (interest_level BETWEEN 1 AND 5),
        interest_why TEXT,
        applied_at TEXT,
        outcome TEXT,
        extract_status TEXT NOT NULL DEFAULT 'pending'
            CHECK (extract_status IN ('pending', 'extracting', 'ready', 'error')),
        extract_error TEXT,
        analysis_status TEXT NOT NULL DEFAULT 'none'
            CHECK (analysis_status IN ('none', 'running', 'ready', 'error')),
        analysis_error TEXT,
        questions_status TEXT NOT NULL DEFAULT 'none',
        questions_error TEXT,
        study_status TEXT NOT NULL DEFAULT 'none',
        study_error TEXT,
        pitch_status TEXT NOT NULL DEFAULT 'none',
        pitch_error TEXT,
        resume_status TEXT NOT NULL DEFAULT 'none',
        resume_error TEXT,
        busy_since TEXT,
        created_at TEXT NOT NULL DEFAULT {_TS_DEFAULT}
    );
    CREATE UNIQUE INDEX idx_jobs_public_id ON jobs(public_id);

    CREATE TABLE job_requirements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('must', 'nice')),
        skill TEXT NOT NULL,
        skill_display TEXT NOT NULL,
        level TEXT,
        evidence_text TEXT
    );

    -- Versioned: UNIQUE(job_id, version) is the cross-writer claim.
    CREATE TABLE fit_analyses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        version INTEGER NOT NULL,
        score INTEGER NOT NULL,
        band TEXT NOT NULL,
        strengths_json TEXT NOT NULL,
        gaps_json TEXT NOT NULL,
        study_areas_json TEXT NOT NULL,
        alignment_json TEXT,
        created_at TEXT NOT NULL DEFAULT {_TS_DEFAULT},
        UNIQUE (job_id, version)
    );

    -- "I have this" on a Fit-tab gap: checks the gap off without mutating the
    -- immutable gaps_json snapshot. Keyed to the specific analysis row, so a
    -- re-analysis starts clean.
    CREATE TABLE fit_gap_resolutions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fit_analysis_id INTEGER NOT NULL REFERENCES fit_analyses(id) ON DELETE CASCADE,
        requirement_key TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT {_TS_DEFAULT},
        UNIQUE (fit_analysis_id, requirement_key)
    );

    CREATE TABLE questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
        type TEXT NOT NULL CHECK (type IN ('technical', 'behavioral', 'situational')),
        skill TEXT NOT NULL,
        skill_display TEXT NOT NULL,
        difficulty TEXT NOT NULL CHECK (difficulty IN ('easy', 'medium', 'hard')),
        text TEXT NOT NULL,
        ideal_answer_criteria TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'intake' CHECK (source IN ('intake', 'session', 'manual')),
        created_at TEXT NOT NULL DEFAULT {_TS_DEFAULT}
    );

    CREATE TABLE interview_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT NOT NULL DEFAULT {_UUID4},
        user_id INTEGER NOT NULL REFERENCES users(id),
        scope TEXT NOT NULL DEFAULT 'job'
            CHECK (scope IN ('job', 'mixer', 'global', 'study')),
        job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
        mixer_job_ids_json TEXT,
        label TEXT,
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'completed', 'abandoned')),
        assessment_status TEXT NOT NULL DEFAULT 'none'
            CHECK (assessment_status IN ('none', 'running', 'ready', 'error')),
        assessment_error TEXT,
        setup_status TEXT NOT NULL DEFAULT 'ready'
            CHECK (setup_status IN ('generating', 'ready', 'error')),
        setup_error TEXT,
        busy_since TEXT,
        started_at TEXT NOT NULL DEFAULT {_TS_DEFAULT},
        completed_at TEXT
    );
    CREATE UNIQUE INDEX idx_sessions_public_id ON interview_sessions(public_id);

    CREATE TABLE session_answers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT NOT NULL DEFAULT {_UUID4},
        session_id INTEGER NOT NULL REFERENCES interview_sessions(id) ON DELETE CASCADE,
        question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        answer_text TEXT,
        score INTEGER,
        feedback TEXT,
        grade_status TEXT NOT NULL DEFAULT 'unanswered'
            CHECK (grade_status IN ('unanswered', 'grading', 'ready', 'error')),
        grade_error TEXT,
        -- One interactive follow-up per answer: a weak answer (score <= 3) earns
        -- one probing question, graded the same way. Inline so ordering,
        -- progress counts, and assessment aggregation are untouched.
        followup_status TEXT NOT NULL DEFAULT 'none'
            CHECK (followup_status IN ('none', 'awaiting', 'grading', 'ready', 'error')),
        followup_question TEXT,
        followup_criteria TEXT,
        followup_answer TEXT,
        followup_score INTEGER,
        followup_feedback TEXT,
        followup_error TEXT,
        busy_since TEXT,
        answered_at TEXT
    );
    CREATE UNIQUE INDEX idx_answers_public_id ON session_answers(public_id);

    CREATE TABLE assessments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL REFERENCES interview_sessions(id) ON DELETE CASCADE,
        summary TEXT NOT NULL,
        per_skill_json TEXT NOT NULL,
        next_actions_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT {_TS_DEFAULT}
    );

    -- job_id NULL = the cross-job "focus plan".
    CREATE TABLE study_guides (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
        version INTEGER NOT NULL,
        content_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT {_TS_DEFAULT}
    );
    CREATE UNIQUE INDEX idx_study_guides_job_version
        ON study_guides(job_id, version) WHERE job_id IS NOT NULL;
    CREATE UNIQUE INDEX idx_study_guides_global_version
        ON study_guides(user_id, version) WHERE job_id IS NULL;

    -- Per-job "tell me about yourself" variants, versioned like fit_analyses.
    CREATE TABLE pitches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        version INTEGER NOT NULL,
        content_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT {_TS_DEFAULT},
        UNIQUE (job_id, version)
    );

    -- Per-job resume content rewritten around that job's must-haves.
    CREATE TABLE resumes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        version INTEGER NOT NULL,
        content_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT {_TS_DEFAULT},
        UNIQUE (job_id, version)
    );

    CREATE TABLE awards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        earned_at TEXT NOT NULL DEFAULT {_TS_DEFAULT},
        meta_json TEXT,
        UNIQUE (user_id, kind)
    );

    -- Insights are produced in versioned runs. Current insights = non-dismissed
    -- rows of the latest 'ready' run; staleness = users.insights_seq >
    -- seen_seq. The partial unique index makes the running-row INSERT the
    -- claim. run_id is SET NULL so pruning old runs preserves dismissed rows —
    -- they are the permanent "don't resurface this" list.
    CREATE TABLE insight_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        status TEXT NOT NULL DEFAULT 'running'
            CHECK (status IN ('running', 'ready', 'error')),
        error TEXT,
        seen_seq INTEGER NOT NULL DEFAULT 0,
        started_at TEXT NOT NULL DEFAULT {_TS_DEFAULT},
        finished_at TEXT
    );
    CREATE UNIQUE INDEX idx_insight_runs_one_running
        ON insight_runs(user_id) WHERE status = 'running';

    CREATE TABLE insights (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT NOT NULL DEFAULT {_UUID4},
        user_id INTEGER NOT NULL REFERENCES users(id),
        run_id INTEGER REFERENCES insight_runs(id) ON DELETE SET NULL,
        kind TEXT NOT NULL CHECK (kind IN ('gap', 'strength', 'sector')),
        title TEXT NOT NULL,
        canonical_title TEXT,
        body TEXT NOT NULL,
        evidence_json TEXT,
        dismissed INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT {_TS_DEFAULT}
    );
    CREATE UNIQUE INDEX idx_insights_public_id ON insights(public_id);

    CREATE TABLE application_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('status_change', 'interview', 'feedback', 'note')),
        payload_json TEXT NOT NULL,
        occurred_at TEXT NOT NULL DEFAULT {_TS_DEFAULT}
    );

    CREATE TABLE follow_ups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT NOT NULL DEFAULT {_UUID4},
        job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        due_at TEXT NOT NULL,
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT {_TS_DEFAULT},
        resolved_at TEXT,
        resolution TEXT
    );
    CREATE UNIQUE INDEX idx_followups_public_id ON follow_ups(public_id);

    -- Company Pulse: employer research cached by normalized company name (one
    -- row per employer). status/claimed_at make the lifecycle crash-safe:
    --   pending    → queued for research
    --   submitting → a worker holds the claim, refreshing claimed_at as a
    --                heartbeat; a stale claim means that worker died
    --   ready      → pulse_json holds the validated pulse
    --   error      → this attempt failed; the message is user-visible copy
    CREATE TABLE company_pulses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_name TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'submitting', 'ready', 'error')),
        pulse_json TEXT,
        error TEXT,
        cost_usd REAL NOT NULL DEFAULT 0,
        claimed_at TEXT,
        last_updated TEXT,
        created_at TEXT NOT NULL DEFAULT {_TS_DEFAULT}
    );

    -- One row per search-triggering research request, for the daily limit.
    CREATE TABLE pulse_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        pulse_id INTEGER REFERENCES company_pulses(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('new', 'refresh')),
        created_at TEXT NOT NULL DEFAULT {_TS_DEFAULT}
    );

    -- Local spend ledger: one row per LLM-triggering action (kind = which
    -- pipeline, units ~ how much work it bought). Read by the Account page;
    -- enforced only if you set LLM_DAILY_LIMIT.
    CREATE TABLE llm_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        units INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT {_TS_DEFAULT}
    );

    -- Rollup target for aged llm_requests rows (one row per day/kind).
    CREATE TABLE llm_usage_daily (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        day TEXT NOT NULL,
        kind TEXT NOT NULL,
        units INTEGER NOT NULL,
        PRIMARY KEY (user_id, day, kind)
    );

    CREATE TABLE app_state (
        key TEXT PRIMARY KEY,
        value TEXT
    );

    -- SQLite does not index foreign keys automatically. These back the list
    -- queries, the HTMX status polls, the ON DELETE cascades, and the search.
    CREATE INDEX idx_jobs_user_created        ON jobs(user_id, created_at);
    CREATE INDEX idx_session_answers_session  ON session_answers(session_id);
    CREATE INDEX idx_session_answers_question ON session_answers(question_id);
    CREATE INDEX idx_job_requirements_job     ON job_requirements(job_id);
    CREATE INDEX idx_questions_user           ON questions(user_id);
    CREATE INDEX idx_questions_job            ON questions(job_id);
    CREATE INDEX idx_sessions_user            ON interview_sessions(user_id);
    CREATE INDEX idx_sessions_job             ON interview_sessions(job_id);
    CREATE INDEX idx_assessments_session      ON assessments(session_id);
    CREATE INDEX idx_app_events_job           ON application_events(job_id);
    CREATE INDEX idx_follow_ups_job           ON follow_ups(job_id);
    CREATE INDEX idx_insights_user            ON insights(user_id);
    CREATE INDEX idx_insights_run             ON insights(run_id);
    CREATE INDEX idx_insight_runs_user        ON insight_runs(user_id);
    CREATE INDEX idx_documents_user           ON documents(user_id);
    CREATE INDEX idx_facts_user               ON profile_facts(user_id);
    CREATE INDEX idx_facts_document           ON profile_facts(document_id);
    CREATE INDEX idx_fact_parses_user         ON fact_parses(user_id);
    CREATE INDEX idx_study_guides_job         ON study_guides(job_id);
    CREATE INDEX idx_study_guides_user        ON study_guides(user_id);
    CREATE INDEX idx_pitches_job              ON pitches(job_id);
    CREATE INDEX idx_resumes_job              ON resumes(job_id);
    CREATE INDEX idx_awards_user              ON awards(user_id);
    CREATE INDEX idx_fit_analyses_job         ON fit_analyses(job_id);
    CREATE INDEX idx_fit_gap_res_analysis     ON fit_gap_resolutions(fit_analysis_id);
    CREATE INDEX idx_pulse_requests_user_created ON pulse_requests(user_id, created_at);
    CREATE INDEX idx_pulse_requests_pulse     ON pulse_requests(pulse_id);
    CREATE INDEX idx_llm_requests_user_created   ON llm_requests(user_id, created_at);
    CREATE INDEX idx_pulses_inflight          ON company_pulses(status)
        WHERE status IN ('pending', 'submitting');

    -- The one local account.
    INSERT INTO users (id, name) VALUES (1, '');
    """,
    """
    -- Sessions remember the shape they were requested with, so a setup retry
    -- rebuilds what was asked for instead of a hardcoded default, and carry a
    -- setup_run counter bumped on every retry claim so a superseded build
    -- (reaped or retried past) can never overwrite a newer run's state.
    ALTER TABLE interview_sessions ADD COLUMN question_count INTEGER;
    ALTER TABLE interview_sessions ADD COLUMN include_opener INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE interview_sessions ADD COLUMN setup_run INTEGER NOT NULL DEFAULT 0;
    """,
]


def get_state(key: str) -> str | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def set_state(key: str, value: str | None) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Apply any unapplied migrations. Called from the FastAPI lifespan, so a
    fresh clone becomes a working install on the first `uvicorn app.main:app` —
    there is no separate migrate step to forget."""
    conn = get_conn()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            f"(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT {_TS_DEFAULT})"
        )
        conn.commit()
        applied = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
        for number, script in enumerate(MIGRATIONS[applied:], start=applied + 1):
            # executescript() commits any open transaction first and runs the
            # statements as one batch; the version row lands right after so a
            # crash mid-migration is visible as a missing version, not a
            # half-applied schema silently marked done.
            conn.executescript(script)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (number,))
            conn.commit()
            log.info("applied schema migration %s", number)
    finally:
        conn.close()
