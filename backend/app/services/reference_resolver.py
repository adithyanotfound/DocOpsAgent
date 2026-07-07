import json
import logging
import difflib

from app.core.config import settings

log = logging.getLogger(__name__)


class ReferenceResolver:
    """
    Stage 2 of the pipeline.
    Extracts text references and their intended structural outcome from the user request,
    then deterministically matches the text against the document structure to find object IDs.
    """

    def __init__(self):
        from openai import OpenAI
        self.client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
        )

    def resolve(self, request: str, structure: dict) -> list[dict]:
        """
        Returns a list of resolved references.
        Example:
        [
            {
                "text": "Conclusion",
                "object_id": "paragraph_17",
                "confidence": 0.98,
                "expected_state": [
                    { "type": "position", "expected": "last" },
                    { "type": "property", "property": "include_in_toc", "value": False }
                ]
            }
        ]
        """
        # Step 1: Extract mentions and expected state via LLM
        extracted = self._extract_mentions_llm(request)

        # Step 2: Flatten document tree for searching
        flat_nodes = self._flatten_dom(structure.get("dom", {}))

        resolved_refs = []
        for ref in extracted:
            text = ref.get("text", "")
            expected = ref.get("expected_state", [])
            
            if not text:
                continue

            # Step 3: Deterministic fuzzy match
            best_id, best_conf = self._fuzzy_match(text, flat_nodes)

            # If confidence is below threshold, flag as unresolved
            if best_conf < 80:
                log.warning(f"Low confidence ({best_conf}) for reference '{text}', flagging as unresolved.")
                best_id = None
            
            resolved_refs.append({
                "text": text,
                "object_id": best_id,
                "confidence": round(best_conf / 100.0, 2) if best_id else 0.0,
                "expected_state": expected
            })

        return resolved_refs

    def _extract_mentions_llm(self, request: str) -> list[dict]:
        system_prompt = (
            "You are a structural reference extractor for a document editing assistant.\n"
            "Given a user request to modify a document, extract all entities or sections the user refers to "
            "(e.g., 'Conclusion', 'the first table', 'the bulleted list').\n"
            "For each reference, also extract the intended structural outcome (`expected_state`).\n\n"
            "Valid types for expected_state:\n"
            "- position: indicates where the object should be moved (e.g., 'last', 'first', 'before:[text]', 'after:[text]')\n"
            "- property: boolean properties (e.g., property: 'include_in_toc', value: false)\n"
            "- content: general description if it's being replaced or content modified.\n\n"
            "Return JSON matching this schema:\n"
            "{\n"
            "  \"references\": [\n"
            "    {\n"
            "      \"text\": \"<the exact phrase used by the user, e.g. 'Conclusion'>\",\n"
            "      \"expected_state\": [\n"
            "        { \"type\": \"position\", \"expected\": \"last\" },\n"
            "        { \"type\": \"property\", \"property\": \"include_in_toc\", \"value\": false }\n"
            "      ]\n"
            "    }\n"
            "  ]\n"
            "}\n"
            "If the request does not target specific existing elements, return an empty array."
        )

        response = self.client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": request},
            ],
            temperature=0,
            max_tokens=512,
            response_format={"type": "json_object"},
        )

        raw = (response.choices[0].message.content or "{}").strip()
        try:
            data = json.loads(raw)
            return data.get("references", [])
        except json.JSONDecodeError:
            log.error("Failed to decode reference extraction JSON.")
            return []

    def _flatten_dom(self, node: dict) -> list[dict]:
        nodes = []
        if node.get("id"):
            nodes.append({
                "id": node["id"],
                "text": node.get("text", "").strip(),
                "role": node.get("role", ""),
                "type": node.get("type", "")
            })
        
        for child in node.get("children", []) + node.get("rows", []) + node.get("cells", []):
            nodes.extend(self._flatten_dom(child))
            
        return nodes

    def _fuzzy_match(self, query: str, flat_nodes: list[dict]) -> tuple[str | None, int]:
        best_id = None
        best_score = 0
        
        query_lower = query.lower()

        for node in flat_nodes:
            text = node["text"]
            if not text:
                continue
                
            # Exact match gets 100
            if text.lower() == query_lower:
                return node["id"], 100
                
            # Otherwise use partial ratio (simulated with difflib)
            seq = difflib.SequenceMatcher(None, query_lower, text.lower())
            # A rough estimate of partial ratio
            score = seq.ratio() * 100
            
            # Boost score if the role matches typical queries (e.g. "heading")
            if "heading" in query_lower and node["role"] == "heading":
                score = min(100, score + 10)
                
            if score > best_score:
                best_score = score
                best_id = node["id"]
                
        return best_id, int(best_score)

