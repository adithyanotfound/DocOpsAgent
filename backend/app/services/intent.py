import json
import re

from app.core.config import settings


class IntentClassifier:
    """Classifies the user's editing intent.

    When an OpenAI key is available, the model is asked to return structured
    JSON indicating whether the user is targeting a specific slide or
    paragraph, and which index.  Falls back to the original regex approach
    when no key is configured or the API call fails.
    """

    def classify(self, request: str, chat_history: list[dict] | None = None) -> dict:
        if settings.openai_api_key:
            try:
                return self._classify_with_llm(request, chat_history or [])
            except Exception:
                pass
        return self._classify_local(request)

    # ------------------------------------------------------------------
    # LLM path
    # ------------------------------------------------------------------

    def _classify_with_llm(self, request: str, chat_history: list[dict]) -> dict:
        from openai import OpenAI

        client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
        )
        system_prompt = (
            "You are an assistant that classifies document editing requests.\n"
            "Given a user instruction and the conversation history, return a JSON object with the following keys:\n"
            "  - direct_target (bool): true if the user is explicitly targeting a specific slide or paragraph by number.\n"
            "  - semantic_search_required (bool): true if the target must be found by searching content.\n"
            "  - semantic_query (str): the search query to use to find the target. If the request is a follow-up (e.g. 'make it shorter', 'capitalize the first letter'), infer the subject from history and write a query to find the original text block.\n"
            "  - slide (int | null): the 1-based slide number if mentioned or implied from history, otherwise null.\n"
            "  - paragraph (int | null): the 1-based paragraph number if mentioned or implied from history, otherwise null.\n"
            "Return ONLY valid JSON with no commentary."
        )
        
        history_str = ""
        if chat_history:
            history_str = "Previous conversation history:\n"
            for msg in chat_history[-5:]:
                role = "User" if msg["role"] == "user" else "Agent"
                history_str += f"{role}: {msg['content']}\n"
            history_str += "\n"

        user_prompt = f"{history_str}User Instruction: {request}"

        response = client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
            max_tokens=128,
            response_format={"type": "json_object"},
        )
        raw = (response.choices[0].message.content or "{}").strip()
        data = json.loads(raw)
        return {
            "direct_target": bool(data.get("direct_target", False)),
            "semantic_search_required": bool(data.get("semantic_search_required", True)),
            "semantic_query": data.get("semantic_query") or request,
            "slide": data.get("slide") if isinstance(data.get("slide"), int) else None,
            "paragraph": data.get("paragraph") if isinstance(data.get("paragraph"), int) else None,
        }

    # ------------------------------------------------------------------
    # Local fallback
    # ------------------------------------------------------------------

    def _classify_local(self, request: str) -> dict:
        slide = re.search(r"\bslide\s+(\d+)\b", request, flags=re.IGNORECASE)
        paragraph = re.search(r"\bparagraph\s+(\d+)\b", request, flags=re.IGNORECASE)
        return {
            "direct_target": bool(slide or paragraph),
            "semantic_search_required": not bool(slide or paragraph),
            "slide": int(slide.group(1)) if slide else None,
            "paragraph": int(paragraph.group(1)) if paragraph else None,
        }
