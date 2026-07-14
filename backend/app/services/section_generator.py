"""Section Generator — generates structured content for each planned section.

For each section in the document plan:
  1. Retrieves the relevant KB chunks specified in the plan
  2. Calls LLM to generate grounded, professional content
  3. Returns structured output (paragraphs, bullets, tables) that maps
     directly to python-docx operations in DocumentAssembler

The generator runs sections concurrently to reduce latency.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
from typing import Any

from app.services.llm_client import LLMClient, LLMRequest

log = logging.getLogger(__name__)

_SECTION_SYSTEM_PROMPT = """You are an expert content writer for professional corporate and audit documents.

Your task: Generate the content for a single section of a formal business document.

RULES:
1. GROUNDING: Base your content primarily on the provided KB context. Only use general knowledge to fill gaps or provide context. If KB context is provided, every factual claim should come from it.
2. PROFESSIONAL TONE: Write in formal, concise, third-person language suitable for company audits and reports.
3. STRUCTURE: Return structured JSON — NOT free-form text. This JSON will be used to assemble the actual document.
4. TABLES: When generating tables, include professional caption text (e.g., "Table 1: Revenue Comparison Q2 vs Q3"). Extract precise numbers/data from KB context. Mark estimated data as approximate if not in KB.
5. ACCURACY: Do not invent specific numbers, names, or dates that are not in the KB context. Use "N/A" or "TBD" for missing data.
6. LISTS: Bullet points should be concise (one sentence each), actionable, and specific.
7. NO PLACEHOLDERS: Every text element must contain real content — never "[placeholder]" or "Lorem ipsum".

Return JSON in this exact structure:
{
  "elements": [
    {"type": "paragraph", "text": "Full paragraph text.", "style": "Normal"},
    {"type": "bullet_list", "items": ["Item 1", "Item 2"], "style": "List Bullet"},
    {
      "type": "table_caption",
      "text": "Table N: Descriptive Caption",
      "style": "Caption"
    },
    {
      "type": "table",
      "headers": ["Column 1", "Column 2", "Column 3"],
      "rows": [["val", "val", "val"], ...],
      "style": "Table Grid"
    }
  ]
}
"""


class SectionGenerator:
    """Generates structured content for individual document sections."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm

    def generate_all_sections(
        self,
        document_plan: dict,
        kb_context: list[dict],
        template_analysis: dict,
        max_workers: int = 5,
    ) -> list[dict]:
        """Generate all sections concurrently.

        Returns a list of generated section dicts with 'heading' and 'elements'.
        """
        sections = document_plan.get("sections", [])
        generation_notes = document_plan.get("generation_notes", "")
        doc_title = document_plan.get("document_title", "")
        table_counter = {"n": 1}  # Mutable counter shared across sections

        # Build context objects
        context_objs = []
        for idx, section_plan in enumerate(sections):
            chunk_ids = section_plan.get("kb_chunk_ids", [])
            # chunk_ids can be ints (indices) or strings - normalise
            relevant_chunks = []
            for cid in chunk_ids:
                try:
                    i = int(cid)
                    if 0 <= i < len(kb_context):
                        relevant_chunks.append(kb_context[i])
                except (ValueError, TypeError):
                    pass

            context_objs.append({
                "section_plan": section_plan,
                "relevant_chunks": relevant_chunks,
                "doc_title": doc_title,
                "generation_notes": generation_notes,
                "style_catalog": template_analysis.get("style_catalog", {}),
                "table_style": template_analysis.get("table_style", {}),
                "section_idx": idx,
            })

        results: list[dict | None] = [None] * len(sections)

        def _generate_one(args: tuple[int, dict]) -> tuple[int, dict]:
            idx, ctx = args
            return idx, self._generate_section(ctx, table_counter)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(_generate_one, (i, ctx)): i for i, ctx in enumerate(context_objs)}
            for future in concurrent.futures.as_completed(futures):
                try:
                    idx, result = future.result()
                    results[idx] = result
                except Exception as exc:
                    idx = futures[future]
                    log.error("Section %d generation failed: %s", idx, exc)
                    # Use fallback
                    sp = sections[idx]
                    results[idx] = {
                        "heading": sp.get("heading", "Section"),
                        "heading_level": sp.get("heading_level", 1),
                        "style_name": sp.get("style_name", "Heading 1"),
                        "elements": [
                            {
                                "type": "paragraph",
                                "text": f"[Content for {sp.get('heading', 'this section')} could not be generated. Please review and complete manually.]",
                                "style": "Normal",
                            }
                        ],
                    }

        return [r for r in results if r is not None]

    def _generate_section(self, ctx: dict, table_counter: dict) -> dict:
        """Generate content for a single section."""
        section_plan = ctx["section_plan"]
        content_type = section_plan.get("content_type", "narrative")

        # TOC sections are handled by the assembler
        if content_type == "toc":
            return {
                "heading": section_plan.get("heading", "Table of Contents"),
                "heading_level": section_plan.get("heading_level", 1),
                "style_name": section_plan.get("style_name", "Heading 1"),
                "elements": [{"type": "toc_placeholder"}],
            }

        # Skip sections
        if content_type == "skip":
            return {
                "heading": section_plan.get("heading", ""),
                "heading_level": section_plan.get("heading_level", 1),
                "style_name": section_plan.get("style_name", "Heading 1"),
                "elements": [],
            }

        llm = self._llm or LLMClient()

        # Assign table number atomically
        table_spec = section_plan.get("table_spec")
        current_table_n = None
        if table_spec:
            current_table_n = table_counter["n"]
            table_counter["n"] += 1
            # Update caption with correct number
            caption = table_spec.get("caption", f"Table {current_table_n}")
            if "Table N:" in caption or "Table N " in caption:
                caption = caption.replace("Table N", f"Table {current_table_n}")
            elif not re.match(r"Table \d+", caption):
                caption = f"Table {current_table_n}: {caption}"
            table_spec = {**table_spec, "caption": caption}

        kb_text = self._format_kb_chunks(ctx["relevant_chunks"])

        user_prompt = (
            f"DOCUMENT TITLE: {ctx['doc_title']}\n\n"
            f"SECTION HEADING: {section_plan.get('heading')}\n"
            f"CONTENT TYPE: {content_type}\n"
            f"INSTRUCTIONS: {section_plan.get('instructions', 'Write professional content for this section.')}\n\n"
            f"KB CONTEXT (ground your content in this data):\n{kb_text}\n\n"
        )

        if table_spec:
            user_prompt += (
                f"TABLE SPECIFICATION:\n"
                f"  Caption: {table_spec.get('caption')}\n"
                f"  Columns: {table_spec.get('columns')}\n"
                f"  Notes: {table_spec.get('notes', 'Extract data from KB context')}\n\n"
            )
        if ctx.get("generation_notes"):
            user_prompt += f"OVERALL DOCUMENT NOTES: {ctx['generation_notes']}\n\n"

        user_prompt += "Generate the section content as JSON following the required structure."

        response = llm.complete(LLMRequest(
            system_prompt=_SECTION_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.2,
            max_tokens=3000,
            json_mode=True,
        ))

        elements = []
        parsed = response.json or {}
        if isinstance(parsed, dict) and "elements" in parsed:
            elements = parsed["elements"]
        elif isinstance(parsed, list):
            elements = parsed

        # Validate and clean elements
        elements = self._validate_elements(elements, content_type)

        return {
            "heading": section_plan.get("heading", ""),
            "heading_level": section_plan.get("heading_level", 1),
            "style_name": section_plan.get("style_name", "Heading 1"),
            "elements": elements,
        }

    def _format_kb_chunks(self, chunks: list[dict]) -> str:
        if not chunks:
            return "(No specific KB data — use general knowledge for this section)"
        lines: list[str] = []
        for i, chunk in enumerate(chunks[:8]):
            meta = chunk.get("metadata", {})
            source = meta.get("source", "Unknown")
            page = meta.get("page", "")
            section = meta.get("section", "")
            loc = f"[{source}"
            if page:
                loc += f", p.{page}"
            if section:
                loc += f", §{section}"
            loc += "]"
            lines.append(f"[{i+1}] {loc}\n{chunk.get('text', '')[:700]}")
        return "\n\n".join(lines)

    def _validate_elements(self, elements: list, content_type: str) -> list[dict]:
        """Ensure elements have the required structure."""
        valid = []
        for el in elements:
            if not isinstance(el, dict):
                continue
            el_type = el.get("type", "paragraph")
            if el_type == "paragraph":
                if el.get("text"):
                    valid.append({
                        "type": "paragraph",
                        "text": str(el["text"]),
                        "style": el.get("style", "Normal"),
                    })
            elif el_type == "bullet_list":
                items = el.get("items", [])
                if items and isinstance(items, list):
                    valid.append({
                        "type": "bullet_list",
                        "items": [str(i) for i in items if i],
                        "style": el.get("style", "List Bullet"),
                    })
            elif el_type == "numbered_list":
                items = el.get("items", [])
                if items and isinstance(items, list):
                    valid.append({
                        "type": "numbered_list",
                        "items": [str(i) for i in items if i],
                        "style": el.get("style", "List Number"),
                    })
            elif el_type == "table_caption":
                if el.get("text"):
                    valid.append({
                        "type": "table_caption",
                        "text": str(el["text"]),
                        "style": el.get("style", "Caption"),
                    })
            elif el_type == "table":
                headers = el.get("headers", [])
                rows = el.get("rows", [])
                if headers:
                    valid.append({
                        "type": "table",
                        "headers": [str(h) for h in headers],
                        "rows": [[str(c) for c in row] for row in rows if row],
                        "style": el.get("style", "Table Grid"),
                    })
            elif el_type == "toc_placeholder":
                valid.append({"type": "toc_placeholder"})
        return valid


import re  # noqa: E402
