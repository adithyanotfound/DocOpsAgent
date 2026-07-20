"""Content Enricher — post-processes generated operations to fill in substantive content.

Two responsibilities:
1. **Section content generation**: Any `insert_block` operation whose `data` array is
   empty or contains only placeholder text gets replaced with real, contextually relevant
   content via a single batched LLM call.

2. **Visible ToC generation**: `insert_toc` operations are converted into visible
   2-column tables (Section | Page) built from the document's actual heading structure,
   so the ToC renders correctly in the PDF preview (Word's native TOC field requires
   a field update, which never happens during server-side conversion).

Uses LLMClient for provider-agnostic LLM calls (Gemini by default).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Placeholder detection
# ---------------------------------------------------------------------------

_PLACEHOLDER_MARKERS = (
    "[placeholder]", "[content]", "[text]", "[add", "[insert",
    "lorem ipsum", "placeholder", "content goes here", "...", "tbd", "n/a",
)


def _is_placeholder(text: str) -> bool:
    t = text.strip().lower()
    if not t:
        return True
    return any(m in t for m in _PLACEHOLDER_MARKERS)


def _block_needs_enrichment(op: dict) -> bool:
    """Return True if this insert_block op needs content generation."""
    if op.get("op_type") != "layout_op":
        return False
    params = op.get("parameters", {})
    if params.get("action") != "insert_block":
        return False
    data = params.get("data", [])
    if not data:
        return True
    body_items = [d for d in data if d.get("role") != "heading"]
    if not body_items:
        return True

    for d in body_items:
        if d.get("role") in ("table", "toc_field") and (d.get("rows") or d.get("headers") or d.get("field_text")):
            return False
        if d.get("text", "").strip() and not _is_placeholder(d.get("text", "")):
            return False

    return True


# ---------------------------------------------------------------------------
# Document context extraction
# ---------------------------------------------------------------------------

def _extract_document_context(structure: dict) -> dict:
    """Pull a concise context object from the document structure with cumulative word-count page estimation."""
    dom_children = structure.get("dom", {}).get("children", [])

    headings: list[dict] = []
    body_samples: list[str] = []
    cumulative_words = 0

    for el in dom_children:
        text = el.get("text", "").strip()
        word_count = len(text.split()) if text else 0
        if el.get("type") == "table":
            for row in el.get("rows", []):
                for cell in row:
                    word_count += len(str(cell).split())

        role = el.get("role", "body")
        if el.get("type") == "paragraph" and role == "heading" and text:
            est_page = max(1, (cumulative_words // 400) + 1)
            headings.append({
                "text": text,
                "level": el.get("heading_level", 1),
                "estimated_page": est_page,
            })
        elif role == "body" and text and len(body_samples) < 6:
            body_samples.append(text[:300])

        cumulative_words += word_count

    return {
        "headings": headings,
        "body_samples": body_samples,
    }


def _build_doc_summary(ctx: dict) -> str:
    """Build a short human-readable document summary for the LLM."""
    parts = []
    if ctx["headings"]:
        heading_list = ", ".join(h["text"] for h in ctx["headings"][:10])
        parts.append(f"Document sections: {heading_list}.")
    if ctx["body_samples"]:
        parts.append("Excerpt: " + " ".join(ctx["body_samples"][:2])[:400])
    return " ".join(parts) if parts else "General business document."


# ---------------------------------------------------------------------------
# Native Word ToC generation
# ---------------------------------------------------------------------------

def _sanitize_bookmark_name(name: str) -> str:
    import re
    cleaned = re.sub(r'[^a-zA-Z0-9_]', '_', str(name))
    if not cleaned or not (cleaned[0].isalpha() or cleaned[0] == '_'):
        cleaned = '_' + cleaned
    return cleaned[:40]


def _make_visible_toc(insert_toc_op: dict, ctx: dict) -> dict:
    """Convert an insert_toc op into an insert_block op with a 2-column table and PAGEREF fields."""
    before_id = insert_toc_op.get("parameters", {}).get("before_id")
    after_id = insert_toc_op.get("parameters", {}).get("after_id")

    headings = ctx.get("headings", [])
    if not headings:
        headings = [{"text": "No headings found", "level": 1, "estimated_page": 1}]

    rows = []
    for i, h in enumerate(headings):
        indent = "\u00a0\u00a0\u00a0\u00a0" * (h.get("level", 1) - 1)
        h_id = h.get("heading_id") or f"heading_{i+1}"
        bmk_name = f"_Ref_{_sanitize_bookmark_name(h_id)}"
        page_val = str(h.get("estimated_page", 1))
        heading_text = f"{indent}{h['text']}"
        rows.append([
            heading_text,
            {
                "pageref": bmk_name,
                "page": page_val,
                "heading_id": h.get("heading_id"),
            }
        ])

    data = [
        {"role": "heading", "text": "Table of Contents", "heading_level": 1},
        {
            "role": "table",
            "headers": ["Section", "Page"],
            "rows": rows,
            "style": "toc",
        },
    ]

    params: dict = {"action": "insert_block", "data": data}
    if before_id:
        params["before_id"] = before_id
    elif after_id:
        params["after_id"] = after_id

    return {"op_type": "layout_op", "target_id": None, "parameters": params}


# ---------------------------------------------------------------------------
# Batched content generation via LLM
# ---------------------------------------------------------------------------

def _enrich_sections_with_llm(
    ops_needing_enrichment: list[tuple[int, dict]],
    doc_summary: str,
    original_request: str,
    llm,
) -> dict[int, list[dict]]:
    """Call LLM once to generate content for all insert_block ops that need it."""
    from app.services.llm_client import LLMRequest

    sections = []
    for idx, op in ops_needing_enrichment:
        data = op.get("parameters", {}).get("data", [])
        headings_in_op = [d["text"] for d in data if d.get("role") == "heading" and d.get("text")]
        section_title = headings_in_op[0] if headings_in_op else "New Section"
        sections.append({"index": idx, "title": section_title})

    response = llm.complete(LLMRequest(
        system_prompt=(
            "You are a professional document writer. Given a document's context and a list of new sections "
            "that need to be added, generate substantive, professional content for each section.\n\n"
            "RULES:\n"
            "1. For each section, produce 2-3 paragraphs of real, relevant content based on the document context.\n"
            "2. Content must be specific to the document's domain — NOT generic filler or placeholders.\n"
            "3. Each paragraph should be 2-5 sentences.\n"
            "4. For a 'Conclusion' section: summarize the key points from the document and suggest next steps.\n"
            "5. For a 'Risks and Challenges' section: identify specific risks relevant to the document's domain.\n"
            "6. For any other section: write content appropriate to the section title and document context.\n"
            "7. Return ONLY a valid JSON object:\n"
            "   {\n"
            '     "sections": [\n'
            "       {\n"
            '         "index": <number>,\n'
            '         "title": "<section title>",\n'
            '         "data": [\n'
            '           {"role": "heading", "text": "<title>", "heading_level": 2},\n'
            '           {"role": "body", "text": "<paragraph 1>"},\n'
            '           {"role": "body", "text": "<paragraph 2>"},\n'
            '           {"role": "body", "text": "<paragraph 3>"}\n'
            "         ]\n"
            "       }\n"
            "     ]\n"
            "   }\n"
            "8. Do NOT include markdown fences, commentary, or any text outside the JSON object."
        ),
        user_prompt=(
            f"Document context:\n{doc_summary}\n\n"
            f"User's original request: {original_request}\n\n"
            f"Sections needing content:\n{json.dumps(sections, indent=2)}\n\n"
            f"Current date: {datetime.now().strftime('%B %d, %Y')}\n\n"
            "Generate professional, relevant content for each section above."
        ),
        temperature=0.4,
        max_tokens=4096,
        json_mode=True,
    ))

    parsed = response.json or {}
    result: dict[int, list[dict]] = {}
    for section in parsed.get("sections", []):
        idx = section.get("index")
        data = section.get("data", [])
        if isinstance(idx, int) and isinstance(data, list):
            result[idx] = data

    return result


def _format_kb_chunks_for_enricher(chunks: list[dict]) -> str:
    lines = []
    for i, c in enumerate(chunks[:15]):
        meta = c.get("metadata", {})
        source = meta.get("source") or meta.get("doc_id") or "KB Document"
        page = meta.get("page", "")
        loc = f"[{source}"
        if page:
            loc += f", p.{page}"
        loc += "]"
        lines.append(f"[chunk:{i+1}] {loc}\n{c.get('text', '')[:1200]}")
    return "\n\n".join(lines)


def _enrich_sections_with_kb_grounding(
    ops_needing_enrichment: list[tuple[int, dict]],
    doc_summary: str,
    original_request: str,
    kb_evidence: list[dict],
    llm,
) -> dict[int, list[dict]]:
    """Generate content for insert_block ops grounded strictly in provided KB evidence."""
    from app.services.llm_client import LLMRequest

    sections = []
    for idx, op in ops_needing_enrichment:
        data = op.get("parameters", {}).get("data", [])
        headings_in_op = [d["text"] for d in data if d.get("role") == "heading" and d.get("text")]
        section_title = headings_in_op[0] if headings_in_op else "New Section"
        sections.append({"index": idx, "title": section_title})

    formatted_kb = _format_kb_chunks_for_enricher(kb_evidence)

    system_prompt = (
        "You are an expert content writer for professional corporate and audit documents.\n"
        "Your task: Generate substantive content for new document section(s) based STRICTLY on the provided Knowledge Base evidence.\n\n"
        "RULES:\n"
        "1. STRICT GROUNDING: Every factual claim (numbers, percentages, dates, names, statistics, initiatives) MUST come directly from the provided KB context.\n"
        "2. If data for a specific topic is NOT in the KB evidence, do NOT fabricate or estimate details. Just write about what you DO have evidence for.\n"
        "3. NEVER write 'information not available', 'N/A', 'data not provided', or any disclaimers. Just omit topics without data.\n"
        "4. INLINE CITATION: Include citation tags [chunk:N] where N matches the chunk number in the KB CONTEXT when stating factual claims from evidence.\n"
        "5. PROFESSIONAL TONE: Formal, concise, third-person language.\n"
        "6. Return ONLY a valid JSON object:\n"
        "   {\n"
        '     "sections": [\n'
        "       {\n"
        '         "index": <number>,\n'
        '         "title": "<section title>",\n'
        '         "data": [\n'
        '           {"role": "heading", "text": "<title>", "heading_level": 2},\n'
        '           {"role": "body", "text": "<paragraph 1> [chunk:1]"},\n'
        '           {"role": "body", "text": "<paragraph 2> [chunk:2]"}\n'
        "         ]\n"
        "       }\n"
        "     ]\n"
        "   }\n"
        "7. Do NOT include markdown fences, commentary, or any text outside the JSON object."
    )

    user_prompt = (
        f"Document Summary:\n{doc_summary}\n\n"
        f"User Request: {original_request}\n\n"
        f"KB CONTEXT (Ground your content ONLY in these excerpts):\n{formatted_kb}\n\n"
        f"Sections needing content:\n{json.dumps(sections, indent=2)}\n\n"
        "Generate professional, grounded content JSON."
    )

    response = llm.complete(LLMRequest(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=0.2,
        max_tokens=4096,
        json_mode=True,
    ))

    parsed = response.json or {}
    result: dict[int, list[dict]] = {}
    for section in parsed.get("sections", []):
        idx = section.get("index")
        data = section.get("data", [])
        if isinstance(idx, int) and isinstance(data, list):
            clean_data = []
            for item in data:
                if isinstance(item, dict) and "text" in item:
                    item_copy = dict(item)
                    item_copy["text"] = re.sub(r'\s*\[chunk:\d+\]', '', str(item["text"]))
                    clean_data.append(item_copy)
                else:
                    clean_data.append(item)
            result[idx] = clean_data

    return result


# ---------------------------------------------------------------------------
# Main enricher class
# ---------------------------------------------------------------------------

class ContentEnricher:
    """Post-processes operations to fill in substantive content and fix ToC.

    Constructor no longer takes api_key/base_url/llm_model directly.
    Pass a pre-built LLMClient instance (or leave None to have one built lazily).
    """

    def __init__(self, llm=None) -> None:
        self._llm = llm

    def enrich(
        self,
        operations: list[dict],
        structure: dict,
        document_type: str,
        original_request: str,
        task: dict | None = None,
    ) -> list[dict]:
        """Enrich operations in-place and return the updated list.

        Only does work for DOCX documents. PPTX is a pass-through.
        """
        if document_type != "docx":
            return operations

        ctx = _extract_document_context(structure)
        doc_summary = _build_doc_summary(ctx)

        enriched = list(operations)

        # ---- Step 1: Convert insert_toc → visible table -----------------
        for i, op in enumerate(enriched):
            if op.get("op_type") == "layout_op":
                params = op.get("parameters", {})
                if params.get("action") == "insert_toc":
                    enriched[i] = _make_visible_toc(op, ctx)
                    log.info(
                        "ContentEnricher: converted insert_toc → visible table at op index %d", i
                    )

        # ---- Step 2: Fill in empty/thin insert_block sections -----------
        ops_needing = [
            (i, op) for i, op in enumerate(enriched)
            if _block_needs_enrichment(op)
        ]

        if not ops_needing:
            return enriched

        log.info(
            "ContentEnricher: %d insert_block op(s) need content enrichment",
            len(ops_needing),
        )

        from app.services.llm_client import LLMClient
        llm = self._llm or LLMClient()

        try:
            kb_evidence = task.get("kb_evidence") if task else None
            if kb_evidence:
                enriched_data_map = _enrich_sections_with_kb_grounding(
                    ops_needing_enrichment=ops_needing,
                    doc_summary=doc_summary,
                    original_request=original_request,
                    kb_evidence=kb_evidence,
                    llm=llm,
                )
            else:
                enriched_data_map = _enrich_sections_with_llm(
                    ops_needing_enrichment=ops_needing,
                    doc_summary=doc_summary,
                    original_request=original_request,
                    llm=llm,
                )
            for idx, new_data in enriched_data_map.items():
                if new_data:
                    enriched[idx]["parameters"]["data"] = new_data
                    log.info(
                        "ContentEnricher: enriched op %d with %d content items",
                        idx, len(new_data),
                    )
        except Exception as exc:
            log.warning(
                "ContentEnricher: LLM call failed — %s. Using original data.", exc
            )

        return enriched
