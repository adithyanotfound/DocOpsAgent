"""Document processor: extract and apply text edits to DOCX and PPTX files.

Run-aware replacement strategy
--------------------------------
We never use proportional character distribution across runs.  Instead we:

1. Concatenate all run texts to get the full paragraph text.
2. Find the longest common prefix and suffix between old and new text to
   isolate the minimal "changed region".
3. Walk the runs and update only the ones that overlap that region.
   - Runs entirely before the region: untouched.
   - First run overlapping the region: gets (its unchanged prefix) +
     (the new replacement text) + (its unchanged suffix if the change ends
     inside this run).
   - Any subsequent runs overlapping the region: get only their unchanged
     trailing suffix (the middle is consumed by the first run).
   - Runs entirely after the region: untouched.

This ensures that formatting (colour, font, size, bold, italic, …) stays on
exactly the same runs as before.  Only the text content of runs that
actually contain the changed characters is modified.

Example
~~~~~~~
Paragraph runs:
  run[0] "Theme Name: "  (green, 12 chars)
  run[1] "Edtech"         (black,  6 chars)

new_text = "Theme Name: Healthtech"
  common prefix  = "Theme Name: "  → 12 chars
  common suffix  = "tech"           →  4 chars
  changed region = chars [12, 14) = "Ed"  →  replaced with "Health"

  run[0] untouched             → "Theme Name: " (green)  ✓
  run[1] "Ed"→"Health" + "tech" → "Healthtech"   (black)  ✓
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from pathlib import Path
from re import sub
from typing import Any

from docx import Document
from lxml import etree
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Pt, Emu

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TextBlock:
    element_id: str
    type: str
    text: str
    metadata: dict


# ---------------------------------------------------------------------------
# Diff helper
# ---------------------------------------------------------------------------

def _changed_region(old: str, new: str) -> tuple[int, int, str]:
    """Return (prefix_len, suffix_len, new_middle) such that:
      old[prefix_len : len(old) - suffix_len]  should become  new_middle
      new_middle == new[prefix_len : len(new) - suffix_len]
    """
    # Longest common prefix
    prefix_len = 0
    min_len = min(len(old), len(new))
    while prefix_len < min_len and old[prefix_len] == new[prefix_len]:
        prefix_len += 1

    # Longest common suffix (bounded so it cannot overlap the prefix)
    old_remaining = len(old) - prefix_len
    new_remaining = len(new) - prefix_len
    max_suffix = min(old_remaining, new_remaining)
    suffix_len = 0
    while (suffix_len < max_suffix
           and old[len(old) - 1 - suffix_len] == new[len(new) - 1 - suffix_len]):
        suffix_len += 1

    new_end = len(new) - suffix_len if suffix_len > 0 else len(new)
    new_middle = new[prefix_len:new_end]
    return prefix_len, suffix_len, new_middle


# Alignment string → pptx enum mapping
_ALIGN_MAP = {
    "left": PP_ALIGN.LEFT,
    "center": PP_ALIGN.CENTER,
    "right": PP_ALIGN.RIGHT,
    "justify": PP_ALIGN.JUSTIFY,
}


# ---------------------------------------------------------------------------
# Main processor
# ---------------------------------------------------------------------------

class DocumentProcessor:

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(self, path: Path, document_type: str) -> dict:
        blocks = (
            self._extract_pptx(path) if document_type == "pptx"
            else self._extract_docx(path)
        )
        return {"document_type": document_type, "blocks": [b.__dict__ for b in blocks]}

    def extract_rich(self, path: Path, document_type: str) -> dict:
        """Extract enriched structure for template analysis (shapes, geometry, formatting)."""
        if document_type == "pptx":
            return self._extract_pptx_rich(path)
        return self.extract(path, document_type)

    def apply_edits(
        self,
        source: Path,
        target: Path,
        document_type: str,
        edits: list[dict],
    ) -> None:
        if document_type == "pptx":
            self._apply_pptx_edits(source, target, edits)
        else:
            self._apply_docx_edits(source, target, edits)

    def apply_slide_plan(
        self,
        source: Path,
        target: Path,
        slide_plan: dict,
    ) -> None:
        """Apply a structured slide plan to a PPTX template.

        The plan specifies which template slides to clone, what content to
        populate, and what formatting to apply.  Slides marked "delete" are
        removed.
        """
        self._apply_pptx_slide_plan(source, target, slide_plan)

    # ------------------------------------------------------------------
    # PPTX: one block per paragraph inside each shape (original)
    # ------------------------------------------------------------------

    def _extract_pptx(self, path: Path) -> list[TextBlock]:
        prs = Presentation(path)
        blocks: list[TextBlock] = []
        for slide_idx, slide in enumerate(prs.slides, start=1):
            for shape_idx, shape in enumerate(slide.shapes):
                if not getattr(shape, "has_text_frame", False):
                    continue
                for para_idx, para in enumerate(shape.text_frame.paragraphs):
                    text = para.text.strip()
                    if not text:
                        continue
                    eid = f"slide_{slide_idx}_shape_{shape_idx}_para_{para_idx}"
                    blocks.append(TextBlock(
                        element_id=eid,
                        type="text",
                        text=text,
                        metadata={
                            "slide": slide_idx,
                            "shape_index": shape_idx,
                            "shape_name": getattr(shape, "name", "Unknown"),
                            "para_index": para_idx,
                        },
                    ))
        return blocks

    # ------------------------------------------------------------------
    # PPTX: rich extraction (includes empty frames, geometry, formatting)
    # ------------------------------------------------------------------

    def _extract_pptx_rich(self, path: Path) -> dict:
        """Extract full template structure with shapes, placeholders, geometry,
        and paragraph-level formatting — including empty text frames."""
        prs = Presentation(path)
        slides_data: list[dict] = []

        for slide_idx, slide in enumerate(prs.slides, start=1):
            shapes_data: list[dict] = []
            for shape_idx, shape in enumerate(slide.shapes):
                shape_info: dict[str, Any] = {
                    "shape_index": shape_idx,
                    "shape_name": getattr(shape, "name", "Unknown"),
                    "left": shape.left,
                    "top": shape.top,
                    "width": shape.width,
                    "height": shape.height,
                    "is_placeholder": shape.is_placeholder,
                    "placeholder_type": None,
                    "has_text_frame": getattr(shape, "has_text_frame", False),
                    "paragraphs": [],
                }
                shape_info["has_table"] = getattr(shape, "has_table", False)
                if shape.is_placeholder:
                    try:
                        shape_info["placeholder_type"] = str(shape.placeholder_format.type)
                    except Exception:
                        shape_info["placeholder_type"] = "unknown"

                if getattr(shape, "has_text_frame", False):
                    for para_idx, para in enumerate(shape.text_frame.paragraphs):
                        para_info = self._extract_paragraph_info(para, para_idx)
                        shape_info["paragraphs"].append(para_info)

                if shape_info["has_table"]:
                    shape_info["table_rows"] = []
                    for row_idx, row in enumerate(shape.table.rows):
                        row_data = []
                        for col_idx, cell in enumerate(row.cells):
                            cell_info = {"row_index": row_idx, "col_index": col_idx, "paragraphs": []}
                            for para_idx, para in enumerate(cell.text_frame.paragraphs):
                                cell_info["paragraphs"].append(self._extract_paragraph_info(para, para_idx))
                            row_data.append(cell_info)
                        shape_info["table_rows"].append(row_data)

                shapes_data.append(shape_info)

            # Determine the slide layout name
            layout_name = ""
            try:
                layout_name = slide.slide_layout.name
            except Exception:
                pass

            slides_data.append({
                "slide_index": slide_idx,
                "layout_name": layout_name,
                "shapes": shapes_data,
            })

        return {
            "document_type": "pptx",
            "slide_count": len(prs.slides),
            "slide_width": prs.slide_width,
            "slide_height": prs.slide_height,
            "slides": slides_data,
        }

    def _extract_paragraph_info(self, para, para_idx: int) -> dict:
        """Extract paragraph text and formatting information."""
        text = para.text  # include untrimmed text
        alignment = None
        if para.alignment is not None:
            try:
                alignment = str(para.alignment).split(".")[-1].split("(")[0].strip().lower()
            except Exception:
                pass

        # Get formatting from the first run (representative)
        font_size = None
        bold = None
        italic = None
        color_hex = None

        if para.runs:
            run = para.runs[0]
            font = run.font
            if font.size is not None:
                font_size = round(font.size.pt, 1)
            bold = font.bold
            italic = font.italic
            try:
                if font.color and font.color.type is not None and font.color.rgb:
                    color_hex = str(font.color.rgb)
            except Exception:
                pass

        return {
            "para_index": para_idx,
            "text": text.strip(),
            "raw_text": text,
            "formatting": {
                "font_size_pt": font_size,
                "bold": bold,
                "italic": italic,
                "color_hex": color_hex,
                "alignment": alignment,
            },
        }

    # ------------------------------------------------------------------
    # PPTX: apply text edits (original)
    # ------------------------------------------------------------------

    def _apply_pptx_edits(self, source: Path, target: Path, edits: list[dict]) -> None:
        prs = Presentation(source)
        edit_map = {e["element_id"]: e for e in edits}
        for slide_idx, slide in enumerate(prs.slides, start=1):
            for shape_idx, shape in enumerate(slide.shapes):
                if not getattr(shape, "has_text_frame", False):
                    continue
                for para_idx, para in enumerate(shape.text_frame.paragraphs):
                    eid = f"slide_{slide_idx}_shape_{shape_idx}_para_{para_idx}"
                    if eid in edit_map:
                        edit = edit_map[eid]
                        log.debug(
                            "PPTX edit [%s]: %r → %r",
                            eid, edit["old_text"], edit["new_text"],
                        )
                        self._apply_run_aware_replacement(para, edit["new_text"])
        prs.save(target)

    # ------------------------------------------------------------------
    # PPTX: slide plan application (new)
    # ------------------------------------------------------------------

    def _apply_pptx_slide_plan(self, source: Path, target: Path, plan: dict) -> None:
        """Apply a structured slide plan to a PPTX template.

        Plan structure::

            {
                "slides": [
                    {
                        "source_slide_index": 1,
                        "action": "populate" | "keep" | "delete",
                        "shapes": [
                            {
                                "shape_index": 0,
                                "paragraphs": [
                                    {
                                        "para_index": 0,
                                        "text": "New text",
                                        "formatting": {
                                            "font_size_pt": 36,
                                            "bold": true,
                                            "color_hex": null,
                                            "alignment": "center"
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        """
        prs = Presentation(source)
        template_slides = list(prs.slides)
        plan_slides = plan.get("slides", [])

        if not plan_slides:
            log.warning("Empty slide plan — saving template as-is.")
            prs.save(target)
            return

        # Phase 1: Build the output slide list by cloning template slides
        # We'll work backwards: first create all needed clones, then delete originals.
        new_slide_xmls: list[tuple[str, Any]] = []  # (action, slide_element or None)

        for entry in plan_slides:
            action = entry.get("action", "populate")
            if action == "delete":
                continue  # skip deleted slides

            src_idx = entry.get("source_slide_index", 1) - 1  # convert to 0-based
            if src_idx < 0 or src_idx >= len(template_slides):
                src_idx = 0  # fallback to first slide

            new_slide_xmls.append((action, src_idx, entry))

        # Phase 2: Build new presentation from template
        new_prs = Presentation(source)
        existing_count = len(list(new_prs.slides))

        # Clone ALL slides defined in the plan, appending them to the END
        for action, src_idx, entry in new_slide_xmls:
            self._clone_slide(new_prs, src_idx)

        # Delete ALL original slides from the beginning (in reverse order to avoid shifting)
        for i in range(existing_count - 1, -1, -1):
            self._delete_slide(new_prs, i)

        # Phase 3: Populate content for each slide
        slides_list = list(new_prs.slides)
        for slide_out_idx, (action, src_idx, entry) in enumerate(new_slide_xmls):
            if slide_out_idx >= len(slides_list):
                break

            slide = slides_list[slide_out_idx]

            # For cloned slides that don't match their source, we need to
            # copy content from the correct template slide
            if action == "keep":
                continue  # leave as-is

            # Populate shapes
            shape_edits = entry.get("shapes", [])
            edit_map = {se.get("shape_index", 0): se for se in shape_edits}
            shapes = list(slide.shapes)
            
            for shape_idx, shape in enumerate(shapes):
                has_tf = getattr(shape, "has_text_frame", False)
                has_t = getattr(shape, "has_table", False)
                
                if shape_idx in edit_map:
                    shape_edit = edit_map[shape_idx]
                    
                    if has_tf:
                        para_edits = shape_edit.get("paragraphs", [])
                        paras = list(shape.text_frame.paragraphs)
                        for para_edit in para_edits:
                            para_idx = para_edit.get("para_index", 0)
                            new_text = para_edit.get("text", "")
                            formatting = para_edit.get("formatting", {})

                            if para_idx < len(paras):
                                para = paras[para_idx]
                                self._set_paragraph_text(para, new_text)
                                self._apply_formatting(para, formatting)
                            else:
                                # Add new paragraph
                                para = shape.text_frame.paragraphs[0] if paras else shape.text_frame.add_paragraph()
                                if para_idx > 0:
                                    para = shape.text_frame.add_paragraph()
                                para.text = ""
                                run = para.add_run()
                                run.text = new_text
                                self._apply_formatting(para, formatting)
                                
                    if has_t and "table_rows" in shape_edit:
                        table_rows = shape_edit.get("table_rows", [])
                        for r_idx, row_edit in enumerate(table_rows):
                            if r_idx >= len(shape.table.rows):
                                break
                            for c_idx, cell_edit in enumerate(row_edit):
                                if c_idx >= len(shape.table.columns):
                                    break
                                cell = shape.table.cell(r_idx, c_idx)
                                para_edits = cell_edit.get("paragraphs", [])
                                paras = list(cell.text_frame.paragraphs)
                                
                                for para_edit in para_edits:
                                    para_idx = para_edit.get("para_index", 0)
                                    new_text = para_edit.get("text", "")
                                    formatting = para_edit.get("formatting", {})
                                    
                                    if para_idx < len(paras):
                                        para = paras[para_idx]
                                        self._set_paragraph_text(para, new_text)
                                        self._apply_formatting(para, formatting)
                                    else:
                                        para = cell.text_frame.paragraphs[0] if paras else cell.text_frame.add_paragraph()
                                        if para_idx > 0:
                                            para = cell.text_frame.add_paragraph()
                                        para.text = ""
                                        run = para.add_run()
                                        run.text = new_text
                                        self._apply_formatting(para, formatting)
                else:
                    # Unedited shape. If it's a placeholder (and NOT a slide number), clear it
                    # to remove leftover boilerplate.
                    if getattr(shape, "is_placeholder", False) and has_tf:
                        try:
                            ph_type = str(shape.placeholder_format.type)
                            if "SLIDE_NUMBER" not in ph_type:
                                # Clear all paragraphs
                                paras = list(shape.text_frame.paragraphs)
                                if paras:
                                    self._set_paragraph_text(paras[0], "")
                                    for p in paras[1:]:
                                        self._set_paragraph_text(p, "")
                        except Exception:
                            pass

        new_prs.save(target)

    def _clone_slide(self, prs: Presentation, slide_index: int) -> None:
        """Deep-clone a slide at the given index and append it to the presentation."""
        template_slide = list(prs.slides)[slide_index]
        slide_layout = template_slide.slide_layout

        # Add a new slide with the same layout
        new_slide = prs.slides.add_slide(slide_layout)

        # Copy all shapes from the template slide to the new slide
        # We do this by copying the XML of each shape
        for shape in template_slide.shapes:
            el = copy.deepcopy(shape._element)
            new_slide.shapes._spTree.append(el)

        # Remove the default placeholder shapes that come with the layout
        # (they duplicate what we just copied)
        sp_tree = new_slide.shapes._spTree
        # Collect placeholders that were auto-created by add_slide
        default_shapes = []
        for sp in sp_tree:
            if sp.tag.endswith("}sp") or sp.tag == "sp":
                # Check if this is a default placeholder (not one we cloned)
                pass  # We'll use a different approach

        # Actually, the cleaner approach: remove all default shapes first,
        # then copy from template
        # Let's redo: remove shapes added by add_slide, keep only our cloned ones
        # The shapes added by add_slide come from the layout's placeholders
        # Our cloned shapes are appended at the end

        # Count shapes from template
        template_shape_count = len(list(template_slide.shapes))

        # The spTree contains: <cNvPr> (non-visual props) + shapes
        # Shapes added by layout come first, our cloned ones come last
        all_sps = [child for child in sp_tree
                   if child.tag.endswith("}sp") or child.tag.endswith("}pic")
                   or child.tag.endswith("}grpSp") or child.tag.endswith("}graphicFrame")
                   or child.tag.endswith("}cxnSp")]

        if len(all_sps) > template_shape_count:
            # Remove the auto-generated shapes (first N shapes minus our cloned ones)
            auto_count = len(all_sps) - template_shape_count
            for sp in all_sps[:auto_count]:
                sp_tree.remove(sp)

        # Copy slide-level properties (background, etc.)
        try:
            if template_slide._element.find(
                "{http://schemas.openxmlformats.org/presentationml/2006/main}bg"
            ) is not None:
                bg = copy.deepcopy(
                    template_slide._element.find(
                        "{http://schemas.openxmlformats.org/presentationml/2006/main}bg"
                    )
                )
                existing_bg = new_slide._element.find(
                    "{http://schemas.openxmlformats.org/presentationml/2006/main}bg"
                )
                if existing_bg is not None:
                    new_slide._element.replace(existing_bg, bg)
                else:
                    new_slide._element.insert(0, bg)
        except Exception:
            pass

    def _delete_slide(self, prs: Presentation, slide_index: int) -> None:
        """Delete a slide from the presentation by index (0-based)."""
        slides = list(prs.slides)
        if slide_index < 0 or slide_index >= len(slides):
            log.warning("Cannot delete slide %d: out of range (total: %d)", slide_index, len(slides))
            return

        slide = slides[slide_index]
        rId = None

        # Find the relationship ID for this slide
        for rel in prs.part.rels.values():
            if rel.target_part == slide.part:
                rId = rel.rId
                break

        if rId is None:
            log.warning("Cannot find relationship for slide %d", slide_index)
            return

        # Remove from slide list XML
        pres_elem = prs.part._element
        nsmap = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main",
                 "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
        sldIdLst = pres_elem.find("p:sldIdLst", nsmap)
        if sldIdLst is not None:
            for sldId in list(sldIdLst):
                if sldId.get(
                    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
                ) == rId:
                    sldIdLst.remove(sldId)
                    break

        # Remove the relationship
        try:
            prs.part.rels.pop(rId)
        except Exception:
            pass

    def _set_paragraph_text(self, paragraph, new_text: str) -> None:
        """Set paragraph text while preserving formatting of the first run."""
        runs = paragraph.runs
        if not runs:
            # No runs exist — create one
            run = paragraph.add_run()
            run.text = new_text
            return

        # Set text on the first run, clear the rest
        runs[0].text = new_text
        for run in runs[1:]:
            run.text = ""

    def _apply_formatting(self, paragraph, formatting: dict) -> None:
        """Apply formatting to a paragraph's runs.

        Only applies values that are not None — None means 'keep existing'.
        """
        if not formatting:
            return

        # Alignment
        alignment = formatting.get("alignment")
        if alignment and alignment in _ALIGN_MAP:
            paragraph.alignment = _ALIGN_MAP[alignment]

        # Font properties — apply to all runs
        font_size = formatting.get("font_size_pt")
        bold = formatting.get("bold")
        italic = formatting.get("italic")
        color_hex = formatting.get("color_hex")

        for run in paragraph.runs:
            font = run.font
            if font_size is not None:
                font.size = Pt(font_size)
            if bold is not None:
                font.bold = bold
            if italic is not None:
                font.italic = italic
            if color_hex is not None and len(color_hex) == 6:
                try:
                    font.color.rgb = RGBColor.from_string(color_hex)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # DOCX: one block per paragraph
    # ------------------------------------------------------------------

    def _extract_docx(self, path: Path) -> list[TextBlock]:
        doc = Document(path)
        blocks: list[TextBlock] = []
        section = "Document"
        for idx, para in enumerate(doc.paragraphs):
            text = para.text.strip()
            if not text:
                continue
            if para.style and para.style.name.lower().startswith("heading"):
                section = text
            blocks.append(TextBlock(
                element_id=f"paragraph_{idx}",
                type="text",
                text=text,
                metadata={"section": section, "paragraph_index": idx},
            ))
        return blocks

    def _apply_docx_edits(self, source: Path, target: Path, edits: list[dict]) -> None:
        doc = Document(source)
        edit_map = {e["element_id"]: e for e in edits}
        for idx, para in enumerate(doc.paragraphs):
            eid = f"paragraph_{idx}"
            if eid in edit_map:
                edit = edit_map[eid]
                log.debug(
                    "DOCX edit [%s]: %r → %r",
                    eid, edit["old_text"], edit["new_text"],
                )
                self._apply_run_aware_replacement(para, edit["new_text"])
        doc.save(target)

    # ------------------------------------------------------------------
    # Core: run-aware replacement
    # ------------------------------------------------------------------

    def _apply_run_aware_replacement(self, paragraph, new_text: str) -> None:
        """Update paragraph text while preserving per-run formatting.

        Only the runs whose characters overlap the changed region are
        modified; all other runs are left completely untouched.
        """
        runs = paragraph.runs
        if not runs:
            run = paragraph.add_run()
            run.text = new_text
            return

        old_text = "".join(r.text for r in runs)

        if old_text == new_text:
            log.debug("  No change (text identical), skipping.")
            return

        # Log run structure before modification (debug only).
        self._log_runs("  BEFORE", runs)

        prefix_len, suffix_len, new_middle = _changed_region(old_text, new_text)
        old_changed_start = prefix_len
        old_changed_end   = len(old_text) - suffix_len

        log.debug(
            "  Changed region: chars [%d, %d) %r → %r",
            old_changed_start, old_changed_end,
            old_text[old_changed_start:old_changed_end],
            new_middle,
        )

        # Walk runs and update only those overlapping [old_changed_start, old_changed_end).
        run_pos = 0
        new_middle_placed = False

        for run in runs:
            r_len   = len(run.text)
            r_start = run_pos
            r_end   = run_pos + r_len
            run_pos  = r_end

            if r_end <= old_changed_start or r_start >= old_changed_end:
                # Run is entirely outside the changed region — leave it alone.
                continue

            # Run overlaps with the changed region.
            # Compute the portion of this run that falls BEFORE the change.
            prefix_in_run = max(0, old_changed_start - r_start)
            prefix_text   = run.text[:prefix_in_run]

            # Compute the portion of this run that falls AFTER the change.
            suffix_start_in_run = max(0, old_changed_end - r_start)
            suffix_text         = run.text[suffix_start_in_run:]

            if not new_middle_placed:
                # First overlapping run: inject the replacement text here.
                run.text = prefix_text + new_middle + suffix_text
                new_middle_placed = True
                log.debug(
                    "  run updated: %r  (prefix=%r, middle=%r, suffix=%r)",
                    run.text, prefix_text, new_middle, suffix_text,
                )
            else:
                # Subsequent overlapping runs: their "changed" content has
                # already been absorbed by the first run; keep only the
                # trailing unchanged portion.
                run.text = suffix_text
                log.debug("  run cleared to suffix: %r", run.text)

        # Log run structure after modification (debug only).
        self._log_runs("  AFTER ", runs)

    # ------------------------------------------------------------------
    # Debug helper
    # ------------------------------------------------------------------

    @staticmethod
    def _log_runs(label: str, runs) -> None:
        if not log.isEnabledFor(logging.DEBUG):
            return
        for i, run in enumerate(runs):
            try:
                color = run.font.color
                rgb = color.rgb if color.type is not None else "inherit"
            except Exception:
                rgb = "?"
            log.debug(
                "%s run[%d] text=%r bold=%s italic=%s color=%s",
                label, i, run.text, run.font.bold, run.font.italic, rgb,
            )


# ---------------------------------------------------------------------------
# Normalise helper (used elsewhere)
# ---------------------------------------------------------------------------

def normalize_text(value: str) -> str:
    return sub(r"\s+", " ", value).strip()
