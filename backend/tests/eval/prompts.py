"""Prompt list for the evaluation harness.

Each prompt is classified as either:
1. Deterministic: Has a checkable expected state (e.g. table row count, font property, element position).
   Includes a concrete assertion.
2. Subjective: Generative or subjective changes (e.g. "sound more professional").
   Flagged for LLM-judge check instead of a concrete assertion.
"""

from typing import TypedDict, Any, Literal

class EvalPrompt(TypedDict):
    id: str
    prompt: str
    type: Literal["deterministic", "subjective"]
    # Deterministic assertion format: (element_type, property, expected_value)
    # Subjective assertion: LLM Judge criteria string
    assertion: Any

PROMPT_LIST: list[EvalPrompt] = [
    # --- Deterministic Prompts ---
    {
        "id": "format_heading_color",
        "prompt": "Change the color of the Introduction heading to blue.",
        "type": "deterministic",
        "assertion": {
            "element_role": "heading",
            "element_text_match": "Introduction",
            "property": "color_hex",
            "expected_value": "0000FF"
        }
    },
    {
        "id": "table_add_row",
        "prompt": "Add a new row to the end of Table 1.",
        "type": "deterministic",
        "assertion": {
            "element_role": "table",
            "ordinal": 1,
            "property": "row_count_delta",
            "expected_value": 1
        }
    },
    {
        "id": "move_section",
        "prompt": "Move the Conclusion section to be before the Executive Summary.",
        "type": "deterministic",
        "assertion": {
            "element_role": "heading",
            "element_text_match": "Conclusion",
            "property": "is_before_heading",
            "expected_value": "Executive Summary"
        }
    },
    {
        "id": "insert_image_dimensions",
        "prompt": "Insert the attached logo image on the first page and resize it to 2x2 inches.",
        "type": "deterministic",
        "assertion": {
            "element_role": "image",
            "property": "size_inches",
            "expected_value": (2.0, 2.0)
        }
    },
    {
        "id": "change_list_type",
        "prompt": "Convert the bulleted list under 'Next Steps' to a numbered list.",
        "type": "deterministic",
        "assertion": {
            "element_role": "list",
            "parent_heading": "Next Steps",
            "property": "list_type",
            "expected_value": "numbered"
        }
    },
    {
        "id": "add_toc",
        "prompt": "Add a Table of Contents to the beginning of the document.",
        "type": "deterministic",
        "assertion": {
            "element_role": "toc",
            "property": "exists_at_index",
            "expected_value": 0
        }
    },

    # --- Subjective / Generative Prompts ---
    {
        "id": "rewrite_professional",
        "prompt": "Rewrite the executive summary to sound more professional and formal.",
        "type": "subjective",
        "assertion": "Did the output plausibly satisfy the request to make the text sound more professional and formal?"
    },
    {
        "id": "modernize_design",
        "prompt": "Make the document look more modern and visually appealing.",
        "type": "subjective",
        "assertion": "Did the document design receive formatting improvements that make it look more modern?"
    },
    {
        "id": "expand_content",
        "prompt": "Expand the section on 'Market Growth' with more details about Q3 performance.",
        "type": "subjective",
        "assertion": "Did the section on Market Growth expand significantly and plausibly incorporate details about Q3 performance?"
    },
    {
        "id": "summarize_table",
        "prompt": "Add a paragraph after Table 2 summarizing its key findings.",
        "type": "subjective",
        "assertion": "Is there a new paragraph immediately following Table 2 that accurately summarizes the table's data?"
    }
]
