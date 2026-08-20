"""Provider for any OpenAI-compatible chat-completions endpoint:
OpenAI, OpenRouter, Ollama (/v1), llama.cpp server, vLLM, etc.

Configure with LLM_PROVIDER=openai, LLM_BASE_URL, LLM_MODEL, LLM_API_KEY.
For OpenRouter set LLM_PROVIDER=openrouter and only LLM_MODEL + OPENROUTER_API_KEY
are required — the base URL defaults to https://openrouter.ai/api/v1 and optional
attribution headers are attached (see app/llm/base.py).

Extraction strategy: the target schema is always embedded in the system
prompt (weak/local models need it), and response_format json_schema is
requested on top when the server supports it. If the server rejects
response_format, we retry once without it and rely on the prompt + local
validation. Thinking models (e.g. qwen via Ollama) spend tokens on a
separate reasoning channel before content, so token budgets stay generous.
"""

import json
import logging
import re
from typing import TypeVar

import openai
from app.config import settings
from app.llm.base import LLMError
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

log = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text.strip())


def _status_error(exc: "openai.APIStatusError") -> LLMError:
    """Upstream error body -> log; user sees only the status class. The internal
    base URL and provider message never reach the browser (see app/user_errors)."""
    log.warning(
        "LLM endpoint error %s: %s", exc.status_code, getattr(exc, "message", "")
    )
    if exc.status_code == 429:
        return LLMError("The AI service is rate-limited right now — try again shortly.")
    return LLMError(
        f"The AI service returned an error ({exc.status_code}) — try again."
    )


class OpenAICompatProvider:
    def __init__(
        self,
        model: str,
        base_url: str | None,
        api_key: str | None,
        default_headers: dict[str, str] | None = None,
        extra_body: dict | None = None,
    ):
        # Local servers (Ollama, llama.cpp) ignore the key but the SDK requires one.
        # default_headers carries e.g. OpenRouter's optional attribution headers;
        # extra_body carries OpenRouter's no-training routing preference (only for
        # OpenRouter — strict OpenAI-compatible servers reject unknown body fields).
        self.client = openai.OpenAI(
            base_url=base_url,
            api_key=api_key or "not-needed",
            timeout=settings.llm_timeout_s,  # local models can be slow: raise LLM_TIMEOUT_S
            max_retries=1,
            default_headers=default_headers,
        )
        self.model = model
        self.extra_body = extra_body

    def _create(self, *, system: str, prompt: str, max_tokens: int, **kwargs):
        try:
            return self.client.chat.completions.create(
                model=self.model,
                max_completion_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                extra_body=self.extra_body,
                **kwargs,
            )
        except openai.APIConnectionError as exc:
            log.warning("could not reach LLM endpoint at %s", self.client.base_url)
            raise LLMError(
                "Could not reach the AI service — check your network."
            ) from exc

    def complete(self, *, system: str, prompt: str, max_tokens: int = 16000) -> str:
        try:
            response = self._create(system=system, prompt=prompt, max_tokens=max_tokens)
        except openai.APIStatusError as exc:
            raise _status_error(exc) from exc
        content = response.choices[0].message.content or ""
        if not content and response.choices[0].finish_reason == "length":
            raise LLMError(
                "Model ran out of tokens before producing output (thinking model?). Increase max_tokens."
            )
        return content

    def extract(
        self, *, system: str, prompt: str, schema: type[T], max_tokens: int = 16000
    ) -> T:
        json_schema = schema.model_json_schema()
        system_with_schema = (
            f"{system}\n\n"
            "Respond ONLY with a single JSON object that validates against this JSON Schema "
            "- no markdown fences, no commentary:\n"
            f"{json.dumps(json_schema)}"
        )
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "schema": json_schema},
        }

        use_response_format = True
        last_exc: Exception | None = None
        attempts = 0
        while attempts < 2:  # invalid output gets exactly one retry
            attempts += 1
            kwargs = {"response_format": response_format} if use_response_format else {}
            try:
                response = self._create(
                    system=system_with_schema,
                    prompt=prompt,
                    max_tokens=max_tokens,
                    **kwargs,
                )
            except openai.APIStatusError as exc:
                if use_response_format:
                    # Server doesn't support json_schema response_format; the
                    # capability probe doesn't count as a validation attempt.
                    use_response_format = False
                    attempts -= 1
                    continue
                raise _status_error(exc) from exc

            content = response.choices[0].message.content or ""
            if not content and response.choices[0].finish_reason == "length":
                raise LLMError(
                    "Model ran out of tokens before producing output (thinking model?). Increase max_tokens."
                )
            try:
                return schema.model_validate(json.loads(_strip_fences(content)))
            except (json.JSONDecodeError, ValidationError) as exc:
                last_exc = exc
        log.warning(
            "model output failed validation for %s", schema.__name__, exc_info=last_exc
        )
        raise LLMError(
            "The AI returned an unusable response — try again."
        ) from last_exc
