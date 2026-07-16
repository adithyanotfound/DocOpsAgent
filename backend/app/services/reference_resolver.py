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

    def resolve(self, target_hint: str, outline: dict, task_description: str = "") -> dict:
        """Resolve a target hint string to a dictionary with matching element IDs and optional section range."""
        if not target_hint:
            return {"ids": []}

        hint_lower = target_hint.lower().strip()

        # Pass 1: Deterministic matching
        resolved = self._structural_resolve(hint_lower, outline)
        if resolved and resolved.get("ids"):
            log.info("Pass 1: Deterministic resolve matched target '%s' to: %s", target_hint, resolved)
            return resolved

        # Pass 2: Semantic fallback (LLM-assisted)
        resolved_ids = self._semantic_resolve(target_hint, outline, task_description)
        log.info("Pass 2: Semantic fallback resolved target '%s' to: %s", target_hint, resolved_ids)
        return {"ids": resolved_ids}

    def _section_last_content_id(self, section: dict) -> str | None:
        """Return the ID of the last non-heading element in a section.

        This is used as `after_anchor_id` so that insertions go at the end of the
        section's existing content, not immediately after the heading.
        """
        elements = section.get("elements", [])
        # Walk in reverse — skip the heading itself (first element = the heading)
        for el in reversed(elements):
            if el.get("role") != "heading" and el.get("id"):
                return el["id"]
        # All content is just the heading, fall back to section_end_id
        return section.get("section_end_id") or section.get("heading_id")

    def _find_move_sections(self, hint: str, outline: dict) -> dict | None:
        """Detect 'X above Y', 'X below Y', 'X before Y', 'X after Y' move patterns.

        Returns a dict with:
          - section_to_move: section dict
          - anchor_section: section dict
          - direction: 'before' | 'after'
        Or None if not matched.
        """
        sections = outline.get("sections", [])

        def _match_section(query: str) -> dict | None:
            query = query.strip()
            query_normalized = re.sub(r'^\d+\.\s*', '', query).strip()
            query_no_section = query_normalized.replace("the ", "").replace(" section", "").strip()
            for s in sections:
                if s.get("heading_id") == "start":
                    continue
                name = s.get("heading", "").lower()
                name_normalized = re.sub(r'^\d+\.\s*', '', name).strip()
                if (query == name or query_normalized == name_normalized or
                        query_no_section == name_normalized or
                        query_normalized in name_normalized or
                        name_normalized in query_normalized):
                    return s
            return None

        for pattern, direction in [
            (r'^(.+?)\s+(?:above|before)\s+(.+)$', 'before'),
            (r'^(.+?)\s+(?:below|after)\s+(.+)$', 'after'),
        ]:
            m = re.match(pattern, hint.strip())
            if m:
                a_query = m.group(1).strip()
                b_query = m.group(2).strip()
                # Strip leading verbs from section A
                for verb in ("move ", "place ", "put ", "swap ", "exchange "):
                    if a_query.startswith(verb):
                        a_query = a_query[len(verb):]
                # Strip trailing qualifiers
                a_query = a_query.replace(" sections", "").replace(" section", "").strip()
                b_query = b_query.replace(" sections", "").replace(" section", "").strip()

                sec_a = _match_section(a_query)
                sec_b = _match_section(b_query)
                if sec_a and sec_b and sec_a != sec_b:
                    return {
                        "section_to_move": sec_a,
                        "anchor_section": sec_b,
                        "direction": direction,
                    }

        return None

    def _find_two_sections(self, hint: str, outline: dict) -> list[dict] | None:
        """Try to identify exactly two sections from a hint containing 'and'.

        Returns a list of two section dicts, or None if fewer than 2 were found.
        """
        sections = outline.get("sections", [])
        
        def _match_section(query: str) -> dict | None:
            query = query.strip()
            query_normalized = re.sub(r'^\d+\.\s*', '', query).strip()
            query_no_section = query_normalized.replace("the ", "").replace(" section", "").strip()
            for s in sections:
                if s.get("heading_id") == "start":
                    continue
                name = s.get("heading", "").lower()
                name_normalized = re.sub(r'^\d+\.\s*', '', name).strip()
                if (query == name or query_normalized == name_normalized or
                        query_no_section == name_normalized or
                        query_normalized in name_normalized or
                        name_normalized in query_normalized):
                    return s
            return None
        
        # Split on " and " — first try splitting evenly
        parts = hint.split(" and ")
        if len(parts) < 2:
            return None
        
        # Handle "swap A and B" — strip leading verbs
        clean_parts = []
        for p in parts:
            p = p.strip()
            for verb in ("swap ", "exchange ", "switch ", "move "):
                if p.startswith(verb):
                    p = p[len(verb):]
            # Strip trailing qualifiers
            p = p.replace(" sections", "").replace(" section", "").strip()
            clean_parts.append(p)
        
        matched = []
        for part in clean_parts:
            s = _match_section(part)
            if s and s not in matched:
                matched.append(s)
        
        return matched if len(matched) == 2 else None

    def _structural_resolve(self, hint: str, outline: dict) -> dict | None:
        indices = outline.get("indices", {})
        sections = outline.get("sections", [])

        # Detect move intent: "X above Y", "X below Y", "X before Y", "X after Y"
        # Must check BEFORE the swap check because "X above Y" doesn't contain " and "
        if any(kw in hint for kw in (" above ", " below ")):
            move_result = self._find_move_sections(hint, outline)
            if move_result:
                s_move = move_result["section_to_move"]
                s_anchor = move_result["anchor_section"]
                direction = move_result["direction"]
                result: dict = {
                    "ids": [s_move["heading_id"]],
                    "section_range": {
                        "start_id": s_move.get("section_start_id"),
                        "end_id": s_move.get("section_end_id"),
                    },
                }
                if direction == "before":
                    result["before_anchor_id"] = s_anchor.get("section_start_id")
                else:
                    result["after_anchor_id"] = s_anchor.get("section_end_id")
                return result

        # Detect swap intent: "action items and key metrics sections" or similar
        # Pattern: "[Name A] and [Name B] sections" or "swap [Name A] and [Name B]"
        if " and " in hint and ("section" in hint or "swap" in hint or "exchange" in hint or "switch" in hint):
            matched_sections = self._find_two_sections(hint, outline)
            if matched_sections and len(matched_sections) == 2:
                s_a, s_b = matched_sections
                return {
                    "ids": [s_a["heading_id"], s_b["heading_id"]],
                    "section_a_range": {
                        "start_id": s_a.get("section_start_id"),
                        "end_id": s_a.get("section_end_id"),
                    },
                    "section_b_range": {
                        "start_id": s_b.get("section_start_id"),
                        "end_id": s_b.get("section_end_id"),
                    },
                }

        # Match "table of contents" or "toc"
        if "table of contents" in hint or hint == "toc":
            for s in sections:
                if s.get("semantic_type") == "toc":
                    return {"ids": [s["heading_id"]]}
            # Return first section if it matches TOC heading
            if sections and "contents" in sections[0]["heading"].lower():
                return {"ids": [sections[0]["heading_id"]]}

        # Match ordinals: "table N" or "table number N"
        table_match = re.search(r"\btable\s*(?:number\s*)?(\d+)\b", hint)
        if table_match:
            table_num = table_match.group(1)
            tbl_id = indices.get("tables_by_ordinal", {}).get(table_num)
            if tbl_id:
                return {"ids": [tbl_id]}

        # Match ordinals: "image N" or "image number N" or "figure N" or "figure number N"
        image_match = re.search(r"\b(?:image|figure|photo)\s*(?:number\s*)?(\d+)\b", hint)
        if image_match:
            img_num = image_match.group(1)
            img_id = indices.get("images_by_ordinal", {}).get(img_num)
            if img_id:
                return {"ids": [img_id]}

        # Match "all headings" or "headings"
        if hint in ("all headings", "headings", "section headings", "heading"):
            heading_ids = []
            for s in sections:
                if s.get("heading_id") and s["heading_id"] != "start":
                    heading_ids.append(s["heading_id"])
            if heading_ids:
                return {"ids": heading_ids}

        # Match "all tables" or "tables"
        if hint in ("all tables", "tables", "both tables"):
            return {"ids": list(indices.get("tables_by_ordinal", {}).values())}

        # Match "all images" or "images" or "logos"
        if hint in ("all images", "images", "both images", "logos", "logo"):
            return {"ids": list(indices.get("images_by_ordinal", {}).values())}

        # Match "last paragraph" or "last element"
        if hint in ("last paragraph", "last element", "end of document", "the end", "the end of the document"):
            last_id = indices.get("last_element_id")
            if last_id:
                return {"ids": [last_id]}

        # Match "first paragraph" or "first element"
        if hint in ("first paragraph", "first element", "beginning of document", "the beginning", "the beginning of the document", "start of document"):
            first_id = indices.get("first_content_id")
            if first_id:
                return {"ids": [first_id]}

        # Try heading name matching with numbered prefix stripping
        # e.g. hint "executive summary" matches stored name "1. executive summary"
        headings_by_name = indices.get("headings_by_name", {})

        matched_h_id = None

        # Normalize hint: strip leading number prefix like "1." or "2."
        hint_normalized = re.sub(r'^\d+\.\s*', '', hint).strip()
        hint_no_section = hint_normalized.replace("the ", "").replace(" section", "").strip()

        # First: exact match (covers plain and numbered-stripped cases)
        if hint in headings_by_name:
            matched_h_id = headings_by_name[hint]
        elif hint_normalized in headings_by_name:
            matched_h_id = headings_by_name[hint_normalized]
        else:
            # Substring match
            for name, h_id in headings_by_name.items():
                name_normalized = re.sub(r'^\d+\.\s*', '', name).strip()
                
                is_match = (
                    hint == name or
                    hint_normalized == name_normalized or
                    hint_normalized in name_normalized or
                    name_normalized in hint_normalized or
                    hint_no_section == name_normalized or
                    hint_no_section in name_normalized
                )
                
                if is_match:
                    matched_h_id = h_id
                    break

        if matched_h_id:
            # Determine intent: section-level or just the heading element?
            # Keywords that indicate section-level intent
            is_section_intent = (
                "section" in hint or
                "after" in hint or
                "below" in hint or
                "end of" in hint or
                "bottom of" in hint or
                "inside" in hint
            )

            # Find the matching section object to get boundary IDs
            matched_section = None
            for s in outline.get("sections", []):
                if s.get("heading_id") == matched_h_id:
                    matched_section = s
                    break

            if is_section_intent and matched_section:
                section_ids = [matched_h_id]
                section_range = {
                    "start_id": matched_section.get("section_start_id"),
                    "end_id": matched_section.get("section_end_id")
                }
                for el in matched_section.get("elements", []):
                    if el.get("id") and el["id"] != matched_h_id:
                        section_ids.append(el["id"])

                # after_anchor_id = last CONTENT element in section (not the heading)
                after_anchor_id = self._section_last_content_id(matched_section)

                result = {"ids": section_ids, "section_range": section_range}
                if after_anchor_id:
                    result["after_anchor_id"] = after_anchor_id
                return result

            # Heading-only intent (e.g. editing heading text/format)
            return {"ids": [matched_h_id]}

        # Match "slide N" or "slide number N" (for PPTX)
        slide_match = re.search(r"\bslide\s*(?:number\s*)?(\d+)\b", hint)
        if slide_match:
            slide_num = slide_match.group(1)
            slide_id = indices.get("slides_by_index", {}).get(slide_num)
            if slide_id:
                return {"ids": [slide_id]}

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
