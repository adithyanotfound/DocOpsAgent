import json

from app.core.config import settings


class Reviewer:
    """Reviews whether proposed edits satisfy the user request.

    Uses an LLM if configured; falls back to a simple heuristic.

    The LLM is instructed to be lenient: if the edits are directionally
    correct (e.g. the user asked to change X and the text was changed),
    mark satisfied=True rather than demanding perfection.
    """

    def review(self, request: str, edits: list[dict]) -> dict:
        if not edits:
            return {"satisfied": False, "feedback": "No editable targets were found."}

        # If no text actually changed, skip LLM and return False immediately.
        any_changed = any(edit["old_text"] != edit["new_text"] for edit in edits)
        if not any_changed:
            return {"satisfied": False, "feedback": "None of the text blocks were changed."}

        if settings.openai_api_key:
            try:
                return self._review_with_llm(request, edits)
            except Exception:
                pass

        return self._review_local(request, edits)

    # ------------------------------------------------------------------
    # LLM path
    # ------------------------------------------------------------------

    def _review_with_llm(self, request: str, edits: list[dict]) -> dict:
        from openai import OpenAI

        client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
        )

        # Only send blocks where text actually changed to keep the prompt small.
        changed_edits = [e for e in edits if e["old_text"] != e["new_text"]]
        edits_text = "\n\n".join(
            f"Block {i + 1}:\n  Before: {edit['old_text']}\n  After:  {edit['new_text']}"
            for i, edit in enumerate(changed_edits)
        )

        system_prompt = (
            "You are a lenient document editing reviewer.\n"
            "Given a user instruction and a list of before/after text changes, "
            "decide whether the edits are directionally correct — i.e., they "
            "make a reasonable attempt to satisfy the instruction.\n"
            "Be lenient: if the text was changed in a way that addresses the "
            "instruction (even partially), return satisfied=true.\n"
            "Only return satisfied=false if the edits are completely wrong or "
            "the text was not changed at all.\n"
            "Return JSON with:\n"
            "  - satisfied (bool)\n"
            "  - feedback (str): concise improvement suggestion if not satisfied, else \"\"\n"
            "Return ONLY valid JSON."
        )
        user_prompt = f"Instruction: {request}\n\nChanges made:\n{edits_text}"

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
        return {
            "satisfied": bool(data.get("satisfied", True)),  # default to True if uncertain
            "feedback": str(data.get("feedback", "")),
        }

    # ------------------------------------------------------------------
    # Local fallback
    # ------------------------------------------------------------------

    def _review_local(self, request: str, edits: list[dict]) -> dict:
        # If any block changed, consider it satisfied.
        return {"satisfied": True, "feedback": ""}
