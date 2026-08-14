# JobPrep Works — Open Edition

**Interview prep that tells you the truth, running entirely on your machine.**

Upload your résumé and career documents. Paste the jobs you actually want. Get
an honest read on where you stand — not an encouraging one — then drill the gaps
with AI mock interviews that push back when your answer is thin.

One SQLite file. No accounts, no sign-in, no subscription, no telemetry, no
cloud service in the middle. You bring your own model — Anthropic, OpenAI,
OpenRouter, or a local Ollama that never sends your documents anywhere. MIT
licensed.

---

## Quick start

```sh
git clone https://github.com/calvincs/JobPrepWorks-OpenEdition.git
cd JobPrepWorks-OpenEdition
scripts/setup                 # venv + dependencies + .env
$EDITOR .env                  # uncomment one provider block in section 1
scripts/run                   # → http://127.0.0.1:8000
```

That's the whole install. No Docker, no database server, no Node, no build step.

**Requirements:** Python 3.11+ (SQLite comes with it). That's it.

**Want to look around first?** Set `LLM_PROVIDER=mock` in `.env` and every
feature returns canned sample data instantly — no API key, no network.

**Setting this up with a coding agent?** Point it at [`llm.txt`](llm.txt),
which has install, configuration, verification, and troubleshooting written for
an agent rather than a person.

---

## What it does

**Build a profile from what you've already written.** Upload résumés, project
write-ups, performance reviews — anything. Each document is parsed into
structured facts (roles, skills, education, certifications, projects), each one
carrying the sentence it came from. Facts are de-duplicated across documents:
the same role described three ways becomes one entry, enriched by all three. You
can also just type or dictate what you've done and let it structure that.

**Add the jobs you want.** Paste a posting or upload a file. It's broken into
must-haves and nice-to-haves, seniority, sector, and pay range.

**Get an honest fit score.** Your evidenced facts against that job's actual
requirements. Strengths cite the evidence. Gaps say what's missing and what
studying it would take. It is not designed to make you feel good — if you're a
40% fit, it says 40%, and it says why.

**Practice against the real thing.** Mock interviews generated from that job's
requirements, mixed technical, behavioral, and situational. Answer by typing or
speaking. Every answer is graded 1–5 against explicit criteria, with specific
feedback. A weak answer earns a follow-up question that probes exactly the thing
you skipped — the way a real interviewer would. Finish a session and get an
assessment: per-skill breakdown and concrete next actions.

**Study what actually matters.** Per-job study guides, plus a cross-job focus
plan built from the gaps that keep recurring. Any topic can be drilled on the
spot with one instantly-graded question.

**Research the employer.** Company Pulse pulls ratings, recurring complaints,
and recent news from the live web, with every claim linked to its source. Where
the evidence is thin, it says so instead of padding.

**Bring it together.** A tailored résumé and a "tell me about yourself" pitch
rewritten around a specific job's must-haves — grounded only in facts you
actually have, never invented. Track every application from researching through
offer, with follow-up reminders.

**Find anything.** ⌘K searches your facts, requirements, questions, and past
answers.

---

## Choosing a provider

All four are first-class. Pick with `LLM_PROVIDER` in `.env`.

| Provider | Setup | Notes |
|---|---|---|
| **Anthropic** | `ANTHROPIC_API_KEY` | The only one with a model default — a key alone is enough. Web search built in. |
| **OpenAI** | `OPENAI_API_KEY` + `LLM_MODEL` | Web search built in. |
| **OpenRouter** | `OPENROUTER_API_KEY` + `LLM_MODEL` | Hundreds of models behind one key. Web search built in. Prompts are routed only to hosts that don't retain them (see below). |
| **Ollama** | `LLM_MODEL` + a running Ollama | Fully local — your documents never leave the machine. Needs a search API for Company Pulse. |
| Any OpenAI-compatible server | `LLM_BASE_URL` + `LLM_MODEL` | llama.cpp, vLLM, LM Studio, a proxy. |
| **mock** | nothing | Canned data. For demos, UI work, and offline. |

For Ollama, use a model with solid instruction-following and JSON output —
extraction runs against real schemas, and models under about 7B struggle. Raise
`LLM_NUM_CTX` as far as your hardware allows: Ollama's own default of 4096
silently truncates long résumés and postings.

### Company Pulse and web search

Employer research needs the web. Anthropic, OpenAI, and OpenRouter search
server-side, so it works with no extra setup. A local model can't — so the app
does the searching itself and hands the results to your model. Set one of:

- **`TAVILY_API_KEY`** — built for LLM use, easiest, free tier
- **`BRAVE_API_KEY`** — independent index, free tier
- **`SEARXNG_URL`** — a SearXNG instance, including one you run yourself, so no
  search vendor sees your queries either

With none of them and a local model, the Pulse tab hides itself and everything
else works normally. `WEB_SEARCH=off` disables it outright.

---

## Where your data lives

Everything is in one directory:

```
data/
├── jobprep.db      your entire database — jobs, facts, sessions, everything
└── uploads/        the documents you uploaded, as files
```

Back it up by copying `data/`. Move it to another machine the same way. To start
over, stop the app and delete it — the schema rebuilds itself on the next start.
There is no "wipe my data" button, because `rm -rf data/` is the button, and it
can't half-succeed. Settings prints both paths so you always know what to remove.

Nothing is sent anywhere except the model provider you configured, and the web
searches Company Pulse runs when you ask it to research a company. There is no
analytics, no crash reporting, no phone-home. On OpenRouter, prompts are routed
only to hosts that don't retain or train on them (`OPENROUTER_NO_TRAINING=1`, on
by default). With Ollama, nothing leaves your machine at all.

---

## Cost

You pay your provider directly, at their rates. There is no subscription and
nothing is gated.

Settings shows what today cost you, broken down by feature. If you want a
backstop against a runaway loop or a mis-click on a batch action, set
`LLM_DAILY_LIMIT` to a number of actions per day. It's a brake, not a plan —
raising it never unlocks anything, because nothing is locked.

Company research is metered separately (`PULSE_DAILY_LIMIT`) because live web
search costs meaningfully more than a plain completion. Pulses are cached per
company and shared across your jobs, so looking at the same employer twice is
free.

---

## Security

**There is no login.** Anyone who can reach the port has full access to your
documents and can spend your API credits. Keep it on `127.0.0.1` — which is what
`scripts/run` does — unless you have put real authentication in front of it.

Within that, the app is built to be safe on a machine you also browse the web
on:

- Cross-site state-changing requests are refused, so a page you visit can't POST
  to `127.0.0.1` and delete your data.
- A strict Content-Security-Policy; no inline scripts, no external script hosts.
- Company names and job postings reach prompts as tagged data with explicit
  instructions not to follow anything inside them. Web content is treated as
  untrusted throughout, and URLs from model output are validated before any of
  them are rendered as links.
- Pipeline errors show curated copy; exception text, stack traces, and provider
  responses go to the log, never the browser.
- Upload size, posting length, and answer length are all capped before anything
  reaches a prompt.

Found something? Open an issue — but please don't include your `.env`.

---

## For coding agents

If you're an AI agent installing this for someone, the full instructions —
including verification and a troubleshooting table — are in
**[`llm.txt`](llm.txt)**. Read that file. The short version:

```sh
git clone https://github.com/calvincs/JobPrepWorks-OpenEdition.git && cd JobPrepWorks-OpenEdition
scripts/setup                                  # venv + deps + .env
# edit .env: uncomment ONE provider block in section 1
scripts/run                                    # → http://127.0.0.1:8000
curl -s http://127.0.0.1:8000/health           # → {"status":"ok"}
```

- Requirements are Python 3.11+ and nothing else. No Docker, no Postgres, no Node.
- `LLM_MODEL` is required for every provider except `anthropic` and `mock`.
- Read the startup log: lines beginning `Config:` name exactly what's missing.
  Resolve them before reporting success.
- The Pulse tab being hidden with a local model is expected, not a failure —
  it needs `TAVILY_API_KEY`, `BRAVE_API_KEY`, or `SEARXNG_URL`.
- Verify offline with `.venv/bin/python -m pytest -q` (no network, no key).
- **Never** print or commit `.env`, never invent an API key or model id, and
  never bind the server to `0.0.0.0` — there is no authentication.
- Tell the user where their data lives (`data/`) and what to do first
  (Profile → upload a résumé, then Jobs → paste a posting).

---

## Development

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest              # ~260 tests, no network, no API key
scripts/run --reload
```

The suite runs against a throwaway SQLite file with the mock provider, so it's
fast and completely offline.

The stack is deliberately small: FastAPI + Jinja2 + HTMX, one hand-written
stylesheet, a handful of hand-written JS files, no bundler, no framework. See
[`CLAUDE.md`](CLAUDE.md) for the architecture and the conventions to follow when
changing it.

```
app/
├── routers/     HTTP layer — parse request, call a service, render a template
├── services/    all business logic and all SQL
├── llm/         provider abstraction (anthropic / openai-compat / ollama / mock)
├── models/      Pydantic schemas for structured LLM output
├── templates/   Jinja2 + HTMX
├── db.py        schema, migrations, connections
└── main.py      app assembly, middleware, background loops
```

---

## Honest limitations

- **Single user, single machine.** No sharing, no sync, no mobile app.
- **AI output is AI output.** The fit analysis and grades are a well-grounded
  second opinion, not a verdict. Company Pulse summarizes third-party sources
  that may be outdated or wrong — the links are there so you can check.
- **Small local models produce noticeably worse results.** The extraction
  schemas and grading rubrics are demanding. If Ollama output looks thin, that's
  usually the model, not the app.
- **Voice input** uses your browser's speech API, so it needs a browser that has
  one (Chrome, Edge, Safari).

---

## License

MIT — see [LICENSE](LICENSE). Do what you want with it.

Vendored third-party assets, all permissively licensed and unmodified:
[htmx](https://htmx.org) (0BSD), [Lucide](https://lucide.dev) icons (ISC), and
the Geist and Anton typefaces (SIL Open Font License — see
`app/static/fonts/`).
