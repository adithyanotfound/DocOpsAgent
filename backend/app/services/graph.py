"""LangGraph-based document agent workflow.

Unified staged loop pipeline:
1. build_outline  — Parses DOM and constructs outline indices
2. plan_tasks     — Decomposes requests into ordered atomic tasks
3. Loop Decision  — Loops over tasks one-by-one:
   - resolve_task_references
   - fetch_task_context
   - generate_task_operations
   - validate_task_operations
4. execute_operations — Applies all accumulated operations
5. verify_semantic — Computes outline diff and verifies outcome via LLM
6. Repair Decision — Loops back to task 0 with feedback if unsatisfied
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TypedDict
from pathlib import Path

from app.services.operations import validate_operation

log = logging.getLogger(__name__)

from langgraph.graph import StateGraph, END
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AgentRun, DocumentStructure, DocumentVersion, Message
from app.repositories import WorkspaceRepository
from app.services.analyzer import DocumentAnalyzer
from app.services.content_enricher import ContentEnricher
from app.services.document_processor import DocumentProcessor
from app.services.editor import ContentEditor
from app.services.task_planner import TaskPlanner
from app.services.operation_generator import OperationGenerator
from app.services.reference_resolver import ReferenceResolver
from app.services.preview import PreviewService
from app.services.retrieval import RetrievalService
from app.services.verifier import Verifier
from app.services.slide_planner import SlidePlanner
from app.services.storage import StorageService
from app.services.outline_builder import OutlineBuilder
from app.services.context_fetcher import ContextFetcher
from app.services.run_store import run_store
from app.services.template_analyzer import TemplateAnalyzer
from app.services.document_planner import DocumentPlanner
from app.services.section_generator import SectionGenerator
from app.services.document_assembler import DocumentAssembler
from app.services.kb_retrieval import KBRetrievalService
from app.services.fact_verifier import FactVerifier

MAX_ITERATIONS = 4


# ---------------------------------------------------------------------------
# State Definition
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    workspace_id: str
    run_id: str
    request: str
    original_request: str
    iteration: int

    document_type: str
    current_version: int
    latest_version_number: int
    source_document_path: str
    structure: dict

    chat_history: list[dict]
    attached_image_path: str | None

    # Redesigned loop state
    outline: dict
    analysis: dict
    tasks: list[dict]
    current_task_index: int
    task_results: list[dict]
    accumulated_ops: list[dict]
    failed_tasks_feedback: dict[int, str]

    resolved_ids: dict
    element_context: dict[str, dict]
    generated_ops: list[dict]
    verify_result: dict

    # Legacy compatibility fields
    targets: list[dict]
    edits: list[dict]
    review: dict
    mode: str
    template_structure: dict
    slide_plan: dict
    operations: list[dict]
    op_summaries: list[str]
    op_categories: list[str]
    missed_tasks: list[str]
    needs_image: bool
    needs_image_message: str
    new_version_number: int | None
    thoughts: list[str]
    satisfied: bool

    # DOCX generation mode fields
    template_analysis: dict
    kb_context: list[dict]
    document_plan: dict
    generated_sections: list[dict]


# ---------------------------------------------------------------------------
# Graph Class
# ---------------------------------------------------------------------------

class DocumentAgentGraph:
    def __init__(self, db: Session, provider: str | None = None, model: str | None = None) -> None:
        from app.services.llm_client import LLMClient
        self.llm = LLMClient(provider=provider, model=model)

        self.db = db
        self.repo = WorkspaceRepository(db)
        
        # Redesigned services
        self.planner = TaskPlanner(llm=self.llm)
        self.reference_resolver = ReferenceResolver(llm=self.llm)
        self.context_fetcher = ContextFetcher()
        self.op_generator = OperationGenerator(llm=self.llm)
        self.verifier = Verifier(llm=self.llm)

        # Legacy / Helper services
        self.retrieval = RetrievalService()
        self.analyzer = DocumentAnalyzer(llm=self.llm)
        self.processor = DocumentProcessor()
        self.editor = ContentEditor(llm=self.llm)
        self.slide_planner = SlidePlanner(llm=self.llm)
        self.content_enricher = ContentEnricher(llm=self.llm)
        self.storage = StorageService()
        self.preview = PreviewService()

        # DOCX generation services
        self.template_analyzer = TemplateAnalyzer()
        self.doc_planner = DocumentPlanner(llm=self.llm)
        self.section_generator = SectionGenerator(llm=self.llm)
        self.doc_assembler = DocumentAssembler()
        self.kb_retrieval = KBRetrievalService()
        self.fact_verifier = FactVerifier(llm=self.llm)

        self._graph = self._build()

    # ------------------------------------------------------------------
    # Graph Construction
    # ------------------------------------------------------------------

    def _build(self):
        workflow = StateGraph(AgentState)

        # Unified staged loop pipeline nodes
        workflow.add_node("read_document",             self._read_document)
        workflow.add_node("build_outline",             self._build_outline)
        workflow.add_node("analyze_document",          self._analyze_document)
        workflow.add_node("plan_tasks",                 self._plan_tasks)
        workflow.add_node("resolve_task_references",    self._resolve_task_references)
        workflow.add_node("fetch_task_context",         self._fetch_task_context)
        workflow.add_node("generate_task_operations",   self._generate_task_operations)
        workflow.add_node("validate_task_operations",   self._validate_task_operations)
        workflow.add_node("execute_operations",         self._execute_operations)
        workflow.add_node("verify_semantic",            self._verify_semantic)
        workflow.add_node("finalize_operations",        self._finalize_operations)

        # Generate mode nodes (presentation template generation)
        workflow.add_node("plan_slides",                self._plan_slides)
        workflow.add_node("review_plan",                self._review_plan)
        workflow.add_node("apply_slide_plan",           self._apply_slide_plan)

        # DOCX generation pipeline nodes
        workflow.add_node("analyze_template",           self._analyze_template)
        workflow.add_node("retrieve_kb_context",        self._retrieve_kb_context)
        workflow.add_node("plan_document",              self._plan_document)
        workflow.add_node("generate_sections",          self._generate_sections)
        workflow.add_node("assemble_document",          self._assemble_document)

        # Topology Flow
        workflow.set_entry_point("read_document")
        workflow.add_edge("read_document", "build_outline")
        workflow.add_edge("build_outline", "analyze_document")
        workflow.add_edge("analyze_document", "plan_tasks")

        # Routing decision after task planning
        workflow.add_conditional_edges(
            "plan_tasks",
            self._route_planning,
            {
                "generate": "plan_slides",
                "docx_generate": "analyze_template",
                "operations": "resolve_task_references",
                "finalize": "finalize_operations",
            }
        )

        # Slide planning sub-graph
        workflow.add_edge("plan_slides", "review_plan")
        workflow.add_conditional_edges(
            "review_plan",
            self._should_continue_generate,
            {
                "refine": "plan_slides",
                "commit": "apply_slide_plan",
            }
        )
        workflow.add_edge("apply_slide_plan", END)

        # DOCX generation sub-graph
        workflow.add_edge("analyze_template", "retrieve_kb_context")
        workflow.add_edge("retrieve_kb_context", "plan_document")
        workflow.add_edge("plan_document", "generate_sections")
        workflow.add_edge("generate_sections", "assemble_document")
        workflow.add_edge("assemble_document", "finalize_operations")

        # Operations staged loop sub-graph
        workflow.add_edge("resolve_task_references", "fetch_task_context")
        workflow.add_edge("fetch_task_context", "generate_task_operations")
        workflow.add_edge("generate_task_operations", "validate_task_operations")

        workflow.add_conditional_edges(
            "validate_task_operations",
            self._decide_loop,
            {
                "next_task": "resolve_task_references",
                "needs_image": "finalize_operations",
                "execute": "execute_operations",
            }
        )

        workflow.add_edge("execute_operations", "verify_semantic")

        workflow.add_conditional_edges(
            "verify_semantic",
            self._should_continue_v3,
            {
                "repair": "resolve_task_references",
                "commit": "finalize_operations",
            }
        )

        workflow.add_edge("finalize_operations", END)

        return workflow.compile()

    # ------------------------------------------------------------------
    # Public Entry Point
    # ------------------------------------------------------------------

    async def run(
        self,
        workspace_id: str,
        request: str,
        run_id: str,
        attached_image_path: str | None = None,
    ) -> AgentRun:
        workspace = self.repo.get(workspace_id)
        if workspace is None:
            raise ValueError("Workspace not found")

        run = AgentRun(workspace_id=workspace_id, status="running")
        self.db.add(run)
        self.db.add(Message(workspace_id=workspace_id, role="user", content=request))
        self.db.commit()
        self.db.refresh(run)

        structure_row = self.repo.structure(workspace_id, workspace.current_version)
        latest = self.repo.latest_version(workspace_id)

        if latest is None or structure_row is None:
            raise ValueError("Workspace has no current document version")

        current_doc = self.repo.version(workspace_id, workspace.current_version)

        db_messages = self.repo.messages(workspace_id)
        chat_history = []
        for m in db_messages[:-1]:
            if m.role == "user":
                chat_history.append({"role": "user", "content": m.content})
            elif m.role == "assistant":
                try:
                    parsed = json.loads(m.content)
                    if parsed.get("text"):
                        chat_history.append({"role": "assistant", "content": parsed["text"]})
                except Exception:
                    chat_history.append({"role": "assistant", "content": m.content})

        initial_state: AgentState = {
            "workspace_id": workspace_id,
            "run_id": run_id,
            "request": request,
            "original_request": request,
            "iteration": 0,
            "document_type": workspace.document_type,
            "current_version": workspace.current_version,
            "latest_version_number": latest.version_number if latest else 1,
            "source_document_path": current_doc.document_path if current_doc else latest.document_path,
            "structure": structure_row.structure_json,
            "chat_history": chat_history,
            "attached_image_path": attached_image_path,
            
            # Staged loop states
            "outline": {},
            "tasks": [],
            "current_task_index": 0,
            "task_results": [],
            "accumulated_ops": [],
            "failed_tasks_feedback": {},
            "resolved_ids": [],
            "element_context": {},
            "generated_ops": [],
            "verify_result": {},

            # Legacy compatibility fields
            "targets": [],
            "edits": [],
            "review": {},
            "mode": "operations",
            "template_structure": {},
            "slide_plan": {},
            "operations": [],
            "op_summaries": [],
            "op_categories": [],
            "missed_tasks": [],
            "needs_image": False,
            "needs_image_message": "",
            "new_version_number": None,
            "thoughts": [],
            "satisfied": False,

            # DOCX generation fields
            "template_analysis": {},
            "kb_context": [],
            "document_plan": {},
            "generated_sections": [],
        }

        try:
            final_state: AgentState = await self._graph.ainvoke(initial_state)
            await self._finalise(workspace_id, run, final_state)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            run.status = "failed"
            run.completed_at = datetime.utcnow()
            error_msg = str(exc)
            content = json.dumps({
                "type": "agent_response",
                "thoughts": [f"An error occurred: {error_msg}"],
                "text": f"The agent encountered an error: {error_msg}",
                "version_number": None,
                "version_label": None,
                "needs_image": False,
            })
            self.db.add(Message(workspace_id=workspace_id, role="assistant", content=content))
            self.db.commit()
            run_store.fail(run_id, error_msg)

        return run

    # ------------------------------------------------------------------
    # Node Methods
    # ------------------------------------------------------------------

    async def _read_document(self, state: AgentState) -> dict:
        thought = "Reading the document structure and locating the current version."
        await self._thought(state, thought)
        return {"thoughts": state["thoughts"] + [thought]}

    async def _build_outline(self, state: AgentState) -> dict:
        thought = "Building hierarchical semantic outline of your document..."
        await self._thought(state, thought)
        
        outline = OutlineBuilder.build(state["structure"], state["document_type"])
        return {
            "outline": outline,
            "thoughts": state["thoughts"] + [thought]
        }

    async def _analyze_document(self, state: AgentState) -> dict:
        analysis = state["structure"].get("analysis")
        if not analysis:
            thought = "Performing global semantic analysis of document themes and structure..."
            await self._thought(state, thought)
            analysis = self.analyzer.analyze(state["structure"])
            
            # Save analysis back to structure json in db
            state["structure"]["analysis"] = analysis
            workspace_id = state["workspace_id"]
            struct_row = self.repo.structure(workspace_id, state["latest_version_number"])
            if struct_row:
                struct_row.structure_json = state["structure"]
                self.db.commit()
            
            return {
                "analysis": analysis,
                "thoughts": state["thoughts"] + [thought]
            }
        else:
            return {"analysis": analysis}

    async def _plan_tasks(self, state: AgentState) -> dict:
        thought = "Extracting relevant content context for planning..."
        await self._thought(state, thought)

        # Retrieve relevant context blocks using RAG
        relevant_docs = []
        try:
            # First try Qdrant
            if self.retrieval and hasattr(self.retrieval, "retrieve"):
                relevant_docs = self.retrieval.retrieve(state["request"], state["structure"], limit=5)
        except Exception as e:
            pass

        # Fetch the full text for these blocks
        relevant_ids = [doc["element_id"] for doc in relevant_docs]
        context_blocks = self.context_fetcher.fetch(relevant_ids, state["structure"])
        
        thought2 = "Planning structured decomposition of request into atomic tasks..."
        await self._thought(state, thought2)

        tasks = self.planner.plan(
            request=state["request"], 
            outline=state["outline"], 
            chat_history=state["chat_history"],
            analysis=state["analysis"],
            relevant_blocks=context_blocks
        )
        
        # Route to appropriate mode based on document type and task type
        has_generate = any(t["task_type"] == "generate" for t in tasks)
        has_docx_generate = any(t["task_type"] == "docx_generate" for t in tasks)

        if has_docx_generate and state["document_type"] == "docx":
            mode = "docx_generate"
        elif has_generate and state["document_type"] == "pptx":
            mode = "generate"
        else:
            mode = "operations"

        # Log plan summary
        planned_tasks = "\n".join(f"  - {t['description']} (target: {t['target_hint']})" for t in tasks)
        thought3 = f"Planned {len(tasks)} task(s):\n{planned_tasks}"
        await self._thought(state, thought3)

        return {
            "tasks": tasks,
            "mode": mode,
            "current_task_index": 0,
            "accumulated_ops": [],
            "task_results": [],
            "failed_tasks_feedback": {},
            "thoughts": state["thoughts"] + [thought, thought2, thought3]
        }

    async def _resolve_task_references(self, state: AgentState) -> dict:
        task = state["tasks"][state["current_task_index"]]
        thought = f"Resolving references for task {state['current_task_index'] + 1}: '{task['description']}'..."
        await self._thought(state, thought)

        resolved_ids = self.reference_resolver.resolve(
            task.get("target_hint", ""),
            state["outline"],
            task.get("description", "")
        )
        thought2 = f"Resolved targets to element IDs: {resolved_ids}"
        await self._thought(state, thought2)

        return {
            "resolved_ids": resolved_ids,
            "thoughts": state["thoughts"] + [thought, thought2]
        }

    async def _fetch_task_context(self, state: AgentState) -> dict:
        ids_to_fetch = state["resolved_ids"].get("ids", [])
        element_context = self.context_fetcher.fetch(ids_to_fetch, state["structure"])
        return {"element_context": element_context}

    async def _generate_task_operations(self, state: AgentState) -> dict:
        task_idx = state["current_task_index"]
        task = state["tasks"][task_idx]
        
        # Fetch repair feedback if this task failed verifier previously
        feedback = state["failed_tasks_feedback"].get(task_idx)
        
        # Get previous ops for this task (if any)
        prev_ops = None
        if state["iteration"] > 0 and task_idx < len(state["task_results"]):
            prev_ops = state["task_results"][task_idx].get("ops")

        thought = f"Generating document operations for task: '{task['description']}'..."
        await self._thought(state, thought)

        ops = self.op_generator.generate_for_task(
            task=task,
            resolved_ids=state["resolved_ids"],
            element_context=state["element_context"],
            outline=state["outline"],
            attached_image_path=state.get("attached_image_path"),
            previous_ops=prev_ops,
            verifier_feedback=feedback,
        )

        return {
            "generated_ops": ops,
            "thoughts": state["thoughts"] + [thought]
        }

    async def _validate_task_operations(self, state: AgentState) -> dict:
        task_idx = state["current_task_index"]
        task = state["tasks"][task_idx]
        ops = state["generated_ops"]

        # Validate operations
        valid_ops = []
        needs_image = False
        needs_image_msg = ""

        for op in ops:
            if op.get("op_type") == "needs_image":
                needs_image = True
                needs_image_msg = op.get("parameters", {}).get("message", "")
                break
            
            try:
                valid_ops.append(validate_operation(op))
            except Exception as e:
                log.warning("Task operation failed validation: %s — %s", op, e)

        # Save result
        results = list(state["task_results"])
        result_entry = {
            "task": task,
            "resolved_ids": state["resolved_ids"],
            "ops": valid_ops,
        }
        if task_idx < len(results):
            results[task_idx] = result_entry
        else:
            results.append(result_entry)

        accumulated = state["accumulated_ops"] + valid_ops

        return {
            "accumulated_ops": accumulated,
            "task_results": results,
            "needs_image": needs_image,
            "needs_image_message": needs_image_msg,
            "current_task_index": task_idx + 1,
        }

    async def _execute_operations(self, state: AgentState) -> dict:
        ops = state["accumulated_ops"]
        if not ops:
            thought = "No operations to apply."
            await self._thought(state, thought)
            return {"thoughts": state["thoughts"] + [thought]}

        # Enrich operations (Visible TOC conversion, placeholder generation)
        enriched_ops = self.content_enricher.enrich(
            operations=ops,
            structure=state["structure"],
            document_type=state["document_type"],
            original_request=state["original_request"],
        )

        thought = f"Applying {len(enriched_ops)} document operations..."
        await self._thought(state, thought)

        # Write to a temporary version path
        temp_path = self.storage.workspace_dir(state["workspace_id"]) / f"temp_v3_{state['iteration']}.{state['document_type']}"
        
        changed, summaries = self.processor.apply_operations(
            source=Path(state["source_document_path"]),
            target=temp_path,
            document_type=state["document_type"],
            operations=enriched_ops,
        )

        # Re-extract structure + rebuild outline so that repair cycles use fresh UIDs.
        # The temp_path DOM has been mutated by operations above; the old structure_json
        # still points to stale IDs from before the edit. Without this, any repair cycle
        # that calls resolve_task_references will resolve against old IDs that no longer
        # exist in the document, causing "Failed to find block boundaries" errors.
        new_structure = self.processor.extract(temp_path, state["document_type"])
        new_outline = OutlineBuilder.build(new_structure, state["document_type"])

        return {
            "source_document_path": str(temp_path),
            "op_summaries": summaries,
            "structure": new_structure,
            "outline": new_outline,
            "thoughts": state["thoughts"] + [thought]
        }

    async def _verify_semantic(self, state: AgentState) -> dict:
        thought = "Verifying document changes against user request..."
        await self._thought(state, thought)

        # Re-extract document outline after execution
        new_structure = self.processor.extract(Path(state["source_document_path"]), state["document_type"])
        after_outline = OutlineBuilder.build(new_structure, state["document_type"])

        verify_result = self.verifier.verify_semantic(
            request=state["original_request"],
            tasks=state["tasks"],
            before_outline=state["outline"],
            after_outline=after_outline,
        )

        log.info("Semantic verification result: %s", verify_result)

        if verify_result["all_satisfied"]:
            thought2 = "✓ All planned tasks satisfied successfully!"
        else:
            unsatisfied = [t for t in verify_result["tasks"] if not t["satisfied"]]
            thought2 = f"Verification failed. {len(unsatisfied)} task(s) need correction: {unsatisfied}"
        
        await self._thought(state, thought2)

        return_state = {
            "verify_result": verify_result,
            "structure": new_structure,
            "thoughts": state["thoughts"] + [thought, thought2]
        }

        # If repair is needed, reset the staged loop states
        if not verify_result["all_satisfied"]:
            if state["iteration"] < MAX_ITERATIONS:
                result_feedback = {}
                first_failed_idx = len(state["tasks"])
                for item in verify_result.get("tasks", []):
                    if not item["satisfied"]:
                        result_feedback[item["index"]] = item["feedback"]
                        if item["index"] < first_failed_idx:
                            first_failed_idx = item["index"]
                
                new_accumulated_ops = []
                for i in range(first_failed_idx):
                    if i < len(state.get("task_results", [])):
                        new_accumulated_ops.extend(state["task_results"][i].get("ops", []))
                
                baseline_path = self.storage.version_document_path(state["workspace_id"], state["current_version"], state["document_type"])
                
                return_state.update({
                    "iteration": state["iteration"] + 1,
                    "current_task_index": first_failed_idx,
                    "accumulated_ops": new_accumulated_ops,
                    "failed_tasks_feedback": result_feedback,
                    "source_document_path": str(baseline_path),
                })
            else:
                # We reached max iterations. Do not reset source_document_path
                return_state.update({
                    "iteration": state["iteration"] + 1,
                })

        return return_state

    async def _finalize_operations(self, state: AgentState) -> dict:
        if state["needs_image"]:
            return {}

        new_version = state["latest_version_number"] + 1
        document_path = self.storage.version_document_path(state["workspace_id"], new_version, state["document_type"])
        
        import shutil
        if str(state["source_document_path"]) != str(document_path):
            shutil.copy2(state["source_document_path"], document_path)
        
        pdf_path = self.storage.version_pdf_path(state["workspace_id"], new_version)
        await self.preview.convert_to_pdf(document_path, pdf_path)
        
        workspace = self.repo.get(state["workspace_id"])
        workspace.current_version = new_version
        
        self.db.add(DocumentVersion(
            workspace_id=state["workspace_id"],
            version_number=new_version,
            document_path=str(document_path),
            pdf_path=str(pdf_path),
        ))
        self.db.add(DocumentStructure(
            workspace_id=state["workspace_id"],
            version_number=new_version,
            structure_json=state["structure"],
        ))
        self.db.commit()
        
        # Sync updated document structure to vector index
        self.retrieval.sync_workspace(state["workspace_id"], state["structure"])
        
        pdf_url  = f"/api/files/{state['workspace_id']}/v{new_version}.pdf"
        doc_url  = f"/api/files/{state['workspace_id']}/v{new_version}.{state['document_type']}"
        
        run_store.push_event(state["run_id"], {
            "type": "version_created",
            "version_number": new_version,
            "pdf_url": pdf_url,
            "document_url": doc_url,
        })
        
        return {"new_version_number": new_version}

    # ------------------------------------------------------------------
    # Node: plan_slides / review_plan / apply_slide_plan (Generate mode)
    # ------------------------------------------------------------------

    async def _plan_slides(self, state: AgentState) -> dict:
        thought = "Planning slide content and layout for your presentation."
        await self._thought(state, thought)

        # Retrieve template structure
        template_structure = self.processor.extract_rich(state["source_document_path"], state["document_type"])

        slide_plan = self.slide_planner.plan(
            request=state["request"],
            template_structure=template_structure,
            intent={"topic": state["request"]},  # Mimic intent format for legacy Planner compat
            chat_history=state["chat_history"],
        )

        active_count = len([s for s in slide_plan.get("slides", []) if s.get("action") != "delete"])
        thought2 = f"Generated a plan with {active_count} slide(s)."
        await self._thought(state, thought2)

        return {
            "slide_plan": slide_plan,
            "template_structure": template_structure,
            "thoughts": state["thoughts"] + [thought, thought2],
        }

    async def _review_plan(self, state: AgentState) -> dict:
        thought = "Reviewing the slide plan for quality and completeness."
        await self._thought(state, thought)

        review = self.verifier.review_plan(
            state["original_request"], state["slide_plan"], {"topic": state["original_request"]}
        )

        if review["satisfied"]:
            thought2 = "✓ The slide plan looks good — proceeding to apply."
        else:
            if state["iteration"] >= MAX_ITERATIONS:
                thought2 = f"Max refinement rounds reached. Will apply current plan."
            else:
                feedback = review.get("feedback", "")
                thought2 = f"Reviewer flagged issues: {feedback}. Refining plan..."
        
        await self._thought(state, thought2)

        return {
            "review": review,
            "satisfied": review["satisfied"],
            "iteration": state["iteration"] + 1,
            "thoughts": state["thoughts"] + [thought, thought2],
        }

    async def _apply_slide_plan(self, state: AgentState) -> dict:
        workspace_id = state["workspace_id"]
        workspace = self.repo.get(workspace_id)
        slide_plan = state.get("slide_plan", {})

        if not slide_plan.get("slides"):
            thought = "No slide plan to apply — document is unchanged."
            await self._thought(state, thought)
            return {"thoughts": state["thoughts"] + [thought]}

        thought = "Building the presentation from the slide plan..."
        await self._thought(state, thought)

        new_version = state["latest_version_number"] + 1
        document_path = self.storage.version_document_path(workspace_id, new_version, "pptx")

        self.processor.apply_slide_plan(
            source=state["source_document_path"],
            target=document_path,
            slide_plan=slide_plan,
        )

        pdf_path = self.storage.version_pdf_path(workspace_id, new_version)
        await self.preview.convert_to_pdf(document_path, pdf_path)
        new_structure = self.processor.extract(document_path, "pptx")

        workspace.current_version = new_version
        self.db.add(DocumentVersion(
            workspace_id=workspace_id,
            version_number=new_version,
            document_path=str(document_path),
            pdf_path=str(pdf_path),
        ))
        self.db.add(DocumentStructure(
            workspace_id=workspace_id,
            version_number=new_version,
            structure_json=new_structure,
        ))
        self.db.commit()

        pdf_url  = f"/api/files/{workspace_id}/v{new_version}.pdf"
        doc_url  = f"/api/files/{workspace_id}/v{new_version}.pptx"
        
        run_store.push_event(workspace_id, {
            "type": "version_created",
            "version_number": new_version,
            "pdf_url": pdf_url,
            "document_url": doc_url,
        })

        return {"new_version_number": new_version}

    # ------------------------------------------------------------------
    # Routing Decisions
    # ------------------------------------------------------------------

    def _route_planning(self, state: AgentState) -> str:
        mode = state.get("mode", "operations")
        if not state.get("tasks") and mode == "operations":
            return "finalize"
        if mode == "docx_generate":
            return "docx_generate"
        return mode

    def _decide_loop(self, state: AgentState) -> str:
        if state["needs_image"]:
            return "needs_image"
        if state["current_task_index"] < len(state["tasks"]):
            return "next_task"
        return "execute"

    def _should_continue_v3(self, state: AgentState) -> str:
        result = state["verify_result"]
        if result.get("all_satisfied") or state["iteration"] > MAX_ITERATIONS:
            return "commit"
        
        return "repair"

    def _should_continue_generate(self, state: AgentState) -> str:
        if state.get("satisfied"):
            return "commit"
        if state["iteration"] > MAX_ITERATIONS:
            return "commit"
        return "refine"

    # ------------------------------------------------------------------
    # DOCX Generation Nodes
    # ------------------------------------------------------------------

    async def _analyze_template(self, state: AgentState) -> dict:
        thought = "Analyzing template structure, styles, and formatting..."
        await self._thought(state, thought)
        template_path = Path(state["source_document_path"])
        analysis = self.template_analyzer.analyze(template_path)
        section_names = [s.get("heading_text", "") for s in analysis.get("sections", [])]
        thought2 = f"Template has {len(section_names)} top-level section(s): {', '.join(section_names[:5])}"
        await self._thought(state, thought2)
        return {
            "template_analysis": analysis,
            "thoughts": state["thoughts"] + [thought, thought2],
        }

    async def _retrieve_kb_context(self, state: AgentState) -> dict:
        thought = "Searching knowledge base for relevant content..."
        await self._thought(state, thought)

        # Fetch all KB chunks for this workspace from DB
        kb_chunks_db = self.repo.list_knowledge_chunks(state["workspace_id"])
        chunks_as_dicts = [
            {
                "text": c.text,
                "chunk_index": c.chunk_index,
                "metadata": {**(c.chunk_metadata or {}), "doc_id": c.document_id},
            }
            for c in kb_chunks_db
        ]

        if not chunks_as_dicts:
            thought2 = "No knowledge base documents found — will use general knowledge."
            await self._thought(state, thought2)
            return {"kb_context": [], "thoughts": state["thoughts"] + [thought, thought2]}

        thought2 = f"Loaded {len(chunks_as_dicts)} KB chunk(s) for per-section retrieval."
        await self._thought(state, thought2)
        return {
            "kb_context": chunks_as_dicts,
            "thoughts": state["thoughts"] + [thought, thought2],
        }

    async def _plan_document(self, state: AgentState) -> dict:
        thought = "Planning document structure section by section..."
        await self._thought(state, thought)

        plan = self.doc_planner.plan(
            user_request=state["original_request"],
            template_analysis=state["template_analysis"],
            kb_context=state["kb_context"],
            chat_history=state["chat_history"],
        )

        n_sections = len(plan.get("sections", []))
        thought2 = f"Document plan created with {n_sections} section(s)."
        await self._thought(state, thought2)
        return {
            "document_plan": plan,
            "thoughts": state["thoughts"] + [thought, thought2],
        }

    async def _generate_sections(self, state: AgentState) -> dict:
        thought = "Generating content for each section (per-section retrieval + inline verification)..."
        await self._thought(state, thought)

        sections = self.section_generator.generate_all_sections(
            document_plan=state["document_plan"],
            kb_context=state["kb_context"],
            template_analysis=state["template_analysis"],
            workspace_id=state["workspace_id"],
            kb_retrieval=self.kb_retrieval,
            fact_verifier=self.fact_verifier,
        )

        thought2 = f"Generated and verified {len(sections)} section(s)."
        await self._thought(state, thought2)
        return {
            "generated_sections": sections,
            "thoughts": state["thoughts"] + [thought, thought2],
        }

    async def _assemble_document(self, state: AgentState) -> dict:
        thought = "Assembling the final document with template formatting..."
        await self._thought(state, thought)

        new_version = state["latest_version_number"] + 1
        document_path = self.storage.version_document_path(
            state["workspace_id"], new_version, state["document_type"]
        )

        success, summaries = self.doc_assembler.assemble(
            template_path=Path(self.storage.version_document_path(
                state["workspace_id"], state["current_version"], state["document_type"]
            )),
            target_path=document_path,
            generated_sections=state["generated_sections"],
            template_analysis=state["template_analysis"],
        )

        thought2 = f"Document assembled: {len(summaries)} sections written."
        await self._thought(state, thought2)

        # Update source_document_path so finalize_operations picks it up
        # We must also extract the new structure so UIDs are stamped on the generated document
        # and the DB gets the correct structure_json.
        new_structure = self.processor.extract(document_path, state["document_type"])

        return {
            "source_document_path": str(document_path),
            "op_summaries": summaries,
            "structure": new_structure,
            "thoughts": state["thoughts"] + [thought, thought2],
        }

    # ------------------------------------------------------------------
    # Thoughts Helpers
    # ------------------------------------------------------------------

    async def _thought(self, state: AgentState, content: str) -> None:
        run_store.push_event(state["run_id"], {
            "type": "thought",
            "content": content,
            "iteration": state["iteration"],
        })

    async def _finalise(self, workspace_id: str, run: AgentRun, state: AgentState) -> None:
        mode = state.get("mode", "operations")
        version_num = state["new_version_number"]

        if state.get("needs_image"):
            msg = state.get("needs_image_message") or "Please upload the requested image."
            content = json.dumps({
                "type": "agent_response",
                "thoughts": state["thoughts"],
                "text": msg,
                "version_number": None,
                "version_label": None,
                "needs_image": True,
            })
            run.status = "completed"
            run.completed_at = datetime.utcnow()
            self.db.add(Message(workspace_id=workspace_id, role="assistant", content=content))
            self.db.commit()
            run_store.complete(state["run_id"], workspace=None)
            return

        if mode == "docx_generate":
            summaries = state.get("op_summaries", [])
            n_sections = len(state.get("generated_sections", []))
            if not version_num:
                summary_text = "I wasn't able to generate the document. Please try again."
            else:
                summary_text = (
                    f"Generated a complete document with {n_sections} section(s) "
                    f"grounded in your knowledge base and matching the template formatting."
                )
        elif mode == "operations":
            summaries = state.get("op_summaries", [])
            if not summaries or not version_num:
                summary_text = "I wasn't able to apply any changes. Please try rephrasing your request."
            else:
                ops_desc = "; ".join(summaries[:5])
                if len(summaries) > 5:
                    ops_desc += f" … and {len(summaries) - 5} more"
                summary_text = f"Done! Decomposed into {len(state['tasks'])} task(s) and applied: {ops_desc}."
        else:
            slide_plan = state.get("slide_plan", {})
            active_slides = [s for s in slide_plan.get("slides", []) if s.get("action") != "delete"]
            count = len(active_slides)
            if count == 0 or not version_num:
                summary_text = "I could not generate content."
            else:
                summary_text = f"Created a {count}-slide presentation."

        version_label = state["original_request"][:50].rstrip()
        content = json.dumps({
            "type": "agent_response",
            "thoughts": state["thoughts"],
            "text": summary_text,
            "version_number": version_num,
            "version_label": version_label,
            "needs_image": False,
        })

        run.status = "completed"
        run.completed_at = datetime.utcnow()
        self.db.add(Message(workspace_id=workspace_id, role="assistant", content=content))
        self.db.commit()

        from app.services.serializers import serialize_workspace
        ws = serialize_workspace(self.repo.get(workspace_id), self.repo)
        run_store.complete(state["run_id"], workspace=ws)
