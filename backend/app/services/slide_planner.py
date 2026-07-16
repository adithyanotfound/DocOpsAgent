"""Slide Planner — generates a structured multi-slide content plan via LLM.

Given a template's rich structure and a user request, the planner produces
a complete slide plan in a single LLM call. The plan specifies:
- Which template slides to use as layout sources.
- Whether to populate, keep, or delete each slide.
- Text content for every text frame, with optional formatting overrides.

Uses LLMClient for provider-agnostic LLM calls (Gemini by default).
"""
from __future__ import annotations

import concurrent.futures
import json
import logging

from app.core.config import settings

log = logging.getLogger(__name__)

MAX_SLIDES = 20  # Hard cap on generated slides


class SlidePlanner:
    """Generates a structured slide plan from a template + user request."""

    def __init__(self, llm=None) -> None:
        self._llm = llm

    def plan(
        self,
        request: str,
        template_structure: dict,
        intent: dict,
        chat_history: list[dict] | None = None,
    ) -> dict:
        """Produce a slide plan dict."""
        if not (settings.gemini_api_key or settings.openai_api_key):
            return self._plan_local(request, template_structure, intent)

        # Generate outline
        outline = self._generate_outline(
            request=request,
            template_structure=template_structure,
            intent=intent,
            chat_history=chat_history or [],
        )

        if not outline:
            return self._plan_local(request, template_structure, intent)

        # Process each slide's content (populate, delete, keep)
        def process_slide(slide_outline: dict) -> dict:
            action = slide_outline.get("action", "populate")
            src_idx = slide_outline.get("source_slide_index", 1)

            if action == "delete":
                return {
                    "source_slide_index": src_idx,
                    "action": "delete",
                    "shapes": [],
                }
            if action == "keep":
                return {
                    "source_slide_index": src_idx,
                    "action": "keep",
                    "shapes": [],
                }

            return self._generate_slide_content(
                request=request,
                template_structure=template_structure,
                slide_outline=slide_outline,
                intent=intent
            )

        # Run concurrent generation (max 10 workers for speed)
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(process_slide, outline))

        # Build final plan
        plan = {"slides": results}
        
        # Validate and sanitise
        return self._sanitise_plan(plan, template_structure)

    def _generate_outline(
        self, request: str, template_structure: dict, intent: dict, chat_history: list[dict]
    ) -> list[dict]:
        from app.services.llm_client import LLMClient, LLMRequest

        llm = self._llm or LLMClient()

        template_desc = self._describe_template(template_structure)
        topic = intent.get("topic", "")

        slide_count = intent.get("slide_count")
        delete_slides = intent.get("delete_slides", [])
        add_count = intent.get("add_slides_count")
        existing_slide_count = template_structure.get("slide_count", 1)

        count_guidance = ""
        if delete_slides:
            count_guidance = f"The user wants to DELETE slides: {delete_slides}. Remove them from the output."
        elif add_count:
            count_guidance = f"Keep existing {existing_slide_count} slides and add {add_count} new ones."
        elif slide_count:
            count_guidance = f"Target exactly {min(slide_count, MAX_SLIDES)} slides."
        else:
            count_guidance = f"Template has {existing_slide_count} slides. Use 5–12 slides depending on topic depth."

        history_str = ""
        if chat_history:
            history_str = "Previous conversation:\n"
            for msg in chat_history[-5:]:
                role = "User" if msg["role"] == "user" else "Agent"
                history_str += f"{role}: {msg['content']}\n"
            history_str += "\n"

        system_prompt = (
            "You are an expert presentation outliner. Your job is to define the structure of a presentation.\n\n"
            "Given a user request, a topic, and a list of available template slide layouts, generate a slide-by-slide outline.\n\n"
            "RULES:\n"
            "1. Choose the best template layout for each slide (source_slide_index).\n"
            "2. Define the exact action ('populate', 'keep', 'delete').\n"
            "3. For 'populate' slides, write a detailed 'outline' containing the main talking points, bullet points, and data to be included on this specific slide.\n\n"
            "OUTPUT FORMAT: Return a JSON object with a key 'outline' containing an array of slides:\n"
            "Each slide: { source_slide_index: int, action: string, outline: string (detailed bullet points of what goes on the slide) }\n\n"
            "Return ONLY valid JSON."
        )

        user_prompt = (
            f"{history_str}User request: {request}\nTopic: {topic}\nSlide count guidance: {count_guidance}\n\n"
            f"Available Layouts:\n{template_desc}"
        )

        response = llm.complete(LLMRequest(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.4,
            max_tokens=4096,
            json_mode=True,
        ))
        
        parsed = response.json or {}
        return parsed.get("outline", [])

    def _generate_slide_content(
        self, request: str, template_structure: dict, slide_outline: dict, intent: dict
    ) -> dict:
        from app.services.llm_client import LLMClient, LLMRequest

        llm = self._llm or LLMClient()

        src_idx = slide_outline.get("source_slide_index", 1)
        action = slide_outline.get("action", "populate")
        outline_text = slide_outline.get("outline", "")
        topic = intent.get("topic", "")

        slides = template_structure.get("slides", [])
        slide_info = next((s for s in slides if s["slide_index"] == src_idx), slides[0] if slides else {})
        layout_desc = self._describe_template({"slides": [slide_info]})

        system_prompt = (
            "You are an expert copywriter and presentation designer. Your task is to write deep, substantive content "
            "for a SINGLE slide, matching its exact layout structure.\n\n"
            "CRITICAL DESIGN RULES:\n"
            "1. NO WALLS OF TEXT. Never write long paragraphs. For body text placeholders, you MUST use short, scannable bullet points.\n"
            "2. PREFIX BULLETS. Prefix each bullet point with the bullet character '• '. Keep bullets under 2 sentences.\n"
            "3. PREVENT OVERLAP (FONT SIZE). If your generated text is longer than the original placeholder text, you MUST "
            "drastically decrease the `font_size_pt` (e.g., from 44 down to 24, or 18 to 12). If you do not reduce the font size, your text will overlap other shapes and ruin the slide!\n"
            "4. PREVENT WEIRD SPACING (ALIGNMENT). Template titles often use 'distributed' alignment which puts massive spaces between letters of long words. "
            "You MUST override the `alignment` property to `'left'` or `'center'` for any title or heading you modify.\n"
            "5. To populate a table, include a 'table_rows' array where each element is an array of cells, and each cell has a 'paragraphs' array.\n\n"
            "FORMATTING OVERRIDES (null = keep template default):\n"
            "- font_size_pt: number or null\n"
            "- bold: true/false or null\n"
            "- italic: true/false or null\n"
            "- color_hex: 6-char hex string (e.g. '2B579A') or null\n"
            "- alignment: 'left'|'center'|'right'|'justify' or null\n\n"
            "OUTPUT FORMAT: Return a JSON object with a key 'shapes' containing an array:\n"
            "[{shape_index, paragraphs: [{para_index, text, formatting}], table_rows: [[{paragraphs: ...}]]}]\n\n"
            "Return ONLY valid JSON."
        )

        user_prompt = (
            f"Topic: {topic}\nSlide Outline to write content for:\n{outline_text}\n\n"
            f"Template Layout Shapes for this slide:\n{layout_desc}"
        )

        response = llm.complete(LLMRequest(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.5,
            max_tokens=4096,
            json_mode=True,
        ))
        
        parsed = response.json or {}
        shapes = parsed.get("shapes", [])
        
        return {
            "source_slide_index": src_idx,
            "action": action,
            "shapes": shapes
        }

    # ------------------------------------------------------------------
    # Local fallback (no LLM)
    # ------------------------------------------------------------------

    def _plan_local(self, request: str, template_structure: dict, intent: dict) -> dict:
        """Simple rule-based plan: keep all slides, populate with basic text."""
        slides = template_structure.get("slides", [])
        delete_slides = set(intent.get("delete_slides", []))
        topic = intent.get("topic", "") or request

        plan_slides: list[dict] = []
        for slide_data in slides:
            slide_idx = slide_data["slide_index"]

            if slide_idx in delete_slides:
                plan_slides.append({
                    "source_slide_index": slide_idx,
                    "action": "delete",
                    "shapes": [],
                })
                continue

            shapes: list[dict] = []
            for shape in slide_data.get("shapes", []):
                if not shape.get("has_text_frame"):
                    continue
                paras = []
                for para in shape.get("paragraphs", []):
                    text = para.get("text", "")
                    if not text:
                        text = topic[:80]
                    paras.append({
                        "para_index": para["para_index"],
                        "text": text,
                        "formatting": {},
                    })
                if paras:
                    shapes.append({
                        "shape_index": shape["shape_index"],
                        "paragraphs": paras,
                    })

            plan_slides.append({
                "source_slide_index": slide_idx,
                "action": "populate",
                "shapes": shapes,
            })

        return {"slides": plan_slides}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _describe_template(self, structure: dict) -> str:
        """Create a concise text description of the template for the LLM prompt."""
        lines: list[str] = []
        slides = structure.get("slides", [])
        lines.append(f"Total template slides: {len(slides)}")

        for slide in slides:
            slide_idx = slide["slide_index"]
            layout = slide.get("layout_name", "unknown")
            lines.append(f"\n--- Slide {slide_idx} (layout: {layout}) ---")

            for shape in slide.get("shapes", []):
                has_tf = shape.get("has_text_frame")
                has_t = shape.get("has_table")
                if not (has_tf or has_t):
                    continue
                shape_idx = shape["shape_index"]
                name = shape.get("shape_name", "?")
                is_ph = shape.get("is_placeholder", False)
                ph_type = shape.get("placeholder_type", "")

                ph_str = f", placeholder: {ph_type}" if is_ph else ""
                lines.append(f"  Shape {shape_idx} ({name}{ph_str}):")

                if has_tf:
                    for para in shape.get("paragraphs", []):
                        pi = para["para_index"]
                        text = para.get("text", "")
                        fmt = para.get("formatting", {})
                        fmt_parts = []
                        if fmt.get("font_size_pt"):
                            fmt_parts.append(f"{fmt['font_size_pt']}pt")
                        if fmt.get("bold"):
                            fmt_parts.append("bold")
                        if fmt.get("alignment"):
                            fmt_parts.append(fmt["alignment"])
                        fmt_str = f" [{', '.join(fmt_parts)}]" if fmt_parts else ""
                        display_text = text if text else "(empty)"
                        if len(display_text) > 80:
                            display_text = display_text[:77] + "..."
                        lines.append(f"    Para {pi}: \"{display_text}\"{fmt_str}")
                
                if has_t:
                    table_rows = shape.get("table_rows", [])
                    lines.append(f"    [TABLE with {len(table_rows)} rows]")
                    for r_idx, row in enumerate(table_rows):
                        row_texts = []
                        for cell in row:
                            cell_text = " ".join(p.get("text", "") for p in cell.get("paragraphs", []))
                            if len(cell_text) > 20:
                                  cell_text = cell_text[:17] + "..."
                            row_texts.append(cell_text if cell_text else "(empty)")
                        lines.append(f"      Row {r_idx}: {row_texts}")

        return "\n".join(lines)

    def _sanitise_plan(self, plan: dict, template_structure: dict) -> dict:
        """Validate and sanitise an LLM-generated plan."""
        slides = plan.get("slides", [])
        template_slide_count = template_structure.get("slide_count", 1)

        sanitised: list[dict] = []
        for slide in slides[:MAX_SLIDES]:
            src = slide.get("source_slide_index", 1)
            if not isinstance(src, int) or src < 1 or src > template_slide_count:
                src = 1

            action = slide.get("action", "populate")
            if action not in ("populate", "keep", "delete"):
                action = "populate"

            shapes: list[dict] = []
            for shape in slide.get("shapes", []):
                si = shape.get("shape_index", 0)
                if not isinstance(si, int) or si < 0:
                    continue
                paras: list[dict] = []
                for para in (shape.get("paragraphs") or []):
                    pi = para.get("para_index", 0)
                    text = str(para.get("text", ""))
                    fmt = para.get("formatting") or {}
                    if not isinstance(fmt, dict):
                        fmt = {}
                    paras.append({
                        "para_index": pi,
                        "text": text,
                        "formatting": {
                            "font_size_pt": fmt.get("font_size_pt"),
                            "bold": fmt.get("bold"),
                            "italic": fmt.get("italic"),
                            "color_hex": fmt.get("color_hex"),
                            "alignment": fmt.get("alignment"),
                        },
                    })
                table_rows = []
                for row in (shape.get("table_rows") or []):
                    if not isinstance(row, list):
                        continue
                    clean_row = []
                    for cell in row:
                        if not isinstance(cell, dict):
                            continue
                        cell_paras = []
                        for para in (cell.get("paragraphs") or []):
                            pi = para.get("para_index", 0)
                            text = str(para.get("text", ""))
                            fmt = para.get("formatting") or {}
                            if not isinstance(fmt, dict):
                                fmt = {}
                            cell_paras.append({
                                "para_index": pi,
                                "text": text,
                                "formatting": {
                                    "font_size_pt": fmt.get("font_size_pt"),
                                    "bold": fmt.get("bold"),
                                    "italic": fmt.get("italic"),
                                    "color_hex": fmt.get("color_hex"),
                                    "alignment": fmt.get("alignment"),
                                },
                            })
                        clean_row.append({"paragraphs": cell_paras})
                    table_rows.append(clean_row)

                clean_shape = {
                    "shape_index": si,
                    "paragraphs": paras,
                }
                if table_rows:
                    clean_shape["table_rows"] = table_rows
                
                shapes.append(clean_shape)

            sanitised.append({
                "source_slide_index": src,
                "action": action,
                "shapes": shapes,
            })

        return {"slides": sanitised}
