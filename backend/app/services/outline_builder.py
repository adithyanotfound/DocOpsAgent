"""Document Outline Builder — builds a hierarchical semantic outline of a document.

Replaces the flat, lossy 80-character truncated structure summary.
Supports both DOCX and PPTX formats.
Provides pre-built lookup maps for deterministic ordinal/named/semantic resolution.
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)


class OutlineBuilder:
    """Builds a hierarchical semantic outline of a document.

    Outputs a structured outline representation that can be fed to the
    Task Planner and Reference Resolver.
    """

    @classmethod
    def build(cls, structure: dict, document_type: str) -> dict:
        """Build the outline.

        Returns a dictionary containing:
        - document_type (str)
        - title (str)
        - element_count (int)
        - sections (list[dict])
        - indices (dict): lookup tables for structural/ordinal resolution
        """
        if document_type == "pptx":
            return cls._build_pptx(structure)
        return cls._build_docx(structure)

    @classmethod
    def _build_docx(cls, structure: dict) -> dict:
        dom_children = structure.get("dom", {}).get("children", [])
        blocks = structure.get("blocks", [])

        sections: list[dict] = []
        indices: dict[str, Any] = {
            "tables_by_ordinal": {},
            "images_by_ordinal": {},
            "headings_by_name": {},
            "last_element_id": None,
            "first_content_id": None,
            "all_elements": [],
        }

        # If dom is empty, construct outline from flat blocks list
        if not dom_children:
            return cls._build_docx_fallback(blocks)

        # Pre-pass to populate index mappings and ordinal counts
        table_counter = 0
        image_counter = 0
        last_id = None
        first_id = None

        # Build flat list of all elements first for easy indexing
        flat_elements = []
        for el in dom_children:
            el_type = el.get("type", "paragraph")
            el_id = el.get("id")
            
            if not el_id:
                continue
            
            if el_type not in ("metadata", "section"):
                last_id = el_id
                if first_id is None:
                    first_id = el_id

            el_summary = {
                "id": el_id,
                "type": el_type,
                "body_index": el.get("body_index"),
            }

            if el_type == "paragraph":
                role = el.get("role", "body")
                text = el.get("text", "").strip()
                el_summary["role"] = role
                el_summary["text_preview"] = text[:120]
                el_summary["text_length"] = len(text)
                
                # Extract style summary
                styles = set()
                for run in el.get("runs", []):
                    s = run.get("style", {})
                    if s.get("bold"): styles.add("bold")
                    if s.get("italic"): styles.add("italic")
                    if s.get("underline"): styles.add("underline")
                    if s.get("strike"): styles.add("strikethrough")
                    if s.get("highlight"): styles.add(f"highlight:{s['highlight']}")
                    if s.get("color"): styles.add(f"color:{s['color']}")
                    if s.get("size"):
                        try:
                            # Round to nearest integer to avoid trivial diff failures
                            styles.add(f"size:{round(float(s['size']))}")
                        except (ValueError, TypeError):
                            styles.add(f"size:{s['size']}")
                    if s.get("font"): styles.add(f"font:{s['font']}")
                
                # Also extract paragraph-level styles that verifier might care about
                p_style = el.get("style", {})
                if p_style.get("line_spacing"):
                    try:
                        styles.add(f"spacing:{round(float(p_style['line_spacing']), 1)}")
                    except (ValueError, TypeError):
                        pass
                if p_style.get("page_break_before"):
                    styles.add("page_break_before")
                if p_style.get("space_before_pt") is not None:
                    styles.add(f"space_before:{round(float(p_style['space_before_pt']))}pt")
                if p_style.get("space_after_pt") is not None:
                    styles.add(f"space_after:{round(float(p_style['space_after_pt']))}pt")
                if p_style.get("left_indent_pt") is not None:
                    styles.add(f"left_indent:{round(float(p_style['left_indent_pt']))}pt")
                if p_style.get("right_indent_pt") is not None:
                    styles.add(f"right_indent:{round(float(p_style['right_indent_pt']))}pt")
                if p_style.get("first_line_indent_pt") is not None:
                    styles.add(f"first_line_indent:{round(float(p_style['first_line_indent_pt']))}pt")
                if p_style.get("keep_with_next"):
                    styles.add("keep_with_next")
                if p_style.get("keep_together"):
                    styles.add("keep_together")

                list_info = el.get("list_info")
                if list_info:
                    if list_info.get("list_type"):
                        styles.add(f"list_type:{list_info.get('list_type')}")
                    if list_info.get("num_fmt"):
                        styles.add(f"list_fmt:{list_info.get('num_fmt')}")
                    if list_info.get("lvl_text"):
                        styles.add(f"list_lvl_text:{list_info.get('lvl_text')}")

                if styles:
                    el_summary["style_summary"] = sorted(list(styles))
                if p_style.get("alignment"):
                    el_summary["alignment"] = p_style["alignment"]

                if role == "heading":
                    hlvl = el.get("heading_level", 1)
                    el_summary["heading_level"] = hlvl
                    # Add to headings index
                    normalized_heading = text.lower().strip()
                    if normalized_heading:
                        indices["headings_by_name"][normalized_heading] = el_id
            elif el_type == "table":
                table_counter += 1
                rows = el.get("row_count", len(el.get("rows", [])))
                cols = el.get("col_count", 0)
                el_summary["role"] = "table"
                el_summary["ordinal_label"] = f"Table {table_counter}"
                el_summary["rows"] = rows
                el_summary["cols"] = cols
                t_style = el.get("style", {})
                t_styles = []
                if t_style.get("style_name"):
                    t_styles.append(f"table_style:{t_style['style_name']}")
                if t_style.get("has_custom_borders"):
                    t_styles.append("custom_borders")
                if t_style.get("border_colors"):
                    t_styles.append(f"border_colors:{','.join(t_style['border_colors'])}")
                
                # Check for custom cell background colors
                cell_bgs = set()
                for row in el.get("rows", []):
                    for cell in row.get("cells", []):
                        if cell.get("bg_color"):
                            cell_bgs.add(cell["bg_color"])
                if cell_bgs:
                    t_styles.append(f"cell_bgs:{','.join(list(cell_bgs))}")
                
                # Check for cell vertical alignments and paragraph alignments
                valigns = set()
                aligns = set()
                table_text = []
                for row in el.get("rows", []):
                    for cell in row.get("cells", []):
                        if cell.get("vertical_alignment"):
                            valigns.add(cell["vertical_alignment"])
                        for child in cell.get("children", []):
                            if child.get("text"):
                                table_text.append(child["text"])
                            c_style = child.get("style", {})
                            if c_style and c_style.get("alignment"):
                                aligns.add(c_style["alignment"])
                if valigns:
                    t_styles.append(f"valign:{','.join(sorted(valigns))}")
                if aligns:
                    t_styles.append(f"align:{','.join(sorted(aligns))}")

                if table_text:
                    full_text = " | ".join(table_text)
                    el_summary["text_preview"] = full_text[:120]
                    el_summary["text_length"] = len(full_text)

                if t_styles:
                    el_summary["style_summary"] = t_styles
                
                indices["tables_by_ordinal"][str(table_counter)] = el_id
            elif el_type == "image":
                image_counter += 1
                w_emu = el.get("width_emu", 0)
                h_emu = el.get("height_emu", 0)
                w_cm = round(w_emu / 360000, 1) if w_emu else "?"
                h_cm = round(h_emu / 360000, 1) if h_emu else "?"
                el_summary["role"] = "image"
                el_summary["ordinal_label"] = f"Image {image_counter}"
                el_summary["size"] = f"{w_cm}cm x {h_cm}cm"
                el_summary["alt_text"] = el.get("description", "")
                indices["images_by_ordinal"][str(image_counter)] = el_id
            elif el_type == "section":
                el_summary["role"] = "section"
                if el.get("style"):
                    el_summary["style_summary"] = [f"{k}:{v}" for k, v in el["style"].items()]
            elif el_type == "metadata":
                el_summary["role"] = "metadata"
                if el.get("properties"):
                    el_summary["style_summary"] = [f"{k}:{v}" for k, v in el["properties"].items()]
            
            flat_elements.append(el_summary)

        indices["last_element_id"] = last_id
        indices["first_content_id"] = first_id
        indices["all_elements"] = flat_elements

        # Group flat elements hierarchically into sections
        current_section = {
            "heading": "Document Start",
            "heading_id": "start",
            "semantic_type": "section",
            "heading_level": 0,
            "ordinal": 0,
            "body_index_range": [0, 0],
            "elements": [],
        }
        
        section_counter = 0

        for el in flat_elements:
            # A heading marks a new section
            if el.get("role") == "heading":
                # Save previous section if it had content/elements
                if current_section["elements"] or current_section["heading_id"] != "start":
                    if current_section["elements"]:
                        start_idx = current_section["elements"][0].get("body_index", 0)
                        end_idx = current_section["elements"][-1].get("body_index", 0)
                        current_section["body_index_range"] = [start_idx, end_idx]
                    sections.append(current_section)

                # Heuristic: detect TOC section
                text = el.get("text_preview", "")
                is_toc = False
                if "table of contents" in text.lower() or "contents" in text.lower() or "toc" == text.lower().strip():
                    is_toc = True

                section_counter += 1
                current_section = {
                    "heading": text,
                    "heading_id": el["id"],
                    "semantic_type": "toc" if is_toc else "section",
                    "heading_level": el.get("heading_level", 1),
                    "ordinal": section_counter,
                    "body_index_range": [el.get("body_index", 0), el.get("body_index", 0)],
                    "elements": [el],
                }
            else:
                current_section["elements"].append(el)

        # Append last section
        if current_section["elements"] or current_section["heading_id"] != "start":
            if current_section["elements"]:
                start_idx = current_section["elements"][0].get("body_index", 0)
                end_idx = current_section["elements"][-1].get("body_index", 0)
                current_section["body_index_range"] = [start_idx, end_idx]
            sections.append(current_section)

        # Document title is inferred from first section heading
        title = "Untitled Document"
        if sections and len(sections) > 0:
            first_sect = sections[0]
            if first_sect["heading_id"] != "start":
                title = first_sect["heading"]
            elif len(sections) > 1:
                title = sections[1]["heading"]

        return {
            "document_type": "docx",
            "title": title,
            "element_count": len(flat_elements),
            "sections": sections,
            "indices": {
                "tables_by_ordinal": indices["tables_by_ordinal"],
                "images_by_ordinal": indices["images_by_ordinal"],
                "headings_by_name": indices["headings_by_name"],
                "last_element_id": indices["last_element_id"],
                "first_content_id": indices["first_content_id"],
            }
        }

    @classmethod
    def _build_docx_fallback(cls, blocks: list) -> dict:
        """Fallback to build outline from flat blocks array."""
        flat_elements = []
        indices: dict[str, Any] = {
            "tables_by_ordinal": {},
            "images_by_ordinal": {},
            "headings_by_name": {},
            "last_element_id": None,
            "first_content_id": None,
        }

        table_counter = 0
        image_counter = 0
        last_id = None
        first_id = None

        for idx, b in enumerate(blocks):
            el_id = b.get("element_id")
            if not el_id:
                continue
            
            last_id = el_id
            if first_id is None:
                first_id = el_id

            meta = b.get("metadata", {})
            role = meta.get("role", "body")
            text = b.get("text", "").strip()

            el_summary = {
                "id": el_id,
                "type": "paragraph",
                "body_index": idx,
                "role": role,
                "text_preview": text[:120],
                "text_length": len(text),
            }

            if role == "heading":
                el_summary["heading_level"] = meta.get("heading_level", 1)
                normalized_heading = text.lower().strip()
                if normalized_heading:
                    indices["headings_by_name"][normalized_heading] = el_id
            
            flat_elements.append(el_summary)

        indices["last_element_id"] = last_id
        indices["first_content_id"] = first_id

        # Since it's fallback flat data, return a single big section
        sections = [{
            "heading": "Document Body",
            "heading_id": "body",
            "semantic_type": "section",
            "heading_level": 0,
            "ordinal": 1,
            "body_index_range": [0, len(flat_elements)],
            "elements": flat_elements,
        }]

        return {
            "document_type": "docx",
            "title": "Document (Flat fallback)",
            "element_count": len(flat_elements),
            "sections": sections,
            "indices": indices,
        }

    @classmethod
    def _build_pptx(cls, structure: dict) -> dict:
        slides = structure.get("slides", [])
        sections = []
        indices: dict[str, Any] = {
            "slides_by_index": {},
            "images_by_ordinal": {},
            "tables_by_ordinal": {},
        }

        image_counter = 0
        table_counter = 0

        for slide in slides:
            slide_idx = slide.get("slide_index", 1)
            layout = slide.get("layout_name", "unknown")
            elements = []

            for shape in slide.get("shapes", []):
                shape_idx = shape.get("shape_index", 0)
                shape_type = "shape"
                
                el_summary = {
                    "id": f"slide_{slide_idx}_shape_{shape_idx}",
                    "shape_index": shape_idx,
                    "shape_name": shape.get("shape_name", ""),
                }

                if shape.get("has_text_frame"):
                    shape_type = "text_frame"
                    text = " ".join(p.get("text", "") for p in shape.get("paragraphs", []))
                    el_summary["type"] = "text_frame"
                    el_summary["text_preview"] = text[:120]
                    el_summary["text_length"] = len(text)
                elif shape.get("has_table"):
                    shape_type = "table"
                    table_counter += 1
                    el_summary["type"] = "table"
                    el_summary["ordinal_label"] = f"Table {table_counter}"
                    el_summary["rows"] = len(shape.get("table_rows", []))
                    indices["tables_by_ordinal"][str(table_counter)] = el_summary["id"]
                elif shape.get("has_image") or "image" in shape.get("shape_name", "").lower():
                    shape_type = "image"
                    image_counter += 1
                    el_summary["type"] = "image"
                    el_summary["ordinal_label"] = f"Image {image_counter}"
                    indices["images_by_ordinal"][str(image_counter)] = el_summary["id"]

                elements.append(el_summary)

            slide_title = next((el.get("text_preview", "") for el in elements if el.get("type") == "text_frame" and el.get("text_preview")), f"Slide {slide_idx}")
            
            section = {
                "heading": slide_title,
                "heading_id": f"slide_{slide_idx}",
                "semantic_type": "slide",
                "ordinal": slide_idx,
                "layout": layout,
                "elements": elements,
            }
            sections.append(section)
            indices["slides_by_index"][str(slide_idx)] = f"slide_{slide_idx}"

        return {
            "document_type": "pptx",
            "title": "Presentation Structure",
            "element_count": len(slides),
            "sections": sections,
            "indices": indices,
        }
