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

import logging
from dataclasses import dataclass
from pathlib import Path
from re import sub
from typing import Any

from docx import Document
from pptx import Presentation
from pptx.dml.color import RGBColor

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

    # ------------------------------------------------------------------
    # PPTX: one block per paragraph inside each shape
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
                            "para_index": para_idx,
                        },
                    ))
        return blocks

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
