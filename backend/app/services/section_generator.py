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
import re
from typing import Any

from app.services.llm_client import LLMClient, LLMRequest

log = logging.getLogger(__name__)

_SECTION_SYSTEM_PROMPT = """You are an expert content writer for professional corporate and audit documents.

Your task: Generate the content for a single section of a formal business document.

RULES:
1. STRICT GROUNDING: Every factual claim (numbers, percentages, currency values, dates, names, statistics) MUST come from the provided KB context.
   - If data for a topic is NOT in the KB context, simply SKIP that topic entirely. Write about what you DO have evidence for.
   - NEVER write "information not available", "N/A", "data not provided", or any similar disclaimer. Just omit topics without data.
   - You MAY use general knowledge ONLY for: transitions, explanations of concepts, and structural sentences.
   - For tables: only include rows/columns you have actual data for. A smaller table with real data is far better than a large table with gaps.
2. PROFESSIONAL TONE: Formal, concise, third-person language.
3. STRUCTURE: Return structured JSON.
4. INLINE CITATION: Every factual claim from the KB MUST include a citation tag [chunk:N] where N matches the chunk number in the KB CONTEXT above.
   Example: "Revenue grew by 11.2% year-over-year [chunk:3]."
   Claims without [chunk:N] tags must NOT contain specific numbers, dates, or statistics.
5. TABLE CITATION: Each table must include a source annotation as the last element: {"type": "table_source", "chunks": [3, 7]}
   listing which chunks the table data was drawn from.
6. QUANTITATIVE ACCURACY: Numbers must appear verbatim in the cited chunk. Do not round, estimate, or "improve" numbers.
7. LISTS: Concise bullet points, one sentence each.
8. NO PLACEHOLDERS: Never "[placeholder]" or "Lorem ipsum".

Return JSON in this exact structure:
{
  "elements": [
    {"type": "paragraph", "text": "Full paragraph text [chunk:1].", "style": "Normal"},
    {"type": "bullet_list", "items": ["Item 1 [chunk:2]"], "style": "List Bullet"},
    {
      "type": "table_caption",
      "text": "Table N: Descriptive Caption",
      "style": "Caption"
    },
    {
      "type": "table",
      "headers": ["Column 1", "Column 2"],
      "rows": [["val", "val"], ...],
      "style": "Table Grid"
    },
    {"type": "table_source", "chunks": [1, 2]}
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
        workspace_id: str,
        kb_retrieval: Any = None,
        fact_verifier: Any = None,
        max_workers: int = 5,
    ) -> list[dict]:
        """Generate all sections sequentially.

        Returns a list of generated section dicts with 'heading' and 'elements'.
        """
        sections = document_plan.get("sections", [])
        generation_notes = document_plan.get("generation_notes", "")
        doc_title = document_plan.get("document_title", "")
        table_counter = {"n": 1}

        results: list[dict] = []
        covered_themes: list[str] = []

        for idx, section_plan in enumerate(sections):
            # Per-section retrieval via Qdrant
            section_query = f"{section_plan.get('heading', '')}: {section_plan.get('instructions', '')}"
            used_semantic = True
            
            if kb_retrieval:
                section_chunks, used_semantic = kb_retrieval.retrieve_for_section(
                    workspace_id=workspace_id,
                    section_query=section_query,
                    fallback_chunks=kb_context,
                    limit=15,
                )
            else:
                section_chunks = kb_context[:15]
            
            if not used_semantic:
                log.warning("Section '%s': used keyword fallback (semantic unavailable)", section_plan.get('heading'))

            ctx = {
                "section_plan": section_plan,
                "section_chunks": section_chunks,
                "doc_title": doc_title,
                "generation_notes": generation_notes,
                "style_catalog": template_analysis.get("style_catalog", {}),
                "table_style": template_analysis.get("table_style", {}),
                "section_idx": idx,
            }

            try:
                result = self._generate_section(ctx, table_counter, covered_themes)
                
                # Inline verification
                if fact_verifier and section_chunks:
                    result = self._verify_inline(result, section_chunks, fact_verifier)
                
                results.append(result)
                
                # Extract themes
                theme = self._extract_theme(result)
                covered_themes.append(
                    f"Section '{result.get('heading', '')}': covered {theme}"
                )
            except Exception as exc:
                log.error("Section %d generation failed: %s", idx, exc)
                results.append({
                    "heading": section_plan.get("heading", "Section"),
                    "heading_level": section_plan.get("heading_level", 1),
                    "style_name": section_plan.get("style_name", "Heading 1"),
                    "elements": [
                        {
                            "type": "paragraph",
                            "text": f"[Content for {section_plan.get('heading', 'this section')} could not be generated. Please review and complete manually.]",
                            "style": "Normal",
                        }
                    ],
                })

        return results

    def _verify_inline(self, result: dict, section_chunks: list[dict], verifier) -> dict:
        """Verify a section's elements against the chunks it was generated from."""
        verified = verifier.verify_section(
            section_elements=result.get("elements", []),
            kb_chunks=section_chunks,
            section_heading=result.get("heading", ""),
        )
        return {**result, "elements": verified.verified_elements}

    def _extract_theme(self, result: dict) -> str:
        """Extract topic-level summary of what a section covered (no specific numbers)."""
        themes = []
        for el in result.get("elements", []):
            if el.get("type") == "paragraph":
                text = el.get("text", "")[:80]
                # Strip numbers to keep it theme-level
                text = re.sub(r'[\d,]+\.?\d*[%$₹€£]?', '', text).strip()
                if text:
                    themes.append(text)
            elif el.get("type") == "table":
                themes.append("data table")
        return "; ".join(themes[:3]) if themes else "general narrative"

    def _generate_section(self, ctx: dict, table_counter: dict, covered_themes: list[str]) -> dict:
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

        section_chunks = ctx.get("section_chunks", [])
        kb_text = self._format_kb_chunks(section_chunks)

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
                f"  Notes: {table_spec.get('notes', 'Extract data from KB context')}\n"
                f"  Table Number: Must be exactly Table {current_table_n}\n\n"
            )
        if ctx.get("generation_notes"):
            user_prompt += f"OVERALL DOCUMENT NOTES: {ctx['generation_notes']}\n\n"

        if covered_themes:
            user_prompt += "TOPICS COVERED IN EARLIER SECTIONS (do not reproduce the detailed analysis, but you may reference these topics briefly if relevant to this section):\n"
            for theme in covered_themes:
                user_prompt += f"- {theme}\n"
            user_prompt += "\n"

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
            return "(No KB data is directly relevant to this section. Write a brief, general structural paragraph appropriate for this section heading. Do NOT include any specific numbers, statistics, or claims. Keep it to 2-3 sentences maximum.)"
        lines: list[str] = []
        for i, chunk in enumerate(chunks[:12]):
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
            lines.append(f"[{i+1}] {loc}\n{chunk.get('text', '')[:1500]}")
        return "\n\n".join(lines)

    def _validate_elements(self, elements: list, content_type: str) -> list[dict]:
        """Ensure elements have the required structure and strip raw citation tags."""
        valid = []
        for el in elements:
            if not isinstance(el, dict):
                continue
            el_type = el.get("type", "paragraph")
            if el_type == "paragraph":
                if el.get("text"):
                    clean_text = re.sub(r'\s*\[chunk:\d+\]', '', str(el["text"]))
                    valid.append({
                        "type": "paragraph",
                        "text": clean_text,
                        "style": el.get("style", "Normal"),
                    })
            elif el_type == "bullet_list":
                items = el.get("items", [])
                if items and isinstance(items, list):
                    clean_items = [re.sub(r'\s*\[chunk:\d+\]', '', str(i)) for i in items if i]
                    valid.append({
                        "type": "bullet_list",
                        "items": clean_items,
                        "style": el.get("style", "List Bullet"),
                    })
            elif el_type == "numbered_list":
                items = el.get("items", [])
                if items and isinstance(items, list):
                    clean_items = [re.sub(r'\s*\[chunk:\d+\]', '', str(i)) for i in items if i]
                    valid.append({
                        "type": "numbered_list",
                        "items": clean_items,
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
            elif el_type == "table_source":
                chunks = el.get("chunks", [])
                if chunks:
                    valid.append({
                        "type": "table_source",
                        "chunks": [int(c) for c in chunks],
                    })
            elif el_type == "toc_placeholder":
                valid.append({"type": "toc_placeholder"})
        return valid

