"""Unit tests for pipeline fixes:
1. Native TOC field generation (OperationGenerator emits insert_toc, ContentEnricher converts to 2-column table with PAGEREF fields).
2. Renumbering vs Duplication and Page-Break prompt rules in TaskPlanner.
3. Multi-task intermediate reference resolution.
"""
import unittest
from unittest.mock import MagicMock, patch

from app.services.operation_generator import OperationGenerator
from app.services.content_enricher import ContentEnricher
from app.services.task_planner import TaskPlanner, PLANNER_SYSTEM_PROMPT
from app.services.reference_resolver import ReferenceResolver


class TestPipelineFixes(unittest.TestCase):

    def test_native_toc_field_generation_and_enrichment(self):
        """Test that TOC request generates insert_toc op and enriches to a native Word TOC field."""
        op_gen = OperationGenerator(llm=MagicMock())

        task = {
            "task_type": "layout_op",
            "description": "Add Table of Contents at the beginning",
        }
        resolved_ids = {"ids": [], "before_anchor_id": "sec_0"}
        element_context = {}
        outline = {
            "document_type": "docx",
            "title": "Quarterly Report",
            "sections": [
                {"heading": "Executive Summary", "heading_id": "sec_0"},
                {"heading": "Key Metrics", "heading_id": "sec_1"},
            ]
        }

        # Mock LLM returning insert_toc
        llm_response = MagicMock()
        llm_response.json = [
            {
                "op_type": "layout_op",
                "target_id": None,
                "parameters": {
                    "action": "insert_toc",
                    "before_id": "sec_0"
                }
            }
        ]
        op_gen._llm.complete.return_value = llm_response

        ops = op_gen.generate_for_task(task, resolved_ids, element_context, outline)

        # 1. Assert operation generated is insert_toc (NOT insert_block)
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["op_type"], "layout_op")
        self.assertEqual(ops[0]["parameters"]["action"], "insert_toc")
        self.assertNotEqual(ops[0]["parameters"]["action"], "insert_block")

        # 2. Enrich with ContentEnricher
        doc_structure = {
            "dom": {
                "type": "document",
                "children": [
                    {"type": "paragraph", "role": "heading", "text": "Executive Summary", "heading_level": 1},
                    {"type": "paragraph", "role": "heading", "text": "Key Metrics", "heading_level": 1},
                ]
            }
        }

        enricher = ContentEnricher()
        enriched_ops = enricher.enrich(ops, doc_structure, "docx", "add a table of contents")

        # 3. Assert enriched operation contains a 2-column table with Section & Page headers
        self.assertEqual(len(enriched_ops), 1)
        toc_op = enriched_ops[0]
        self.assertEqual(toc_op["parameters"]["action"], "insert_block")

        data = toc_op["parameters"]["data"]
        self.assertEqual(data[0]["role"], "heading")
        self.assertEqual(data[0]["text"], "Table of Contents")

        table_item = data[1]
        self.assertEqual(table_item["role"], "table")
        self.assertEqual(table_item["headers"], ["Section", "Page"])

        # Assert rows contain PAGEREF dicts targeting bookmarks
        rows = table_item.get("rows", [])
        self.assertEqual(len(rows), 2)
        self.assertIn("Executive Summary", rows[0][0])
        self.assertIsInstance(rows[0][1], dict)
        self.assertEqual(rows[0][1]["pageref"], "_Ref_heading_1")

    def test_toc_table_pageref_fields(self):
        """Regression test: Verify TOC table contains PAGEREF fields and target headings contain bookmarks."""
        import tempfile
        from pathlib import Path
        import docx
        from app.services.document_processor import DocumentProcessor

        doc = docx.Document()
        doc.add_paragraph("Intro")
        doc.add_heading("Section 1", level=1)
        doc.add_paragraph("Content 1")

        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as f_src, \
             tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as f_dst:
            src_path = Path(f_src.name)
            dst_path = Path(f_dst.name)

        try:
            doc.save(src_path)
            processor = DocumentProcessor()
            structure = processor.extract(src_path, "docx")

            enricher = ContentEnricher()
            body_els = [el for el in structure.get("dom", {}).get("children", []) if el.get("id") and el.get("type") == "paragraph"]
            target_id = body_els[0]["id"] if body_els else "element_0"

            insert_toc_op = {
                "op_type": "layout_op",
                "target_id": None,
                "parameters": {"action": "insert_toc", "before_id": target_id}
            }
            enriched_ops = enricher.enrich([insert_toc_op], structure, "docx", "add table of contents")

            success, msgs = processor.apply_operations(src_path, dst_path, "docx", enriched_ops)
            self.assertTrue(success)

            reopened_doc = docx.Document(dst_path)
            # 1. Assert table exists with Section and Page headers
            self.assertTrue(len(reopened_doc.tables) > 0)
            toc_table = reopened_doc.tables[0]
            self.assertEqual(toc_table.rows[0].cells[0].text.strip(), "Section")
            self.assertEqual(toc_table.rows[0].cells[1].text.strip(), "Page")

            # 2. Assert cell 1 contains PAGEREF field XML
            cell_xml = toc_table.rows[1].cells[1].paragraphs[0]._p.xml
            self.assertIn("PAGEREF _Ref_heading_1", cell_xml)

            # 3. Assert target heading paragraph contains bookmark _Ref_heading_1
            found_bookmark = False
            for p in reopened_doc.paragraphs:
                if "_Ref_heading_1" in p._p.xml:
                    found_bookmark = True
                    break

            self.assertTrue(found_bookmark, "Target heading paragraph does not contain _Ref_heading_1 bookmark")

        finally:
            if src_path.exists():
                src_path.unlink()
            if dst_path.exists():
                dst_path.unlink()

    def test_toc_cumulative_word_count_page_estimation(self):
        """Regression test Bug 2: Assert estimated page numbers use cumulative word count, not heading count."""
        from app.services.content_enricher import _extract_document_context

        # 10 headings in a ~800-word document (~80 words per section)
        children = []
        for i in range(10):
            children.append({"type": "paragraph", "role": "heading", "text": f"Heading {i+1}", "heading_level": 1})
            text = "word " * 80
            children.append({"type": "paragraph", "role": "body", "text": text})

        structure = {"dom": {"type": "document", "children": children}}
        ctx = _extract_document_context(structure)

        headings = ctx["headings"]
        self.assertEqual(len(headings), 10)

        estimated_pages = [h["estimated_page"] for h in headings]
        max_page = max(estimated_pages)
        self.assertLess(max_page, 5)
        self.assertNotEqual(max_page, len(headings))

    def test_toc_reinsertion_bookmark_deduplication(self):
        """Test that re-running TOC insertion reuses bookmarks and creates no duplicate bookmark names/collisions."""
        import tempfile
        from pathlib import Path
        import docx
        from docx.oxml.ns import qn
        from app.services.document_processor import DocumentProcessor

        doc = docx.Document()
        doc.add_paragraph("Intro text")
        doc.add_heading("Executive Summary", level=1)
        doc.add_paragraph("Summary content")

        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as f_src, \
             tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as f_v1, \
             tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as f_v2:
            src_path = Path(f_src.name)
            v1_path = Path(f_v1.name)
            v2_path = Path(f_v2.name)

        try:
            doc.save(src_path)
            processor = DocumentProcessor()
            enricher = ContentEnricher()

            # Pass 1: Insert first TOC
            struct1 = processor.extract(src_path, "docx")
            p1_id = [c["id"] for c in struct1["dom"]["children"] if c.get("type") == "paragraph"][0]
            op1 = enricher.enrich([{"op_type": "layout_op", "parameters": {"action": "insert_toc", "before_id": p1_id}}], struct1, "docx", "add TOC")
            processor.apply_operations(src_path, v1_path, "docx", op1)

            # Pass 2: Re-run TOC insertion on v1_path -> v2_path
            struct2 = processor.extract(v1_path, "docx")
            p2_id = [c["id"] for c in struct2["dom"]["children"] if c.get("type") == "paragraph"][0]
            op2 = enricher.enrich([{"op_type": "layout_op", "parameters": {"action": "insert_toc", "before_id": p2_id}}], struct2, "docx", "add TOC again")
            processor.apply_operations(v1_path, v2_path, "docx", op2)

            v2_doc = docx.Document(v2_path)
            bmk_names = [bmk.get(qn("w:name")) for bmk in v2_doc.element.body.iter(qn("w:bookmarkStart"))]

            # Assert no duplicate bookmark names exist
            self.assertEqual(len(bmk_names), len(set(bmk_names)), f"Found duplicate bookmark names: {bmk_names}")

        finally:
            for p in (src_path, v1_path, v2_path):
                if p.exists():
                    p.unlink()

    def test_toc_duplicate_heading_texts_unique_bookmarks(self):
        """Test that headings with identical text get distinct, non-colliding bookmarks."""
        import tempfile
        from pathlib import Path
        import docx
        from docx.oxml.ns import qn
        from app.services.document_processor import DocumentProcessor

        doc = docx.Document()
        doc.add_paragraph("Intro")
        doc.add_heading("Key Metrics", level=1)
        doc.add_paragraph("First metrics content")
        doc.add_heading("Key Metrics", level=1)
        doc.add_paragraph("Second metrics content")

        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as f_src, \
             tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as f_dst:
            src_path = Path(f_src.name)
            dst_path = Path(f_dst.name)

        try:
            doc.save(src_path)
            processor = DocumentProcessor()
            enricher = ContentEnricher()

            struct = processor.extract(src_path, "docx")
            p_id = [c["id"] for c in struct["dom"]["children"] if c.get("type") == "paragraph"][0]
            op = enricher.enrich([{"op_type": "layout_op", "parameters": {"action": "insert_toc", "before_id": p_id}}], struct, "docx", "add TOC")
            processor.apply_operations(src_path, dst_path, "docx", op)

            reopened_doc = docx.Document(dst_path)
            headings = [p for p in reopened_doc.paragraphs if p.text.strip() == "Key Metrics"]
            self.assertEqual(len(headings), 2)

            bmk1 = headings[0]._p.find('.//' + qn('w:bookmarkStart'))
            bmk2 = headings[1]._p.find('.//' + qn('w:bookmarkStart'))

            self.assertIsNotNone(bmk1)
            self.assertIsNotNone(bmk2)
            self.assertNotEqual(bmk1.get(qn('w:name')), bmk2.get(qn('w:name')), "Duplicate heading text produced colliding bookmark names!")

        finally:
            for p in (src_path, dst_path):
                if p.exists():
                    p.unlink()

    def test_update_fields_schema_element_ordering(self):
        """Test that w:updateFields appears before w:compat, w:rsids, m:mathPr, and w14:docId in settings.xml."""
        import docx
        from docx.oxml.ns import qn
        from app.services.document_processor import _enable_update_fields

        doc = docx.Document()
        _enable_update_fields(doc)

        settings_children = [child.tag for child in doc.settings.element]
        uf_tag = qn('w:updateFields')
        self.assertIn(uf_tag, settings_children, "w:updateFields missing from doc.settings")

        uf_idx = settings_children.index(uf_tag)

        # Tags that ECMA-376 requires to come AFTER w:updateFields
        after_tags = (qn('w:compat'), qn('w:rsids'), qn('m:mathPr'), qn('w:clrSchemeMapping'), qn('w14:docId'))
        for tag in after_tags:
            if tag in settings_children:
                tag_idx = settings_children.index(tag)
                self.assertLess(
                    uf_idx, tag_idx,
                    f"w:updateFields at index {uf_idx} must appear BEFORE {tag} at index {tag_idx} in settings.xml"
                )

    def test_planner_prompt_rules_for_renumbering_and_toc(self):
        """Test that PLANNER_SYSTEM_PROMPT contains disambiguation rules and cleaned examples."""
        # Rule 16 disambiguation rule must exist
        self.assertIn("RENUMBERING VS DUPLICATION", PLANNER_SYSTEM_PROMPT)
        self.assertIn("emit a text_edit task", PLANNER_SYSTEM_PROMPT)

        # Rule 15 TOC rule must exist
        self.assertIn("TABLE OF CONTENTS (TOC)", PLANNER_SYSTEM_PROMPT)

        # Rule 17 TOC Page Numbers rule must exist
        self.assertIn("FIXING TOC PAGE NUMBERS", PLANNER_SYSTEM_PROMPT)

        # Rule 1 example must not contain page breaks
        self.assertNotIn("add page break before Action Items", PLANNER_SYSTEM_PROMPT)

    def test_planner_renumbering_task_decomposition(self):
        """Regression test: 'make it 3' request generates text_edit and NO page breaks or duplicate_block."""
        planner = TaskPlanner(llm=MagicMock())

        request = "no no the duplicate key metrics is still 2 make it 3 and rest +1 also add numbering to risk and conclusion sections"
        outline = {
            "document_type": "docx",
            "title": "Report",
            "sections": [
                {"heading": "2. Key Metrics", "heading_id": "sec_1"},
                {"heading": "Risks and Challenges", "heading_id": "sec_2"},
                {"heading": "Conclusion", "heading_id": "sec_3"},
            ]
        }

        llm_response = MagicMock()
        llm_response.json = {
            "tasks": [
                {
                    "task_type": "text_edit",
                    "description": "Change heading text from 2. Key Metrics to 3. Key Metrics",
                    "target_hint": "Key Metrics heading",
                    "dependencies": []
                },
                {
                    "task_type": "text_edit",
                    "description": "Add numbering to Risks and Conclusion section headings",
                    "target_hint": "Risks and Conclusion headings",
                    "dependencies": []
                }
            ]
        }
        planner._llm.complete.return_value = llm_response

        tasks = planner.plan(request, outline)

        task_types = [t["task_type"] for t in tasks]
        self.assertIn("text_edit", task_types)
        self.assertNotIn("duplicate_block", task_types)
        self.assertNotIn("insert_page_break", [t["description"] for t in tasks])
        self.assertFalse(any("page break" in t["description"].lower() for t in tasks))


if __name__ == "__main__":
    unittest.main()
