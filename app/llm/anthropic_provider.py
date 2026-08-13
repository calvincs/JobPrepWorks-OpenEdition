import logging
from typing import TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.llm.base import LLMError

T = TypeVar("T", bound=BaseModel)

log = logging.getLogger(__name__)


def _status_error(exc: "anthropic.APIStatusError") -> LLMError:
    """The upstream API error body can carry key names, org ids, or internal
    detail, so it goes to the log; the user sees only the status class (LLMError
    text is rendered to the browser). See app/user_errors.py."""
    log.warning("Anthropic API error %s: %s", exc.status_code, getattr(exc, "message", ""))
    if exc.status_code == 429:
        return LLMError("The AI service is rate-limited right now — try again shortly.")
    return LLMError(f"The AI service returned an error ({exc.status_code}) — try again.")


class AnthropicProvider:
    """Anthropic Claude: adaptive thinking, structured outputs via
    messages.parse. The key comes from LLM_API_KEY or ANTHROPIC_API_KEY;
    passing None lets the SDK fall back to its own environment lookup."""

    def __init__(self, model: str, api_key: str | None = None):
        # Explicit timeout: the SDK default (~10 min) would pin a threadpool
        # worker for the whole hang when the upstream is degraded. Retries on
        # 429/5xx/connection errors stay with the SDK (exponential backoff).
        self.client = anthropic.Anthropic(
            api_key=api_key or None, timeout=settings.llm_timeout_s, max_retries=2
        )
        self.model = model

    def complete(self, *, system: str, prompt: str, max_tokens: int = 16000) -> str:
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                thinking={"type": "adaptive"},
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APITimeoutError as exc:  # subclasses APIConnectionError
            raise LLMError("The AI service took too long — try again.") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("Could not reach the Anthropic API — check your network.") from exc
        except anthropic.APIStatusError as exc:
            raise _status_error(exc) from exc
        if response.stop_reason == "refusal":
            raise LLMError("The model declined this request.")
        if response.stop_reason == "max_tokens":
            raise LLMError("The AI response was cut off — try again, or use a shorter input.")
        return next((b.text for b in response.content if b.type == "text"), "")

    def extract(self, *, system: str, prompt: str, schema: type[T], max_tokens: int = 16000) -> T:
        last_exc: Exception | None = None
        for _attempt in range(2):  # SPEC section 9: invalid output retries once
            try:
                response = self.client.messages.parse(
                    model=self.model,
                    max_tokens=max_tokens,
                    system=system,
                    thinking={"type": "adaptive"},
                    messages=[{"role": "user", "content": prompt}],
                    output_format=schema,
                )
                if response.stop_reason == "refusal":
                    raise LLMError("The model declined this request.")
                if response.stop_reason == "max_tokens":
                    # Truncation is not invalid output — retrying the same
                    # prompt would just truncate again and burn a full call.
                    raise LLMError(
                        "The AI response was cut off — try again, or use a shorter input."
                    )
                if response.parsed_output is None:
                    raise ValidationError.from_exception_data("empty parsed_output", [])
                return response.parsed_output
            except anthropic.APITimeoutError as exc:  # subclasses APIConnectionError
                raise LLMError("The AI service took too long — try again.") from exc
            except anthropic.APIConnectionError as exc:
                raise LLMError("Could not reach the Anthropic API — check your network.") from exc
            except anthropic.APIStatusError as exc:
                raise _status_error(exc) from exc
            except ValidationError as exc:
                last_exc = exc
        # The schema class name is internal detail: log it, keep the user copy generic.
        log.warning("model output failed validation for %s", schema.__name__, exc_info=last_exc)
        raise LLMError("The AI returned an unusable response — try again.") from last_exc
