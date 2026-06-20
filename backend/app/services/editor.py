import re

from app.core.config import settings


class ContentEditor:
    """Rewrites document text blocks to fulfil a user request.

    When an OpenAI API key is available the rewrite is delegated to the
    configured model.  When no key is configured (or when the API call
    fails) it falls back to the original deterministic heuristics so the
    platform remains usable without credentials.
    """

    def rewrite(self, request: str, text: str, metadata: dict | None = None, chat_history: list[dict] | None = None) -> str:
        if settings.openai_api_key:
            try:
                return self._rewrite_with_llm(request, text, metadata or {}, chat_history or [])
            except Exception:
                pass
        return self._rewrite_local(request, text)

    # ------------------------------------------------------------------
    # LLM path
    # ------------------------------------------------------------------

    def _rewrite_with_llm(self, request: str, text: str, metadata: dict, chat_history: list[dict]) -> str:
        from openai import OpenAI
        import json

        client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
        )
        system_prompt = (
            "You are a precise document editing assistant. "
            "You will be given an editing instruction, a specific text block from a document, "
            "and its metadata (such as slide number, paragraph index, or section heading).\n\n"
            "IMPORTANT RULES:\n"
            "1. Only rewrite the text if it is DIRECTLY relevant to the instruction.\n"
            "2. If the text block is NOT related to the instruction (e.g. the instruction targets a "
            "different topic, section, or slide), return the original text EXACTLY as-is.\n"
            "3. Never add, remove, or change content that is out of scope of the instruction.\n"
            "4. Return ONLY the (possibly rewritten) text — no commentary, no markdown, no quotes."
        )
        
        meta_str = json.dumps(metadata) if metadata else "None"
        
        # Build chat history context
        history_str = ""
        if chat_history:
            history_str = "Previous conversation context:\n"
            for msg in chat_history[-5:]: # Only include the last 5 messages to avoid blowing up context
                role = "User" if msg["role"] == "user" else "Agent"
                history_str += f"{role}: {msg['content']}\n"
            history_str += "\n"

        user_prompt = (
            f"{history_str}"
            f"Editing instruction: {request}\n\n"
            f"Text block to consider:\n{text}\n\n"
            f"Block metadata: {meta_str}\n\n"
            "Return the rewritten text if relevant, or the original text unchanged if not relevant."
        )
        response = client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=1024,
        )
        result = (response.choices[0].message.content or text).strip()
        return result

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
