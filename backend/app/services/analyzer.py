import logging
import json
from app.services.llm_client import LLMClient, LLMRequest

log = logging.getLogger(__name__)

class DocumentAnalyzer:
    """Analyzes document structure to extract high-level context (theme, purpose)."""

    def __init__(self, llm: LLMClient | None = None):
        self._llm = llm or LLMClient()

    def analyze(self, structure: dict) -> dict:
        """Analyze the document and return a summary of its style and purpose."""
        if not structure:
            return {}

        # Just take a highly simplified version of the structure to save tokens
        simplified = []
        for s in structure.get("sections", []):
            simplified.append(f"Section: {s.get('heading', 'Untitled')}")
            for el in s.get("elements", [])[:5]:  # Just first few elements
                simplified.append(f"- {el.get('type')}: {el.get('text_preview', '')}")

        doc_preview = "\n".join(simplified)
        
        system_prompt = (
            "You are an expert document analyst. "
            "Given a structural preview of a document, provide a high-level analysis.\n"
            "Identify the likely purpose of the document (e.g., Marketing Report, Business Proposal, Recipe).\n"
            "Identify the current stylistic theme if any (e.g., Formal, Modern, Minimalist).\n"
            "Provide a short 2-3 sentence summary of the document's overall context.\n"
            "Return JSON matching this schema:\n"
            "{\n"
            '  "purpose": "...",\n'
            '  "theme": "...",\n'
            '  "summary": "..."\n'
            "}"
        )

        try:
            response = self._llm.complete(LLMRequest(
                system_prompt=system_prompt,
                user_prompt=f"Document Preview:\n{doc_preview}",
                temperature=0,
                max_tokens=256,
                json_mode=True,
            ))
            return response.json or {}
        except Exception as e:
            log.error(f"Failed to analyze document: {e}")
            return {}
