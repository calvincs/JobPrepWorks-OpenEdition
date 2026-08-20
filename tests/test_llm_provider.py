"""Provider selection wiring (app/llm/base.py + the config resolution helpers).

The point of these tests is the promise the README makes: pick a provider in
.env and the app configures itself. get_provider() is lru_cached and reads
app.config.settings at call time, so each test swaps that and clears the cache
before AND after — otherwise the mock provider conftest set up would leak into
every other test.
"""

import pytest

import app.llm.base as base
from app import config
from app.config import Settings
from app.llm.base import LLMError


def _provider_for(monkeypatch, **overrides):
    monkeypatch.setattr(config, "settings", Settings(**overrides))
    base.get_provider.cache_clear()
    return base.get_provider()


@pytest.fixture(autouse=True)
def _reset_provider_cache():
    yield
    base.get_provider.cache_clear()


# ── Anthropic ────────────────────────────────────────────────────────────────


def test_anthropic_needs_only_a_key(monkeypatch):
    """The one provider with a model default: a key alone is enough to start."""
    # Settings() fills unspecified fields from the real environment, so a
    # developer's own LLM_MODEL in .env would otherwise stand in for the default.
    monkeypatch.delenv("LLM_MODEL", raising=False)
    p = _provider_for(monkeypatch, llm_provider="anthropic", llm_api_key="sk-ant-test")
    assert type(p).__name__ == "AnthropicProvider"
    assert p.model == config.DEFAULT_ANTHROPIC_MODEL


def test_anthropic_model_override(monkeypatch):
    p = _provider_for(
        monkeypatch, llm_provider="anthropic", llm_model="claude-opus-5", llm_api_key="k"
    )
    assert p.model == "claude-opus-5"


# ── OpenRouter ───────────────────────────────────────────────────────────────


def test_openrouter_defaults_base_url_and_attribution(monkeypatch):
    """LLM_PROVIDER=openrouter needs only a model + key: the base URL defaults
    to OpenRouter's and the optional attribution headers are attached."""
    p = _provider_for(
        monkeypatch,
        llm_provider="openrouter",
        llm_model="anthropic/claude-sonnet-4.5",
        llm_base_url=None,
        llm_api_key="sk-or-test",
        openrouter_referer="http://127.0.0.1:8000",
        openrouter_title="JobPrep Works",
    )
    assert type(p).__name__ == "OpenAICompatProvider"
    assert p.model == "anthropic/claude-sonnet-4.5"
    assert str(p.client.base_url).rstrip("/") == config.OPENROUTER_BASE_URL
    assert p.client.default_headers.get("X-Title") == "JobPrep Works"
    assert p.client.default_headers.get("HTTP-Referer") == "http://127.0.0.1:8000"


def test_openrouter_sends_no_training_routing_preference(monkeypatch):
    """Career documents ride in these prompts, so the no-retention routing
    preference must actually be on the wire, not just a settings flag."""
    p = _provider_for(
        monkeypatch, llm_provider="openrouter", llm_model="m", llm_api_key="k",
        openrouter_no_training=True,
    )
    assert p.extra_body == {"provider": {"data_collection": "deny"}}


def test_openrouter_no_training_can_be_turned_off(monkeypatch):
    p = _provider_for(
        monkeypatch, llm_provider="openrouter", llm_model="m", llm_api_key="k",
        openrouter_no_training=False,
    )
    assert p.extra_body is None


def test_explicit_base_url_overrides_openrouter_default(monkeypatch):
    p = _provider_for(
        monkeypatch,
        llm_provider="openrouter",
        llm_model="m",
        llm_base_url="https://proxy.example/v1",
        llm_api_key="k",
    )
    assert str(p.client.base_url).rstrip("/") == "https://proxy.example/v1"


# ── OpenAI and other compatible servers ──────────────────────────────────────


def test_openai_defaults_its_base_url_and_gets_no_openrouter_headers(monkeypatch):
    p = _provider_for(
        monkeypatch, llm_provider="openai", llm_model="gpt-4o", llm_base_url=None, llm_api_key="k"
    )
    assert str(p.client.base_url).rstrip("/") == config.OPENAI_BASE_URL
    assert p.client.default_headers.get("X-Title") is None
    assert p.extra_body is None


def test_local_openai_compatible_server_requires_a_base_url(monkeypatch):
    """llama.cpp/vLLM have no canonical URL, so the app must say so plainly
    rather than silently pointing at api.openai.com."""
    with pytest.raises(LLMError, match="LLM_BASE_URL"):
        _provider_for(monkeypatch, llm_provider="llamacpp", llm_model="m", llm_base_url=None)


# ── Ollama ───────────────────────────────────────────────────────────────────


def test_ollama_defaults_to_localhost(monkeypatch):
    p = _provider_for(monkeypatch, llm_provider="ollama", llm_model="llama3.1:8b")
    assert type(p).__name__ == "OllamaProvider"
    assert p.chat_url == "http://localhost:11434/api/chat"


def test_ollama_tolerates_an_openai_style_base_url(monkeypatch):
    """Someone who pasted the /v1 URL from another tool still gets the native
    API, which is the only way num_ctx and structured output are available."""
    p = _provider_for(
        monkeypatch, llm_provider="ollama", llm_model="m", llm_base_url="http://box:11434/v1"
    )
    assert p.chat_url == "http://box:11434/api/chat"


# ── Misconfiguration ─────────────────────────────────────────────────────────


def test_missing_model_is_a_clear_error(monkeypatch):
    with pytest.raises(LLMError, match="LLM_MODEL"):
        _provider_for(monkeypatch, llm_provider="openrouter", llm_model="", llm_api_key="k")


def test_unknown_provider_lists_the_valid_ones(monkeypatch):
    with pytest.raises(LLMError) as exc:
        _provider_for(monkeypatch, llm_provider="gemini", llm_model="m")
    assert "anthropic" in str(exc.value) and "ollama" in str(exc.value)


def test_config_warnings_name_the_missing_key(monkeypatch):
    monkeypatch.setattr(
        config, "settings", Settings(llm_provider="anthropic", llm_api_key=None)
    )
    assert "ANTHROPIC_API_KEY" in " ".join(config.llm_config_warnings())


def test_config_warnings_name_the_missing_model(monkeypatch):
    monkeypatch.setattr(
        config, "settings", Settings(llm_provider="openrouter", llm_model="", llm_api_key="k")
    )
    assert "LLM_MODEL" in " ".join(config.llm_config_warnings())


def test_config_warnings_quiet_for_a_complete_setup(monkeypatch):
    monkeypatch.setattr(
        config,
        "settings",
        Settings(llm_provider="anthropic", llm_model="claude-sonnet-5", llm_api_key="k"),
    )
    assert config.llm_config_warnings() == []


# ── Web-search resolution (the piece that decides if Pulse can run) ──────────


@pytest.mark.parametrize(
    "provider,expected",
    [("anthropic", "native"), ("openai", "native"), ("openrouter", "native")],
)
def test_auto_search_prefers_native_where_the_provider_has_it(monkeypatch, provider, expected):
    monkeypatch.setattr(
        config, "settings", Settings(llm_provider=provider, llm_model="m", web_search="auto")
    )
    assert config.search_backend_name() == expected
    assert config.pulse_available()


def test_auto_search_falls_back_to_a_configured_api_for_local_models(monkeypatch):
    monkeypatch.setattr(
        config,
        "settings",
        Settings(llm_provider="ollama", llm_model="m", web_search="auto", tavily_api_key="tvly-x"),
    )
    assert config.search_backend_name() == "tavily"
    assert config.pulse_available()


def test_local_model_with_no_search_key_disables_pulse(monkeypatch):
    monkeypatch.setattr(
        config, "settings", Settings(llm_provider="ollama", llm_model="m", web_search="auto")
    )
    assert config.search_backend_name() == "none"
    assert not config.pulse_available()


def test_explicit_backend_wins_over_a_native_capable_provider(monkeypatch):
    """Forcing a standalone backend is how you keep searches off the model
    provider even when it could do them itself."""
    monkeypatch.setattr(
        config,
        "settings",
        Settings(llm_provider="anthropic", llm_model="m", web_search="searxng",
                 searxng_url="http://localhost:8888"),
    )
    assert config.search_backend_name() == "searxng"


def test_search_can_be_turned_off_entirely(monkeypatch):
    monkeypatch.setattr(
        config, "settings", Settings(llm_provider="anthropic", llm_model="m", web_search="off")
    )
    assert config.search_backend_name() == "none"
    assert not config.pulse_available()


def test_research_enabled_false_disables_pulse_regardless(monkeypatch):
    monkeypatch.setattr(
        config,
        "settings",
        Settings(llm_provider="anthropic", llm_model="m", research_enabled=False),
    )
    assert not config.pulse_available()


# ── API key resolution ───────────────────────────────────────────────────────
#
# Settings() reads the environment through default_factory, so these drive the
# real env vars rather than passing overrides.

_KEY_VARS = ("LLM_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")


def _key_for(monkeypatch, provider, **env):
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", provider)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings().llm_api_key


def test_another_vendors_key_is_not_sent_to_anthropic(monkeypatch):
    """An OPENAI_API_KEY exported in the shell must not be handed to Anthropic:
    it used to be, and surfaced as an upstream 401 on a key the user never
    meant to send."""
    assert _key_for(monkeypatch, "anthropic", OPENAI_API_KEY="sk-proj-openai") is None
    assert (
        _key_for(monkeypatch, "anthropic", OPENAI_API_KEY="sk-proj-openai", ANTHROPIC_API_KEY="sk-ant-x")
        == "sk-ant-x"
    )


def test_openrouter_ignores_an_openai_key(monkeypatch):
    assert _key_for(monkeypatch, "openrouter", OPENAI_API_KEY="sk-proj-openai") is None
    assert _key_for(monkeypatch, "openrouter", OPENROUTER_API_KEY="sk-or-x") == "sk-or-x"


def test_llm_api_key_wins_over_the_vendor_name(monkeypatch):
    key = _key_for(monkeypatch, "anthropic", LLM_API_KEY="explicit", ANTHROPIC_API_KEY="vendor")
    assert key == "explicit"


def test_key_is_stripped(monkeypatch):
    """A trailing newline pasted into .env is otherwise sent verbatim and
    rejected upstream as an invalid key."""
    assert _key_for(monkeypatch, "anthropic", ANTHROPIC_API_KEY=" sk-ant-x\n") == "sk-ant-x"
    assert _key_for(monkeypatch, "anthropic", ANTHROPIC_API_KEY="   ") is None


def test_missing_key_warning_names_the_key_that_is_set_instead(monkeypatch):
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-openai")
    monkeypatch.setattr(config, "settings", Settings(llm_provider="anthropic", llm_api_key=None))
    warning = "\n".join(config.llm_config_warnings())
    assert "ANTHROPIC_API_KEY is unset" in warning
    assert "OPENAI_API_KEY is set" in warning


# ── Wire format ──────────────────────────────────────────────────────────────


def test_openai_compat_sends_max_completion_tokens(monkeypatch):
    """Newer OpenAI models reject max_tokens with a 400 in favor of
    max_completion_tokens; OpenRouter, llama.cpp, and vLLM all accept the new
    name, so it is the one that must be on the wire."""
    from types import SimpleNamespace

    from app.llm.openai_compat_provider import OpenAICompatProvider

    p = OpenAICompatProvider(model="m", base_url="http://box/v1", api_key="k")
    sent = {}

    def fake_create(**kwargs):
        sent.update(kwargs)
        message = SimpleNamespace(content="ok")
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])

    monkeypatch.setattr(p.client.chat.completions, "create", fake_create)
    assert p.complete(system="s", prompt="p", max_tokens=123) == "ok"
    assert sent["max_completion_tokens"] == 123
    assert "max_tokens" not in sent
