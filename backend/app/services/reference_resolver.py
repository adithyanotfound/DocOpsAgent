"""Reference Resolver — resolves target hints to stable DOM element IDs.

Two-pass resolution Strategy:
1. Deterministic Structural Matching (No LLM): Handles ordinal lookups
   ("table 2", "third paragraph"), exact named sections ("Conclusion"),
   and structural roles ("all headings", "bulleted lists").
2. LLM-assisted Semantic Matching: Falls back to Gemini to resolve
   ambiguous descriptions ("the section discussing growth metrics").
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.services.llm_client import LLMClient, LLMRequest

log = logging.getLogger(__name__)


class ReferenceResolver:
    """Resolves target_hint to concrete DOM element IDs from the outline."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm

    def resolve(self, target_hint: str, outline: dict, task_description: str = "") -> list[str]:
        """Resolve a target hint string to a list of matching element IDs."""
        if not target_hint:
            return []

        hint_lower = target_hint.lower().strip()

        # Pass 1: Deterministic matching
        resolved = self._structural_resolve(hint_lower, outline)
        if resolved:
            log.info("Pass 1: Deterministic resolve matched target '%s' to: %s", target_hint, resolved)
            return resolved

        # Pass 2: Semantic fallback (LLM-assisted)
        resolved = self._semantic_resolve(target_hint, outline, task_description)
        log.info("Pass 2: Semantic fallback resolved target '%s' to: %s", target_hint, resolved)
        return resolved

    def _structural_resolve(self, hint: str, outline: dict) -> list[str] | None:
        indices = outline.get("indices", {})
        sections = outline.get("sections", [])

        # Match "table of contents" or "toc"
        if "table of contents" in hint or hint == "toc":
            for s in sections:
                if s.get("semantic_type") == "toc":
                    return [s["heading_id"]]
            # Return first section if it matches TOC heading
            if sections and "contents" in sections[0]["heading"].lower():
                return [sections[0]["heading_id"]]

        # Match ordinals: "table N" or "table number N"
        table_match = re.search(r"\btable\s*(?:number\s*)?(\d+)\b", hint)
        if table_match:
            table_num = table_match.group(1)
            tbl_id = indices.get("tables_by_ordinal", {}).get(table_num)
            if tbl_id:
                return [tbl_id]

        # Match ordinals: "image N" or "image number N" or "figure N" or "figure number N"
        image_match = re.search(r"\b(?:image|figure|photo)\s*(?:number\s*)?(\d+)\b", hint)
        if image_match:
            img_num = image_match.group(1)
            img_id = indices.get("images_by_ordinal", {}).get(img_num)
            if img_id:
                return [img_id]

        # Match "all headings" or "headings"
        if hint in ("all headings", "headings", "section headings", "heading"):
            heading_ids = []
            for s in sections:
                if s.get("heading_id") and s["heading_id"] != "start":
                    heading_ids.append(s["heading_id"])
            if heading_ids:
                return heading_ids

        # Match "all tables" or "tables"
        if hint in ("all tables", "tables", "both tables"):
            return list(indices.get("tables_by_ordinal", {}).values())

        # Match "all images" or "images" or "logos"
        if hint in ("all images", "images", "both images", "logos", "logo"):
            return list(indices.get("images_by_ordinal", {}).values())

        # Match "last paragraph" or "last element"
        if hint in ("last paragraph", "last element", "end of document", "the end", "the end of the document"):
            last_id = indices.get("last_element_id")
            if last_id:
                return [last_id]

        # Match "first paragraph" or "first element"
        if hint in ("first paragraph", "first element", "beginning of document", "the beginning", "the beginning of the document", "start of document"):
            first_id = indices.get("first_content_id")
            if first_id:
                return [first_id]

        # Match exact heading name
        exact_id = indices.get("headings_by_name", {}).get(hint)
        if exact_id:
            return [exact_id]
        
        # Try substring match on heading names (e.g. "highlights" matching "Key Highlights")
        for name, h_id in indices.get("headings_by_name", {}).items():
            if hint in name or name in hint:
                return [h_id]

        # Match "slide N" or "slide number N" (for PPTX)
        slide_match = re.search(r"\bslide\s*(?:number\s*)?(\d+)\b", hint)
        if slide_match:
            slide_num = slide_match.group(1)
            slide_id = indices.get("slides_by_index", {}).get(slide_num)
            if slide_id:
                return [slide_id]

        return None

    def _semantic_resolve(self, target_hint: str, outline: dict, task_description: str = "") -> list[str]:
        llm = self._llm or LLMClient()

        # Build a list of candidate elements for the LLM to choose from
        candidates = []
        for s in outline.get("sections", []):
            if s.get("heading_id") and s["heading_id"] != "start":
                candidates.append({
                    "id": s["heading_id"],
                    "type": "heading",
                    "text": s["heading"],
                })
            for el in s.get("elements", []):
                if el.get("type") in ("paragraph", "table", "image"):
                    candidates.append({
                        "id": el["id"],
                        "type": el["type"],
                        "text": el.get("text_preview", ""),
                        "ordinal_label": el.get("ordinal_label", ""),
                    })

        # Cut down candidate list to prevent token overload
        candidates = candidates[:150]

        import json
        system_prompt = (
            "You are a document target resolver.\n"
            "Given a target description (how the user referred to some part of the document) "
            "and a list of all elements in the document, identify the element ID(s) that "
            "the user is targeting.\n\n"
            "CRITICAL RULES:\n"
            "1. Choose ONLY from the list of provided elements. Do NOT invent IDs.\n"
            "2. If the user targets a section (e.g. 'the executive summary'), return ALL element IDs contained within that section (e.g. the heading ID, followed by all paragraphs, tables, images, etc. in that section).\n"
            "3. If the user targets a specific paragraph (e.g. 'the paragraph about revenue growth'), return the ID of that paragraph.\n"
            "4. Return multiple IDs ONLY if the reference clearly targets multiple elements (e.g. 'all paragraphs in section X', or 'the entire section').\n"
            "5. The provided elements may lack explicit types (e.g., all elements might be marked as 'paragraph'). You MUST infer semantic roles (headings, list items, etc.) based on the actual 'text' content and structural patterns (like short phrases followed by multiple sentences).\n\n"
            "Return a JSON object with a single key 'ids' containing an array of matched element IDs:\n"
            "{\n"
            '  "ids": ["id_1", "id_2"]\n'
            "}"
        )

        user_prompt = (
            f"Task context: {task_description}\n"
            f"Target hint: {target_hint}\n\n"
            f"Candidate document elements:\n{json.dumps(candidates, indent=2)}"
        )

        import logging
        log = logging.getLogger(__name__)
        
        response = llm.complete(LLMRequest(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0,
            max_tokens=4096,
            json_mode=True,
        ))

        parsed = response.json or {}
        ids = parsed.get("ids", [])
        
        log.warning(f"TARGET_HINT: {target_hint}")
        log.warning(f"RAW LLM RESPONSE: {response.text}")
        log.warning(f"PARSED JSON: {parsed}")
        print(f"TARGET_HINT: {target_hint}")
        print(f"RAW LLM RESPONSE: {response.text}")
        print(f"PARSED JSON: {parsed}")
        
        return [str(i) for i in ids if isinstance(i, str)]
