"""Structured operation definitions for the document editing pipeline.

Every operation the LLM can produce is described here.  The document
processor dispatches on ``op_type`` to call the correct handler.

Op type taxonomy
----------------
text_edit      — rewrite text content of a paragraph (existing behaviour)
text_format    — change formatting of a paragraph/run (bold, font, color …)
table_op       — create/edit/delete tables and their cells
image_op       — insert/replace/resize/style images
shape_op       — add/edit text boxes and shapes
theme_op       — change slide/document background, colors, gradients
slide_op       — add/delete/duplicate/reorder/hide slides
chart_op       — change chart type, colors, labels, data
ai_design_op   — AI-directed normalizations (font, spacing, hierarchy)
"""
from __future__ import annotations

from typing import Any, Literal


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class Operation:
    """Base class — not used directly; here for documentation only."""
    pass


# ---------------------------------------------------------------------------
# Type aliases (used as JSON keys)
# ---------------------------------------------------------------------------

OpType = Literal[
    "text_edit",
    "text_format",
    "table_op",
    "image_op",
    "shape_op",
    "theme_op",
    "slide_op",
    "chart_op",
    "ai_design_op",
    "layout_op",
    "needs_image",
]


# ---------------------------------------------------------------------------
# Parameter schemas (all fields optional — None means "keep existing")
# ---------------------------------------------------------------------------

class TextEditParams:
    """Parameters for a text content rewrite."""
    new_text: str


class TextFormatParams:
    """Parameters for formatting a targeted paragraph."""
    bold: bool | None
    italic: bool | None
    underline: bool | None
    strikethrough: bool | None
    font_family: str | None          # e.g. "Arial", "Calibri"
    font_size_pt: float | None
    color_hex: str | None            # 6-char hex e.g. "FF0000"
    highlight_hex: str | None
    alignment: str | None            # "left"|"center"|"right"|"justify"
    line_spacing: float | None       # multiplier e.g. 1.5
    char_spacing: float | None       # pt
    superscript: bool | None
    subscript: bool | None
    shadow: bool | None


class TableOpParams:
    """Parameters for a table CRUD operation."""
    action: str  # "create"|"delete"|"add_row"|"remove_row"|"add_col"|"remove_col"
                 # "merge_cells"|"set_cell_bg"|"set_borders"|"alternate_rows"
                 # "populate"|"set_header_format"|"sort_data"
    rows: int | None
    cols: int | None
    header_row: bool | None
    alternate_row_colors: list[str] | None   # two 6-char hex strings
    data: list[list[str]] | None             # row-major cell content
    row_index: int | None                    # for add/remove row
    col_index: int | None                    # for add/remove col
    merge_from: tuple[int, int] | None       # (row, col)
    merge_to: tuple[int, int] | None         # (row, col)
    cell_bg_hex: str | None
    border_color_hex: str | None
    border_width_pt: float | None
    position: dict[str, float] | None        # {left_pct, top_pct, width_pct, height_pct}
    cell_padding_pt: float | None
    cell_alignment: str | None


class ImageOpParams:
    """Parameters for image operations."""
    action: str  # "insert"|"replace"|"remove"|"resize"|"reposition"|"rotate"
                 # "bring_forward"|"send_backward"|"set_transparency"|"set_border"
                 # "rounded_corners"|"shadow"
    image_path: str | None           # server-side absolute path to uploaded image
    position: dict[str, float] | None   # {left_pct, top_pct, width_pct, height_pct}
    maintain_aspect_ratio: bool | None
    rotation_degrees: float | None
    transparency_pct: float | None      # 0–100
    border_color_hex: str | None
    border_width_pt: float | None
    rounded_corners: bool | None
    shadow: bool | None
    crop: dict[str, float] | None       # {top_pct, left_pct, right_pct, bottom_pct}


class ShapeOpParams:
    """Parameters for shape / text-box operations."""
    action: str  # "add_textbox"|"delete"|"resize"|"move"|"rotate"|"duplicate"
                 # "set_fill"|"set_outline"|"set_transparency"|"group"|"ungroup"
                 # "bring_forward"|"send_backward"|"align"|"distribute"
    text: str | None
    position: dict[str, float] | None
    fill_color_hex: str | None
    outline_color_hex: str | None
    outline_width_pt: float | None
    transparency_pct: float | None
    rotation_degrees: float | None
    corner_radius_pt: float | None
    group_shape_indices: list[int] | None


class ThemeOpParams:
    """Parameters for theme/color/background operations."""
    action: str  # "set_bg_color"|"set_bg_gradient"|"set_bg_pattern"
                 # "apply_theme_colors"|"corporate_branding"
    scope: str | None                # "all_slides"|"current_slide"
    bg_color_hex: str | None
    gradient_start_hex: str | None
    gradient_end_hex: str | None
    gradient_direction: str | None   # "horizontal"|"vertical"|"diagonal"
    accent_colors: list[str] | None  # list of 6-char hex strings


class SlideOpParams:
    """Parameters for slide-level operations."""
    action: str  # "add"|"delete"|"duplicate"|"reorder"|"hide"|"unhide"
                 # "rename_title"|"apply_layout"|"change_size"
    after_index: int | None          # for "add"/"duplicate": insert after this 1-based index
    from_index: int | None           # for "reorder": source position (1-based)
    to_index: int | None             # for "reorder": target position (1-based)
    layout_name: str | None
    title: str | None                # for "rename_title"


class ChartOpParams:
    """Parameters for chart editing operations."""
    action: str  # "change_type"|"update_data"|"set_series_colors"|"update_labels"
                 # "update_axis_labels"|"show_legend"|"hide_legend"|"apply_theme"
    chart_type: str | None           # "bar"|"line"|"pie"|"scatter"|"column"
    series_colors: list[str] | None  # hex colors per series
    data: list[list[Any]] | None     # new chart data (row-major)
    legend_position: str | None      # "top"|"bottom"|"left"|"right"|"none"
    x_axis_label: str | None
    y_axis_label: str | None
    data_labels_visible: bool | None


class AiDesignOpParams:
    """Parameters for AI-driven design normalization."""
    action: str  # "normalize_fonts"|"normalize_spacing"|"improve_hierarchy"
                 # "balance_whitespace"|"remove_overlaps"|"auto_resize_text"
                 # "make_consistent"|"generate_speaker_notes"|"convert_bullets_to_diagram"
                 # "improve_readability"|"detect_clutter"
    scope: str | None                # "all_slides"|"slide:{n}"
    target_font: str | None          # for normalize_fonts
    base_font_size_pt: float | None


# ---------------------------------------------------------------------------
# Unified operation dict (as produced by the LLM and consumed by processor)
# ---------------------------------------------------------------------------

def validate_operation(op: dict) -> dict:
    """Light validation / normalisation of a raw LLM-produced operation dict.
    
    Returns the op dict with defaults filled in, or raises ValueError if
    the op is structurally invalid.
    """
    if not isinstance(op, dict):
        raise ValueError(f"Operation must be a dict, got {type(op)}")
    
    op_type = op.get("op_type")
    valid_types = {
        "text_edit", "text_format", "table_op", "image_op",
        "shape_op", "theme_op", "slide_op", "chart_op", "layout_op",
        "ai_design_op", "needs_image", "list_op", "find_replace",
    }
    if op_type not in valid_types:
        raise ValueError(f"Unknown op_type: {op_type!r}")
    
    op.setdefault("target_id", None)
    op.setdefault("parameters", {})
    
    return op


def needs_image_response(reason: str = "") -> dict:
    """Build a special operation that signals the agent needs an image upload."""
    return {
        "op_type": "needs_image",
        "target_id": None,
        "parameters": {
            "message": (
                reason or
                "To insert an image, please attach it to your next message "
                "using the 📎 paperclip icon below the chat input."
            )
        }
    }
