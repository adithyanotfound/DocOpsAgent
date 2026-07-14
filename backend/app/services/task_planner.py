"""Task Planner — decomposes user requests into ordered lists of atomic tasks.

Replaces the regex-based intent classification and routes any request
to a sequence of structured editing/layout/formatting steps.
"""
from __future__ import annotations

import logging
from typing import Any

from app.services.llm_client import LLMClient, LLMRequest

log = logging.getLogger(__name__)


PLANNER_SYSTEM_PROMPT = """You are a document editing task planner.
Given a user request, chat history, and a document outline, decompose the request into
an ordered list of atomic tasks. Each task represents one discrete change.

Available task types:
- text_edit: Rewrite text content of a specific element (editing text, sentences, paragraphs). NEVER use this for formatting (fonts, colors, alignment, spacing) - use text_format instead!
- text_format: Change formatting (font family/name, font style, bold, color, size, alignment, margins, spacing, bullets, etc.)
- table_op: Create, modify, or delete tables, columns, rows, or cell contents/styling
- image_op: Insert, replace, resize, style, reposition, or remove images
- layout_op: Move sections, insert page breaks, add/remove sections, add/remove Table of Contents (TOC)
- list_op: Convert list formats (bullets, numbered, checklist), add list items, sort lists
- find_replace: Global text find and replace across the document
- theme_op: Slide/page background, margin settings, corporate themes, color palettes
- meta_op: Modify document metadata (Title, Author, Subject, Keywords)
- section_op: Modify document section properties (Margins, orientation, page size)
- style_op: Modify built-in global DOCX styles (Heading 1, Normal, etc.)
- slide_op: Add, delete, duplicate, hide, or reorder slides (for presentations only)
- generate: Create full slide presentation content from scratch (for presentations only)
- docx_generate: Generate a comprehensive, full document from scratch using the DOCX template structure and the workspace knowledge base. Use this ONLY for DOCX documents when the user asks to "create", "generate", "write", "draft", "produce", or "build" a full document or major new sections (e.g., "create a financial report", "generate meeting minutes", "write a Q3 summary report"). Do NOT use for targeted edits to existing content.

For each task, provide:
- task_type: one of the types above
- description: what to do, in plain language (e.g., "Change heading font color to dark green")
- target_hint: which element(s) to target, using names, ordinals, or roles from the outline
  (e.g., "Table 3", "the Conclusion section", "all headings", "paragraph 5", "the bulleted list")
- dependencies: list of 0-based task indices this task depends on (usually empty, unless task B must run after task A, e.g. add section then add content to it)

CRITICAL RULES:
1. Decompose EVERY distinct action in the request. If the user says "add page break before Action Items, change all headings to green, and move Table 1 to the end", you MUST output 3 separate tasks.
2. Group repetitive actions: If adding multiple items to the same list, or applying the same format to a group of elements, combine them into a SINGLE task (e.g., "Add 3 new bullet points to the Highlights list"). Do NOT split them into one task per item.
3. Ordering matters: tasks must be ordered logically so they can be executed sequentially.
4. Be precise with target_hint so the resolver can map them accurately. Use ordinal indicators from the outline (like "Table 1", "Section 2") if present. If the request applies to all instances of a type (e.g., "all headings", "all tables", "all images"), use exactly that phrase for target_hint (e.g., "all headings"). Do NOT use "entire document" for these.
5. DO NOT create image_op tasks (like adding or replacing images/logos) unless the user EXPLICITLY asks you to add, replace, or modify an image. Do not invent image tasks to "improve" the document.
6. FONT & STYLING INTENT: If the user says "change font to X", "make text Arial", or "use Helvetica", this is ALWAYS a `text_format` task (changing the font family). Do NOT use `text_edit` for these requests!
7. DOCX GENERATION INTENT: If the user asks to "create", "generate", "write", "draft", or "produce" a complete document, report, or major new content (e.g., "create a Q3 financial report", "generate minutes of meeting", "write a compliance report"), and the document type is DOCX, use a single `docx_generate` task. The target_hint should describe the type of document to generate.
8. KNOW YOUR LIMITATIONS: The document engine natively supports:
   - Text: bold, italic, underline, strikethrough, highlight_color, font name, font size (pt), and font color (RGB hex).
   - Paragraphs: left/center/right/justify align, space before/after (pt), line spacing (e.g., 1.5), page breaks, left/right/first-line indents (pt), and keep with next / keep together.
   - Tables: modify columns/rows, cell text formatting, cell backgrounds, cell vertical alignments, column widths, row alternate colors, header formatting, and borders.
   - Layout & Sections: page orientation (landscape/portrait), margins, and exact page dimensions.
   - Global Styles: modify built-in DOCX styles globally.
   - Metadata: modify document properties (Title, Author, Subject, Keywords).
   - Headers/Footers: edit contents within headers and footers just like normal body text.
9. DO NOT invent unsupported tasks (e.g., floating images, rounded corners, drop shadows). For aesthetic requests (e.g., "make it modern"), creatively combine the SUPPORTED properties (like changing heading fonts to sans-serif, using elegant dark gray colors, adjusting page layout, and adding paragraph spacing).

Return ONLY a JSON object:
{
  "tasks": [
    {
      "task_type": "...",
      "description": "...",
      "target_hint": "...",
      "dependencies": []
    }
  ]
}
"""


class TaskPlanner:
    """Decomposes complex requests into atomic task lists."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm

    def plan(
        self,
        request: str,
        outline: dict,
        chat_history: list[dict] = None,
        analysis: dict = None,
        relevant_blocks: dict = None
    ) -> list[dict]:
        """Generate a task list from a user request and outline."""
        llm = self._llm or LLMClient()

        history_str = ""
        if chat_history:
            history_str = "Recent conversation history:\n"
            for msg in chat_history[-5:]:
                role = "User" if msg["role"] == "user" else "Agent"
                history_str += f"{role}: {msg['content']}\n"
            history_str += "\n"

        import json
        outline_summary = json.dumps({
            "document_type": outline.get("document_type"),
            "title": outline.get("title"),
            "element_count": outline.get("element_count"),
            "sections": [
                {
                    "heading": s.get("heading"),
                    "heading_id": s.get("heading_id"),
                    "semantic_type": s.get("semantic_type"),
                    "ordinal": s.get("ordinal"),
                    "elements": [
                        {
                            "type": el.get("type"),
                            "role": el.get("role"),
                            "id": el.get("id"),
                            "ordinal_label": el.get("ordinal_label"),
                            "text_preview": el.get("text_preview"),
                        } for el in s.get("elements", [])
                    ]
                } for s in outline.get("sections", [])
            ]
        }, indent=2)

        context_str = ""
        if analysis:
            context_str += f"Global Document Analysis:\n{json.dumps(analysis, indent=2)}\n\n"
        if relevant_blocks:
            context_str += f"Relevant Full-Text Blocks (from Semantic Search):\n{json.dumps(relevant_blocks, indent=2)}\n\n"

        user_prompt = (
            f"{history_str}"
            f"Document Outline (Truncated Preview):\n{outline_summary}\n\n"
            f"{context_str}"
            f"User Request: {request}"
        )

        response = llm.complete(LLMRequest(
            system_prompt=PLANNER_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.0,
            max_tokens=1024,
            json_mode=True,
        ))

        parsed = response.json or {}
        tasks = parsed.get("tasks", [])
        
        # Simple validation
        validated = []
        for task in tasks:
            if isinstance(task, dict) and task.get("task_type") and task.get("description"):
                validated.append({
                    "task_type": task["task_type"],
                    "description": task["description"],
                    "target_hint": task.get("target_hint") or "all",
                    "dependencies": [d for d in task.get("dependencies", []) if isinstance(d, int)],
                })
        return validated
