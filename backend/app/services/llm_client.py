"""Provider-agnostic LLM client abstraction.

All pipeline stages (Task Planner, Operation Generator, Reference Resolver,
Content Enricher, Reviewer, etc.) call through this single interface.
Provider-specific code lives ONLY in this module.

Supported providers
-------------------
  "gemini"  — Google Gemini via the ``google-genai`` SDK (default)
  "openai"  — OpenAI-compatible API via the ``openai`` SDK (fallback / legacy)

Configuration (via Settings / .env)
------------------------------------
  LLM_PROVIDER       = "gemini" | "openai"       (default: "gemini")
  GEMINI_API_KEY     = <your Gemini API key>      (required when provider=gemini)
  LLM_MODEL          = "gemini-2.5-flash"         (default)
  OPENAI_API_KEY     = <key>                      (required when provider=openai)
  OPENAI_BASE_URL    = <optional base url>        (openai only)

Usage
-----
    from app.services.llm_client import LLMClient, LLMRequest

    llm = LLMClient()
    response = llm.complete(LLMRequest(
        system_prompt="You are a ...",
        user_prompt="Do X and return JSON.",
        json_mode=True,
        temperature=0,
        max_tokens=1024,
    ))
    data = response.json   # already parsed dict/list, or None if parse failed
    raw  = response.text   # raw string always available
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------

@dataclass
class LLMRequest:
    """Provider-agnostic request specification.

    Callers never import ``openai`` or ``google.genai`` — they build an
    ``LLMRequest`` and pass it to ``LLMClient.complete()``.
    """
    system_prompt: str
    user_prompt: str
    temperature: float = 0.0
    max_tokens: int = 2048
    json_mode: bool = False
    """If True, the provider is asked to return valid JSON.
    ``response.json`` will be the parsed result (dict or list).
    Use this for every structured-output call."""


@dataclass
class LLMResponse:
    """Normalised response from any LLM provider."""
    text: str
    """Raw text content — always set, even when json_mode=True."""
    json: dict | list | None = None
    """Parsed JSON when json_mode=True and parsing succeeded, otherwise None."""
    usage: dict = field(default_factory=dict)
    """Token usage info keyed by provider-specific names."""


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------

class LLMClient:
    """Singleton-style LLM client — instantiate once, inject into services.

    The instance is created in ``DocumentAgentGraph.__init__`` and passed
    to every service via constructor injection so there is a single
    connection-pool / retry budget for the whole pipeline run.
    """

    def __init__(self, provider: str | None = None, model: str | None = None) -> None:
        self._provider: str = provider or settings.llm_provider
        self._model: str = model or settings.llm_model
        self._client = self._build_client()

    # ------------------------------------------------------------------
    # Client factory
    # ------------------------------------------------------------------

    def _build_client(self) -> Any:
        if self._provider == "gemini":
            from google import genai  # type: ignore[import]
            if not settings.gemini_api_key:
                raise RuntimeError(
                    "GEMINI_API_KEY is not set. "
                    "Add it to .env or set LLM_PROVIDER=openai to use OpenAI."
                )
            return genai.Client(api_key=settings.gemini_api_key)

        # Fallback: OpenAI-compatible
        from openai import OpenAI
        if not settings.openai_api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. "
                "Set GEMINI_API_KEY and LLM_PROVIDER=gemini to use Gemini."
            )
        return OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Execute a completion request against the configured provider."""
        if self._provider == "gemini":
            return self._complete_gemini(request)
        return self._complete_openai(request)

    # ------------------------------------------------------------------
    # Gemini provider
    # ------------------------------------------------------------------

    def _complete_gemini(self, req: LLMRequest) -> LLMResponse:
        from google.genai import types  # type: ignore[import]

        config_kwargs: dict[str, Any] = {
            "temperature": req.temperature,
            "max_output_tokens": req.max_tokens,
            "system_instruction": req.system_prompt,
        }
        if req.json_mode:
            # Ask Gemini to respond with JSON.
            # NOTE: Gemini 3.5 Flash supports constrained JSON output via
            # response_mime_type. We do NOT pass response_schema here because
            # our operations involve open-ended arrays that don't map cleanly
            # to a single Pydantic model. Use plain json mode + parse manually.
            config_kwargs["response_mime_type"] = "application/json"

        try:
            response = self._client.models.generate_content(
                model=self._model,
                contents=req.user_prompt,
                config=types.GenerateContentConfig(**config_kwargs),
            )
        except Exception as exc:
            log.error("Gemini API call failed: %s", exc)
            raise

        text = (response.text or "").strip()
        parsed = self._try_parse_json(text) if req.json_mode else None
        return LLMResponse(text=text, json=parsed)

    # ------------------------------------------------------------------
    # OpenAI provider
    # ------------------------------------------------------------------

    def _complete_openai(self, req: LLMRequest) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": req.system_prompt},
                {"role": "user", "content": req.user_prompt},
            ],
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
        }
        if req.json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            response = self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            log.error("OpenAI API call failed: %s", exc)
            raise

        text = (response.choices[0].message.content or "").strip()
        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
            }
        parsed = self._try_parse_json(text) if req.json_mode else None
        return LLMResponse(text=text, json=parsed, usage=usage)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _try_parse_json(text: str) -> dict | list | None:
        """Attempt to parse text as JSON. Returns None on failure."""
        if not text:
            return None
        # Strip accidental markdown fences that some models add
        stripped = text
        if stripped.startswith("```json"):
            stripped = stripped[7:]
        if stripped.startswith("```"):
            stripped = stripped[3:]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        stripped = stripped.strip()

        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            log.warning("LLM returned non-JSON text (first 200 chars): %s", text[:200])
            return None
