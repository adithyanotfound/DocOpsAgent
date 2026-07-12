import logging
from docx.shared import Pt, Inches

log = logging.getLogger(__name__)

def extract_metadata(doc) -> dict:
    """Extracts core properties as a metadata dictionary."""
    try:
        cp = doc.core_properties
        return {
            "title": cp.title or "",
            "author": cp.author or "",
            "subject": cp.subject or "",
            "keywords": cp.keywords or "",
        }
    except Exception as e:
        log.error(f"Failed to extract metadata: {e}")
        return {}

def extract_advanced_paragraph_style(para) -> dict:
    """Extracts advanced paragraph styles (indents, pagination)."""
    style = {}
    try:
        pf = para.paragraph_format
        if pf.left_indent is not None:
            style["left_indent_pt"] = pf.left_indent.pt
        if pf.right_indent is not None:
            style["right_indent_pt"] = pf.right_indent.pt
        if pf.first_line_indent is not None:
            style["first_line_indent_pt"] = pf.first_line_indent.pt
        if pf.keep_with_next is not None:
            style["keep_with_next"] = pf.keep_with_next
        if pf.keep_together is not None:
            style["keep_together"] = pf.keep_together
    except Exception as e:
        pass
    return style

def apply_metadata(doc, params: dict) -> str:
    """Updates the core properties of the document."""
    try:
        cp = doc.core_properties
        if "title" in params:
            cp.title = params["title"]
        if "author" in params:
            cp.author = params["author"]
        if "subject" in params:
            cp.subject = params["subject"]
        if "keywords" in params:
            cp.keywords = params["keywords"]
        return "Updated document metadata."
    except Exception as e:
        log.error(f"Failed to update metadata: {e}")
        return f"Failed to update metadata: {e}"

def apply_section_formatting(doc, target_id: str, params: dict) -> str:
    """Updates page size, orientation, and margins for a section."""
    try:
        # Default to first section if no target specified
        section = doc.sections[0]
        
        # In python-docx, sections are usually accessed by index. 
        # If target_id is something like "section_0", we can parse it.
        if target_id and target_id.startswith("section_"):
            try:
                idx = int(target_id.split("_")[1])
                if 0 <= idx < len(doc.sections):
                    section = doc.sections[idx]
            except ValueError:
                pass
                
        action = params.get("action")
        if action == "set_margins":
            margins = params.get("margins", {})
            if "top_inches" in margins:
                section.top_margin = Inches(margins["top_inches"])
            if "bottom_inches" in margins:
                section.bottom_margin = Inches(margins["bottom_inches"])
            if "left_inches" in margins:
                section.left_margin = Inches(margins["left_inches"])
            if "right_inches" in margins:
                section.right_margin = Inches(margins["right_inches"])
            return f"Updated margins for section."
            
        elif action == "set_page_size":
            from docx.enum.section import WD_ORIENT
            orientation = params.get("orientation", "portrait").lower()
            if orientation == "landscape":
                section.orientation = WD_ORIENT.LANDSCAPE
                # Swap width and height if changing to landscape
                if section.page_width < section.page_height:
                    section.page_width, section.page_height = section.page_height, section.page_width
            else:
                section.orientation = WD_ORIENT.PORTRAIT
                if section.page_width > section.page_height:
                    section.page_width, section.page_height = section.page_height, section.page_width
            
            # Optional: set explicit width/height
            if "width_inches" in params:
                section.page_width = Inches(params["width_inches"])
            if "height_inches" in params:
                section.page_height = Inches(params["height_inches"])
                
            return f"Updated page setup for section."
            
        return "No section action performed."
    except Exception as e:
        log.error(f"Failed to apply section formatting: {e}")
        return f"Failed to apply section formatting: {e}"

def apply_global_style(doc, params: dict) -> str:
    """Modifies a global named style in the document."""
    try:
        style_name = params.get("style_name")
        if not style_name or style_name not in doc.styles:
            return f"Style '{style_name}' not found."
            
        style = doc.styles[style_name]
        
        if "font_name" in params:
            style.font.name = params["font_name"]
        if "font_size_pt" in params:
            style.font.size = Pt(params["font_size_pt"])
        if "bold" in params:
            style.font.bold = params["bold"]
        if "color_hex" in params:
            from docx.shared import RGBColor
            c = str(params["color_hex"]).strip().lstrip("#")
            if len(c) == 6:
                style.font.color.rgb = RGBColor(int(c[:2], 16), int(c[2:4], 16), int(c[4:], 16))
                
        return f"Updated global style '{style_name}'."
    except Exception as e:
        log.error(f"Failed to apply global style: {e}")
        return f"Failed to apply global style: {e}"
