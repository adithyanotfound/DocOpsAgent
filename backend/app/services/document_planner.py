"""Document Planner — plans the full document structure before generation.

Given:
  - User's generation request
  - Template analysis (section skeleton, styles)
  - KB context chunks (retrieved from the knowledge base)

The planner produces a section-by-section plan specifying:
  - Which headings to use (template-derived by default)
  - Content type per section (narrative, bullet list, table, toc, skip)
  - What KB chunks are relevant to each section
  - Table specifications (columns, auto-numbered caption)
  - Whether to include a table of contents
  - Any cross-references or special elements

Rules:
  1. Template headings are STRICTLY preserved by default.
  2. The LLM may add sub-sections or add a TOC/Conclusion only if
     the user explicitly requests it.
  3. If no KB data is relevant to a section, LLM uses general knowledge.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.services.llm_client import LLMClient, LLMRequest

log = logging.getLogger(__name__)

_PLANNER_SYSTEM_PROMPT = """You are an expert document planning assistant for professional, audit-grade business documents.

Your job: Given a user's request, the document template's section structure, and retrieved knowledge base context, produce a comprehensive section-by-section document plan.

CRITICAL RULES:
1. TEMPLATE HEADINGS: You MUST preserve ALL headings defined in the template. Do not rename, skip, or reorder template headings unless the user explicitly says to.
2. NEW SECTIONS: You may ADD new sections (e.g., Table of Contents, Conclusion, additional sub-sections) ONLY when the user explicitly requests them.
3. GROUNDING: For each section, identify which KB chunks are most relevant. If no KB data is available for a section, use "kb_chunk_ids": [] and rely on general knowledge.
4. TABLE NUMBERING: If a section contains a table, assign it a sequential number (Table 1, Table 2, ...) with a descriptive caption.
5. PROFESSIONAL FORMAT: Plan content that looks like a professional internal document: concise narrative paragraphs, well-structured tables with headers, numbered bullet points for key findings.
6. TOC: Include a "toc" section at the very beginning ONLY if the user requests it or the template already has one.

Return ONLY valid JSON in this exact structure:
{
  "document_title": "...",
  "sections": [
    {
      "heading": "Section heading text (from template)",
      "heading_level": 1,
      "style_name": "Heading 1",
      "content_type": "narrative" | "bullet_list" | "table" | "mixed" | "toc" | "skip",
      "instructions": "What content to write here (specific, grounded in the data available)",
      "kb_chunk_ids": ["chunk_id_1", "chunk_id_2"],
      "table_spec": null | {
        "caption": "Table N: Descriptive title",
        "columns": ["Col1", "Col2", "Col3"],
        "data_source": "kb",
        "notes": "what data to extract from KB for table rows"
      },
      "bullet_count": null | 5,
      "subsections": []
    }
  ],
  "generation_notes": "Any high-level notes for the generator about tone, focus, or constraints"
}
"""


class DocumentPlanner:
    """Plans the full document structure for generation."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm

    def plan(
        self,
        user_request: str,
        template_analysis: dict,
        kb_context: list[dict],
        chat_history: list[dict] | None = None,
    ) -> dict:
        """Produce a comprehensive document plan.

        Args:
            user_request: What the user wants to generate.
            template_analysis: Output of TemplateAnalyzer.analyze().
            kb_context: Retrieved KB chunks (may be empty if no KB).
            chat_history: Recent conversation for context.
        """
        llm = self._llm or LLMClient()

        # Build context strings
        sections_summary = self._format_template_sections(template_analysis)
        kb_summary = self._format_kb_context(kb_context)
        history_str = self._format_history(chat_history)

        user_prompt = (
            f"{history_str}"
            f"USER REQUEST:\n{user_request}\n\n"
            f"TEMPLATE SECTION STRUCTURE (these headings MUST be preserved):\n{sections_summary}\n\n"
            f"KNOWLEDGE BASE CONTEXT (use this data to ground content):\n{kb_summary}\n\n"
            "Now produce the complete document plan following all rules."
        )

        response = llm.complete(LLMRequest(
            system_prompt=_PLANNER_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.1,
            max_tokens=4096,
            json_mode=True,
        ))

        plan = response.json or {}
        if not plan.get("sections"):
            # Fallback: generate a minimal plan from template sections
            plan = self._fallback_plan(user_request, template_analysis)

        # Enrich plan with KB chunk references
        plan = self._enrich_with_kb_refs(plan, kb_context)

        return plan

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _format_template_sections(self, analysis: dict) -> str:
        sections = analysis.get("sections", [])
        if not sections:
            return "(No heading structure detected — generate logical sections based on the request)"

        lines: list[str] = []
        for s in sections:
            indent = "  " * (s.get("heading_level", 1) - 1)
            lines.append(f"{indent}- [{s['style_name']}] {s['heading_text']}")
            for sub in s.get("subsections", []):
                sub_indent = "  " * (sub.get("heading_level", 2) - 1)
                lines.append(f"{sub_indent}  - [{sub['style_name']}] {sub['heading_text']}")

        return "\n".join(lines)

    def _format_kb_context(self, chunks: list[dict]) -> str:
        if not chunks:
            return "(No knowledge base documents uploaded — use general knowledge)"

        lines: list[str] = []
        for i, chunk in enumerate(chunks[:20]):  # Limit to top 20 chunks in prompt
            meta = chunk.get("metadata", {})
            source = meta.get("source", "Unknown")
            page = meta.get("page", "")
            section = meta.get("section", "")
            loc = f"[Source: {source}"
            if page:
                loc += f", Page {page}"
            if section:
                loc += f", Section: {section}"
            loc += "]"
            lines.append(f"Chunk {i} {loc}:\n{chunk.get('text', '')[:500]}")

        return "\n\n".join(lines)

    def _format_history(self, history: list[dict] | None) -> str:
        if not history:
            return ""
        lines: list[str] = ["Recent conversation:\n"]
        for msg in history[-3:]:
            role = "User" if msg["role"] == "user" else "Agent"
            lines.append(f"{role}: {msg['content'][:200]}")
        return "\n".join(lines) + "\n\n"

    def _enrich_with_kb_refs(self, plan: dict, kb_context: list[dict]) -> dict:
        """Tag each section with relevant KB chunk indices based on keyword overlap."""
        if not kb_context:
            return plan

        for section in plan.get("sections", []):
            if section.get("kb_chunk_ids"):
                continue  # LLM already assigned
            # Simple keyword match to assign chunks
            heading = section.get("heading", "").lower()
            instructions = section.get("instructions", "").lower()
            relevant: list[str] = []
            for i, chunk in enumerate(kb_context):
                text = chunk.get("text", "").lower()
                # Score based on heading and instruction words
                words = set(re.findall(r"[a-z]+", heading + " " + instructions))
                score = sum(1 for w in words if len(w) > 3 and w in text)
                if score >= 2:
                    relevant.append(str(i))
            section["kb_chunk_ids"] = relevant[:5]  # Top 5 relevant chunks

        return plan

    def _fallback_plan(self, request: str, analysis: dict) -> dict:
        """Minimal fallback plan when LLM fails."""
        sections_raw = analysis.get("sections", [])
        sections = []
        for s in sections_raw:
            sections.append({
                "heading": s.get("heading_text", "Section"),
                "heading_level": s.get("heading_level", 1),
                "style_name": s.get("style_name", "Heading 1"),
                "content_type": "narrative",
                "instructions": f"Write professional content for: {s.get('heading_text', 'this section')}",
                "kb_chunk_ids": [],
                "table_spec": None,
                "subsections": [],
            })
        if not sections:
            sections = [{
                "heading": "Content",
                "heading_level": 1,
                "style_name": "Heading 1",
                "content_type": "narrative",
                "instructions": request,
                "kb_chunk_ids": [],
                "table_spec": None,
                "subsections": [],
            }]
        return {"document_title": request[:80], "sections": sections, "generation_notes": ""}


import re  # noqa: E402 — imported here to avoid circular at top level
