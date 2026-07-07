"""Operation Generator — LLM-powered node that converts a user request into
a list of structured document operations.

This replaces `generate_edits` for the 'operations' pipeline branch, which
covers everything except plain text rewrites and full deck generation:

  - Rich text formatting (bold, font, color, alignment, …)
  - Table CRUD
  - Image insertion / replacement / resizing
  - Shape / text-box manipulation
  - Theme, background, color changes
  - Slide-level operations (add, delete, duplicate, reorder)
  - Chart editing
  - AI-driven design normalization

The LLM is given a compact JSON representation of the document structure
and produces an operation list that the DocumentProcessor can execute.
"""
from __future__ import annotations

import json
import logging

from app.core.config import settings
from app.services.operations import needs_image_response, validate_operation

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt building helpers
# ---------------------------------------------------------------------------

_OPERATION_SCHEMA = """
Available operation types and their parameter schemas:

1. text_edit — Rewrite text content (with optional inline formatting for the replaced text)
   target_id: string (ID of the paragraph, text box, or cell from the DOM)
   parameters: {new_text, bold, italic, underline, font_family, font_size_pt, color_hex}

2. text_format — Apply formatting to a paragraph or run
   target_id: string (ID of the paragraph or run from the DOM, or "all" to apply globally across the document)
   parameters: {bold, italic, underline, strikethrough, font_family,
                font_size_pt, color_hex (6-char hex WITHOUT '#', e.g. "FF0000" for red),
                highlight_hex, match_color_hex (6-char hex WITHOUT '#', ONLY apply if existing color matches this approx hex),
                match_text (exact substring to apply formatting to inside the target paragraph),
                match_role (e.g. "heading", "body", "bullet_point" - ONLY used when target_id is "all"),
                alignment ("left"|"center"|"right"|"justify"),
                space_before_pt (float), space_after_pt (float),
                line_spacing (multiplier), char_spacing (pt),
                superscript, subscript, shadow, page_break_before (boolean — DOCX only, makes the paragraph start on a new page),
                include_in_toc (boolean — DOCX only, sets whether a heading appears in the Table of Contents)
                IMPORTANT: alignment, line_spacing, space_before_pt, space_after_pt, page_break_before, and include_in_toc are PARAGRAPH-LEVEL properties. They CANNOT be used with match_text. If you need to right-align a specific line inside a multi-line paragraph (like a Date), you must use text_edit to replace the paragraph text, adding tabs (\t) before the date to push it to the right.}

3. table_op — Table CRUD
   target_id: string (ID of the table from the DOM, "all" for all tables, or null to create new)
   parameters: {
     action: "create"|"delete"|"add_row"|"remove_row"|"add_col"|"remove_col"
             |"merge_cells"|"set_cell_bg"|"set_borders"|"alternate_rows"
             |"populate"|"set_header_format"|"sort_data"|"set_cell_alignment"|"apply_theme"
             |"set_alignment"|"set_width_pct",
     rows, cols, header_row, alternate_row_colors [hex1, hex2], theme_color_hex,
     data [[row1col1, ...], ...] (For add_col/add_row with a title, provide it in data, e.g. ["Q3"]), 
     row_index, col_index (Must provide these for populate to target the correct row/col, e.g., last column),
     merge_from [row,col], merge_to [row,col],
     cell_bg_hex, border_color_hex, border_width_pt,
     position {left_pct, top_pct, width_pct, height_pct},
     cell_padding_pt, cell_alignment, alignment ("left"|"center"|"right" - for set_alignment),
     width_pct (float, e.g. 1.0 for 100% width - for set_width_pct)
   }

4. image_op — Image operations
   target_id: string (ID of the image shape from the DOM, or null for new)
   parameters: {
     action: "insert"|"replace"|"remove"|"resize"|"reposition"|"rotate"
             |"bring_forward"|"send_backward"|"set_transparency"
             |"set_border"|"rounded_corners"|"shadow"
             |"add_caption",    — [DOCX] Insert a caption paragraph below the image
     image_path (provided by system if image attached),

     [PPTX] position {{left_pct, top_pct, width_pct, height_pct}},
     [PPTX] maintain_aspect_ratio, rotation_degrees, transparency_pct (0-100),
     [PPTX] border_color_hex, border_width_pt, rounded_corners, shadow,
     [PPTX] crop {{top_pct, left_pct, right_pct, bottom_pct}}

     [DOCX] after_id: string        — body_index DOM id to insert image AFTER (for action="insert")
     [DOCX] width_page_pct: float   — image width as fraction of usable page width (e.g. 0.4 = 40%)
     [DOCX] alignment: "left"|"center"|"right"  — horizontal alignment of the image paragraph
     [DOCX] float_position: "left"|"right"|"inline"  — floating layout; use "inline" for no float
     [DOCX] caption_text: string    — caption text for add_caption action
     [DOCX] caption_style: string   — Word style name for caption (default: "Caption")
     [DOCX] alt_text: string        — short description/name of the inserted image (e.g. "Company Logo") to retain context
   }

   DOCX examples:
     Insert logo after title (paragraph_0), 30% page wide, centered:
       {{"op_type": "image_op", "target_id": null, "parameters": {{"action": "insert", "image_path": "<from attached>", "after_id": "paragraph_0", "width_page_pct": 0.3, "alignment": "center", "alt_text": "Company Logo"}}}}

     Place an image IN THE SAME LINE as the title (e.g. right next to the centered text):
       {{"op_type": "image_op", "target_id": "paragraph_0", "parameters": {{"action": "insert_into_paragraph", "image_path": "<from attached>", "width_page_pct": 0.2, "alt_text": "Small Logo"}}}}

     Replace text placeholder (paragraph_4) with an image:
       {{"op_type": "image_op", "target_id": "paragraph_4", "parameters": {{"action": "replace_text", "image_path": "<from attached>", "width_page_pct": 0.5, "alt_text": "Sales Chart"}}}}

     Replace image_0 with a new image at 50% page width:
       {{"op_type": "image_op", "target_id": "image_0", "parameters": {{"action": "replace", "image_path": "<from attached>", "width_page_pct": 0.5}}}}

     Resize image_0 to 40% page width (preserve aspect ratio):
       {{"op_type": "image_op", "target_id": "image_0", "parameters": {{"action": "resize", "width_page_pct": 0.4, "maintain_aspect_ratio": true}}}}

     Move image_0 to the right (right-align its paragraph) (USE THIS FOR "TOP RIGHT" OR "BOTTOM RIGHT" REQUESTS):
       {{"op_type": "image_op", "target_id": "image_0", "parameters": {{"action": "reposition", "alignment": "right"}}}}

     Center image_0:
       {{"op_type": "image_op", "target_id": "image_0", "parameters": {{"action": "reposition", "alignment": "center"}}}}

     Add caption below image_0:
       {{"op_type": "image_op", "target_id": "image_0", "parameters": {{"action": "add_caption", "caption_text": "Figure 1: Quarterly Sales"}}}}

5. shape_op — Shape / text box operations
   target_id: string (ID of the shape from the DOM)
   parameters: {
     action: "add_textbox"|"delete"|"resize"|"move"|"rotate"|"duplicate"
             |"set_fill"|"set_outline"|"set_transparency"|"group"|"ungroup"
             |"bring_forward"|"send_backward"|"align"|"distribute",
     text, position {left_pct, top_pct, width_pct, height_pct},
     fill_color_hex, outline_color_hex, outline_width_pt,
     transparency_pct, rotation_degrees, corner_radius_pt,
     group_shape_indices [list of strings for group/ungroup]
   }

6. theme_op — Background, color, margin, and page-number changes
   target_id: null (or "all")
   parameters: {
     action: "set_bg_color"|"set_bg_gradient"|"apply_theme_colors"|"corporate_branding"
             |"set_margins"         — [DOCX] Set all page margins. Required: margin_inches (float, e.g. 1.5)
             |"add_page_numbers",   — [DOCX] Add centered page numbers to the footer of every section
     scope: "all_slides"|"current_slide",
     bg_color_hex, gradient_start_hex, gradient_end_hex,
     gradient_direction ("horizontal"|"vertical"|"diagonal"),
     accent_colors [list of hex strings],
     margin_inches (float)  — used with action="set_margins"
   }

   DOCX examples:
     Increase margins:    {{"op_type": "theme_op", "target_id": null, "parameters": {{"action": "set_margins", "margin_inches": 1.5}}}}
     Add page numbers:    {{"op_type": "theme_op", "target_id": null, "parameters": {{"action": "add_page_numbers"}}}}

7. slide_op — Slide-level operations
   target_id: string (ID of the slide)
   parameters: {
     action: "add"|"delete"|"duplicate"|"reorder"|"hide"|"unhide"|"rename_title",
     after_index (1-based, for add/duplicate),
     from_index (1-based, for reorder), to_index (1-based, for reorder),
     layout_name, title (for rename_title)
   }

8. chart_op — Chart editing
   target_id: string (ID of the chart shape)
   parameters: {
     action: "change_type"|"update_data"|"set_series_colors"|"update_labels"
             |"update_axis_labels"|"show_legend"|"hide_legend"|"apply_theme",
     chart_type ("bar"|"line"|"pie"|"scatter"|"column"),
     series_colors [hex list], data [[...]], legend_position,
     x_axis_label, y_axis_label, data_labels_visible
   }

9. ai_design_op — AI-driven design normalization
   target_id: null
   parameters: {
     action: "normalize_fonts"|"normalize_spacing"|"improve_hierarchy"
             |"balance_whitespace"|"remove_overlaps"|"auto_resize_text"
             |"make_consistent"|"generate_speaker_notes"
             |"improve_readability"|"detect_clutter",
     scope ("all_slides"|"slide_X"), target_font, base_font_size_pt
   }

10. needs_image — Signal that an image must be attached
    Use ONLY when user wants to explicitly insert or replace an image (action="insert", "insert_into_paragraph", "replace", or "replace_text") AND no image_path is provided in the system prompt.
    DO NOT use needs_image for reposition, resize, or add_caption actions—those apply to existing images!
    parameters: {message: "friendly message asking user to attach image"}

11. layout_op — [DOCX ONLY] Structural layout changes: move sections or insert page breaks
    target_id: null
    parameters: {
      action: "move_block"        — Move a contiguous block of paragraphs/tables to a new position
              |"insert_page_break" — Insert a hard page break immediately before a target element
              |"duplicate_block"  — Clone a section (from start_id to end_id) and insert it
              |"remove_block"     — Delete an entire section (from start_id to end_id)
              |"insert_block"     — Create a brand new section from scratch using a data array
              |"insert_toc"       — Insert a native Word Table of Contents field
    }

    For action="move_block", "duplicate_block", "remove_block":
      start_id: string   — DOM id of the FIRST element in the block to move/copy/delete (e.g. "paragraph_5")
      end_id: string     — DOM id of the LAST element in the block. Include ALL paragraphs between the heading and the next heading/section.
      before_id: string  — (move/copy only) DOM id of the element to insert the block BEFORE
      after_id: string   — (move/copy only) OR use this to insert AFTER. To move a block to the VERY END of the document, set `after_id` to the ID of the last element in the document.

    For action="insert_page_break", "insert_toc":
      before_id: string  — DOM id of the element to insert BEFORE
      after_id: string   — OR DOM id of the element to insert AFTER

    For action="insert_block":
      before_id: string  — DOM id of the element to insert BEFORE
      after_id: string   — OR DOM id of the element to insert AFTER
      data: array        — The paragraphs to insert. E.g. [{"role": "heading", "text": "New Title", "heading_level": 1}, {"role": "body", "text": "Some text"}]

    DOCX examples:
      Move "Action Items" section (paragraph_5 through paragraph_9) to appear before "Highlights" section (paragraph_2):
        {{"op_type": "layout_op", "target_id": null, "parameters": {{"action": "move_block", "start_id": "paragraph_5", "end_id": "paragraph_9", "before_id": "paragraph_2"}}}}

      Insert a page break before the "Action Items" heading (paragraph_5):
        {{"op_type": "layout_op", "target_id": null, "parameters": {{"action": "insert_page_break", "before_id": "paragraph_5"}}}}

12. list_op — [DOCX ONLY] List manipulation: convert format, add/sort items, change bullet char
    target_id: null
    parameters: {{
      action: "convert_type"   — Change list format for a range (bullet→numbered, any→checklist, etc.)
              |"add_items"     — Insert new list items after an anchor paragraph
              |"sort_items"    — Alphabetically sort list paragraphs in a range
              |"set_bullet_char" — Change the bullet character for a list range
    }}

    For action="convert_type":
      start_id: string   — DOM id of the FIRST list paragraph to convert
      end_id: string     — DOM id of the LAST list paragraph to convert
      list_type: "bullet" | "numbered" | "checklist"
      bullet_char: string (optional, custom bullet character)

    For action="add_items":
      after_id: string   — DOM id of the list paragraph to insert NEW items AFTER
      end_id: string     — DOM id of the LAST existing item in the list (for style cloning)
      items: ["text1", "text2", ...]  — text for each new list item

    For action="sort_items":
      start_id: string   — DOM id of the first list paragraph to sort
      end_id: string     — DOM id of the last list paragraph to sort
      order: "asc" | "desc"  (default: "asc")

    For action="set_bullet_char":
      start_id: string   — first list paragraph
      end_id: string     — last list paragraph
      char: string       — the bullet character (e.g. "•", "–", "☐")

    DOCX examples:
      Convert bullet list (paragraph_3 to paragraph_6) to numbered:
        {{"op_type": "list_op", "target_id": null, "parameters": {{"action": "convert_type", "start_id": "paragraph_3", "end_id": "paragraph_6", "list_type": "numbered"}}}}

      Add 3 business highlights after the last bullet (paragraph_6):
        {{"op_type": "list_op", "target_id": null, "parameters": {{"action": "add_items", "after_id": "paragraph_6", "end_id": "paragraph_6", "items": ["Global expansion into 5 new markets", "Customer satisfaction score of 94%", "Zero critical security incidents"]}}}}

      Sort list items paragraph_3 through paragraph_8 alphabetically:
        {{"op_type": "list_op", "target_id": null, "parameters": {{"action": "sort_items", "start_id": "paragraph_3", "end_id": "paragraph_8", "order": "asc"}}}}

      Convert list (paragraph_3 to paragraph_8) to a checklist (☐ prefix):
        {{"op_type": "list_op", "target_id": null, "parameters": {{"action": "convert_type", "start_id": "paragraph_3", "end_id": "paragraph_8", "list_type": "checklist"}}}}

13. find_replace — [DOCX ONLY] Global or targeted find and replace
    target_id: string (DOM id of paragraph/table to restrict scope, or "all" for global document replace)
    parameters: {{
      find_text: string (The exact text or regular expression to find)
      replace_text: string (The text to replace it with)
      is_regex: boolean (Optional, default false. Set to true if find_text is a regex pattern)
      match_case: boolean (Optional, default false)
    }}

    DOCX examples:
      Replace "Revenue" with "Net Revenue" everywhere in the document:
        {{"op_type": "find_replace", "target_id": "all", "parameters": {{"find_text": "Revenue", "replace_text": "Net Revenue"}}}}

      Replace all years starting with 202 (e.g. 2024, 2025) with "2026":
        {{"op_type": "find_replace", "target_id": "all", "parameters": {{"find_text": "202[0-9]", "replace_text": "2026", "is_regex": true}}}}
"""


_SYSTEM_PROMPT = f"""You are a precise document editing assistant that converts user instructions
into structured JSON operation lists based on the provided Document Object Model (DOM).

RULES:
1. Return ONLY a valid JSON array of operation objects. No commentary, no markdown fences.
2. Each operation has exactly: "op_type", "target_id", "parameters".
3. Use the stable IDs provided in the DOM to set "target_id".
4. Be surgical — only produce operations that are necessary and will make a VISIBLE, 
   MEANINGFUL improvement. Do not add random changes.
5. If the user asks to insert/replace an image and "image_path" is null, you MUST
   return a single "needs_image" operation asking for attachment.
6. CRITICAL: color_hex must be a plain 6-character hex string WITHOUT the '#' prefix.
   Example: red = "FF0000", blue = "0000FF", green = "008000".
7. CRITICAL: DO NOT invent IDs. You must use the exact `target_id`s provided in the Document structure.
8. CRITICAL: For global formatting targeting specific types of content (e.g. "Make all headings blue", "Center all section headings", "Format body text", "Italicize bullet points"), you MUST ALWAYS use `target_id: "all"` and provide `match_role` in `text_format` using the exact role from the structure hints (e.g. "heading", "body", "bullet_point"). NEVER generate separate operations for each paragraph if the request says "all" or "every"! For global formatting based on an EXISTING color, use `target_id: "all"` and provide `match_color_hex` using the exact hex code from the structure hints. For global table operations (e.g. "Change all tables to blue"), use `table_op` with `target_id: "all"`. This guarantees 100% coverage across the document without hitting operation limits. Example: {{"op_type": "text_format", "target_id": "all", "parameters": {{"italic": true, "match_role": "bullet_point"}}}}
9. Always apply formatting to EVERY matching paragraph — if the user says
   "heading AND subheading", produce one operation per target paragraph (unless using "all").
10. Never produce empty arrays unless the request is genuinely impossible; then produce
    a single ai_design_op with action "detect_clutter" and explain in parameters.
11. If the user asks to format a specific word or phrase (e.g., "Change [Client Name] to green bold"), you MUST produce a `text_format` operation and provide the `match_text` parameter with the exact substring (e.g., "[Client Name]"). Do NOT use `text_edit` for this unless you are actually changing the words themselves. By providing `match_text` to `text_format`, the backend will surgically apply the formatting ONLY to that specific phrase.
12. CRITICAL: If the user provides a multi-step prompt (e.g. do X, Y, and Z), you MUST return an array containing multiple operations that fulfill EVERY part of the request. Do not stop after 1 or 2 operations. You can generate as many operations as needed to fulfill the entire prompt.
13. If the user asks to update a date to today's date, you MUST use the exact CURRENT_DATE provided below. Do not hallucinate random dates. Do NOT output invalid actions or parameters. Stick STRICTLY to the schemas above.
14. When the user asks to modify multiple rows or specific cells in a table, DO NOT use a single table_op with 'set_header_format' unless they only asked for headers. You MUST iterate and generate a 'text_edit' or 'text_format' operation for EACH cell/paragraph you wish to modify, targeting its specific DOM ID.
15. If asked to apply a change globally, find all relevant IDs in the DOM and generate an operation for each, OR use target_id: "all" if the schema supports it.
16. CRITICAL: For text formatting (text_format) across all headings, bodies, or the entire document, you MUST use `target_id: "all"` and `match_role`. DO NOT output a separate text_format operation for each paragraph! This will exceed output token limits.
17. To remove a heading from the Table of Contents or prevent a new heading from appearing in it, you MUST use a `text_format` operation on the heading's target ID and set `include_in_toc: false`. Do NOT pass `include_in_toc` inside the data array of an `insert_block` operation, as it is invalid there.
17. CRITICAL: DO NOT repeat the same operation multiple times. Once you have generated the operations for all steps in the user's prompt, YOU MUST close the JSON array `]` and STOP generating.

DOCX-SPECIFIC RULES (apply when the document type is DOCX):
18. The Document structure shows elements in TRUE DOCUMENT ORDER with a `body_index` field. Use `body_index` to understand which section comes before/after another.
19. To MOVE a section in a DOCX (e.g. "Move Action Items above Highlights"):
    - Use `layout_op` with `action: "move_block"`.
    - `start_id` = the DOM id of the section heading paragraph to move.
    - `end_id` = the DOM id of the LAST paragraph/table that belongs to that section (stop before the next heading).
    - `before_id` = the DOM id of the element you want the block inserted before.
    - Use the `body_index` values in the structure to correctly determine start/end/before IDs.
20. To INSERT A PAGE BREAK before a section in a DOCX:
    - Use `layout_op` with `action: "insert_page_break"` and `before_id` = the DOM id of the heading paragraph.
    - Alternatively, use `text_format` with `page_break_before: true` on the heading paragraph.
    - PREFER `insert_page_break` when the request explicitly says "insert a page break".
    - Use `text_format` with `page_break_before: true` when the request says "start on a new page" or "headings on new pages".
21. To INCREASE PAGE MARGINS in a DOCX: use `theme_op` with `action: "set_margins"` and `margin_inches` (e.g. 1.5 for 1.5-inch margins).
22. To ADD PAGE NUMBERS to a DOCX footer: use `theme_op` with `action: "add_page_numbers"`.
23. For multi-step DOCX structural requests (move + page break + margins + page numbers), produce ALL required operations in one JSON array.

FINAL COMPLETENESS CHECK (MANDATORY — do this before closing the JSON array):
Before you write the closing `]`, mentally review the user's original request and verify:
  - Is EVERY distinct sub-task (reorganize, convert list, format headings, etc.) covered by at least one operation?
  - If you find a sub-task with no operations, ADD the missing operations NOW before closing.
  - Only close the array when every sub-task has been addressed.
  - This check is CRITICAL for multi-step prompts.

DOCX IMAGE RULES (apply when document type is DOCX and request involves images):
24. In DOCX, images are identified by `image_N` DOM IDs in the structure (e.g. 'image_0', 'image_1'). Use these as `target_id` for image operations.
25. To INSERT a new image into a DOCX:
    - Use `image_op` with `action: "insert"`, set `after_id` to the DOM id of the element AFTER which the image should appear.
    - Set `width_page_pct` (e.g. 0.3 = 30% of page width) and `alignment` ("left", "center", or "right").
    - `image_path` MUST be the value provided under "Attached image path" in the prompt. If no image is attached, use `needs_image` instead.
26. To REPLACE an existing image (e.g. replace a placeholder):
    - Use `image_op` with `action: "replace"` and `target_id: "image_0"` (or whichever image the user refers to).
    - Set `width_page_pct` to preserve or change the image size.
27. To RESIZE an image in a DOCX:
    - Use `image_op` with `action: "resize"`, `target_id: "image_N"`, and `width_page_pct` (e.g. 0.4 = 40%).
    - Always include `"maintain_aspect_ratio": true` unless the user explicitly wants distortion.
28. To MOVE/ALIGN an image horizontally in DOCX (left, center, right side of page):
    - Use `image_op` with `action: "reposition"`, `target_id: "image_N"`, and `alignment: "right"` (or "center"/"left").
    - If the user says "float to the right" or "wrap text around image", add `"float_position": "right"`.
29. To ADD A CAPTION below an image in DOCX:
    - Use `image_op` with `action: "add_caption"`, `target_id: "image_N"`, and `caption_text: "Figure 1: ..."` .
    - The caption is automatically styled with the Word 'Caption' style and centered.
30. For multi-step DOCX image requests (insert + resize + reposition + caption), produce ALL operations in one JSON array in logical order: insert first, then resize, then reposition, then caption.

DOCX LIST RULES (apply when document type is DOCX and request involves lists):
31. In DOCX, list paragraphs are identified in the structure with `role='bullet_point'` and include a `list_info` field showing `num_id`, `ilvl`, `list_type` ('bullet'/'numbered'/'checklist'), and `lvl_text`.
32. To CONVERT a bullet/numbered list: use `list_op` with `action: "convert_type"`, `start_id` = first list item DOM id, `end_id` = last list item DOM id, `list_type` = target format.
    - For "numbered list": `list_type: "numbered"`
    - For "checklist" or "checkbox list": `list_type: "checklist"`
    - For "bullet list": `list_type: "bullet"`
33. To ADD NEW ITEMS to a list: use `list_op` with `action: "add_items"`, `after_id` = DOM id of the LAST existing list item, `end_id` = same last item (for style cloning), and `items` = array of text strings for new items.
    - ALWAYS include all new item texts in the `items` array. Do NOT generate separate operations per item.
    - If the user asks to add N items, the `items` array MUST contain exactly N strings.
34. To SORT list items: use `list_op` with `action: "sort_items"`, `start_id` = first item, `end_id` = last item, `order` = "asc" or "desc".
    - After sorting, the physical paragraph IDs change order but the DOM IDs stay the same (they refer to position slots, not text content).
35. To CONVERT A LIST TO CHECKLIST: use `list_op` with `action: "convert_type"` and `list_type: "checklist"`. This sets a ☐ (ballot box) as the bullet character.
36. For multi-step list requests (e.g. convert + add items + sort + convert to checklist), produce ALL operations in one JSON array in LOGICAL ORDER:
    - First: `convert_type` (change the list format)
    - Then: `add_items` (add new items — use the ORIGINAL last item id from the structure, not a new id)
    - Then: `sort_items` (use the ORIGINAL first and last item ids from the structure)
    - Then: another `convert_type` if format changes again (e.g., to checklist)
    - IMPORTANT: All start_id/end_id values in sort_items MUST cover the full list including newly added items. Since add_items inserts AFTER end_id, and sort_items needs to cover all items, for sort_items use the same start_id as the first item and end_id as the original last item (the new items will be inserted after it and also be covered by the sort if they share the same parent region).
    - NOTE: After add_items, the new paragraphs do NOT have stable DOM IDs yet (they were inserted dynamically). Use the sort_items operation on the original range — the backend sorts all list paragraphs found between start_id and end_id including newly inserted ones.

DOCX GENERATIVE CONTENT RULES (apply when user asks to ADD new sections, duplicate, remove, or add Table of Contents):

37. To ADD A NEW SECTION (e.g. "Add a Conclusion section", "Insert a Risks and Challenges section"):
    - Use `layout_op` with `action: "insert_block"`.
    - Set `after_id` to the DOM id of the LAST paragraph of the preceding section. If adding at the end, use the last paragraph in the document.
    - Set `before_id` to the DOM id of the first paragraph of the FOLLOWING section, if inserting in the middle of the document.
    - The `data` array MUST contain:
      a. A heading item: {{"role": "heading", "text": "<Section Title>", "heading_level": 2}}
      b. At least 2 body items with descriptive placeholder text starting with '[placeholder]': {{"role": "body", "text": "[placeholder] <a 1-2 sentence description of what this section should contain>"}}
    - IMPORTANT: The ContentEnricher will replace the body text with rich, contextually appropriate content. Just make the heading correct and provide a meaningful 1-sentence description in each body item starting with '[placeholder]' so the enricher knows what to write.
    - NEVER use `data: []` (empty array) — always include at least a heading item.

38. To ADD A TABLE OF CONTENTS:
    - Use `layout_op` with `action: "insert_toc"`.
    - Set `before_id` to the DOM id of the FIRST content paragraph in the document (to insert the ToC at the very beginning).
    - The system will automatically build a visible table from the document's actual headings. You do NOT need to provide data.

39. To DUPLICATE A SECTION (e.g. "Duplicate the Key Metrics section"):
    - Use `layout_op` with `action: "duplicate_block"`.
    - `start_id` = DOM id of the section's heading paragraph.
    - `end_id` = DOM id of the LAST paragraph/table belonging to that section (stop before the next heading).
    - `after_id` = DOM id of the `end_id` element (to insert the copy immediately after the original).

40. To REMOVE A SECTION (e.g. "Remove the Formatting Playground section"):
    - Use `layout_op` with `action: "remove_block"`.
    - `start_id` = DOM id of the section's heading paragraph.
    - `end_id` = DOM id of the LAST paragraph/table belonging to that section (stop before the next heading).
    - Use the `body_index` field in the structure to find the boundaries: the section ends just before the paragraph with the next lower or equal heading level.

41. For "find and replace" or "replace all occurrences" tasks, use `find_replace` with `target_id: "all"`.
    - Set `is_regex: true` if you are using regex in `find_text` (e.g. replacing all dates, phone numbers).
    - If the user generically asks to replace a "placeholder" (e.g. "change the date placeholder"), you MUST look at the provided DOM to find the exact text of the placeholder (like "[DD Month YYYY]", "<Date>", etc.) and use that exact string (or a regex) in `find_text`.



CURRENT DATE: {{CURRENT_DATE}}

{_OPERATION_SCHEMA}
"""


def _guess_color_name(hex_str: str) -> str:
    """Guess a basic color name from a hex string to help the LLM."""
    hex_str = hex_str.lstrip('#')
    if len(hex_str) != 6: return "unknown"
    try:
        r, g, b = int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16)
        if r > 200 and g > 200 and b > 200: return "white"
        if r < 50 and g < 50 and b < 50: return "black"
        if abs(r-g) < 20 and abs(g-b) < 20: return "gray"
        
        if r > g and r > b: return "red/orange"
        if g > r and g > b: return "green"
        if b > r and b > g: return "blue"
        if r > b and g > b and abs(r-g) < 30: return "yellow"
    except Exception:
        pass
    return "unknown"


def _build_structure_summary(structure: dict, document_type: str) -> str:
    """Build a compact text summary of the document structure for the LLM."""
    lines = []
    
    blocks = structure.get("blocks", [])
    
    def _format_colors(colors: list) -> str:
        if not colors: return ""
        named = [f"#{c} ({_guess_color_name(c)})" for c in colors]
        return f" colors={named}"

    if document_type == "pptx":
        lines.append(f"Document: PPTX with {len(blocks)} text block(s)")
        for b in blocks:
            meta = b.get("metadata", {})
            role = meta.get("role", "none")
            color_hint = _format_colors(meta.get("colors"))
            lines.append(
                f"  target_id='{b.get('element_id')}' "
                f"role='{role}'{color_hint} "
                f"text={b.get('text', '')[:80]!r}"
            )
    else:
        # DOCX: emit elements in true document body order using the DOM
        dom_children = (
            structure.get("dom", {}).get("children", [])
        )

        if dom_children:
            # DOM is already in body order after the fix
            lines.append(f"Document: DOCX with {len(dom_children)} top-level block(s) in document order")
            lines.append("NOTE: body_index shows the true sequential order of elements in the document.")
            lines.append("Use body_index to reason about which section comes before/after another.")
            lines.append("")

            shown = 0
            for el in dom_children:
                if shown >= 150:
                    lines.append(f"  ... and {len(dom_children) - shown} more elements (truncated)")
                    break

                el_type = el.get("type", "?")
                el_id = el.get("id", "?")
                body_idx = el.get("body_index", "?")

                if el_type == "paragraph":
                    role = el.get("role", "body")
                    text = el.get("text", "")[:80]
                    extra = ""
                    if role == "heading":
                        hlvl = el.get("heading_level", "")
                        pbk = el.get("style", {}).get("page_break_before")
                        pbk_str = f" page_break_before={pbk}" if pbk is not None else ""
                        extra = f" heading_level={hlvl}{pbk_str}"
                    elif role == "bullet_point":
                        li = el.get("list_info", {})
                        lt = li.get("list_type", "bullet")
                        nid = li.get("num_id", "?")
                        ilvl = li.get("ilvl", 0)
                        extra = f" list_type={lt!r}  num_id={nid}  ilvl={ilvl}"
                    lines.append(
                        f"  body_index={body_idx}  target_id='{el_id}'  role='{role}'{extra}  text={text!r}"
                    )
                elif el_type == "table":
                    rows = el.get("row_count", len(el.get("rows", [])))
                    cols = el.get("col_count", 0)
                    lines.append(
                        f"  body_index={body_idx}  target_id='{el_id}'  role='table'  rows={rows} cols={cols}"
                    )
                elif el_type == "image":
                    w_emu = el.get("width_emu", 0)
                    h_emu = el.get("height_emu", 0)
                    # Convert EMU to cm for human readability (1 cm = 360000 EMU)
                    w_cm = round(w_emu / 360000, 1) if w_emu else "?"
                    h_cm = round(h_emu / 360000, 1) if h_emu else "?"
                    desc = el.get("description", "")
                    align = el.get("alignment", "")
                    desc_str = f"  description={desc!r}" if desc else ""
                    lines.append(
                        f"  body_index={body_idx}  target_id='{el_id}'  role='inline_image'"
                        f"  size={w_cm}cm x {h_cm}cm  alignment={align!r}{desc_str}"
                    )
                else:
                    lines.append(f"  body_index={body_idx}  target_id='{el_id}'  role='{el_type}'")
                shown += 1
        else:
            # Fallback: use blocks list (old path)
            lines.append(f"Document: DOCX with {len(blocks)} text block(s)")
            for b in blocks[:150]:
                meta = b.get("metadata", {})
                role = meta.get("role", "none")
                color_hint = _format_colors(meta.get("colors"))
                lines.append(
                    f"  target_id='{b.get('element_id')}' "
                    f"role='{role}'{color_hint} "
                    f"text={b.get('text', '')[:80]!r}"
                )
            if len(blocks) > 150:
                lines.append(f"  ... and {len(blocks) - 150} more paragraphs")
    
    return "\n".join(lines)


class OperationGenerator:
    """Converts a user request into a list of structured document operations."""

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
        """Generate operations for the given request.
        
        Returns a list of validated operation dicts.
        Falls back to a helpful error operation if LLM is unavailable.
        """
        if settings.openai_api_key:
            try:
                return self._generate_with_llm(
                    request, structure, document_type, chat_history,
                    intent, attached_image_path, previous_ops, reviewer_feedback,
                    missed_tasks,
                )
            except Exception as exc:
                log.exception("LLM operation generation failed: %s", exc)
        
        return self._generate_fallback(request, intent, attached_image_path)

    # ------------------------------------------------------------------
    # LLM path
    # ------------------------------------------------------------------

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
        from openai import OpenAI
        from datetime import datetime
        sys_prompt = _SYSTEM_PROMPT.replace("{CURRENT_DATE}", datetime.now().strftime("%B %d, %Y"))

        client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
        )

        messages = [{"role": "system", "content": sys_prompt}]
        
        # Add summary of what we found in structure
        structure_summary = _build_structure_summary(structure, document_type)
        
        # Build history context
        history_str = ""
        if chat_history:
            history_str = "Recent conversation:\n"
            for msg in chat_history[-4:]:
                role = "User" if msg["role"] == "user" else "Agent"
                history_str += f"{role}: {msg['content'][:200]}\n"
            history_str += "\n"

        # Image context
        image_str = ""
        if attached_image_path:
            image_str = f"Attached image path (use this for image_op): {attached_image_path}\n"
        else:
            image_str = "No image attached.\n"

        # Previous ops + feedback (for refinement rounds)
        refinement_str = ""
        if reviewer_feedback and previous_ops:
            missed_str = ""
            if missed_tasks:
                missed_str = (
                    f"\nSub-tasks still MISSING (must be included in your new list):\n"
                    + "\n".join(f"  - {t}" for t in missed_tasks)
                    + "\n"
                )
            refinement_str = (
                f"\nPrevious attempt (context only — do NOT copy these verbatim):\n{json.dumps(previous_ops, indent=2)}\n"
                f"Reviewer feedback: {reviewer_feedback}\n"
                f"{missed_str}"
                "IMPORTANT: Regenerate the COMPLETE operation list from scratch, fixing the issues above.\n"
                "Your new list replaces the previous one entirely — include ALL operations needed,\n"
                "both the ones that were correct AND the fixed/new ones for the missing sub-tasks.\n"
                "Do NOT emit a partial list.\n"
            )

        # Build op_categories hint
        op_categories = intent.get("op_categories", [])
        if not op_categories:
            primary = intent.get("op_category", "")
            op_categories = [primary] if primary else []
        categories_str = ""
        if op_categories:
            categories_str = (
                f"Operation categories needed for this request: {', '.join(op_categories)}\n"
                "You MUST produce operations covering ALL of the above categories.\n"
            )

        # Build a brief document summary for generative requests
        # (helps the LLM correctly place new sections and write descriptive placeholder text)
        doc_summary_str = ""
        if document_type == "docx":
            dom_children = structure.get("dom", {}).get("children", [])
            heading_titles = [
                el.get("text", "").strip()
                for el in dom_children
                if el.get("type") == "paragraph" and el.get("role") == "heading"
                and el.get("text", "").strip()
            ]
            body_excerpts = [
                el.get("text", "").strip()[:200]
                for el in dom_children
                if el.get("type") == "paragraph" and el.get("role") == "body"
                and el.get("text", "").strip()
            ][:3]
            if heading_titles or body_excerpts:
                doc_summary_str = "Document summary for context:\n"
                if heading_titles:
                    doc_summary_str += f"  Sections: {', '.join(heading_titles[:12])}\n"
                if body_excerpts:
                    doc_summary_str += f"  Content excerpt: {' '.join(body_excerpts)[:400]}\n"
                doc_summary_str += "\n"

        user_prompt = (
            f"{history_str}"
            f"{image_str}"
            f"{categories_str}"
            f"{doc_summary_str}"
            f"Document structure:\n{structure_summary}\n\n"
            f"{refinement_str}"
            f"User instruction: {request}\n\n"
            "IMPORTANT: Before closing the JSON array, verify that every sub-task in the user "
            "instruction has at least one operation covering it. Add any missing operations now.\n"
            "Return a JSON array of operation objects."
        )

        response = client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=8192,
            response_format={"type": "json_object"},
        )

        raw = (response.choices[0].message.content or "{}").strip()
        
        # Rescue truncated JSON arrays if max_tokens was hit
        if not raw.endswith("]") and not raw.endswith("}"):
            last_brace = raw.rfind("}")
            if last_brace != -1:
                # If it started with an object wrapper, close both the array and object
                if raw.startswith("{"):
                    raw = raw[:last_brace+1] + "\n]\n}"
                else:
                    raw = raw[:last_brace+1] + "\n]"
                
        # The model returns a JSON object — we accept either {"operations": [...]} or [...]
        try:
            parsed = json.loads(raw)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print("RAW OUTPUT WAS:", raw)
            raise e
            
        if isinstance(parsed, list):
            ops_raw = parsed
        elif isinstance(parsed, dict):
            # Try common wrapper keys
            ops_raw = (
                parsed.get("operations")
                or parsed.get("ops")
                or parsed.get("result")
                or [parsed]
            )
        else:
            ops_raw = []
            
        # Deduplicate identical operations
        unique_ops = []
        seen_ops = set()
        for op in ops_raw:
            if isinstance(op, dict):
                import json as _json
                op_str = _json.dumps(op, sort_keys=True)
                if op_str not in seen_ops:
                    seen_ops.add(op_str)
                    unique_ops.append(op)

        # Validate each operation
        validated = []
        for op in unique_ops:
            try:
                validated.append(validate_operation(op))
            except ValueError as e:
                log.warning("Skipping invalid operation: %s — %s", op, e)

        if not validated:
            log.warning("LLM returned no valid operations for request: %r", request)
        
        return validated

    # ------------------------------------------------------------------
    # Fallback (no LLM)
    # ------------------------------------------------------------------

    def _generate_fallback(
        self,
        request: str,
        intent: dict,
        attached_image_path: str | None,
    ) -> list[dict]:
        """Minimal heuristic fallback when the LLM is unavailable."""
        category = intent.get("op_category", "")
        
        # Only fallback insert/replace to needs_image if explicitly falling back
        # Note: the LLM is smart enough to generate needs_image natively when needed.
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

        # Generic AI design fallback
        return [validate_operation({
            "op_type": "ai_design_op",
            "target": {},
            "parameters": {
                "action": "improve_readability",
                "scope": "all_slides",
            },
        })]
