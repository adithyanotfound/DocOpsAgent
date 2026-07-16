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
    category: str
    prompt: str
    type: Literal["deterministic", "subjective"]
    assertion: Any

PROMPT_LIST: list[EvalPrompt] = [
    # Text Editing
    {"id": "text_edit_grammar", "category": "Text Editing", "prompt": "Correct all grammar and spelling mistakes.", "type": "subjective", "assertion": "Did the document have its spelling and grammar mistakes corrected? Check for changes in text that fix obvious typos."},
    {"id": "text_edit_shorten", "category": "Text Editing", "prompt": "Shorten the executive summary to under 80 words.", "type": "subjective", "assertion": "Is the executive summary text significantly shorter and under 80 words?"},

    # Font & Text Formatting
    {"id": "font_headings_blue", "category": "Font & Text Formatting", "prompt": "Change all headings to 18pt dark blue bold.", "type": "subjective", "assertion": "Are all headings 18pt, bold, and colored dark blue?"},
    {"id": "font_body_calibri", "category": "Font & Text Formatting", "prompt": "Make the body text Calibri 11pt.", "type": "subjective", "assertion": "Is the body text font family Calibri and font size 11pt?"},
    {"id": "font_highlight_revenue", "category": "Font & Text Formatting", "prompt": "Highlight all occurrences of 'Revenue' in yellow.", "type": "subjective", "assertion": "Are the occurrences of the word 'Revenue' highlighted in yellow?"},
    {"id": "font_table_header", "category": "Font & Text Formatting", "prompt": "Make every table header bold with white text.", "type": "subjective", "assertion": "Are the text elements inside the first row of all tables bold and white?"},
    {"id": "font_line_spacing", "category": "Font & Text Formatting", "prompt": "Increase line spacing to 1.5 throughout the document.", "type": "subjective", "assertion": "Is the line spacing set to 1.5 across the body paragraphs?"},
    {"id": "font_center_headings", "category": "Font & Text Formatting", "prompt": "Center all section headings.", "type": "subjective", "assertion": "Are all section headings center aligned?"},
    {"id": "font_italic_bullets", "category": "Font & Text Formatting", "prompt": "Make all bullet points italic.", "type": "subjective", "assertion": "Are all list bullet points italicized?"},
    {"id": "font_client_name", "category": "Font & Text Formatting", "prompt": "Change every occurrence of '[Client Name]' to green bold text.", "type": "subjective", "assertion": "Is the text '[Client Name]' styled as green and bold wherever it appears?"},

    # Tables
    {"id": "table_q3_metrics", "category": "Tables", "prompt": "Add a new row for Q3 metrics in the Key Metrics table.", "type": "subjective", "assertion": "Was a new row added to the Key Metrics table containing data for Q3?"},
    {"id": "table_add_target", "category": "Tables", "prompt": "Add a new column called 'Target'.", "type": "subjective", "assertion": "Was a new column named 'Target' added to the tables?"},
    {"id": "table_delete_change", "category": "Tables", "prompt": "Delete the 'Change' column.", "type": "subjective", "assertion": "Was the 'Change' column removed from the tables?"},
    {"id": "table_merge_header", "category": "Tables", "prompt": "Merge the first two header cells.", "type": "subjective", "assertion": "Were the first two cells in the table header merged?"},
    {"id": "table_alternate_colors", "category": "Tables", "prompt": "Alternate row colors between white and light gray.", "type": "subjective", "assertion": "Do the table rows have alternating background colors (white and light gray)?"},
    {"id": "table_sort_revenue", "category": "Tables", "prompt": "Sort the metrics table by Revenue.", "type": "subjective", "assertion": "Are the rows in the metrics table sorted according to the Revenue column values?"},
    {"id": "table_blue_borders", "category": "Tables", "prompt": "Change all table borders to blue.", "type": "subjective", "assertion": "Do the tables have blue borders?"},
    {"id": "table_center_text", "category": "Tables", "prompt": "Center all text inside both tables.", "type": "subjective", "assertion": "Is all text inside the tables center aligned?"},
    {"id": "table_dark_header", "category": "Tables", "prompt": "Make the first row of each table have a dark blue background with white text.", "type": "subjective", "assertion": "Does the first row of each table have a dark blue cell background and white font color?"},

    # Content Updates
    {"id": "content_replace_openai", "category": "Content Updates", "prompt": "Replace '[Client Name]' with OpenAI.", "type": "subjective", "assertion": "Was '[Client Name]' replaced with 'OpenAI' throughout the document?"},
    {"id": "content_replace_team", "category": "Content Updates", "prompt": "Replace '[Team Name]' with AI Research.", "type": "subjective", "assertion": "Was '[Team Name]' replaced with 'AI Research' throughout the document?"},
    {"id": "content_date_today", "category": "Content Updates", "prompt": "Update the report date to today's date.", "type": "subjective", "assertion": "Was the report date placeholder updated to the current date?"},
    {"id": "content_revenue_value", "category": "Content Updates", "prompt": "Change Revenue from $1.4M to $2.1M.", "type": "subjective", "assertion": "Was the Revenue value changed from $1.4M to $2.1M?"},
    {"id": "content_customer_count", "category": "Content Updates", "prompt": "Update the customer count to 12,500.", "type": "subjective", "assertion": "Was the customer count value updated to 12,500?"},
    {"id": "content_action_completed", "category": "Content Updates", "prompt": "Mark every action item as Completed.", "type": "subjective", "assertion": "Are all action items in the Action Items table marked with the status 'Completed'?"},
    {"id": "content_action_add", "category": "Content Updates", "prompt": "Add a new action item for 'Prepare Investor Presentation.'", "type": "subjective", "assertion": "Was a new row added to the Action Items table for 'Prepare Investor Presentation'?"},

    # Layout
    {"id": "layout_move_actions", "category": "Layout", "prompt": "Move the Action Items section above Highlights.", "type": "subjective", "assertion": "Is the Action Items section located before the Highlights section?"},
    {"id": "layout_page_break", "category": "Layout", "prompt": "Insert a page break before the Action Items section.", "type": "subjective", "assertion": "Is there a page break before the Action Items heading?"},
    {"id": "layout_two_column", "category": "Layout", "prompt": "Add a two-column layout for the Highlights section.", "type": "subjective", "assertion": "Is the Highlights section formatted with a two-column layout?"},
    {"id": "layout_increase_margins", "category": "Layout", "prompt": "Increase page margins.", "type": "subjective", "assertion": "Were the page margins increased?"},
    {"id": "layout_page_numbers", "category": "Layout", "prompt": "Add page numbers to the footer.", "type": "subjective", "assertion": "Are page numbers added to the document footer?"},

    # Colors
    {"id": "color_corporate_blue", "category": "Colors", "prompt": "Apply a corporate blue color scheme", "type": "subjective", "assertion": "Does the document use a corporate blue color scheme for headings, tables, or accents?"},
    {"id": "color_headings_green", "category": "Colors", "prompt": "Make all headings dark green.", "type": "subjective", "assertion": "Are all headings colored dark green?"},
    {"id": "color_tables_light_blue", "category": "Colors", "prompt": "Change all tables to a light blue theme.", "type": "subjective", "assertion": "Do the tables have a light blue styling or background?"},
    {"id": "color_title_red", "category": "Colors", "prompt": "Give the title a red font color.", "type": "subjective", "assertion": "Is the main title text colored red?"},
    {"id": "color_bg_gray", "category": "Colors", "prompt": "Change the page background to light gray.", "type": "subjective", "assertion": "Is the document's page background color set to light gray?"},

    # Lists
    {"id": "list_numbered", "category": "Lists", "prompt": "Convert the Highlights bullet list into a numbered list.", "type": "subjective", "assertion": "Is the Highlights list formatted as a numbered list instead of bullet points?"},
    {"id": "list_add_items", "category": "Lists", "prompt": "Add three more business highlights.", "type": "subjective", "assertion": "Were three new items added to the business highlights list?"},
    {"id": "list_sort_alpha", "category": "Lists", "prompt": "Alphabetically sort the bullet points.", "type": "subjective", "assertion": "Are the items in the bulleted list sorted in alphabetical order?"},
    {"id": "list_checklist", "category": "Lists", "prompt": "Convert the list into a checklist.", "type": "subjective", "assertion": "Is the list formatted with checklist (checkbox) bullets?"},

    # Find & Replace
    {"id": "replace_q2_q3", "category": "Find & Replace", "prompt": "Replace every occurrence of 'Q2' with 'Q3'.", "type": "subjective", "assertion": "Was 'Q2' replaced with 'Q3' everywhere in the text?"},
    {"id": "replace_pending", "category": "Find & Replace", "prompt": "Replace 'Pending' with 'Completed'.", "type": "subjective", "assertion": "Was 'Pending' replaced with 'Completed'?"},
    {"id": "replace_revenue", "category": "Find & Replace", "prompt": "Replace every occurrence of 'Revenue' with 'Net Revenue'.", "type": "subjective", "assertion": "Was 'Revenue' replaced with 'Net Revenue' everywhere?"},
    {"id": "replace_dates", "category": "Find & Replace", "prompt": "Replace all dates with the current year.", "type": "subjective", "assertion": "Were all date placeholders replaced with the current year?"},

    # Object Positioning
    {"id": "pos_tables_center", "category": "Object Positioning", "prompt": "Align both tables to the center.", "type": "subjective", "assertion": "Are the tables center-aligned on the page?"},
    {"id": "pos_tables_width", "category": "Object Positioning", "prompt": "Make both tables the same width.", "type": "subjective", "assertion": "Are the tables adjusted to have the same width?"},
    {"id": "pos_center_headings", "category": "Object Positioning", "prompt": "Center every heading.", "type": "subjective", "assertion": "Are all headings center-aligned?"},
    {"id": "pos_date_right", "category": "Object Positioning", "prompt": "Right-align the date.", "type": "subjective", "assertion": "Is the date paragraph right-aligned?"},
    {"id": "pos_title_lower", "category": "Object Positioning", "prompt": "Move the title 20 points lower on the page.", "type": "subjective", "assertion": "Was the title moved down or given 20 points of space before it?"},

    # Document Structure
    {"id": "struct_toc", "category": "Document Structure", "prompt": "Add a Table of Contents at the beginning.", "type": "subjective", "assertion": "Is there a Table of Contents block at the very beginning of the document?"},
    {"id": "struct_risks", "category": "Document Structure", "prompt": "Insert a new section called 'Risks and Challenges.'", "type": "subjective", "assertion": "Was a new heading and section called 'Risks and Challenges' inserted?"},
    {"id": "struct_conclusion", "category": "Document Structure", "prompt": "Add a Conclusion section.", "type": "subjective", "assertion": "Was a new Conclusion section added?"},
    {"id": "struct_duplicate_metrics", "category": "Document Structure", "prompt": "Duplicate the Key Metrics section.", "type": "subjective", "assertion": "Is the Key Metrics section (heading and table) duplicated?"},
    {"id": "struct_remove_playground", "category": "Document Structure", "prompt": "Remove the Formatting Playground section.", "type": "subjective", "assertion": "Is the Formatting Playground section completely removed from the document?"},
]
