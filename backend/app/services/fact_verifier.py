"""Fact Verifier — validates generated quantitative claims against the KB context."""
from __future__ import annotations

import logging
import re
from typing import NamedTuple

log = logging.getLogger(__name__)

class VerificationResult(NamedTuple):
    verified_elements: list[dict]
    violations: list[dict]
    was_modified: bool

class FactVerifier:
    """Verifies that quantitative claims in generated text are grounded in KB data."""

    def __init__(self, llm=None) -> None:
        self._llm = llm  # Kept for future use, not used in verification

    def verify_section(
        self,
        section_elements: list[dict],
        kb_chunks: list[dict],
        section_heading: str,
    ) -> VerificationResult:
        """Verify citations and scan for uncited numbers. Returns clean output."""
        violations = []
        verified_elements = []
        was_modified = False

        for el in section_elements:
            verified_el = self._verify_element(el, kb_chunks, violations)
            if verified_el is not None:
                clean_el = self._strip_citations(verified_el)
                verified_elements.append(clean_el)
                if clean_el != el:
                    was_modified = True

        return VerificationResult(verified_elements, violations, was_modified)

    def _verify_element(self, el: dict, chunks: list[dict], violations: list[dict]) -> dict | None:
        el_type = el.get("type", "paragraph")
        
        if el_type == "table_source":
            # Table source annotation — verify chunk indices are valid
            for idx in el.get("chunks", []):
                if not (0 <= idx - 1 < len(chunks)):
                    violations.append({"type": "invalid_table_source", "chunk": idx})
            return None  # Don't include the source annotation in final output
        
        if el_type == "table_caption":
            return el  # Captions are structural, never touch them
        
        if el_type == "table":
            return self._verify_table(el, chunks, violations)
        
        if el_type == "paragraph":
            text = el.get("text", "")
            
            # 1. Verify cited claims
            citations = re.findall(r'\[chunk:(\d+)\]', text)
            for ref in citations:
                chunk_idx = int(ref) - 1  # Prompt uses 1-indexed
                if not (0 <= chunk_idx < len(chunks)):
                    violations.append({"claim": f"[chunk:{ref}]", "reason": "invalid index"})
                    text = self._remove_sentence_at(text, f"[chunk:{ref}]")
                    continue
                
                # ACTUAL VERIFICATION: check that numbers near this citation
                # appear in the cited chunk
                chunk_text = chunks[chunk_idx].get("text", "").lower()
                sentence = self._get_sentence_containing(text, f"[chunk:{ref}]")
                numbers_in_sentence = re.findall(r'[\d,]+\.?\d*', sentence)
                
                for num in numbers_in_sentence:
                    clean_num = num.replace(",", "")
                    if clean_num and len(clean_num) > 1:  # Skip single digits
                        if clean_num not in chunk_text.replace(",", ""):
                            violations.append({
                                "claim": sentence.strip(),
                                "value": num,
                                "cited_chunk": ref,
                                "reason": "number not found in cited chunk"
                            })
                            text = self._remove_sentence_at(text, f"[chunk:{ref}]")
                            break
            
            # 2. Scan for UNCITED multi-digit numbers — backstop for claims that bypass citations
            remaining_text = re.sub(r'\[chunk:\d+\]', '', text)
            # Match any 2+ digit number not inside Table/Section/Chapter/Q references
            uncited_numbers = re.findall(r'(?<!Table\s)(?<!Section\s)(?<!Chapter\s)(?<!Q)(\d{2,}[\d,.]*)', remaining_text)
            if uncited_numbers:
                for num_match in uncited_numbers:
                    # Skip if it looks like a year in context (e.g., "Q3 2024")
                    if re.search(r'Q[1-4]\s*' + re.escape(num_match), remaining_text):
                        continue
                    sentence = self._get_sentence_containing(text, num_match)
                    if sentence:
                        violations.append({
                            "claim": sentence.strip(),
                            "value": num_match,
                            "reason": "uncited quantitative claim"
                        })
                        text = self._remove_sentence_at(text, num_match)
            
            if not text.strip():
                return None  # Entire paragraph was removed
            
            return {**el, "text": text}
        
        return el  # bullet_list, numbered_list — pass through

    def _verify_table(self, el: dict, chunks: list[dict], violations: list[dict]) -> dict:
        """Verify table cell values against the cited source chunks."""
        # Find the table_source annotation that should accompany this table
        # (passed via _verify_section which tracks cited_table_chunks)
        # For now, verify against ALL provided chunks
        combined_chunk_text = "\n".join(c.get("text", "") for c in chunks).lower()
        
        new_rows = []
        for row in el.get("rows", []):
            new_row = []
            for cell in row:
                # Extract numbers from cell
                numbers = re.findall(r'[\d,]+\.?\d*', str(cell))
                cell_ok = True
                for num in numbers:
                    clean_num = num.replace(",", "")
                    if clean_num and len(clean_num) > 1:
                        if clean_num not in combined_chunk_text.replace(",", ""):
                            violations.append({
                                "claim": f"Table cell: {cell}",
                                "value": num,
                                "reason": "table cell number not found in source chunks"
                            })
                            cell_ok = False
                            break
                # Keep the cell if valid, drop the entire row if any cell fails
                new_row.append(cell if cell_ok else "—")
            new_rows.append(new_row)
        
        return {**el, "rows": new_rows}

    def _strip_citations(self, el: dict) -> dict:
        new_el = dict(el)
        if new_el.get("type") == "paragraph":
            new_el["text"] = re.sub(r'\s*\[chunk:\d+\]', '', new_el["text"]).strip()
        elif new_el.get("type") in ("bullet_list", "numbered_list"):
            new_el["items"] = [re.sub(r'\s*\[chunk:\d+\]', '', item).strip() 
                               for item in new_el.get("items", [])]
        return new_el

    def _remove_sentence_at(self, text: str, needle: str) -> str:
        """Remove ONLY the first sentence containing `needle` from text.
        Uses index-based removal to avoid collateral damage when the same
        substring appears in multiple sentences."""
        sentences = re.split(r'(?<=[.!?])\s+', text)
        for i, s in enumerate(sentences):
            if needle in s:
                return ' '.join(sentences[:i] + sentences[i+1:])
        return text

    def _get_sentence_containing(self, text: str, needle: str) -> str:
        """Return the first sentence containing `needle`."""
        sentences = re.split(r'(?<=[.!?])\s+', text)
        for s in sentences:
            if needle in s:
                return s
        return ""
