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
    all_thin = all(
        not d.get("text", "").strip() or _is_placeholder(d.get("text", ""))
        for d in body_items
    )
    return all_thin


# ---------------------------------------------------------------------------
# Document context extraction
# ---------------------------------------------------------------------------

def _extract_document_context(structure: dict) -> dict:
    """Pull a concise context object from the document structure."""
    dom_children = structure.get("dom", {}).get("children", [])

    headings: list[dict] = []
    body_samples: list[str] = []

    for el in dom_children:
        if el.get("type") != "paragraph":
            continue
        role = el.get("role", "body")
        text = el.get("text", "").strip()
        if not text:
            continue
        if role == "heading":
            headings.append({
                "text": text,
                "level": el.get("heading_level", 1),
            })
        elif role == "body" and len(body_samples) < 6:
            body_samples.append(text[:300])

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
# Visible ToC generation
# ---------------------------------------------------------------------------

def _make_visible_toc(insert_toc_op: dict, ctx: dict) -> dict:
    """Convert an insert_toc op into an insert_block op with a visible 2-column table."""
    before_id = insert_toc_op.get("parameters", {}).get("before_id")
    after_id = insert_toc_op.get("parameters", {}).get("after_id")

    headings = ctx["headings"]
    if not headings:
        headings = [{"text": "No headings found", "level": 1}]

    rows = [["Section", "Page"]]
    for i, h in enumerate(headings):
        indent = "\u00a0\u00a0\u00a0\u00a0" * (h["level"] - 1)  # NBSP indent for sub-headings
        rows.append([f"{indent}{h['text']}", str(i + 1)])

    data = [
        {"role": "heading", "text": "Table of Contents", "heading_level": 1},
        {
            "role": "table",
            "headers": rows[0],
            "rows": rows[1:],
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
