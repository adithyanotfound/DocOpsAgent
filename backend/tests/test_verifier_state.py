"""Unit tests verifying state key isolation between baseline 'outline' and post-execution 'current_outline'."""
import asyncio
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

from app.services.graph import DocumentAgentGraph


class TestVerifierState(unittest.TestCase):

    def test_outline_state_isolation_on_execute(self):
        """Verify that _execute_operations updates current_outline while keeping baseline outline intact."""
        db_mock = MagicMock()
        graph = DocumentAgentGraph(db=db_mock)

        baseline_outline = {
            "sections": [
                {"heading": "Title", "heading_id": "sec_0", "elements": [{"id": "el_1", "type": "paragraph", "text_preview": "Old Title"}]}
            ]
        }
        baseline_structure = {"dom": {"type": "document", "children": []}}

        state = {
            "workspace_id": "ws_123",
            "run_id": "run_456",
            "request": "Change title to New Title",
            "original_request": "Change title to New Title",
            "iteration": 0,
            "document_type": "docx",
            "current_version": 1,
            "latest_version_number": 1,
            "source_document_path": "/tmp/test.docx",
            "structure": baseline_structure,
            "initial_structure": baseline_structure,
            "outline": baseline_outline,
            "current_outline": baseline_outline,
            "accumulated_ops": [{"op_type": "text_edit", "target_id": "el_1", "parameters": {"new_text": "New Title"}}],
            "thoughts": [],
        }

        mutated_structure = {"dom": {"type": "document", "children": [{"id": "el_1", "text": "New Title"}]}}
        mutated_outline = {
            "sections": [
                {"heading": "Title", "heading_id": "sec_0", "elements": [{"id": "el_1", "type": "paragraph", "text_preview": "New Title"}]}
            ]
        }

        with patch.object(graph.content_enricher, "enrich", side_effect=lambda **kw: kw["operations"]), \
             patch.object(graph.storage, "workspace_dir", return_value=Path("/tmp")), \
             patch.object(graph.processor, "apply_operations", return_value=(True, ["Rewrote paragraph"])), \
             patch.object(graph.processor, "extract", return_value=mutated_structure), \
             patch("app.services.graph.OutlineBuilder.build", return_value=mutated_outline):

            updated_state = asyncio.run(graph._execute_operations(state))

            # Update state dictionary as LangGraph would
            state.update(updated_state)

            # Assert baseline outline was NOT overwritten
            self.assertEqual(state["outline"], baseline_outline)
            self.assertIsNot(state["outline"], state["current_outline"])

            # Assert current_outline reflects post-execution changes
            self.assertEqual(state["current_outline"], mutated_outline)
            self.assertNotEqual(state["outline"], state["current_outline"])

    def test_verifier_receives_distinct_before_and_after_outlines(self):
        """Verify that _verify_semantic compares original 'outline' against new post-execution outline."""
        db_mock = MagicMock()
        graph = DocumentAgentGraph(db=db_mock)

        baseline_outline = {
            "sections": [
                {"heading": "Title", "heading_id": "sec_0", "elements": [{"id": "el_1", "type": "paragraph", "text_preview": "Old Title"}]}
            ]
        }
        post_execution_outline = {
            "sections": [
                {"heading": "Title", "heading_id": "sec_0", "elements": [{"id": "el_1", "type": "paragraph", "text_preview": "New Title"}]}
            ]
        }

        state = {
            "workspace_id": "ws_123",
            "run_id": "run_456",
            "request": "Change title to New Title",
            "original_request": "Change title to New Title",
            "iteration": 0,
            "document_type": "docx",
            "current_version": 1,
            "latest_version_number": 1,
            "source_document_path": "/tmp/temp_v3_0.docx",
            "structure": {},
            "outline": baseline_outline,
            "current_outline": post_execution_outline,
            "tasks": [{"index": 0, "description": "Change title to New Title"}],
            "thoughts": [],
        }

        received_before = None
        received_after = None

        def mock_verify_semantic(request, tasks, before_outline, after_outline):
            nonlocal received_before, received_after
            received_before = before_outline
            received_after = after_outline
            return {"all_satisfied": True, "tasks": [{"index": 0, "satisfied": True}]}

        with patch.object(graph.processor, "extract", return_value={}), \
             patch("app.services.graph.OutlineBuilder.build", return_value=post_execution_outline), \
             patch.object(graph.verifier, "verify_semantic", side_effect=mock_verify_semantic):

            asyncio.run(graph._verify_semantic(state))

            # Assert before_outline and after_outline were distinct objects with different contents
            self.assertIsNotNone(received_before)
            self.assertIsNotNone(received_after)
            self.assertEqual(received_before, baseline_outline)
            self.assertEqual(received_after, post_execution_outline)
            self.assertNotEqual(received_before, received_after)

    def test_repair_resets_current_outline_and_structure_to_baseline(self):
        """Verify that when verifier fails, repair resets current_outline and structure back to baseline."""
        db_mock = MagicMock()
        graph = DocumentAgentGraph(db=db_mock)

        baseline_outline = {"sections": [{"heading": "Old Title"}]}
        baseline_structure = {"dom": {"type": "document", "text": "Old"}}
        mutated_outline = {"sections": [{"heading": "New Title"}]}
        mutated_structure = {"dom": {"type": "document", "text": "New"}}

        state = {
            "workspace_id": "ws_123",
            "run_id": "run_456",
            "request": "Modify document",
            "original_request": "Modify document",
            "iteration": 0,
            "document_type": "docx",
            "current_version": 1,
            "latest_version_number": 1,
            "source_document_path": "/tmp/temp_v3_0.docx",
            "structure": mutated_structure,
            "initial_structure": baseline_structure,
            "outline": baseline_outline,
            "current_outline": mutated_outline,
            "tasks": [{"index": 0, "description": "Modify document"}],
            "task_results": [],
            "thoughts": [],
        }

        with patch.object(graph.processor, "extract", return_value=mutated_structure), \
             patch("app.services.graph.OutlineBuilder.build", return_value=mutated_outline), \
             patch.object(graph.verifier, "verify_semantic", return_value={"all_satisfied": False, "tasks": [{"index": 0, "satisfied": False, "feedback": "Failed"}]}), \
             patch.object(graph.storage, "version_document_path", return_value=Path("/tmp/v1.docx")):

            result = asyncio.run(graph._verify_semantic(state))
            state.update(result)

            # Check that structure and current_outline were restored to baseline
            self.assertEqual(state["structure"], baseline_structure)
            self.assertEqual(state["current_outline"], baseline_outline)


if __name__ == "__main__":
    unittest.main()
