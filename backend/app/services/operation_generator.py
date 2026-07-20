"""Operation Generator — generates structured document operations.

Supports two pipelines:
1. Legacy Pipeline: single-shot operations generation for the entire request.
2. New staged Pipeline: generates operations for a single focused task at a time.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from app.core.config import settings
from app.services.llm_client import LLMClient, LLMRequest
from app.services.operations import validate_operation, needs_image_response
from app.services.outline_builder import OutlineBuilder

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-task JSON schemas for Phase 4
# ---------------------------------------------------------------------------

_TEXT_EDIT_SCHEMA = """Return a JSON array of text_edit operations:
[
  {
    "op_type": "text_edit",
    "target_id": "element_id_to_edit" or ["id1", "id2"],
    "parameters": {
      "new_text": "The complete new rewritten text content. NEVER use HTML tags (e.g. <b>, <span>) or Markdown here. NEVER use this tool to change fonts, colors, alignment, or spacing - use text_format for that!"
    }
  }
]
"""

_TEXT_FORMAT_SCHEMA = """Return a JSON array of text_format operations:
[
  {
    "op_type": "text_format",
    "target_id": "element_id_to_format" or "all" or ["id1", "id2"],
    "parameters": {
      "match_text": "exact substring to format (optional, if omitted formats the entire element)",
      "match_role": "heading" or "bullet_point" or "body" (optional),
      "bold": true/false/null,
      "italic": true/false/null,
      "underline": true/false/null,
      "font_family": "Arial"/"Calibri"/null,
      "font_size_pt": 11.5 or null,
      "color_hex": "0000FF" (MUST be 6-char hex like "FF0000" for red, never use words like "red") or null,
      "highlight_hex": "FFFF00" (6-char hex) or null,
      "alignment": "left"/"center"/"right"/"justify" or null,
      "line_spacing": 1.5 or null,
      "char_spacing": 1.0 or null,
      "space_before_pt": 12.0 or null,
      "space_after_pt": 12.0 or null
    }
  }
]
"""

_TABLE_OP_SCHEMA = """Return a JSON array of table_op operations:
[
  {
    "op_type": "table_op",
    "target_id": "table_element_id_or_null" or ["id1", "id2"],
    "parameters": {
      "action": "create"|"delete"|"add_row"|"remove_row"|"add_col"|"remove_col"|"merge_cells"|"set_cell_bg"|"set_borders"|"alternate_rows"|"populate"|"sort_data"|"apply_theme"|"set_header_format"|"set_cell_alignment"|"set_alignment"|"set_width_pct",
      "action_notes": "set_header_format makes the first row dark blue with white text. set_cell_bg sets the background of a specific row/col.",
      "rows": number_of_rows_or_null,
      "cols": number_of_cols_or_null,
      "header_row": true/false/null,
      "alternate_row_colors": ["FFFFFF", "F0F0F0"] or null,
      "data": [["cell1", "cell2"], ["cell3", "cell4"]] or null,
      "row_index": "0-based index or null",
      "col_index": "0-based index or null",
      "sort_by_column": "Name of the column to sort by (for sort_data)",
      "cell_alignment": "left|center|right|justify (for set_cell_alignment)",
      "alignment": "left|center|right (for set_alignment of the entire table)",
      "width_pct": 1.0,
      "before_id": "id_of_anchor_to_insert_before",
      "after_id": "id_of_anchor_to_insert_after",
      "cell_bg_hex": "HEX" or null,
      "border_color_hex": "HEX" or null,
      "theme_color_hex": "HEX" or null,
      "border_width_pt": number_or_null
    }
  }
]
"""

_IMAGE_OP_SCHEMA = """Return a JSON array of image_op operations:
[
  {
    "op_type": "image_op",
    "target_id": "image_element_id_or_null" or ["id1", "id2"],
    "parameters": {
      "action": "insert"|"replace"|"remove"|"resize"|"reposition"|"rotate"|"rounded_corners"|"shadow",
      "image_path": "path_to_image_or_placeholder_or_null",
      "position": {
        "left_pct": 0.1,
        "top_pct": 0.2,
        "width_pct": 0.4,
        "height_pct": 0.5
      },
      "maintain_aspect_ratio": true/false/null,
      "rotation_degrees": number_or_null,
      "border_color_hex": "HEX" or null,
      "border_width_pt": number_or_null,
      "rounded_corners": true/false/null,
      "shadow": true/false/null
    }
  }
]
"""

_LAYOUT_OP_SCHEMA = """Return a JSON array of layout_op operations.
Each operation has "op_type": "layout_op", "target_id": null, and a "parameters" object.

ACTIONS AND THEIR PARAMETERS:

=== move_block ===
Move a section (heading + content) to a new location. One section relocates; the other stays.
{
  "action": "move_block",
  "start_id": "ID of the first element of the section to MOVE (from Section Range start_id)",
  "end_id": "ID of the last element of the section to MOVE (from Section Range end_id)",
  "before_id": "ID of the element to insert BEFORE (use 'Move anchor' before_id if provided)",
  "after_id": "ID of the element to insert AFTER (use 'Move anchor' after_id if provided)"
}
NOTE: Provide EITHER before_id OR after_id, not both. Use the 'Move anchor' values from the context.

=== swap_sections ===
Exchange two sections — both sections trade positions.
{
  "action": "swap_sections",
  "section_a_start_id": "first element of section A (from Section A range)",
  "section_a_end_id": "last element of section A",
  "section_b_start_id": "first element of section B (from Section B range)",
  "section_b_end_id": "last element of section B"
}

=== insert_page_break ===
Insert a hard page break immediately before a specific element.
{
  "action": "insert_page_break",
  "before_id": "ID of the element to insert the page break before (the section heading ID)"
}
NOTE: Use the heading ID of the section you want to start on a new page as before_id.

=== insert_block ===
Insert new content (paragraphs, bullets, tables, headings) at a specific location.
{
  "action": "insert_block",
  "after_id": "ID of the element to insert AFTER (prefer the Section insertion anchor after_id)",
  "data": [
    {"role": "heading", "text": "Heading text", "heading_level": 2},
    {"role": "body", "text": "Full paragraph prose (2-4 sentences, NOT bullet points)"},
    {"role": "bullet_point", "text": "Single bullet item text"},
    {"role": "table", "headers": ["Col 1", "Col 2"], "rows": [["A", "B"], ["C", "D"]]}
  ]
}

=== set_columns ===
Set a multi-column page layout (e.g., two columns side by side).
{
  "action": "set_columns",
  "num_columns": 2,
  "column_gap_inches": 0.5
}

=== remove_block ===
Delete a range of elements from the document.
{
  "action": "remove_block",
  "start_id": "ID of first element to remove",
  "end_id": "ID of last element to remove"
}

=== duplicate_block ===
Duplicate a range of elements to another location.
{
  "action": "duplicate_block",
  "start_id": "ID of first element to duplicate",
  "end_id": "ID of last element to duplicate",
  "after_id": "ID of element to insert after"
}

=== insert_toc ===
Insert a Table of Contents.
{
  "action": "insert_toc",
  "before_id": "ID of element to insert BEFORE (or null)",
  "after_id": "ID of element to insert AFTER (or null)"
}

CRITICAL RULES:
1. move_block != swap_sections. Use move_block when the user says 'move X above/below Y' (only X relocates).
   Use swap_sections when the user says 'swap X and Y' (both sections change positions).
2. For move_block: set start_id/end_id from the 'Section Range' in context, and before_id/after_id
   from the 'Move anchor' values provided. DO NOT set both before_id and after_id.
3. For insert_page_break: set before_id to the heading element ID of the section that should start
   on the new page. Use the 'Target Element ID(s)' from context if it contains a heading ID.
4. For insert_block: GROUP ALL items into ONE operation. If the user asks for 3 paragraphs,
   emit ONE insert_block with ALL 3 items in data[]. NEVER emit multiple insert_block ops.
5. For insert_toc: When the task asks to add a Table of Contents (TOC), ALWAYS use action 'insert_toc'. NEVER use 'insert_block' or generate paragraph prose for a Table of Contents. The TOC is formatted as a 2-column table (Section | Page) with live Word PAGEREF fields.
6. For set_columns: this applies a document-level column layout — use when user asks for
   'two-column layout', 'multi-column', or 'side-by-side columns'.
7. For TOC dot leaders & formatting: Requests to adjust TOC dot leaders, extend dotted lines to the right, or format TOC entry alignment must NEVER emit 'set_alignment', 'text_format', or generic paragraph formatting operations. TOC tab stops and dot leaders are managed natively in Word via w:tab w:leader='dot' w:val='right'.
8. Output ONLY the raw JSON array — no markdown, no commentary.
"""


_LIST_OP_SCHEMA = """Return a JSON array of list_op operations:
[
  {
    "op_type": "list_op",
    "target_id": "null or 'all' or list of IDs",
    "parameters": {
      "action": "convert_type"|"add_items"|"sort_items"|"set_bullet_char",
      "start_id": "first_item_element_id_or_null",
      "end_id": "last_item_element_id_or_null",
      "after_id": "insert_after_this_element_id_or_null",
      "list_type": "bullet"|"numbered"|"checklist",
      "items": ["list item 1 text", "list item 2 text"],
      "bullet_char": "char_or_null"
    }
  }
]
"""

_FIND_REPLACE_SCHEMA = """Return a JSON array of find_replace operations:
[
  {
    "op_type": "find_replace",
    "target_id": "all",
    "parameters": {
      "find_text": "text_to_find",
      "replace_text": "text_to_replace_with",
      "is_regex": false,
      "match_case": false
    }
  }
]
"""

_THEME_OP_SCHEMA = """Return a JSON array of theme_op operations:
[
  {
    "op_type": "theme_op",
    "target_id": "null or 'all' or list of IDs",
    "parameters": {
      "action": "set_bg_color"|"set_margins"|"add_page_numbers"|"apply_theme_colors",
      "bg_color_hex": "HEX_or_null",
      "margin_inches": number_or_null,
      "accent_colors": ["HEX1", "HEX2"] or null
    }
  }
]
"""

_SLIDE_OP_SCHEMA = """Return a JSON array of slide_op operations (PPTX only):
[
  {
    "op_type": "slide_op",
    "target_id": "slide_id_or_null" or ["id1", "id2"],
    "parameters": {
      "action": "add"|"delete"|"duplicate"|"reorder"|"hide"|"unhide"|"rename_title"|"apply_layout",
      "after_index": 1_based_index_or_null,
      "from_index": 1_based_index_or_null,
      "to_index": 1_based_index_or_null,
      "layout_name": "layout_name_or_null",
      "title": "new_title_or_null"
    }
  }
]
"""

_AI_DESIGN_OP_SCHEMA = """Return a JSON array of ai_design_op operations:
[
  {
    "op_type": "ai_design_op",
    "target_id": null,
    "parameters": {
      "action": "normalize_fonts"|"normalize_spacing"|"improve_hierarchy"|"balance_whitespace"|"remove_overlaps"|"improve_readability",
      "scope": "all_slides"|"slide:1" or null,
      "target_font": "Calibri" or null,
      "base_font_size_pt": 11 or null
    }
  }
]
"""

_SCHEMA_BY_TYPE = {
    "text_edit": _TEXT_EDIT_SCHEMA,
    "text_format": _TEXT_FORMAT_SCHEMA,
    "table_op": _TABLE_OP_SCHEMA,
    "image_op": _IMAGE_OP_SCHEMA,
    "layout_op": _LAYOUT_OP_SCHEMA,
    "list_op": _LIST_OP_SCHEMA,
    "find_replace": _FIND_REPLACE_SCHEMA,
    "theme_op": _THEME_OP_SCHEMA,
    "slide_op": _SLIDE_OP_SCHEMA,
    "ai_design_op": _AI_DESIGN_OP_SCHEMA,
}


# ---------------------------------------------------------------------------
# Legacy prompt (retained for backward compatibility of single-shot operations node)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are a precise document editing operations generator.
You convert document editing instructions into a list of structured operations.

Available Operation Types:
1. text_edit: Rewrite text content of a targeted paragraph. NEVER use this tool to change fonts, colors, or alignment - use text_format for that!
   - target_id: paragraph element ID or list of IDs
   - parameters: { new_text: str }

2. text_format: Apply formatting to a paragraph.
   - target_id: paragraph element ID or list of IDs
   - parameters: { bold: bool, italic: bool, underline: bool, strikethrough: bool, font_family: str, font_size_pt: float, color_hex: str, highlight_hex: str, alignment: 'left'|'center'|'right'|'justify', line_spacing: float, char_spacing: float, space_before_pt: float, space_after_pt: float, match_role: str }

3. table_op: Create, modify or style tables.
   - target_id: table element ID (or null for insert) or list of IDs
   - parameters: { action: 'create'|'delete'|'add_row'|'remove_row'|'add_col'|'remove_col'|'merge_cells'|'set_cell_bg'|'set_borders'|'alternate_rows'|'populate'|'sort_data'|'apply_theme'|'set_header_format'|'set_alignment'|'set_width_pct', alignment: 'left'|'center'|'right', width_pct: float, rows: int, cols: int, before_id: str, after_id: str, alternate_row_colors: list[str], data: list[list[str]], cell_bg_hex: str, border_color_hex: str, theme_color_hex: str, border_width_pt: float }

4. image_op: Insert or style images.
   - target_id: image element ID (or null for insert) or list of IDs
   - parameters: { action: 'insert'|'replace'|'remove'|'resize'|'reposition'|'rounded_corners'|'shadow', image_path: str, position: { left_pct: float, top_pct: float, width_pct: float, height_pct: float } }

5. layout_op: Manipulate document pages / section order.
   - target_id: null or 'all' or list of IDs
   - parameters: { action: 'move_block'|'insert_page_break'|'remove_block'|'duplicate_block'|'insert_block'|'insert_toc', start_id: str, end_id: str, before_id: str, after_id: str, data: list[dict] }
     (data array for insert_block supports {"role": "heading", "text": "..."}, {"role": "body", "text": "..."}, {"role": "table", "headers": [...], "rows": [...]})

6. list_op: List manipulations.
   - target_id: null or 'all' or list of IDs
   - parameters: { action: 'convert_type'|'add_items'|'sort_items'|'set_bullet_char', start_id: str, end_id: str, after_id: str, list_type: 'bullet'|'numbered'|'checklist', items: list[str] }

7. find_replace: Global search and replace.
   - target_id: 'all'
   - parameters: { find_text: str, replace_text: str, is_regex: bool }

8. theme_op: Theme setting operations.
   - target_id: null or 'all' or list of IDs
   - parameters: { action: 'set_bg_color'|'set_margins'|'add_page_numbers'|'apply_theme_colors', bg_color_hex: str, margin_inches: float }

Return ONLY a JSON array of operations.
"""


class OperationGenerator:
    """Generates structured operations from editing tasks."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm

    # ------------------------------------------------------------------
    # Stage-focused generate_for_task (Phase 4)
    # ------------------------------------------------------------------

    def generate_for_task(
        self,
        task: dict,
        resolved_ids: dict,
        element_context: dict[str, dict],
        outline: dict,
        attached_image_path: str | None = None,
        previous_ops: list[dict] | None = None,
        verifier_feedback: str | None = None,
        relevant_blocks: dict | None = None,
    ) -> list[dict]:
        """Generate operations for a SINGLE task in the planner's sequence."""
        task_type = task["task_type"]
        schema = _SCHEMA_BY_TYPE.get(task_type)
        if not schema:
            log.warning("No operational schema found for task type: %s", task_type)
            return []

        # If it's an image insertion task but no image is attached, return needs_image
        if task_type == "image_op" and not attached_image_path:
            desc_lower = task["description"].lower()
            if "insert" in desc_lower or "add" in desc_lower or "replace" in desc_lower:
                return [needs_image_response(f"To satisfy: '{task['description']}', please upload an image.")]

        llm = self._llm or LLMClient()

        # Build task-specific context
        import json
        ids_list = resolved_ids.get("ids", []) if isinstance(resolved_ids, dict) else resolved_ids
        after_anchor_id = resolved_ids.get("after_anchor_id") if isinstance(resolved_ids, dict) else None
        section_range = resolved_ids.get("section_range") if isinstance(resolved_ids, dict) else None
        
        resolved_str = json.dumps(ids_list)
        context_str = json.dumps(element_context, indent=2)
        
        # Include full sections list in outline summary for layout_op so the model 
        # can pick correct section boundaries for swap_sections
        if task_type == "layout_op":
            outline_summary = json.dumps({
                "document_type": outline.get("document_type"),
                "title": outline.get("title"),
                "indices": outline.get("indices"),
                "sections": [
                    {
                        "heading": s.get("heading"),
                        "heading_id": s.get("heading_id"),
                        "section_start_id": s.get("section_start_id"),
                        "section_end_id": s.get("section_end_id"),
                    }
                    for s in outline.get("sections", [])
                    if s.get("heading_id") != "start"
                ],
            }, indent=2)
        else:
            outline_summary = json.dumps({
                "document_type": outline.get("document_type"),
                "title": outline.get("title"),
                "indices": outline.get("indices"),
            }, indent=2)

        repair_str = ""
        if verifier_feedback and previous_ops:
            repair_str = (
                f"\n=== REPAIR FEEDBACK ===\n"
                f"Your previous operations for this task: {json.dumps(previous_ops)}\n"
                f"Verifier Feedback: {verifier_feedback}\n"
                f"Please fix the operations to satisfy this feedback.\n"
            )

        # Build anchor hint for section-end insertions, swaps, and moves
        before_anchor_id = resolved_ids.get("before_anchor_id") if isinstance(resolved_ids, dict) else None
        anchor_hint = ""
        if before_anchor_id:
            anchor_hint = f"\nMove anchor - insert the section BEFORE this element (use as before_id in move_block): {before_anchor_id}\n"
        elif after_anchor_id:
            anchor_hint = f"\nSection insertion anchor (use this as after_id): {after_anchor_id}\n"
        if section_range:
            anchor_hint += f"Section range (the content to move): start_id={section_range.get('start_id')}, end_id={section_range.get('end_id')}\n"
            
        section_a_range = resolved_ids.get("section_a_range") if isinstance(resolved_ids, dict) else None
        section_b_range = resolved_ids.get("section_b_range") if isinstance(resolved_ids, dict) else None
        
        if section_a_range and section_b_range:
            anchor_hint += f"Section A range (for swap): start_id={section_a_range.get('start_id')}, end_id={section_a_range.get('end_id')}\n"
            anchor_hint += f"Section B range (for swap): start_id={section_b_range.get('start_id')}, end_id={section_b_range.get('end_id')}\n"


        system_prompt = (
            f"You are a document operations generator specializing in '{task_type}' changes.\n"
            f"Given a target task, the target element IDs, the content of those elements, "
            f"and the document outline, generate the structured operations required to perform the change.\n\n"
            f"FORMAT SCHEMA FOR '{task_type}':\n{schema}\n"
            "RULES:\n"
            "1. Output ONLY the raw JSON array. Do not include markdown wraps (like ```json) or commentary.\n"
            "2. Use the resolved target IDs exactly as provided. Do NOT invent IDs.\n"
            "3. Make sure all parameters strictly match the schema fields.\n"
            "4. If you receive Repair Feedback, you MUST alter your previous operations to address the error. If the verifier repeatedly complains that a style (like paragraph spacing) was not applied, it may be unsupported by the extraction engine—in that case, do not emit the exact same JSON again; skip it or try an alternative.\n"
            "5. CRITICAL FOR list_op and layout_op: 'target_id' MUST be null! Place the provided Target Element IDs into 'start_id' and 'end_id' instead."
        )

        user_prompt = (
            f"Task: {task['description']}\n"
            f"Target Element ID(s): {resolved_str}\n"
            f"{anchor_hint}"
            f"Element Context: {context_str}\n"
            f"Outline: {outline_summary}\n"
            f"Attached Image: {attached_image_path or 'None'}\n"
        )
        if relevant_blocks:
            user_prompt += f"Relevant Full-Text Blocks (from Knowledge Base):\n{json.dumps(relevant_blocks, indent=2)}\n"
        user_prompt += f"{repair_str}"

        response = llm.complete(LLMRequest(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.0,
            max_tokens=2048,
            json_mode=True,
        ))

        parsed = response.json
        ops_raw = []
        if isinstance(parsed, list):
            ops_raw = parsed
        elif isinstance(parsed, dict):
            ops_raw = parsed.get("operations") or parsed.get("ops") or [parsed]

        # Populate image_path if needed
        for op in ops_raw:
            if isinstance(op, dict) and op.get("op_type") == "image_op" and attached_image_path:
                op.setdefault("parameters", {})
                if not op["parameters"].get("image_path"):
                    op["parameters"]["image_path"] = attached_image_path

        # Validate
        validated = []
        for op in ops_raw:
            try:
                validated.append(validate_operation(op))
            except ValueError as e:
                log.warning("Task op validation failed: %s — %s", op, e)

        return validated

    # ------------------------------------------------------------------
    # Legacy generate (retained for backward compatibility)
    # ------------------------------------------------------------------

    def generate(
        self,
        request: str,
        structure: dict,
        document_type: str,
        chat_history: list[dict],
        intent: dict,
        attached_image_path: str | None = None,
        previous_ops: list[dict] | None = None,
        reviewer_feedback: str | None = None,
        missed_tasks: list[str] | None = None,
    ) -> list[dict]:
        """Legacy single-shot operation generator."""
        if settings.gemini_api_key or settings.openai_api_key:
            try:
                return self._generate_with_llm(
                    request, structure, document_type, chat_history,
                    intent, attached_image_path, previous_ops, reviewer_feedback,
                    missed_tasks,
                )
            except Exception as exc:
                log.exception("LLM legacy operations generation failed: %s", exc)
        
        return self._generate_fallback(request, intent, attached_image_path)

    def _generate_with_llm(
        self,
        request: str,
        structure: dict,
        document_type: str,
        chat_history: list[dict],
        intent: dict,
        attached_image_path: str | None,
        previous_ops: list[dict] | None,
        reviewer_feedback: str | None,
        missed_tasks: list[str] | None = None,
    ) -> list[dict]:
        llm = self._llm or LLMClient()
        sys_prompt = _SYSTEM_PROMPT.replace("{CURRENT_DATE}", datetime.now().strftime("%B %d, %Y"))
        
        outline = OutlineBuilder.build(structure, document_type)
        structure_summary = json.dumps(outline, indent=2)
        
        history_str = ""
        if chat_history:
            history_str = "Recent conversation:\n"
            for msg in chat_history[-4:]:
                role = "User" if msg["role"] == "user" else "Agent"
                history_str += f"{role}: {msg['content'][:200]}\n"
            history_str += "\n"

        image_str = ""
        if attached_image_path:
            image_str = f"Attached image path: {attached_image_path}\n"

        refinement_str = ""
        if reviewer_feedback and previous_ops:
            missed_str = ""
            if missed_tasks:
                missed_str = f"\nMissing tasks: {missed_tasks}\n"
            refinement_str = f"\nPrevious attempt: {json.dumps(previous_ops)}\nFeedback: {reviewer_feedback}\n{missed_str}"

        user_prompt = (
            f"{history_str}"
            f"{image_str}"
            f"Document outline:\n{structure_summary}\n\n"
            f"{refinement_str}"
            f"User instruction: {request}"
        )

        import logging
        log = logging.getLogger(__name__)

        response = llm.complete(LLMRequest(
            system_prompt=sys_prompt,
            user_prompt=user_prompt,
            temperature=0,
            max_tokens=4096,
            json_mode=True,
        ))

        log.warning(f"OPERATION GENERATOR RAW RESPONSE: {response.text}")
        print(f"OPERATION GENERATOR RAW RESPONSE: {response.text}")

        parsed = response.json or {}
        if isinstance(parsed, list):
            ops_raw = parsed
        elif isinstance(parsed, dict):
            ops_raw = parsed.get("operations") or parsed.get("ops") or [parsed]
        else:
            ops_raw = []

        validated = []
        for op in ops_raw:
            try:
                validated.append(validate_operation(op))
            except Exception as e:
                log.warning("Legacy op validation failed: %s", e)

        return validated

    def _generate_fallback(
        self,
        request: str,
        intent: dict,
        attached_image_path: str | None,
    ) -> list[dict]:
        category = intent.get("op_category", "")
        if category == "image_op" and attached_image_path:
            slide = intent.get("slide", 1) or 1
            return [validate_operation({
                "op_type": "image_op",
                "target": {"slide": slide, "shape_index": None},
                "parameters": {
                    "action": "insert",
                    "image_path": attached_image_path,
                    "position": {"left_pct": 0.1, "top_pct": 0.2, "width_pct": 0.4, "height_pct": 0.5},
                    "maintain_aspect_ratio": True,
                },
            })]

        if category == "slide_op":
            lowered = request.lower()
            if "add" in lowered or "new" in lowered:
                return [validate_operation({
                    "op_type": "slide_op",
                    "target": {"slide": intent.get("slide", 1) or 1},
                    "parameters": {"action": "add", "after_index": intent.get("slide", 1) or 1},
                })]
            if "delete" in lowered or "remove" in lowered:
                return [validate_operation({
                    "op_type": "slide_op",
                    "target": {"slide": intent.get("slide", 1) or 1},
                    "parameters": {"action": "delete"},
                })]

        return [validate_operation({
            "op_type": "ai_design_op",
            "target": {},
            "parameters": {
                "action": "improve_readability",
                "scope": "all_slides",
            },
        })]
