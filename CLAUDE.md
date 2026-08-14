# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## What this is

JobPrep Works — Open Edition is a **single-user, local, MIT-licensed** interview
prep tool: upload career documents, add target jobs, get an honest fit analysis,
drill with adaptive mock interviews. It runs on the user's own machine against
whichever LLM provider they configure.

There are **no accounts, no authentication, no billing, and no plans**. Every
feature is always available; the user pays their provider directly. If you find
yourself adding a gate, a tier, or a sign-in, stop — that's a different product.

`README.md` is the pitch and setup guide; `llm.txt` is the same for an agent.

## Commands

No build step, no linter configured. Python + one hand-written stylesheet. There
**is** a pytest suite with coverage.

```sh
scripts/setup                    # venv + requirements + .env (idempotent)
scripts/run                      # http://127.0.0.1:8000
scripts/run --reload             # auto-restart while editing

# fast, deterministic, no API calls — the right mode for UI and logic work
LLM_PROVIDER=mock scripts/run --reload

# tests: throwaway SQLite file + mock provider, no network, no key
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest                     # -q --cov=app, HTML in htmlcov/

# quick checks
python3 -m compileall -q app/
node --check app/static/app.js
```

**Verifying changes safely.** The app writes to a real database and real
uploaded files. To exercise anything that writes, point it at a throwaway data
directory with the mock provider — never at the user's `data/`:

```sh
JOBPREP_DATA_DIR=/tmp/jp-scratch LLM_PROVIDER=mock .venv/bin/python -c "
from starlette.testclient import TestClient
from app.main import app
with TestClient(app) as c: ...
"
```

`init_db()` runs from the FastAPI lifespan, which `TestClient` triggers; to
drive services directly, call it yourself first.

## Architecture

**Stack:** FastAPI + Jinja2 + HTMX (vendored at `app/static/htmx.min.js`) +
SQLite. HTMX is the only client framework; there is no bundler. All CSS is one
file (`app/static/style.css`); the only JS is `app.js`, `theme.js`,
`theme-boot.js`, `dictation.js`, `resume-print.js` — hand-written, loaded
`defer`.

**Layering (respect it):**
- `app/routers/*.py` — thin HTTP layer: parse the request, call a service,
  render a template or partial. No SQL and no business logic beyond a couple of
  inline status claims.
- `app/services/*.py` — all business logic and **all SQL**.
- `app/db.py` — owns the schema, migrations, and `get_conn()`.
- `app/web.py` — Jinja2 setup + template globals/filters: `icon()` (Lucide
  sprite at `/static/icons.svg`), `static_url()` (mtime cache-buster —
  **templates must reference `/static` assets through it**, never as bare
  paths), `md` (mistune markdown, `escape=True`), `from_json`, `humandate`,
  `theme_pref()`, `pulse_available`.

**Identity.** There is one user (`config.DEFAULT_USER_ID`). `app/identity.py`
provides `current_user_id(request)`; every table still carries `user_id` and
**`user_id` stays a required parameter on every service function** — never
default it. That discipline is why the ownership tests still pass and why a
multi-user fork would only have to change `identity.py`.

**URL structure.** Everything lives under **`/app`**: `main.py` builds
`app_router = APIRouter(prefix="/app")` and every feature router is included
there, never on `app` directly. `/` redirects into `/app`; `/health` and
`/static` are the only other paths. `tests/test_shell.py` pins that surface
against an explicit allowlist. Templates and redirects hardcode `/app/...`
(there is no URL helper). Middleware in `main.py` stamps security headers on
every response and origin-guards unsafe methods; the CSP is `script-src 'self'`,
so **no inline `<script>` in templates** (test-enforced — the theme anti-flash
bootstrap is the blocking `/static/theme-boot.js`; inline `style="--pct: …"`
attributes are allowed).

**Database: SQLite.** One file (`JOBPREP_DB`, default `data/jobprep.db`), WAL
mode, foreign keys ON per connection. `get_conn()` opens a fresh connection per
unit of work and `close()` returns/rolls back — connections are cheap and never
shared across threads. Conventions that matter:

- `?` placeholders and `datetime('now')` are native. Timestamps are **TEXT**,
  UTC, `'YYYY-MM-DD HH:MM:SS'` — compared as strings, which sorts correctly
  because the format is fixed-width.
- Use `RETURNING id` + `cur.fetchone()[0]` for inserted ids.
- Constraint failures arrive as `db.UniqueViolation` / `db.ForeignKeyViolation`
  / `db.CheckViolation` (SQLite reports all three as one type; `db.py` maps
  them). Catch the specific one — versioned inserts distinguish "lost the
  version race" from "the parent row was deleted under me".
- SQLite has no `NULLS LAST`: write `col IS NULL, col DESC`. `OFFSET` requires
  a `LIMIT` (`LIMIT -1 OFFSET ?`). No DML inside a CTE.
- SQLite does **not** auto-index foreign keys — a new FK or a hot
  `user_id`-scoped query needs its own index.
- Search is plain `LIKE` matching (`app/routers/search.py`), not a full-text
  index: one person's data is thousands of rows, and there's nothing to keep in
  sync. Escape user input for `%`/`_` — `_terms()` does.

**Migrations** are an append-only `MIGRATIONS` list in `app/db.py`, applied by
`init_db()` from the lifespan and tracked in `schema_migrations`. Entry 1 is the
whole consolidated schema. **Never edit an applied entry — always append**, or
existing installs silently skip your change.

**Opaque public ids.** Every URL-addressable row (jobs, sessions, documents,
facts, answers, follow-ups, insights, fact_parses) carries a `public_id` uuid;
**the internal integer PK never appears in a URL or response**. Routes take
`{..._pid}` path params and resolve them at the boundary with
`db.resolve_id(table, pid, where=..., params=...)` (validates the uuid, applies
the ownership predicate, returns the internal int or None → the `owned_job` /
`owned_session` / … dependencies 404 on None). Services keep taking internal
ints. When a route builds a redirect it converts back with
`db.public_id_of(table, id)`; a query feeding a template that links a row must
select that row's `public_id` (e.g. `j.public_id AS job_pid`).

**LLM provider abstraction** (`app/llm/`): `get_provider()` (`base.py`,
`@lru_cache`) dispatches on `settings.llm_provider` to Anthropic /
OpenAI-compatible (OpenAI, OpenRouter, llama.cpp, vLLM) / native Ollama / Mock.
The one contract that matters:

```python
get_provider().extract(system=..., prompt=..., schema=SomePydanticModel) -> SomePydanticModel  # validated
```

Schemas live in `app/models/extraction.py`; prompts in `app/llm/prompts.py`.
**Adding a pipeline = new Pydantic schema + prompt + a canned entry in
`mock_provider.py`'s `CANNED` dict** (keyed by `schema.__name__`) — without the
canned entry, mock runs raise `LLMError`. `base.py` reads `config.settings` at
call time (not a module-level import) so tests can swap it.

Model/base-URL resolution lives in `config.py` (`resolved_model()`,
`resolved_base_url()`): only Anthropic has a built-in model default, so
`LLM_MODEL` is required elsewhere and `llm_config_warnings()` says so at boot.

**Async work + polling (the core UX pattern).** Long LLM operations (document
extraction, fit analysis, question generation, grading, assessments, insights,
study guides, pitch, résumé) run as FastAPI `BackgroundTasks`. Each writes a
status column (`extract_status`, `analysis_status`, `grade_status`, …) stepping
`running → ready | error`. The HTMX partial returned to the browser
**self-polls** with `hx-trigger="every 2s"` against a status endpoint and swaps
itself once terminal. There is no websocket/SSE. When adding an async
operation: set the status, enqueue the task, return a partial that polls.

**Stale-pipeline reaper** (`app/services/reaper.py`, `_reaper` lifespan task):
background pipelines are in-process, so killing the server would strand rows in
their in-flight status polling forever. Every claim UPDATE (and every INSERT
that creates a row already in-flight) also stamps `busy_since`; the reaper flips
rows stale past `REAPER_STALE_MINUTES` to `'error'` with
`USER_ERROR_INTERRUPTED` (retryable). **A new async pipeline must stamp
`busy_since` at its claim and get a `_PIPELINES` entry in reaper.py.** Company
Pulse keeps its own claim/heartbeat recovery. Tests set `REAPER_INTERVAL=0` and
call `reaper.sweep()`.

**Company Pulse** (`app/services/pulse.py` + `app/services/websearch.py`, job
detail → Pulse tab): employer research, deliberately outside `get_provider()`
because server-side web search is provider-specific. Two paths:

- **native** — Anthropic / OpenAI / OpenRouter search inside one model call; we
  get prose back and run it through `validate_pulse()` (+ one repair pass).
- **app-side search** — everything else (Ollama, llama.cpp): `websearch.py`
  runs a fixed query playbook against Tavily / Brave / SearXNG and the results
  go to the model through the normal `extract(schema=CompanyPulseOut)`
  contract, so Ollama enforces the schema server-side.

`config.search_backend_name()` resolves which, and `pulse_available()` decides
whether the tab renders at all. `LLM_PROVIDER=mock` short-circuits with
`CANNED_PULSE`. Pulses are cached globally by normalized company name
(`canonical_company()` in `app/text.py`), held `PULSE_TTL_DAYS`, and metered per
UTC day in `pulse_requests` (`PULSE_DAILY_LIMIT`) — both enforced server-side,
never just in the UI. The `error` column is **user-visible product copy**
(`USER_ERROR_*` constants only — exception text and internals go to the log).
`validate_pulse()` runs on every path, including schema-validated output,
because a schema guarantees types but not that a `url` field holds a safe URL.

**Local spend ledger** (`app/services/usage.py`): every LLM-triggering action
records a row in `llm_requests` (`kind` = pipeline, `units` ≈ batch size). This
is **not** a plan — it exists so the Settings page can show what today cost, and
so `LLM_DAILY_LIMIT` (0 = unlimited, the default) can brake a runaway loop.
Three call shapes, always synchronous and **before** any row or enqueue:
`spend()` raises `QuotaExceeded` (handled in `main.py` — hx gets 429 + toast,
plain forms get `limit.html`); `check()` + `record()` bracket an atomic status
claim so a lost claim isn't charged; `try_spend()` for flows that must degrade
instead of erroring. Chained pipelines (intake → fit → insights; grade →
follow-up) ride the triggering action's unit. Pulse has its own ledger and is
not double-recorded. A lifespan sweeper (`_usage_sweeper`) rolls aged rows into
`llm_usage_daily` and prunes them.

**Postgres-isms are gone.** This fork was ported from Postgres; if you copy code
from that lineage, watch for `tsvector`/`websearch_to_tsquery`, `NULLS LAST`,
`gen_random_uuid()`, `pg_advisory_xact_lock`, `FOR UPDATE SKIP LOCKED`, `::type`
casts, `to_char`, and DML-in-CTE — none of them work here.

**HTMX conventions:** fixed-id containers targeted by `hx-target`/`hx-swap`;
success feedback via an `HX-Trigger` response header carrying a `toast` event
that `app.js` renders; destructive confirms via a styled `<dialog>` (`app.js`
intercepts `htmx:confirm` and `data-confirm`); the global search is a
command-palette modal (⌘K) hitting `GET /app/search/results`.

**Import-cycle constraints (real footguns):** `app/services/profile.py` imports
`documents.py`, and `jobs.py → questions.py → profile.py`. Therefore
**`profile.py` must not import `jobs.py`**, and **`documents.py` must not import
`profile.py`**. Dependency-free helpers go in `app/text.py` (e.g. `canonical()`
for dedup normalization).

**Profile facts — dedup + provenance:** facts extracted from documents are
de-duplicated (deterministic canonical-name match, then a validated AI
reconciliation pass) and enriched across documents. Provenance is the
`fact_sources` join table (a fact can have many source documents); the legacy
`profile_facts.document_id` column is deprecated. Roles carry a structured
`organization` that is part of the dedup key, so same-titled roles at different
employers don't merge. The read-check-write is serialized by an in-process lock
tied to the connection's lifetime (`_take_fact_lock`) — keep it **out** of any
path that makes an LLM call.

**Config & theming:** all settings come from env / `.env` via `app/config.py`
(`Settings`, a frozen dataclass read once at import — changing `.env` requires a
restart). Theming uses CSS `light-dark()` tokens with a system-preference
default; the preference is stored on the user row (`users.theme`) and stamped
onto `<html>` by `base.html` via `theme_pref()`, with localStorage as the
anti-flash cache only.

**Settings page** (`app/routers/account.py`, `/app/account`): résumé header
details (name, email, phone — injected at render time, never sent to the model),
display preference, a live readout of the configured provider/model/search
backend, today's usage, and the on-disk paths of the database and uploads.

There is deliberately **no destructive action** here. The whole state is one
SQLite file plus one directory, so deleting them is the reset — it needs no
code, can't half-succeed, and can't be triggered by a stray click or a
cross-site POST. Don't reintroduce a wipe button; print the paths instead.
