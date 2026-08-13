"""Native Ollama provider (/api/chat).

LLM_PROVIDER=ollama uses this instead of the OpenAI-compat endpoint because
the native API exposes three things the compat layer doesn't:
- think: false          — disables thinking on models that support it
- options.num_ctx       — per-request context window (the compat layer runs
                          Ollama's default 4096, silently truncating long docs)
- format: <json schema> — server-side structured outputs

Models without thinking support reject the think field; we probe once and
remember.
"""

import json
import logging
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import OLLAMA_BASE_URL, settings
from app.llm.base import LLMError

T = TypeVar("T", bound=BaseModel)

log = logging.getLogger(__name__)


class OllamaProvider:
    def __init__(self, model: str, base_url: str | None, num_ctx: int = 16384):
        root = (base_url or OLLAMA_BASE_URL).rstrip("/")
        if root.endswith("/v1"):  # tolerate an OpenAI-compat style base URL
            root = root[: -len("/v1")].rstrip("/")
        self.chat_url = f"{root}/api/chat"
        self.model = model
        self.num_ctx = num_ctx
        self._send_think = True

    def _chat(self, *, system: str, prompt: str, max_tokens: int, format_schema: dict | None) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "options": {"num_ctx": self.num_ctx, "num_predict": max_tokens},
        }
        if format_schema is not None:
            payload["format"] = format_schema
        if self._send_think:
            payload["think"] = False

        try:
            response = httpx.post(self.chat_url, json=payload, timeout=settings.llm_timeout_s)
        except httpx.HTTPError as exc:
            log.warning("could not reach Ollama server at %s", self.chat_url)
            raise LLMError("Could not reach the AI service — check your network.") from exc

        if response.status_code >= 400:
            try:
                detail = response.json().get("error", response.text)
            except ValueError:
                detail = response.text
            if self._send_think and "think" in str(detail).lower():
                # Model doesn't accept the think field; drop it and retry once.
                self._send_think = False
                return self._chat(
                    system=system, prompt=prompt, max_tokens=max_tokens, format_schema=format_schema
                )
            # The server body can echo internals — log it, show only the status.
            log.warning("Ollama error %s: %s", response.status_code, detail)
            raise LLMError(f"The AI service returned an error ({response.status_code}) — try again.")

        data = response.json()
        content = (data.get("message") or {}).get("content", "")
        if not content and data.get("done_reason") == "length":
            raise LLMError("Model ran out of tokens before producing output. Increase max_tokens.")
        return content

    def complete(self, *, system: str, prompt: str, max_tokens: int = 16000) -> str:
        return self._chat(system=system, prompt=prompt, max_tokens=max_tokens, format_schema=None)

    def extract(self, *, system: str, prompt: str, schema: type[T], max_tokens: int = 16000) -> T:
        json_schema = schema.model_json_schema()
        system_json = f"{system}\n\nRespond with a single JSON object in the required structure."
        last_exc: Exception | None = None
        for _attempt in range(2):  # invalid output gets exactly one retry
            content = self._chat(
                system=system_json, prompt=prompt, max_tokens=max_tokens, format_schema=json_schema
            )
            try:
                return schema.model_validate(json.loads(content))
            except (json.JSONDecodeError, ValidationError) as exc:
                last_exc = exc
        log.warning("model output failed validation for %s", schema.__name__, exc_info=last_exc)
        raise LLMError("The AI returned an unusable response — try again.") from last_exc
