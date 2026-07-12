"""Content editor — rewrites document text blocks to fulfil a user request.

Uses LLMClient for provider-agnostic LLM calls (Gemini by default).
Falls back to deterministic heuristics when no LLM is available.
"""
from __future__ import annotations

import re


class ContentEditor:
    """Rewrites document text blocks to fulfil a user request.

    When an LLM is available the rewrite is delegated to it.
    Falls back to deterministic heuristics so the platform remains
    usable without credentials.
    """

    def __init__(self, llm=None) -> None:
        # Accept optional injected LLMClient; constructed lazily if not provided
        self._llm = llm

    def rewrite(
        self,
        request: str,
        text: str,
        metadata: dict | None = None,
        chat_history: list[dict] | None = None,
    ) -> str:
        from app.core.config import settings
        if settings.gemini_api_key or settings.openai_api_key:
            try:
                return self._rewrite_with_llm(request, text, metadata or {}, chat_history or [])
            except Exception:
                pass
        return self._rewrite_local(request, text)

    # ------------------------------------------------------------------
    # LLM path
    # ------------------------------------------------------------------

    def _rewrite_with_llm(
        self,
        request: str,
        text: str,
        metadata: dict,
        chat_history: list[dict],
    ) -> str:
        import json
        from app.services.llm_client import LLMClient, LLMRequest

        llm = self._llm or LLMClient()

        system_prompt = (
            "You are a precise document editing assistant evaluating ONE specific text block at a time.\n"
            "You will be given an editing instruction, the text block itself, and its metadata.\n\n"
            "IMPORTANT RULES:\n"
            "1. You must decide if the provided text block is the INTENDED TARGET of the instruction.\n"
            "2. If the user asks to edit a 'title', 'heading', or 'topic', you MUST ASSUME the provided "
            "text block IS the target and rewrite it, UNLESS the text block is obviously a footer, "
            "slide number (like `‹#›` or a plain digit), or a specific field label (like `Theme Name:`).\n"
            "3. If you decide the text block IS the target, return ONLY the new rewritten text.\n"
            "4. If you decide the text block is NOT the target, you MUST return the ORIGINAL TEXT EXACTLY AS-IS. "
            "Do not return any other text.\n"
            "5. Provide no commentary, markdown, or quotes.\n"
            "6. For formatting-related instructions (e.g. 'make it bold', 'center align', 'change font size'), "
            "you cannot change formatting directly — only change the TEXT content. Return the text as-is if the "
            "instruction is purely about formatting.\n"
            "7. When writing content, ensure it is professional, well-structured, and compelling. "
            "Use bullet points (•) for lists when appropriate."
        )

        meta_str = json.dumps(metadata) if metadata else "None"

        history_str = ""
        if chat_history:
            history_str = "Previous conversation context:\n"
            for msg in chat_history[-5:]:
                role = "User" if msg["role"] == "user" else "Agent"
                history_str += f"{role}: {msg['content']}\n"
            history_str += "\n"

        user_prompt = (
            f"{history_str}"
            f"Editing instruction: {request}\n\n"
            f"Text block to consider:\n{text}\n\n"
            f"Block metadata: {meta_str}"
        )

        response = llm.complete(LLMRequest(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.3,
            max_tokens=1024,
            json_mode=False,  # Raw text response
        ))
        return response.text or text

    # ------------------------------------------------------------------
    # Local fallback heuristics
    # ------------------------------------------------------------------

    def _rewrite_local(self, request: str, text: str) -> str:
        lowered = request.lower()
        if "remove" in lowered:
            return self._remove_mentions(request, text)
        if "shorter" in lowered or "summarize" in lowered or "shorten" in lowered:
            return self._shorten(text)
        if "professional" in lowered or "professionally" in lowered:
            return self._professionalize(text)
        if "expand" in lowered:
            return self._expand(text)
        return self._professionalize(text)

    def _shorten(self, text: str) -> str:
        sentences = re.split(r"(?<=[.!?])\s+", text.strip())
        if len(sentences) > 1:
            return " ".join(sentences[: max(1, len(sentences) // 2)])
        words = text.split()
        return " ".join(words[: max(8, int(len(words) * 0.6))])

    def _professionalize(self, text: str) -> str:
        cleaned = re.sub(r"\s+", " ", text).strip()
        replacements = {
            "we got": "we achieved",
            "big": "significant",
            "lots of": "substantial",
            "things": "initiatives",
        }
        for source, target in replacements.items():
            cleaned = re.sub(source, target, cleaned, flags=re.IGNORECASE)
        return cleaned[:1].upper() + cleaned[1:] if cleaned else cleaned

    def _expand(self, text: str) -> str:
        cleaned = self._professionalize(text)
        return f"{cleaned} This reflects a focused, measurable improvement aligned with the broader strategy."

    def _remove_mentions(self, request: str, text: str) -> str:
        match = re.search(r"remove all mentions of\s+(.+?)(?:[.!?]|$)", request, flags=re.IGNORECASE)
        phrase = match.group(1).strip().strip('"') if match else ""
        if not phrase:
            return text
        updated = re.sub(re.escape(phrase), "", text, flags=re.IGNORECASE)
        updated = re.sub(r"\s{2,}", " ", updated)
        updated = re.sub(r"\s+([,.!?])", r"\1", updated)
        return updated.strip()
