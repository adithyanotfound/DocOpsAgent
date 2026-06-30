import json
import re

from app.core.config import settings


# Keywords that suggest the user wants to generate/populate a full deck
# rather than make targeted edits.
_GENERATE_PATTERNS = [
    r"\bcreate\b.*\b(ppt|presentation|deck|slides)\b",
    r"\bgenerate\b.*\b(ppt|presentation|deck|slides|content)\b",
    r"\bmake\b.*\b(ppt|presentation|deck)\b.*\babout\b",
    r"\bfill\b.*\b(template|slides|blank)\b",
    r"\bpopulate\b.*\b(template|slides)\b",
    r"\bbuild\b.*\b(ppt|presentation|deck)\b",
    r"\bprepare\b.*\b(ppt|presentation|deck)\b",
    r"\b(ppt|presentation|deck)\b.*\bon\b",
    r"\badd\s+(\d+\s+)?(more\s+)?slides?\b",
    r"\bdelete\s+(slide|slides)\b",
    r"\bremove\s+(slide|slides)\b",
]


class IntentClassifier:
    """Classifies the user's editing intent.

    Returns a dict with a ``mode`` field:
    - ``"edit"``: targeted text edits (existing behaviour).
    - ``"generate"``: template population / multi-slide content creation.

    When an OpenAI key is available, the model decides the mode and
    extracts relevant fields.  Falls back to regex heuristics otherwise.
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
            "  - mode (str): 'generate' if the user wants to create/populate/fill a presentation or add/delete slides, "
            "'edit' if the user wants to make targeted changes to existing text.\n"
            "  - topic (str): if mode is 'generate', the subject/topic for the presentation. Otherwise empty string.\n"
            "  - slide_count (int | null): if the user specifies a number of slides, that number. Otherwise null.\n"
            "  - delete_slides (list[int]): if the user wants to delete specific slides, list their 1-based numbers. Otherwise empty list.\n"
            "  - add_slides_count (int | null): if the user wants to add N more slides, that number. Otherwise null.\n"
            "  - direct_target (bool): true if the user is explicitly targeting a specific slide or paragraph by number (edit mode only).\n"
            "  - semantic_search_required (bool): true if the target must be found by searching content (edit mode only).\n"
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
            max_tokens=256,
            response_format={"type": "json_object"},
        )
        raw = (response.choices[0].message.content or "{}").strip()
        data = json.loads(raw)

        mode = data.get("mode", "edit")
        if mode not in ("edit", "generate"):
            mode = "edit"

        return {
            "mode": mode,
            "topic": str(data.get("topic", "")),
            "slide_count": data.get("slide_count") if isinstance(data.get("slide_count"), int) else None,
            "delete_slides": [s for s in data.get("delete_slides", []) if isinstance(s, int)],
            "add_slides_count": data.get("add_slides_count") if isinstance(data.get("add_slides_count"), int) else None,
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
        lowered = request.lower()

        # Check for generation patterns
        is_generate = any(re.search(p, lowered) for p in _GENERATE_PATTERNS)

        # Extract topic (rough heuristic: everything after "on", "about", "for")
        topic = ""
        topic_match = re.search(
            r"\b(?:on|about|for|regarding)\s+(.+?)(?:\.|$)", request, flags=re.IGNORECASE
        )
        if topic_match:
            topic = topic_match.group(1).strip()

        # Extract slide count
        slide_count = None
        sc_match = re.search(r"(\d+)\s*(?:slide|page)", lowered)
        if sc_match:
            slide_count = int(sc_match.group(1))

        # Extract delete slide numbers
        delete_slides: list[int] = []
        del_match = re.search(r"(?:delete|remove)\s+slides?\s+([\d,\s]+)", lowered)
        if del_match:
            delete_slides = [int(n.strip()) for n in del_match.group(1).split(",") if n.strip().isdigit()]

        # Extract add slide count
        add_slides_count = None
        add_match = re.search(r"add\s+(\d+)\s+(?:more\s+)?slides?", lowered)
        if add_match:
            add_slides_count = int(add_match.group(1))

        slide = re.search(r"\bslide\s+(\d+)\b", request, flags=re.IGNORECASE)
        paragraph = re.search(r"\bparagraph\s+(\d+)\b", request, flags=re.IGNORECASE)

        return {
            "mode": "generate" if is_generate else "edit",
            "topic": topic if is_generate else "",
            "slide_count": slide_count,
            "delete_slides": delete_slides,
            "add_slides_count": add_slides_count,
            "direct_target": bool(slide or paragraph) and not is_generate,
            "semantic_search_required": not bool(slide or paragraph) and not is_generate,
            "semantic_query": topic or request,
            "slide": int(slide.group(1)) if slide else None,
            "paragraph": int(paragraph.group(1)) if paragraph else None,
        }
