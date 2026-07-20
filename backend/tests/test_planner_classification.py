import unittest
from unittest.mock import MagicMock
from app.services.task_planner import TaskPlanner


class TestPlannerClassification(unittest.TestCase):
    """Test suite specifically asserting Planner task-type classification across diverse requests."""

    def setUp(self):
        self.mock_llm = MagicMock()
        self.planner = TaskPlanner(llm=self.mock_llm)
        self.outline = {
            "document_type": "docx",
            "title": "Corporate Report",
            "sections": [
                {"heading": "Executive Summary", "heading_id": "sec_0"},
                {"heading": "Key Metrics", "heading_id": "sec_1"},
            ]
        }

    def _mock_planner_response(self, task_type: str, description: str, target_hint: str):
        response = MagicMock()
        response.json = {
            "tasks": [
                {
                    "task_type": task_type,
                    "description": description,
                    "target_hint": target_hint,
                    "dependencies": []
                }
            ]
        }
        self.mock_llm.complete.return_value = response

    def test_content_generation_phrasings(self):
        """Test 10 varied phrasings of 'add new content' requests that MUST land on content_generation."""
        content_requests = [
            ("add an environmental impact section", "Add environmental impact section", "after Key Metrics section"),
            ("insert a sustainability section after Executive Summary", "Insert sustainability section", "after Executive Summary section"),
            ("add an ESG metrics summary below Key Metrics", "Add ESG metrics summary", "after Key Metrics section"),
            ("write a section on Q3 financial performance", "Write Q3 financial performance section", "end of document"),
            ("draft a risk mitigation section", "Draft risk mitigation section", "after Key Metrics section"),
            ("add a market analysis overview", "Add market analysis section", "end of document"),
            ("produce a section on compliance highlights", "Produce compliance section", "end of document"),
            ("include an employee safety section", "Include employee safety section", "after Key Metrics section"),
            ("add a CSR initiatives breakdown after Executive Summary", "Add CSR initiatives section", "after Executive Summary section"),
            ("insert 2 paragraphs about renewable energy targets", "Insert renewable energy paragraphs", "after Executive Summary section"),
        ]

        for req_text, desc, hint in content_requests:
            self._mock_planner_response("content_generation", desc, hint)
            tasks = self.planner.plan(req_text, self.outline)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(
                tasks[0]["task_type"],
                "content_generation",
                f"Request '{req_text}' MUST be classified as content_generation"
            )

    def test_non_content_generation_phrasings(self):
        """Test 10 varied non-content requests asserting they are NOT classified as content_generation."""
        non_content_requests = [
            ("add a page break before Key Metrics", "layout_op", "Insert page break", "before Key Metrics section"),
            ("add a table of contents", "layout_op", "Add Table of Contents", "before top of document"),
            ("move Executive Summary below Key Metrics", "layout_op", "Move Executive Summary below Key Metrics", "Executive Summary below Key Metrics"),
            ("swap Executive Summary and Key Metrics", "layout_op", "Swap Executive Summary and Key Metrics", "Executive Summary and Key Metrics"),
            ("change heading font color to dark green", "text_format", "Change heading font color", "all headings"),
            ("rewrite conclusion to be shorter", "text_edit", "Rewrite conclusion paragraph", "Conclusion section"),
            ("add 3 new bullet points to the Highlights list", "list_op", "Add items to list", "Highlights list"),
            ("add a row to Table 1", "table_op", "Add row to Table 1", "Table 1"),
            ("replace logo image with new header graphic", "image_op", "Replace logo image", "logo image"),
            ("set page margins to 1 inch", "theme_op", "Set page margins", "all pages"),
        ]

        for req_text, expected_type, desc, hint in non_content_requests:
            self._mock_planner_response(expected_type, desc, hint)
            tasks = self.planner.plan(req_text, self.outline)
            self.assertEqual(len(tasks), 1)
            self.assertNotEqual(
                tasks[0]["task_type"],
                "content_generation",
                f"Request '{req_text}' must NOT be classified as content_generation"
            )
            self.assertEqual(tasks[0]["task_type"], expected_type)


if __name__ == "__main__":
    unittest.main()
