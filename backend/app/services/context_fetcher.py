"""Context Fetcher — Stage 3 helper of the task graph pipeline.

Pulls full text/structural content for targeted element IDs from the DOM.
Avoids truncation of text fields so the LLM gets full context for editing.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class ContextFetcher:
    """Retrieves full content of elements by ID from the document structure."""

    @classmethod
    def fetch(cls, element_ids: list[str], structure: dict) -> dict[str, Any]:
        """Return a mapping of {element_id: content_details} for requested IDs."""
        if not element_ids:
            return {}

        dom = structure.get("dom", {})
        id_to_element = {}
        cls._walk_dom(dom, id_to_element)

        # For slide presentations, structure might be flat blocks
        blocks = structure.get("blocks", [])
        for b in blocks:
            eid = b.get("element_id")
            if eid and eid not in id_to_element:
                id_to_element[eid] = {
                    "id": eid,
                    "type": "paragraph",
                    "text": b.get("text", ""),
                    "metadata": b.get("metadata", {}),
                }

        results = {}
        for eid in element_ids:
            el = id_to_element.get(eid)
            if el:
                results[eid] = el
            else:
                results[eid] = {"id": eid, "text": "", "error": "Not found in document structure"}

        return results

    @classmethod
    def _walk_dom(cls, node: dict, acc: dict[str, dict]):
        nid = node.get("id")
        if nid:
            acc[nid] = {
                "id": nid,
                "type": node.get("type", "paragraph"),
                "text": node.get("text", ""),
                "role": node.get("role", "body"),
                "heading_level": node.get("heading_level"),
                "style": node.get("style", {}),
                "list_info": node.get("list_info", {}),
                "rows": node.get("rows", []),
                "row_count": node.get("row_count"),
                "col_count": node.get("col_count"),
                "width_emu": node.get("width_emu"),
                "height_emu": node.get("height_emu"),
                "description": node.get("description", ""),
                "alignment": node.get("alignment", ""),
            }
        
        children = node.get("children", [])
        rows = node.get("rows", [])
        cells = node.get("cells", [])

        for child in children + rows + cells:
            if isinstance(child, dict):
                cls._walk_dom(child, acc)
        
        # PPTX shapes have slide_index and shape_index inside slides
        slides = node.get("slides", [])
        for slide in slides:
            slide_idx = slide.get("slide_index")
            for shape in slide.get("shapes", []):
                shape_idx = shape.get("shape_index")
                sid = f"slide_{slide_idx}_shape_{shape_idx}"
                acc[sid] = {
                    "id": sid,
                    "type": "shape",
                    "slide_index": slide_idx,
                    "shape_index": shape_idx,
                    "shape_name": shape.get("shape_name", ""),
                    "has_text_frame": shape.get("has_text_frame"),
                    "has_table": shape.get("has_table"),
                    "has_image": shape.get("has_image"),
                    "paragraphs": shape.get("paragraphs", []),
                    "table_rows": shape.get("table_rows", []),
                }
