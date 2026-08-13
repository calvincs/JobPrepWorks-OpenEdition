"""Provider selection: one contract, four backends (plus a canned mock).

    get_provider().extract(system=..., prompt=..., schema=SomeModel) -> SomeModel

Everything the app asks a model to do goes through `complete()` or `extract()`.
Adding a pipeline means a new Pydantic schema (app/models/extraction.py), a
prompt (app/llm/prompts.py), and a canned entry in mock_provider.CANNED — not
a new provider branch.
"""

from functools import lru_cache
from typing import Protocol, TypeVar

from pydantic import BaseModel

# Imported as a module, not by name: every value below is read at call time so
# a test (or a future reload) can swap app.config.settings and have provider
# selection follow it.
from app import config
from app.config import ANTHROPIC, MOCK, OLLAMA, OPENAI_COMPAT_ALIASES, OPENROUTER

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """A provider call failed after retries. The message IS shown to the user,
    so it must read like product copy — keys, URLs, and upstream error bodies
    stay in the log."""


class LLMProvider(Protocol):
    def complete(self, *, system: str, prompt: str, max_tokens: int = 16000) -> str: ...

    def extract(self, *, system: str, prompt: str, schema: type[T], max_tokens: int = 16000) -> T: ...


# Sent with every OpenRouter request (main pipelines and Company Pulse) when
# OPENROUTER_NO_TRAINING is on: route only to model providers that may not
# retain or train on prompts. Your career documents ride in these prompts.
OPENROUTER_NO_TRAINING_BODY = {"provider": {"data_collection": "deny"}}


def openrouter_headers() -> dict[str, str] | None:
    """Optional attribution headers OpenRouter reads for its dashboards."""
    headers = {}
    if config.settings.openrouter_referer:
        headers["HTTP-Referer"] = config.settings.openrouter_referer
    if config.settings.openrouter_title:
        headers["X-Title"] = config.settings.openrouter_title
    return headers or None


def openrouter_extra_body() -> dict | None:
    return OPENROUTER_NO_TRAINING_BODY if config.settings.openrouter_no_training else None


def _require_model() -> str:
    model = config.resolved_model()
    if not model:
        raise LLMError(
            f"No model is configured. Set LLM_MODEL in your .env "
            f"(LLM_PROVIDER is {config.settings.llm_provider!r})."
        )
    return model


@lru_cache(maxsize=1)
def get_provider() -> LLMProvider:
    """The configured provider, built once per process. Cached, so changing
    .env requires a restart — which is also when the boot-time config warnings
    get a chance to tell you what's missing."""
    provider = config.settings.llm_provider

    if provider == ANTHROPIC:
        from app.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(model=_require_model(), api_key=config.settings.llm_api_key)

    if provider == OLLAMA:
        from app.llm.ollama_provider import OllamaProvider

        return OllamaProvider(
            model=_require_model(),
            base_url=config.settings.llm_base_url,
            num_ctx=config.settings.llm_num_ctx,
        )

    if provider in OPENAI_COMPAT_ALIASES:
        from app.llm.openai_compat_provider import OpenAICompatProvider

        base_url = config.resolved_base_url()
        if not base_url:
            raise LLMError(
                f"No API base URL is configured. Set LLM_BASE_URL in your .env "
                f"(LLM_PROVIDER is {provider!r})."
            )
        is_openrouter = provider == OPENROUTER
        return OpenAICompatProvider(
            model=_require_model(),
            base_url=base_url,
            api_key=config.settings.llm_api_key,
            default_headers=openrouter_headers() if is_openrouter else None,
            extra_body=openrouter_extra_body() if is_openrouter else None,
        )

    if provider == MOCK:
        from app.llm.mock_provider import MockProvider

        return MockProvider()

    raise LLMError(
        f"Unknown LLM_PROVIDER {provider!r}. Use one of: " + ", ".join(sorted(config.KNOWN_PROVIDERS))
    )
