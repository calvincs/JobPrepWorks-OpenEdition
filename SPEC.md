# JobPrep — Specification

A single-user web app that turns a pile of personal documents and job postings into a structured, gamified interview-preparation system: it builds a Profile of the user, extracts what each job actually demands, grades the fit honestly, generates challenging mock interviews, and tracks every application through to an outcome.

This document is the product spec: what the app is meant to do and why. It
predates the Open Edition fork, so where it and the code disagree the code
wins — see `CLAUDE.md` for current architecture. Two deliberate divergences are
noted inline below (full-text search, and the provider default).

---

## 1. Decisions & Constraints

These were settled up front and shape everything below:

| Decision | Choice |
|---|---|
| LLM integration | **Provider-agnostic abstraction layer.** Anthropic, OpenAI, OpenRouter, and Ollama are all first-class, selected by `LLM_PROVIDER`. *(Spec said Anthropic-default; Open Edition treats all four equally — only Anthropic carries a built-in model default.)* |
| Deployment model | **Single-user local app in v1**, but every table carries a `user_id` foreign key so multi-user hosting is possible later without a schema migration. No auth in v1. |
| Frontend | **Jinja2 + HTMX** served by FastAPI itself. No separate frontend build. |
| Notifications | **In-app only** in v1: a dashboard surface of due follow-ups. No email/push. |
| Storage | **SQLite** (single file under `data/`). Uploaded files on disk under `data/uploads/`, metadata in the DB. *(Spec called for FTS5/BM25; the implementation uses substring matching instead — one person's data is thousands of rows, and there is no index to keep in sync. See `app/routers/search.py`.)* |
| Honesty | Grading and feedback must be **critical and evidence-based** — no grade inflation, no sycophancy. This is a product requirement, enforced in prompts and rubrics. |

## 2. Goals

- Help the user become the ideal candidate for any job they target.
- Tell the user the truth about fit: strengths, gaps, and what to study.
- Make preparation repeatable and cumulative — every interview run informs the next.
- Track applications as real-world objects with state, dates, interviews, and outcomes.
- Surface cross-job patterns (common missing skills, sector misalignment) as actionable recommendations.

### Non-goals (v1)

- Multi-user accounts, auth, or hosting.
- Applying to jobs on the user's behalf or scraping job boards.
- Email/push notification delivery.
- Voice/video mock interviews (text only in v1).

## 3. Core Domain Concepts

| Concept | Description |
|---|---|
| **Profile** | The structured knowledge base about the user, extracted from uploaded documents: skills (with evidence and self/derived proficiency), work history, education, certifications, trainings, notable projects. |
| **Document** | An uploaded file (resume, certificate, transcript, etc.). Parsed to text locally, then extracted into Profile facts. Facts link back to their source document. |
| **Job** | A submitted job posting. Holds the raw posting plus extracted structure: requirements (must/nice-to-have), responsibilities, and metadata (title, company, pay, location, seniority, sector). |
| **Fit Analysis** | The upfront, per-job comparison of Job requirements against the Profile: a 0–100 fit score, strengths, gaps, and study recommendations. |
| **Question Bank** | The refined corpus of likely interview questions for a job — technical, behavioral/interpersonal, and situational — generated at job intake and grown over time. |
| **Interview Session** | One mock-interview run: up to 10 questions, user answers, per-answer grading, and an overall assessment. Scoped to a single job, a **mixer** (across selected open applications), or **global** (across all jobs). |
| **Study Guide** | A per-job (or global) prep document, regenerated as performance data accumulates, prioritizing the user's weakest areas. |
| **Insight** | A cross-job finding: e.g. "6 of 9 target jobs require AWS certification; you lack it and score poorly on AWS questions" or "your derived skills align better with sector X." |
| **Application Tracking** | The real-world state of each job application: interest level and why, applied date, interviews held (with notes), outcome, employer feedback, and follow-up reminders. |

## 4. Functional Requirements

### FR-1: Document management & Profile extraction

1. Upload documents via the UI: PDF, DOCX, TXT, MD (images deferred to v2).
2. Files are parsed to plain text locally (`pypdf`, `python-docx`) so extraction stays provider-agnostic; raw file kept on disk.
3. On upload, the LLM extracts structured Profile facts (skills, roles, dates, certifications, education) via structured output against a fixed schema.
4. Each fact records its source document; the user can view, edit, and delete facts (extraction is a draft, the user is the authority).
5. Documents can be listed, viewed, re-extracted, and deleted. Deleting a document flags (not silently deletes) its dependent facts.

### FR-2: Job intake & extraction

1. Submit a job as **free-form pasted text or a file upload** (PDF, DOCX, TXT, MD — reuses the FR-1 parsing pipeline). Both paths land in the same raw-posting text and are parsed into the same common structure.
2. **No URL scraping.** The user may fill in a `url` as an optional metadata field (alongside the other optional fields) purely for reference/linking back to the posting.
3. On intake the LLM extracts: title, company, location, pay range, seniority, sector, **must-have requirements**, **nice-to-have requirements**, responsibilities, and benefits. The emphasis is on what the employer will expect the candidate to *perform and be proficient in*.
4. Extracted requirements are stored as individual rows (skill, level, evidence text) so they can be matched against Profile facts and reused by insights.
5. Intake automatically triggers FR-3 (fit analysis) and seeds FR-4 (question bank).

### FR-3: Fit analysis & grading

1. For each job, compare extracted requirements against the Profile and produce:
   - A **fit score (0–100)** with band labels (Strong / Viable / Stretch / Misaligned).
   - **Strengths**: requirements the user demonstrably meets, with the supporting Profile evidence cited.
   - **Gaps**: requirements the user does not meet or cannot evidence, ranked by importance to the role.
   - **Study areas**: what to learn to close each gap, with concrete suggestions.
2. Analysis must be honest: unsupported skills are gaps, not benefit-of-the-doubt strengths.
3. Analysis is regenerated on demand (e.g. after the Profile changes) and versioned so the user can see fit improving over time.

### FR-4: Question generation

1. At job intake, generate a refined corpus of likely interview questions for the role (the Question Bank), covering:
   - **Technical** questions tied to specific requirements.
   - **Behavioral/interpersonal** questions the role would realistically ask.
   - **Situational** ("difficult scenario") questions.
2. Each question records: type, targeted skill/requirement, difficulty, and ideal-answer criteria (used later for grading).
3. Question selection for a session (FR-5) draws from the bank, prioritizing weak areas from past sessions and avoiding verbatim repeats.

### FR-5: Interview sessions

1. The user can start an impromptu interview at any time, in three scopes:
   - **Per-job**: questions from that job's bank.
   - **Mixer**: questions drawn across a user-selected set of open applications they are actively training for.
   - **Global**: questions synthesized from common requirements across all submitted jobs.
2. A session asks **up to 10 questions**, one at a time; the user answers in free text.
3. Sessions leverage history: the generator receives past performance per skill and previously asked questions, so each run probes weaknesses and escalates difficulty on mastered areas.
4. Sessions can be abandoned mid-run; partial answers are still graded and recorded.

### FR-6: Grading & feedback

1. Each answer is graded against the question's ideal-answer criteria: a 1–5 score plus specific, critical-yet-constructive commentary (what was strong, what was missing, what a top answer includes).
2. On session completion, an overall assessment is produced: per-skill breakdown, strongest/weakest moments, and 2–3 concrete next actions.
3. Grades feed the per-skill performance history that drives FR-4/FR-5 selection and FR-7 study guides.
4. Feedback tone: honest and direct. The rubric explicitly forbids inflated scores and empty praise.

### FR-7: Study guides

1. Per-job study guide generated after fit analysis, structured by gap priority: topic → why it matters for this job → what to study → how it will likely be tested.
2. Regenerated (or incrementally updated) as interview performance data accumulates — mastered topics shrink, weak topics grow.
3. A **global study guide** synthesizes the highest-impact focus areas across all open applications.

### FR-8: Cross-job insights

1. Periodically (and on demand) analyze across all submitted jobs to surface:
   - **Common demands vs. gaps**: e.g. "AWS certification appears in most of your target jobs; you lack it and average 2.1/5 on AWS questions → recommended focus area."
   - **Common strengths**: what consistently matches, usable as interview talking points.
   - **Sector alignment**: if submitted jobs poorly match derived skills, suggest adjacent roles/sectors the user *could* consider, with reasoning.
2. Insights appear on the dashboard and link to the evidence (jobs, requirements, session scores) behind them.
3. **Freshness model**: each analysis is a versioned `insight_run`; the current set is the latest ready run's rows. Anything that changes an input (grading, profile facts, fit scores, job status, employer feedback) bumps a per-user staleness counter — the UI shows when the analysis ran and an inviting "new insights available — refresh" call-to-action when data changed since, rather than silently re-running the LLM. Only job add/delete and the refresh button regenerate; a partial unique index makes the running-run row the cross-worker claim, and bursts coalesce into a single follow-up pass.
4. **Dismissal is permanent**: dismissed insights are retained as a suppression list — fed to the generation prompt and enforced by an exact (kind, canonical title) guard — so a dismissed finding doesn't resurface on the next refresh. Old runs are pruned (newest 10 kept); dismissed rows survive pruning.

### FR-9: Application tracking

1. Each job carries tracking fields: status (`researching → training → applied → interviewing → offer / rejected / withdrawn`), interest level (1–5) and *why* (free text), applied date, and outcome notes.
2. Interviews (real ones) can be logged per job: date, round, format, notes, and how it went.
3. Rejections can record employer feedback, which feeds study guides and insights.
4. All state changes are timestamped events, so history is reviewable.

### FR-10: Follow-ups & in-app notifications

1. The user can create follow-up reminders per job (e.g. "follow up if no reply by <date>"); applying to a job auto-suggests one.
2. A dashboard panel lists due/overdue follow-ups with the key decision framed: *nudge, move on, or keep training?*
3. Stale-application detection: applied ≥ N days ago (default 14) with no logged response surfaces automatically.

### FR-11: Gamification (v1)

1. Preparation progress is made visible and rewarding:
   - **Per-skill mastery trends**: rolling average score per skill, shown as progress bars / trend lines on the dashboard and job detail pages.
   - **Streaks**: consecutive days with at least one completed interview session.
   - **Fit-score deltas**: fit analyses are versioned (FR-3), so the UI shows fit improving as gaps close ("Fit for Acme SRE: 61 → 74").
   - **Session stats & personal bests**: sessions completed, questions answered, best session score, hardest question conquered.
2. **Milestones/badges** derived from real data (first 5/5 answer, 10 sessions completed, a gap closed, a streak of 7): lightweight, stored as awarded events, surfaced on the dashboard.
3. Gamification never distorts honesty — scores stay calibrated (FR-6); the game layer rewards *effort and improvement*, not inflated grades.

### FR-12: Memory & retrieval

1. All substantive text (profile facts, requirements, questions, answers, feedback) is searchable. *(Implemented as multi-term substring matching rather than the FTS5/BM25 this spec called for — see the Storage note above.)*
2. LLM pipelines retrieve context via this layer (targeted queries) rather than dumping whole tables into prompts — keeps prompts small, relevant, and cheap.

## 5. Data Model (SQLite)

All tables carry `user_id` (FK → `users`) and timestamps. v1 seeds a single default user.

```
users               id, name, created_at
documents           id, user_id, filename, path, mime_type, parsed_text, status, uploaded_at
profile_facts       id, user_id, document_id?, kind(skill|role|education|cert|project),
                    name, detail, proficiency?, start/end dates?, evidence_text, user_edited
jobs                id, user_id, title, company, location, pay_min/max, seniority, sector,
                    raw_posting, source(pasted|file), source_document_id?, url?,
                    status, interest_level, interest_why, applied_at, outcome, created_at
job_requirements    id, job_id, kind(must|nice), skill, level, evidence_text
fit_analyses        id, job_id, version, score, band, strengths_json, gaps_json, study_areas_json, created_at
questions           id, user_id, job_id?, type(technical|behavioral|situational),
                    skill, difficulty, text, ideal_answer_criteria, source(intake|session|manual)
interview_sessions  id, user_id, scope(job|mixer|global), job_id?, mixer_job_ids_json?,
                    status(active|completed|abandoned), started_at, completed_at
session_answers     id, session_id, question_id, answer_text, score, feedback, answered_at
assessments         id, session_id, summary, per_skill_json, next_actions_json
study_guides        id, user_id, job_id? (null = global), version, content_json, created_at
insight_runs        id, user_id, status(running|ready|error), error?, seen_seq, started_at, finished_at?
insights            id, user_id, run_id?, kind(gap|strength|sector), title, canonical_title,
                    body, evidence_json, created_at, dismissed
application_events  id, job_id, kind(status_change|interview|feedback|note), payload_json, occurred_at
follow_ups          id, job_id, due_at, reason, resolved_at?, resolution?
awards              id, user_id, kind, title, earned_at, meta_json   -- milestones/badges (FR-11)

-- FTS5 virtual tables (BM25):
fts_profile_facts, fts_requirements, fts_questions, fts_answers, fts_notes
```

`skill` strings are normalized (lowercased canonical name + display name) so "AWS", "Amazon Web Services" match across jobs — this powers FR-8.

Gamification data (FR-11) is mostly derived: streaks, per-skill trends, and session stats are computed from `interview_sessions` / `session_answers` / `fit_analyses`; only badges get their own table (`awards`).

## 6. LLM Provider Abstraction

A thin interface in `app/llm/`; all AI features go through it. No provider SDK types leak past this boundary.

```python
class LLMProvider(Protocol):
    def complete(self, *, system: str, prompt: str, max_tokens: int = 16000) -> str: ...
    def extract(self, *, system: str, prompt: str, schema: type[BaseModel]) -> BaseModel: ...
```

- `extract()` is the workhorse: every pipeline (profile facts, job structure, fit analysis, questions, grading) defines a Pydantic schema and gets validated structured output back.
- **Default provider: `AnthropicProvider`** — official `anthropic` SDK, model `claude-opus-4-8` (configurable), adaptive thinking (`thinking={"type": "adaptive"}`), structured outputs via `client.messages.parse(..., output_format=Schema)`, streaming for long generations (study guides).
- **`OpenAICompatProvider`** — any OpenAI-compatible chat-completions endpoint: OpenAI, OpenRouter, llama.cpp server, vLLM. The target schema is embedded in the system prompt and `response_format: json_schema` is requested when the server supports it (falls back automatically when it doesn't). Handles thinking models whose reasoning consumes output tokens before content.
- **`OllamaProvider`** — Ollama's native `/api/chat` (used for `LLM_PROVIDER=ollama`): disables thinking on thinking-capable models, sets a real per-request context window (`LLM_NUM_CTX`; Ollama's default 4096 silently truncates long documents), and uses server-side structured outputs (`format: <schema>`).
- **`MockProvider`** — canned responses per schema, for keyless UI development (`LLM_PROVIDER=mock`).
- Config via environment / `.env` (see `.env.example`): `LLM_PROVIDER`, `LLM_MODEL`, `ANTHROPIC_API_KEY` or `LLM_BASE_URL`+`LLM_API_KEY`. Adding a provider = one new module implementing the protocol.
- Failures (rate limit, refusal, network) surface to the UI as retryable errors; no silent empty results.

## 7. Architecture

```
app/
  main.py               FastAPI app, router mounting, startup (DB init)
  config.py             settings from env (.env supported)
  db.py                 SQLite connections, schema, migrations
  models/               Pydantic schemas (domain + LLM extraction schemas)
  llm/                  provider protocol, anthropic.py, prompts/
  services/             business logic: profile.py, jobs.py, analysis.py,
                        interviews.py, grading.py, study.py, insights.py, tracking.py
  routers/              HTTP layer: dashboard, documents, jobs, interviews, insights
  templates/            Jinja2 (base layout + per-page, HTMX partials for dynamic bits)
  static/               CSS (one small stylesheet), htmx.min.js
data/                   jobprep.db, uploads/   (gitignored)
```

- **Routers are thin**; services own logic; LLM calls only happen in services via the provider.
- Long LLM operations (intake extraction, analysis, guide generation) run as background tasks; the UI polls a status partial via HTMX rather than blocking the request.
- Interview answering is a simple HTMX form round-trip per question (answer → graded feedback partial → next question).

## 8. UI Map

Simple, clean, navigable — a persistent left nav with these pages:

| Page | Contents |
|---|---|
| **Dashboard** | Due follow-ups, active applications by status, top insights, streak + per-skill mastery widgets, recent milestones, "start an interview" quick actions (per-job / mixer / global). |
| **Profile** | Fact list grouped by kind, edit/delete, document list with upload + re-extract. |
| **Jobs** | Card/table list with fit score, status, interest; "add job" (paste posting text or upload a file; optional fields incl. URL). |
| **Job detail** | Tabs: Overview (extracted structure + tracking fields) · Fit analysis · Question bank · Study guide · Sessions history · Events/notes. |
| **Interview** | Scope picker → one-question-at-a-time flow → per-answer feedback → final assessment. |
| **Insights** | Cross-job findings with evidence links; dismissable. |

## 9. Non-Functional Requirements

- **Honesty**: grading rubrics and prompts explicitly require critical calibration; a mediocre answer scores mediocre. This is tested with fixture answers of known quality.
- **Local-first & private**: all data on the user's machine; the only egress is LLM API calls. Documents never leave except as prompt content to the configured provider.
- **Cost awareness**: retrieval-scoped prompts (FR-12), prompt caching for stable system prompts, and per-feature model override (cheap model for extraction vs. flagship for grading) supported by config.
- **Responsiveness**: page loads are instant (SQLite); LLM-backed operations show progress states, never spinners over 2s without feedback.
- **Resilience**: every LLM pipeline validates against a schema; invalid output retries once, then surfaces an error. Sessions and uploads are never lost to a failed LLM call.

## 10. Delivery Phases

**M1 — Foundation**: DB + migrations, provider layer, document upload → profile extraction, job intake (paste or file upload, optional URL field) → extraction, fit analysis, base UI (Dashboard, Profile, Jobs, Job detail: Overview + Fit tabs).

**M2 — Interview loop**: question bank generation, per-job interview sessions, grading + assessments, study guides, session history, **gamification core** (per-skill mastery trends, streaks, session stats, fit-score deltas). *(The core value loop — the app is genuinely useful at end of M2.)*

**M3 — Compounding value**: mixer + global interviews, cross-job insights, application tracking events + follow-ups/notifications panel, global study guide, milestones/badges, search UI.

## 11. Open Questions (deferred, not blocking)

1. Image/scan document support (OCR or vision-model extraction) — v2.
2. Spaced-repetition scheduling for study topics — possible M3+ enhancement.
3. Export (profile as resume draft, study guide as PDF) — v2.
