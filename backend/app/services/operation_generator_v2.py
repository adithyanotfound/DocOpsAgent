from __future__ import annotations

import json
from app.services.operation_generator import OperationGenerator
from openai import OpenAI
from app.core.config import settings

class OperationGeneratorV2(OperationGenerator):
    """
    Stage 4 of the TaskGraph pipeline.
    Uses resolved references to generate operations. In repair mode,
    only generates operations for failed reasons.
    """

    def generate(
        self,
        request: str,
        structure: dict,
        document_type: str,
        chat_history: list[dict],
        intent: dict,
        task_graph: dict,
        attached_image_path: str | None = None,
        previous_ops: list[dict] | None = None,
    ) -> list[dict]:
        """Generate document operations using TaskGraph context."""
        
        refs = task_graph.get("references", [])
        verification_fails = task_graph.get("verification", [])
        
        return self._generate_with_llm(
            request=request,
            structure=structure,
            document_type=document_type,
            chat_history=chat_history,
            intent=intent,
            references=refs,
            verification_fails=verification_fails,
            previous_ops=previous_ops,
        )

    def _generate_with_llm(
        self,
        request: str,
        structure: dict,
        document_type: str,
        chat_history: list[dict],
        intent: dict,
        references: list[dict],
        verification_fails: list[str],
        previous_ops: list[dict] | None,
    ) -> list[dict]:
        
        from app.services.operation_generator import _SYSTEM_PROMPT, _build_structure_summary
        
        summary = _build_structure_summary(structure, document_type)
        
        prompt_lines = [
            f"Original Request: {request}\n",
            f"Document Structure:\n{summary}\n"
        ]

        if references:
            refs_str = json.dumps(references, indent=2)
            prompt_lines.append(
                f"Resolved References (USE THESE IDs for operations!):\n{refs_str}\n"
                "CRITICAL: Do NOT invent DOM IDs. Use the exact `object_id` provided above "
                "for any target fields (start_id, before_id, target_id, etc).\n"
            )

        if verification_fails:
            fails_str = "\n".join(f"- {f}" for f in verification_fails)
            prev_ops_str = json.dumps(previous_ops or [], indent=2)
            prompt_lines.append(
                "=== REPAIR MODE ===\n"
                f"Previous Operations Attempted:\n{prev_ops_str}\n\n"
                f"Verification Failures:\n{fails_str}\n\n"
                "CRITICAL: You are in repair mode. The previous operations failed to achieve the "
                "structural outcome listed above. Generate NEW operations ONLY to fix these specific failures. "
                "Do NOT regenerate operations that succeeded.\n"
            )

        user_prompt = "\n".join(prompt_lines)

        sys_prompt = _SYSTEM_PROMPT.replace("{document_type}", document_type.upper())

        client = OpenAI(
            api_key=settings.openai_api_key or "",
            base_url=settings.openai_base_url or None,
        )

        response = client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0,
            max_tokens=4000,
            response_format={"type": "json_object"},
        )

        raw = (response.choices[0].message.content or "{}").strip()
        print("!!! RAW OUTPUT V2:", raw)
        try:
            data = json.loads(raw)
            # Accept either dict or list
            if isinstance(data, list):
                ops = data
            elif "op_type" in data:
                ops = [data]
            else:
                ops = data.get("operations") or data.get("ops") or []
            
            # Validate ops using original validation
            from app.services.operation_generator import validate_operation
            valid_ops = []
            for op in ops:
                try:
                    valid_ops.append(validate_operation(op))
                except Exception as e:
                    print("Skipping invalid op:", e)
            return valid_ops
        except Exception:
            return []
