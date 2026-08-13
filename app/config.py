"""Configuration for JobPrep Works — Open Edition.

Everything comes from the environment (or a `.env` file at the repo root).
There are no accounts, no billing, and no hosted services: the app runs on your
machine, talks to whichever LLM provider you configure, and stores everything
in your own Postgres database. `.env.example` documents every knob below.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
# Uploaded files live on disk under the data dir; the database is Postgres.
DATA_DIR = Path(os.getenv("JOBPREP_DATA_DIR", BASE_DIR / "data"))
UPLOADS_DIR = DATA_DIR / "uploads"


# The whole database is one SQLite file inside the data directory. Nothing to
# install, nothing to start, and a backup is `cp jobprep.db elsewhere`. Set
# JOBPREP_DB to move it (an absolute path, or a name relative to the data dir).
def default_db_path() -> Path:
    raw = os.getenv("JOBPREP_DB", "").strip()
    if not raw:
        return DATA_DIR / "jobprep.db"
    p = Path(raw).expanduser()
    return p if p.is_absolute() else DATA_DIR / p


DB_PATH = default_db_path()
TEMPLATES_DIR = BASE_DIR / "app" / "templates"
STATIC_DIR = BASE_DIR / "app" / "static"

# Open Edition is single-user by design: one local person, one row. Every table
# still carries user_id, and every service function still takes it explicitly —
# it is simply always this value.
DEFAULT_USER_ID = 1

# Upload cap bounds the in-memory read + disk write and the downstream prompt
# token cost. Trusted hosts pins the Host header against DNS-rebinding; "*"
# (the default) keeps a loopback install frictionless — set an explicit
# comma-separated list if you ever expose this beyond your machine. Docs are
# off by default; flip ENABLE_DOCS=1 for local API exploration.
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))  # 10 MB
# Cap on pasted/free-text bodies before they reach a prompt (chars, not bytes).
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", str(200_000)))
# Tighter cap for answer-like fields (interview/follow-up answers, freeform
# profile facts). Postings and documents legitimately need MAX_TEXT_CHARS;
# an answer never does — without this, a 200k-char answer makes every grade
# call ~25x the normal token cost.
MAX_ANSWER_CHARS = int(os.getenv("MAX_ANSWER_CHARS", str(8_000)))
TRUSTED_HOSTS = [h.strip() for h in os.getenv("TRUSTED_HOSTS", "*").split(",") if h.strip()]
ENABLE_DOCS = os.getenv("ENABLE_DOCS", "").lower() in ("1", "true", "yes")

# Provider families. "openai" and friends all speak the OpenAI chat-completions
# wire format; LLM_BASE_URL picks the server. Ollama gets a native provider
# (think:false, num_ctx, server-side structured output).
ANTHROPIC = "anthropic"
OLLAMA = "ollama"
OPENAI = "openai"
OPENROUTER = "openrouter"
MOCK = "mock"
OPENAI_COMPAT_ALIASES = frozenset({"openai", "openai-compat", "openrouter", "llamacpp", "vllm"})
KNOWN_PROVIDERS = frozenset({ANTHROPIC, OLLAMA, MOCK}) | OPENAI_COMPAT_ALIASES

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"
OLLAMA_BASE_URL = "http://localhost:11434"

# Only Anthropic gets a built-in model default — its ids are stable and global.
# Everywhere else the right model is an install decision (which OpenRouter
# slug, which local Ollama tag), so LLM_MODEL is required and the app says so
# plainly at boot rather than guessing a name that may not exist.
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"


def _flag(name: str, default: str = "") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # ── SQLite (app/db.py) ──────────────────────────────────────────────────
    # WAL mode lets the background LLM pipelines write while pages read. SQLite
    # still allows only one writer at a time, so a writer that finds the lock
    # held waits up to this long before raising "database is locked" — raise it
    # if you ever see that with several pipelines running at once.
    db_busy_timeout_s: float = field(
        default_factory=lambda: float(os.getenv("DB_BUSY_TIMEOUT", "15"))
    )

    # ── LLM provider (app/llm/) ─────────────────────────────────────────────
    llm_provider: str = field(
        default_factory=lambda: os.getenv("LLM_PROVIDER", ANTHROPIC).strip().lower()
    )
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "").strip())
    # For OpenAI-compatible endpoints (OpenAI, OpenRouter, llama.cpp, vLLM) and
    # native Ollama. Optional for OpenAI/OpenRouter/Ollama: each has a default.
    llm_base_url: str | None = field(default_factory=lambda: os.getenv("LLM_BASE_URL"))
    # One key setting, with the conventional per-vendor names accepted too, so
    # an ANTHROPIC_API_KEY / OPENAI_API_KEY already in your shell just works.
    llm_api_key: str | None = field(
        default_factory=lambda: os.getenv("LLM_API_KEY")
        or os.getenv("OPENROUTER_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("ANTHROPIC_API_KEY")
    )
    # Ollama only: per-request context window (Ollama's own default of 4096
    # truncates long documents and postings well before the model does).
    llm_num_ctx: int = field(default_factory=lambda: int(os.getenv("LLM_NUM_CTX", "16384")))
    # Per-call timeout (seconds). Bounds how long a background pipeline can pin
    # a threadpool worker when the upstream hangs. Local models on modest
    # hardware are slow — raise this rather than lowering LLM_NUM_CTX.
    llm_timeout_s: float = field(default_factory=lambda: float(os.getenv("LLM_TIMEOUT_S", "180")))
    # Capacity of the anyio threadpool that runs BOTH sync route handlers and
    # sync BackgroundTasks (the LLM pipelines). anyio's default (~40) lets a
    # burst of long LLM tasks starve page loads; the work is I/O-bound.
    threadpool_tokens: int = field(
        default_factory=lambda: int(os.getenv("THREADPOOL_TOKENS", "100"))
    )
    # OpenRouter only, optional: attribution shown on openrouter.ai dashboards.
    openrouter_referer: str | None = field(
        default_factory=lambda: os.getenv("OPENROUTER_HTTP_REFERER")
    )
    openrouter_title: str | None = field(
        default_factory=lambda: os.getenv("OPENROUTER_APP_TITLE", "JobPrep Works")
    )
    # Ask OpenRouter to route only to model providers that may not retain or
    # train on prompts. Your career documents ride in these prompts; leave it on
    # unless you have a reason not to (it narrows the pool of eligible hosts).
    openrouter_no_training: bool = field(
        default_factory=lambda: _flag("OPENROUTER_NO_TRAINING", "1")
    )

    # ── Company Pulse: employer research with web search (services/pulse.py) ─
    # research_provider names the model backend for the research run: 'auto'
    # (default) follows LLM_PROVIDER. web_search names how the model reaches the
    # web: 'auto' uses the provider's own server-side search when it has one
    # (Anthropic, OpenRouter, OpenAI) and otherwise falls back to whichever
    # standalone search API you configured; 'tavily'/'brave'/'searxng' force
    # that API for ANY provider (this is how Ollama and llama.cpp get search);
    # 'off' disables Company Pulse entirely and hides the tab.
    research_enabled: bool = field(default_factory=lambda: _flag("RESEARCH_ENABLED", "1"))
    research_provider: str = field(
        default_factory=lambda: os.getenv("RESEARCH_PROVIDER", "auto").strip().lower()
    )
    # Model for the research run. Unset = the app's own LLM_MODEL. Research is
    # long-context, judgment-heavy work: a small local model will produce a
    # thin pulse, so point this at your most capable option if they differ.
    research_model: str = field(default_factory=lambda: os.getenv("RESEARCH_MODEL", "").strip())
    web_search: str = field(default_factory=lambda: os.getenv("WEB_SEARCH", "auto").strip().lower())
    tavily_api_key: str | None = field(default_factory=lambda: os.getenv("TAVILY_API_KEY"))
    brave_api_key: str | None = field(default_factory=lambda: os.getenv("BRAVE_API_KEY"))
    # Any SearXNG instance with the JSON format enabled — including one you run
    # yourself, which keeps the whole pipeline on your own machines.
    searxng_url: str | None = field(
        default_factory=lambda: (os.getenv("SEARXNG_URL") or "").rstrip("/") or None
    )
    # Per-run caps. Each search costs money on a paid API and context tokens
    # everywhere; a run stops early once the model has enough.
    research_max_searches: int = field(
        default_factory=lambda: int(os.getenv("RESEARCH_MAX_SEARCHES", "6"))
    )
    research_max_fetches: int = field(
        default_factory=lambda: int(os.getenv("RESEARCH_MAX_FETCHES", "2"))
    )
    # Hard per-run spend ceiling (USD). Only enforceable where the provider
    # reports cost or publishes prices; local models report nothing and are free.
    research_cost_budget: float = field(
        default_factory=lambda: float(os.getenv("RESEARCH_COST_BUDGET", "1.50"))
    )
    research_timeout_s: float = field(
        default_factory=lambda: float(os.getenv("RESEARCH_TIMEOUT_S", "300"))
    )
    # Pulses are cached by normalized company name and held this many days
    # before a refresh is allowed. The daily limit bounds how many
    # search-triggering lookups run in one UTC day (cache hits are free).
    pulse_ttl_days: int = field(default_factory=lambda: int(os.getenv("PULSE_TTL_DAYS", "3")))
    pulse_daily_limit: int = field(default_factory=lambda: int(os.getenv("PULSE_DAILY_LIMIT", "25")))
    # Seconds between poller sweeps that pick up queued or stranded research
    # (0 disables the poller — tests set 0 and call pulse.sweep() directly).
    pulse_poll_interval: int = field(
        default_factory=lambda: int(os.getenv("PULSE_POLL_INTERVAL", "60"))
    )

    # ── Local spend guard (services/usage.py) ───────────────────────────────
    # You pay for your own tokens, so nothing is gated behind a plan. The
    # ledger records every LLM-triggering action so the Account page can show
    # what today cost you; LLM_DAILY_LIMIT is an optional runaway brake (units
    # per UTC day, 0 = unlimited, the default). Raising it never unlocks a
    # feature — every feature is always fully available.
    llm_daily_limit: int = field(default_factory=lambda: int(os.getenv("LLM_DAILY_LIMIT", "0")))
    # Ledger rows older than this are collapsed into a per-day summary and
    # pruned by a background sweeper (interval in seconds; 0 disables it).
    llm_ledger_retention_days: int = field(
        default_factory=lambda: int(os.getenv("LLM_LEDGER_RETENTION_DAYS", "90"))
    )
    llm_ledger_sweep_interval: int = field(
        default_factory=lambda: int(os.getenv("LLM_LEDGER_SWEEP_INTERVAL", "86400"))
    )

    # ── Stale-pipeline reaper (services/reaper.py) ──────────────────────────
    # Background LLM pipelines are in-process tasks, so killing the server
    # mid-run would strand rows in their in-flight status, polling forever.
    # Every REAPER_INTERVAL seconds (0 disables) rows whose busy_since stamp is
    # older than REAPER_STALE_MINUTES flip to a retryable error. The stale
    # window must exceed the longest legitimate run — raise it for slow local
    # models.
    reaper_interval: int = field(default_factory=lambda: int(os.getenv("REAPER_INTERVAL", "300")))
    reaper_stale_minutes: int = field(
        default_factory=lambda: int(os.getenv("REAPER_STALE_MINUTES", "30"))
    )


settings = Settings()


def resolved_model() -> str:
    """The model id for the main LLM pipelines, with the one built-in default."""
    if settings.llm_model:
        return settings.llm_model
    if settings.llm_provider == ANTHROPIC:
        return DEFAULT_ANTHROPIC_MODEL
    return ""


def resolved_base_url() -> str | None:
    """The API base URL for OpenAI-compatible providers: explicit setting first,
    then the well-known default for OpenAI and OpenRouter. Local servers
    (llama.cpp, vLLM) have no default — LLM_BASE_URL is required for those."""
    if settings.llm_base_url:
        return settings.llm_base_url
    if settings.llm_provider == OPENROUTER:
        return OPENROUTER_BASE_URL
    if settings.llm_provider == OPENAI:
        return OPENAI_BASE_URL
    return None


def research_provider_name() -> str:
    """Which provider family runs Company Pulse. RESEARCH_PROVIDER=auto (the
    default) follows LLM_PROVIDER, so one setting configures the whole app."""
    choice = settings.research_provider
    return settings.llm_provider if choice in ("", "auto") else choice


def search_backend_name() -> str:
    """Which web-search implementation Company Pulse will actually use:
    'native' (the model provider searches server-side), 'tavily', 'brave',
    'searxng', or 'none'. WEB_SEARCH=auto resolves it from what's configured."""
    choice = settings.web_search
    if choice in ("off", "none", "0"):
        return "none"
    if choice in ("tavily", "brave", "searxng", "native"):
        return choice
    # auto: prefer the provider's own server-side search, then a configured API.
    if research_provider_name() in (ANTHROPIC, OPENROUTER, OPENAI):
        return "native"
    if settings.tavily_api_key:
        return "tavily"
    if settings.brave_api_key:
        return "brave"
    if settings.searxng_url:
        return "searxng"
    return "none"


def pulse_available() -> bool:
    """Whether Company Pulse can run at all. Off when RESEARCH_ENABLED=0 or no
    search backend is reachable — the Pulse tab hides itself rather than
    offering a button that can only fail."""
    if not settings.research_enabled:
        return False
    if settings.llm_provider == MOCK:
        return True  # canned pulse; no network
    return search_backend_name() != "none"


def llm_config_warnings() -> list[str]:
    """Boot-time checks on the provider configuration, logged by the lifespan.
    These are the mistakes that otherwise surface ten minutes later as a
    background pipeline failing for no visible reason — say them at startup."""
    out: list[str] = []
    provider = settings.llm_provider
    if provider not in KNOWN_PROVIDERS:
        return [
            f"LLM_PROVIDER={provider!r} is not recognized. Use one of: "
            + ", ".join(sorted(KNOWN_PROVIDERS))
        ]
    if provider == MOCK:
        return ["LLM_PROVIDER=mock — canned responses only, no model is being called."]
    if not resolved_model():
        out.append(f"LLM_MODEL is unset and {provider} has no default — set it in .env.")
    if provider == ANTHROPIC and not settings.llm_api_key:
        out.append("ANTHROPIC_API_KEY is unset — Anthropic calls will fail.")
    if provider in OPENAI_COMPAT_ALIASES:
        if not resolved_base_url():
            out.append(
                f"LLM_BASE_URL is unset and {provider} has no default base URL — set it in .env."
            )
        elif provider in (OPENAI, OPENROUTER) and not settings.llm_api_key:
            key = "OPENAI_API_KEY" if provider == OPENAI else "OPENROUTER_API_KEY"
            out.append(f"No API key found for {provider} — set LLM_API_KEY (or {key}).")
    if settings.research_enabled and search_backend_name() == "none":
        out.append(
            "Company Pulse is enabled but no web search is available for this provider — "
            "set TAVILY_API_KEY, BRAVE_API_KEY, or SEARXNG_URL (or RESEARCH_ENABLED=0). "
            "The Pulse tab stays hidden until then."
        )
    return out


DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
