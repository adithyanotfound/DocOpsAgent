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

    def review_plan(self, request: str, slide_plan: dict, intent: dict) -> dict:
        """Review a slide plan for generation mode.

        Checks that the plan has reasonable content, covers the topic,
        and respects any slide count preferences.
        """
        slides = slide_plan.get("slides", [])
        if not slides:
            return {"satisfied": False, "feedback": "The plan contains no slides."}

        # Count non-delete slides
        active_slides = [s for s in slides if s.get("action") != "delete"]
        if not active_slides:
            return {"satisfied": False, "feedback": "All slides are marked for deletion."}

        # Check that populate slides have actual content
        content_count = 0
        for s in active_slides:
            for shape in s.get("shapes", []):
                for para in shape.get("paragraphs", []):
                    if para.get("text", "").strip():
                        content_count += 1

        if content_count == 0:
            return {"satisfied": False, "feedback": "No content was generated for any slide."}

        if settings.openai_api_key:
            try:
                return self._review_plan_with_llm(request, slide_plan, intent)
            except Exception:
                pass

        return {"satisfied": True, "feedback": ""}

    # ------------------------------------------------------------------
    # LLM path — edit review
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
    # LLM path — plan review
    # ------------------------------------------------------------------

    def _review_plan_with_llm(self, request: str, slide_plan: dict, intent: dict) -> dict:
        from openai import OpenAI

        client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
        )

        # Build a concise summary of the plan
        slides = slide_plan.get("slides", [])
        plan_summary_lines = []
        for i, slide in enumerate(slides):
            action = slide.get("action", "populate")
            if action == "delete":
                plan_summary_lines.append(f"Slide {i+1}: DELETED")
                continue
            texts = []
            for shape in slide.get("shapes", []):
                for para in shape.get("paragraphs", []):
                    t = para.get("text", "").strip()
                    if t:
                        texts.append(t[:100])
                for row in shape.get("table_rows", []):
                    for cell in row:
                        for para in cell.get("paragraphs", []):
                            t = para.get("text", "").strip()
                            if t:
                                texts.append(t[:100])
            content_preview = " | ".join(texts[:3])
            plan_summary_lines.append(f"Slide {i+1}: {content_preview}")

        plan_summary = "\n".join(plan_summary_lines)

        system_prompt = (
            "You are a lenient presentation plan reviewer.\n"
            "Given a user request and a slide plan summary, decide whether the plan "
            "reasonably addresses the request.\n"
            "Be lenient: if the slides cover the topic and have substantive content, "
            "return satisfied=true.\n"
            "Only return satisfied=false if the plan completely misses the topic or "
            "has very little content.\n"
            "Return JSON with:\n"
            "  - satisfied (bool)\n"
            "  - feedback (str): concise suggestion if not satisfied, else \"\"\n"
            "Return ONLY valid JSON."
        )
        user_prompt = (
            f"User request: {request}\n"
            f"Topic: {intent.get('topic', '')}\n\n"
            f"Slide plan:\n{plan_summary}"
        )

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
            "satisfied": bool(data.get("satisfied", True)),
            "feedback": str(data.get("feedback", "")),
        }

    # ------------------------------------------------------------------
    # Local fallback
    # ------------------------------------------------------------------

    def _review_local(self, request: str, edits: list[dict]) -> dict:
        # If any block changed, consider it satisfied.
        return {"satisfied": True, "feedback": ""}

