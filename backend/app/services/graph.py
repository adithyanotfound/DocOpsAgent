"""LangGraph-based document agent workflow.

Graph topology — two-path architecture:

  EDIT MODE (targeted text changes):
    read_document → classify_retrieve → generate_edits → review
                                              ↑                |
                                              └─ (not done) ───┘
                                                       ↓ (done)
                                                apply_edits → END

  GENERATE MODE (template population / multi-slide creation):
    read_document → classify_retrieve → plan_slides → review_plan
                                              ↑                   |
                                              └── (not done) ─────┘
                                                        ↓ (done)
                                                 apply_slide_plan → END

"Done" means either: reviewer is satisfied, OR max iterations reached.
Only ONE document version is created per user prompt, regardless of
how many internal refine-review cycles occur.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import TypedDict

from langgraph.graph import StateGraph, END
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AgentRun, DocumentStructure, DocumentVersion, Message
from app.repositories import WorkspaceRepository
from app.services.content_enricher import ContentEnricher
from app.services.document_processor import DocumentProcessor
from app.services.editor import ContentEditor
from app.services.intent import IntentClassifier
from app.services.operation_generator import OperationGenerator
from app.services.operation_generator_v2 import OperationGeneratorV2
from app.services.reference_resolver import ReferenceResolver
from app.services.preview import PreviewService
from app.services.retrieval import RetrievalService
from app.services.reviewer import Reviewer
from app.services.slide_planner import SlidePlanner
from app.services.storage import StorageService
from app.services.run_store import run_store

# Feature flag for Phase 1 of TaskGraph refactor
USE_TASK_GRAPH_PIPELINE = True


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    workspace_id: str
    run_id: str            # polling key — set once in run()
    request: str           # may be augmented with reviewer feedback
    original_request: str  # never changed
    iteration: int         # 0-based, incremented after each review

    document_type: str
    current_version: int
    latest_version_number: int
    source_document_path: str
    structure: dict

    chat_history: list[dict]

    # Attached image (for image_op)
    attached_image_path: str | None

    # Edit mode fields
    targets: list[dict]
    edits: list[dict]       # best edits so far (updated each iteration)
    review: dict

    # Generate mode fields
    mode: str               # "edit" | "generate" | "operations"
    intent: dict            # full intent classification result
    template_structure: dict  # rich template structure for generation
    slide_plan: dict        # structured slide plan from planner

    # Operations mode fields
    operations: list[dict]   # structured operation list
    op_summaries: list[str]  # human-readable summaries of applied ops
    op_categories: list[str] # all detected op categories for compound prompts
    missed_tasks: list[str]  # sub-tasks flagged as missing by reviewer
    needs_image: bool        # True when agent is asking for an image
    needs_image_message: str # Message to show when needs_image is True

    new_version_number: int | None
    thoughts: list[str]
    satisfied: bool
    reviewer_satisfied: bool
    reviewer_feedback: str
    error: str | None
    
    # TaskGraph state (Phase 1)
    task_graph: dict


MAX_ITERATIONS = 6


# ---------------------------------------------------------------------------
# Graph class
# ---------------------------------------------------------------------------

class DocumentAgentGraph:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = WorkspaceRepository(db)
        self.intent = IntentClassifier()
        self.retrieval = RetrievalService()
        self.processor = DocumentProcessor()
        self.editor = ContentEditor()
        self.reviewer = Reviewer()
        self.planner = SlidePlanner()
        self.op_generator = OperationGenerator()
        self.op_generator_v2 = OperationGeneratorV2()
        self.reference_resolver = ReferenceResolver()
        self.content_enricher = ContentEnricher(
            api_key=settings.openai_api_key or "",
            base_url=settings.openai_base_url or None,
            llm_model=settings.llm_model,
        )
        self.storage = StorageService()
        self.preview = PreviewService()
        self._graph = self._build()

    # ------------------------------------------------------------------
    # Graph construction
    # ------------------------------------------------------------------

    def _build(self):
        workflow = StateGraph(AgentState)

        # Shared nodes
        workflow.add_node("read_document",      self._read_document)
        workflow.add_node("classify_retrieve",  self._classify_retrieve)

        # Edit mode nodes
        workflow.add_node("generate_edits",     self._generate_edits)
        workflow.add_node("review",             self._review)
        workflow.add_node("apply_edits",        self._apply_edits)

        # Generate mode nodes
        workflow.add_node("plan_slides",        self._plan_slides)
        workflow.add_node("review_plan",        self._review_plan)
        workflow.add_node("apply_slide_plan",   self._apply_slide_plan)

        workflow.set_entry_point("read_document")
        workflow.add_edge("read_document", "classify_retrieve")

        # Branch after classification based on mode
        workflow.add_conditional_edges(
            "classify_retrieve",
            self._route_by_mode,
            {"edit": "generate_edits", "generate": "plan_slides", "operations": "resolve_references" if USE_TASK_GRAPH_PIPELINE else "generate_operations"},
        )

        # Edit mode flow
        workflow.add_edge("generate_edits", "review")
        workflow.add_conditional_edges(
            "review",
            self._should_continue_edit,
            {"refine": "generate_edits", "commit": "apply_edits"},
        )
        workflow.add_edge("apply_edits", END)

        # Generate mode flow
        workflow.add_edge("plan_slides", "review_plan")
        workflow.add_conditional_edges(
            "review_plan",
            self._should_continue_generate,
            {"refine": "plan_slides", "commit": "apply_slide_plan"},
        )
        workflow.add_edge("apply_slide_plan", END)

        # Operations mode flow:
        workflow.add_node("enrich_operations",   self._enrich_operations)

        if USE_TASK_GRAPH_PIPELINE:
            # Phase 1: TaskGraph staged deterministic pipeline
            workflow.add_node("resolve_references",  self._resolve_references)
            workflow.add_node("generate_operations_v2", self._generate_operations_v2)
            workflow.add_node("validate_operations", self._validate_operations)
            workflow.add_node("execute_operations",  self._execute_operations)
            workflow.add_node("reread_and_verify",   self._reread_and_verify)
            workflow.add_node("finalize_operations_v2", self._finalize_operations_v2)

            workflow.add_edge("resolve_references", "generate_operations_v2")
            workflow.add_edge("generate_operations_v2", "enrich_operations")
            workflow.add_edge("enrich_operations", "validate_operations")
            workflow.add_edge("validate_operations", "execute_operations")
            workflow.add_edge("execute_operations", "reread_and_verify")
            workflow.add_conditional_edges(
                "reread_and_verify",
                self._should_continue_v2,
                {"repair": "generate_operations_v2", "commit": "finalize_operations_v2"}
            )
            workflow.add_edge("finalize_operations_v2", END)
        else:
            # Legacy operations flow
            workflow.add_node("generate_operations", self._generate_operations)
            workflow.add_node("review_operations",   self._review_operations)
            workflow.add_node("apply_operations",    self._apply_operations)
            
            workflow.add_edge("generate_operations", "enrich_operations")
            workflow.add_edge("enrich_operations", "review_operations")
            workflow.add_conditional_edges(
                "review_operations",
                self._should_continue_operations,
                {"refine": "generate_operations", "commit": "apply_operations"}
            )
            workflow.add_edge("apply_operations", END)

        return workflow.compile()

    # ------------------------------------------------------------------
    # Public entry point
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
        # Exclude the very last message since that is the current request we just added
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
            "targets": [],
            "edits": [],
            "review": {},
            "mode": "edit",  # default, overridden in classify_retrieve
            "intent": {},
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
            "error": None,
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
    # Finalise: persist structured assistant message + notify frontend
    # ------------------------------------------------------------------

    async def _finalise(self, workspace_id: str, run: AgentRun, state: AgentState) -> None:
        mode = state.get("mode", "edit")
        version_num = state["new_version_number"]
        review = state["review"]

        # --- needs_image: no document change, just ask for attachment ---
        if state.get("needs_image"):
            msg = state.get("needs_image_message") or (
                "To insert an image, please attach it to your next message "
                "using the 📎 paperclip icon below the chat input."
            )
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

        # --- Operations mode summary ---
        if mode == "operations":
            summaries = state.get("op_summaries", [])
            if not summaries or not version_num:
                summary_text = (
                    "I wasn't able to apply that change to the document. "
                    "Please try rephrasing your request or providing more detail "
                    "(e.g. which slide, shape, or element to target)."
                )
            else:
                ops_desc = "; ".join(summaries[:5])
                if len(summaries) > 5:
                    ops_desc += f" … and {len(summaries) - 5} more"
                summary_text = f"Done! Applied {len(summaries)} operation(s): {ops_desc}."

        elif mode == "generate":
            slide_plan = state.get("slide_plan", {})
            active_slides = [
                s for s in slide_plan.get("slides", [])
                if s.get("action") != "delete"
            ]
            count = len(active_slides)

            if count == 0 or not version_num:
                summary_text = "I could not generate presentation content for that request."
            else:
                summary_text = (
                    f"Created a {count}-slide presentation. "
                    f"The content has been generated and formatted based on your request."
                )
        else:
            count = len(state["edits"])
            if count == 0 or not version_num:
                summary_text = "I could not find matching editable content for that request."
            elif review.get("satisfied"):
                summary_text = (
                    f"The requested changes have been applied across "
                    f"{count} text block{'s' if count != 1 else ''}."
                )
            else:
                summary_text = (
                    f"I applied the best changes I could across "
                    f"{count} text block{'s' if count != 1 else ''} "
                    f"(ran {state['iteration']} refinement round{'s' if state['iteration'] != 1 else ''}). "
                    "Feel free to ask for further adjustments."
                )

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
        from app.repositories import WorkspaceRepository
        ws = serialize_workspace(WorkspaceRepository(self.db).get(workspace_id), WorkspaceRepository(self.db))
        run_store.complete(state["run_id"], workspace=ws)

    # ------------------------------------------------------------------
    # Routing edges
    # ------------------------------------------------------------------

    def _route_by_mode(self, state: AgentState) -> str:
        mode = state.get("mode", "edit")
        if mode == "generate":
            return "plan_slides"
        elif mode == "operations":
            return "operations"
        return "generate_edits"

    def _should_continue_edit(self, state: AgentState) -> str:
        """Edit mode: refine or commit?"""
        if state["satisfied"]:
            return "commit"
        if not state["edits"]:
            return "commit"
        if state["iteration"] >= MAX_ITERATIONS:
            return "commit"
        return "refine"

    def _should_continue_generate(self, state: AgentState) -> str:
        """Generate mode: refine or commit?"""
        if state.get("satisfied"):
            return "commit"
        plan = state.get("slide_plan", {})
        if not plan.get("slides"):
            return "commit"
        if state["iteration"] >= MAX_ITERATIONS:
            return "commit"
        return "refine"

    def _should_continue_operations(self, state: AgentState) -> str:
        # Hard safety cap — never loop forever
        if state["iteration"] >= MAX_ITERATIONS:
            return "commit"
        # The reviewer must be explicitly satisfied — strict by default
        if state.get("reviewer_satisfied", False):
            return "commit"
        # If no operations were produced at all, nothing to refine
        if not state.get("operations"):
            return "commit"
        return "refine"

    # ------------------------------------------------------------------
    # Node: read_document  (runs once, state pre-loaded in run())
    # ------------------------------------------------------------------

    async def _read_document(self, state: AgentState) -> dict:
        thought = "Reading the document structure and locating the current version."
        await self._thought(state, thought)
        return {"thoughts": state["thoughts"] + [thought]}

    # ------------------------------------------------------------------
    # Node: classify_retrieve
    # ------------------------------------------------------------------

    async def _classify_retrieve(self, state: AgentState) -> dict:
        thought = "Classifying your request to determine the best approach."
        await self._thought(state, thought)

        intent = self.intent.classify(state["request"], state["chat_history"])
        structure = state["structure"]
        document_type = state["document_type"]
        mode = intent.get("mode", "edit")

        if mode == "generate" and document_type == "pptx":
            # Generation mode — extract rich template structure
            thought2 = f"Detected generation request. Analysing template structure for content planning."
            await self._thought(state, thought2)

            template_structure = self.processor.extract_rich(
                state["source_document_path"], document_type
            )

            return {
                "mode": "generate",
                "intent": intent,
                "template_structure": template_structure,
                "thoughts": state["thoughts"] + [thought, thought2],
            }

        # Handle mode fallbacks and thought messages
        if mode == "generate" and document_type != "pptx":
            thought2 = "Generation mode is only supported for PPTX files. Falling back to edit mode."
            await self._thought(state, thought2)
            mode = "edit"
        elif mode == "operations":
            cats = intent.get("op_categories", []) or [intent.get("op_category", "")]
            cats_desc = ", ".join(c for c in cats if c)
            thought2 = f"Identified operations mode — categories: {cats_desc}."
            await self._thought(state, thought2)
        else:
            thought2 = "Identified edit mode — locating target content."
            await self._thought(state, thought2)

        # Extract op_categories from intent for compound prompts
        op_categories = intent.get("op_categories", [])
        if not op_categories:
            primary = intent.get("op_category", "")
            op_categories = [primary] if primary else []

        # Target retrieval is only needed for edit mode
        targets: list[dict] = []
        thought3 = ""
        if mode == "edit":
            if intent["direct_target"] and document_type == "pptx" and intent["slide"]:
                targets = [
                    b for b in structure.get("blocks", [])
                    if b.get("metadata", {}).get("slide") == intent["slide"]
                ]
                target_desc = f"slide {intent['slide']}"
            elif intent["direct_target"] and document_type == "docx" and intent["paragraph"]:
                para_idx = intent["paragraph"] - 1
                targets = [
                    b for b in structure.get("blocks", [])
                    if b.get("metadata", {}).get("paragraph_index") == para_idx
                ]
                target_desc = f"paragraph {intent['paragraph']}"
            else:
                query = intent.get("semantic_query") or state["request"]
                targets = self.retrieval.retrieve(query, structure)
                target_desc = f"{len(targets)} block(s) via semantic search"

            thought3 = (
                "No matching content found." if not targets
                else f"Found {len(targets)} text block(s) to edit ({target_desc})."
            )
            await self._thought(state, thought3)

        thoughts_out = state["thoughts"] + [thought, thought2]
        if thought3:
            thoughts_out = thoughts_out + [thought3]

        return {
            "mode": mode,
            "intent": intent,
            "targets": targets,
            "op_categories": op_categories,
            "thoughts": thoughts_out,
        }

    # ------------------------------------------------------------------
    # Node: generate_edits  (edit mode, may run multiple times)
    # ------------------------------------------------------------------

    async def _generate_edits(self, state: AgentState) -> dict:
        iteration = state["iteration"]
        if iteration == 0:
            thought = "Generating text edits to fulfil your request."
        else:
            feedback = state["review"].get("feedback", "no specific feedback")
            thought = (
                f"Refinement round {iteration}/{MAX_ITERATIONS}: "
                f"Improving edits based on reviewer feedback — {feedback}"
            )
        await self._thought(state, thought)

        edits = []
        for block in state["targets"]:
            edits.append({
                "element_id": block["element_id"],
                "old_text": block["text"],
                "new_text": self.editor.rewrite(
                    state["request"], 
                    block["text"], 
                    block.get("metadata", {}),
                    state["chat_history"]
                ),
            })

        changed = sum(1 for e in edits if e["old_text"] != e["new_text"])
        thought2 = f"Produced {changed} change(s) across {len(edits)} block(s)."
        await self._thought(state, thought2)

        return {
            "edits": edits,
            "thoughts": state["thoughts"] + [thought, thought2],
        }

    # ------------------------------------------------------------------
    # Node: review  (edit mode, may run multiple times — no file I/O)
    # ------------------------------------------------------------------

    async def _review(self, state: AgentState) -> dict:
        thought = "Reviewing whether the planned edits satisfy the original request."
        await self._thought(state, thought)

        review = self.reviewer.review(state["original_request"], state["edits"])

        if review["satisfied"]:
            thought2 = "✓ The planned edits satisfy the request."
        else:
            next_iter = state["iteration"] + 1
            if next_iter >= MAX_ITERATIONS:
                thought2 = (
                    f"Max refinement rounds ({MAX_ITERATIONS}) reached. "
                    "Will apply the best edits produced."
                )
            else:
                feedback = review.get("feedback", "")
                thought2 = f"Reviewer feedback: {feedback or 'edits could be improved'} — refining."

        await self._thought(state, thought2)

        augmented = state["request"]
        if not review["satisfied"] and review.get("feedback"):
            augmented = f"{state['request']}\nReviewer feedback: {review['feedback']}"

        return {
            "review": review,
            "satisfied": review["satisfied"],
            "iteration": state["iteration"] + 1,
            "request": augmented,
            "thoughts": state["thoughts"] + [thought, thought2],
        }

    # ------------------------------------------------------------------
    # Node: apply_edits  (edit mode, runs ONCE at the end)
    # ------------------------------------------------------------------

    async def _apply_edits(self, state: AgentState) -> dict:
        workspace_id = state["workspace_id"]
        workspace = self.repo.get(workspace_id)

        if not state["edits"]:
            thought = "No edits to apply — the document is unchanged."
            await self._thought(state, thought)
            return {"thoughts": state["thoughts"] + [thought]}

        thought = "Applying the final edits to the document and generating a PDF preview."
        await self._thought(state, thought)

        new_version = state["latest_version_number"] + 1
        document_path = self.storage.version_document_path(
            workspace_id, new_version, state["document_type"]
        )
        self.processor.apply_edits(
            source=state["source_document_path"],
            target=document_path,
            document_type=state["document_type"],
            edits=state["edits"],
        )

        pdf_path = self.storage.version_pdf_path(workspace_id, new_version)
        await self.preview.convert_to_pdf(document_path, pdf_path)
        new_structure = self.processor.extract(document_path, state["document_type"])

        import json
        json_path = document_path.with_suffix(".json")
        with json_path.open("w", encoding="utf-8") as f:
            json.dump(new_structure, f, indent=2)

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
        doc_url  = f"/api/files/{workspace_id}/v{new_version}.{state['document_type']}"
        thought2 = f"Version {new_version} saved and PDF preview generated."
        await self._thought(state, thought2)

        # Tell the frontend a new version is ready.
        run_store.push_event(state["run_id"], {
            "type": "version_created",
            "version_number": new_version,
            "pdf_url": pdf_url,
            "document_url": doc_url,
        })

        return {
            "new_version_number": new_version,
            "thoughts": state["thoughts"] + [thought, thought2],
        }

    # ------------------------------------------------------------------
    # Node: plan_slides  (generate mode, may run multiple times)
    # ------------------------------------------------------------------

    async def _plan_slides(self, state: AgentState) -> dict:
        iteration = state["iteration"]
        if iteration == 0:
            thought = "Planning slide content and layout for your presentation."
        else:
            feedback = state["review"].get("feedback", "no specific feedback")
            thought = (
                f"Refinement round {iteration}/{MAX_ITERATIONS}: "
                f"Improving slide plan based on feedback — {feedback}"
            )
        await self._thought(state, thought)

        slide_plan = self.planner.plan(
            request=state["request"],
            template_structure=state["template_structure"],
            intent=state["intent"],
            chat_history=state["chat_history"],
        )

        active_count = len([
            s for s in slide_plan.get("slides", [])
            if s.get("action") != "delete"
        ])
        thought2 = f"Generated a plan with {active_count} slide(s)."
        await self._thought(state, thought2)

        return {
            "slide_plan": slide_plan,
            "thoughts": state["thoughts"] + [thought, thought2],
        }

    # ------------------------------------------------------------------
    # Node: review_plan  (generate mode)
    # ------------------------------------------------------------------

    async def _review_plan(self, state: AgentState) -> dict:
        thought = "Reviewing the slide plan for quality and completeness."
        await self._thought(state, thought)

        review = self.reviewer.review_plan(
            state["original_request"], state["slide_plan"], state["intent"]
        )

        if review["satisfied"]:
            thought2 = "✓ The slide plan looks good — proceeding to apply."
        else:
            next_iter = state["iteration"] + 1
            if next_iter >= MAX_ITERATIONS:
                thought2 = (
                    f"Max refinement rounds ({MAX_ITERATIONS}) reached. "
                    "Will apply the current plan."
                )
            else:
                feedback = review.get("feedback", "")
                thought2 = f"Plan feedback: {feedback or 'could be improved'} — refining."

        await self._thought(state, thought2)

        augmented = state["request"]
        if not review["satisfied"] and review.get("feedback"):
            augmented = f"{state['request']}\nReviewer feedback: {review['feedback']}"

        return {
            "review": review,
            "satisfied": review["satisfied"],
            "iteration": state["iteration"] + 1,
            "request": augmented,
            "thoughts": state["thoughts"] + [thought, thought2],
        }

    # ------------------------------------------------------------------
    # Node: apply_slide_plan  (generate mode, runs ONCE at the end)
    # ------------------------------------------------------------------

    async def _apply_slide_plan(self, state: AgentState) -> dict:
        workspace_id = state["workspace_id"]
        workspace = self.repo.get(workspace_id)
        slide_plan = state.get("slide_plan", {})

        if not slide_plan.get("slides"):
            thought = "No slide plan to apply — the document is unchanged."
            await self._thought(state, thought)
            return {"thoughts": state["thoughts"] + [thought]}

        thought = "Building the presentation from the slide plan and generating a PDF preview."
        await self._thought(state, thought)

        new_version = state["latest_version_number"] + 1
        document_path = self.storage.version_document_path(
            workspace_id, new_version, "pptx"
        )

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
        thought2 = f"Version {new_version} saved — {len([s for s in slide_plan['slides'] if s.get('action') != 'delete'])} slides created with PDF preview."
        await self._thought(state, thought2)

        run_store.push_event(state["run_id"], {
            "type": "version_created",
            "version_number": new_version,
            "pdf_url": pdf_url,
            "document_url": doc_url,
        })

        return {
            "new_version_number": new_version,
            "thoughts": state["thoughts"] + [thought, thought2],
        }

    # ------------------------------------------------------------------
    # Node: generate_operations  (operations mode, runs once)
    # ------------------------------------------------------------------

    async def _generate_operations(self, state: AgentState) -> dict:
        iteration = state.get("iteration", 0)
        missed = state.get("missed_tasks", [])
        op_categories = state.get("op_categories", [])

        if iteration == 0:
            cats_desc = f" (categories: {', '.join(op_categories)}" + ")" if op_categories else ""
            thought = f"Analysing your request{cats_desc} and building a comprehensive list of document operations."
        else:
            feedback = state.get("reviewer_feedback", "no specific feedback")
            missed_desc = f" Missing: {', '.join(missed)}." if missed else ""
            thought = (
                f"Refinement round {iteration}/{MAX_ITERATIONS}: "
                f"Adding missing operations based on reviewer feedback — {feedback}{missed_desc}"
            )
        await self._thought(state, thought)

        # Build intent dict enriched with op_categories for the generator
        enriched_intent = dict(state["intent"])
        if op_categories:
            enriched_intent["op_categories"] = op_categories

        ops = self.op_generator.generate(
            request=state["original_request"],  # Always use original, never augmented
            structure=state["structure"],
            document_type=state["document_type"],
            chat_history=state["chat_history"],
            intent=enriched_intent,
            attached_image_path=state.get("attached_image_path"),
            previous_ops=state.get("operations") if iteration > 0 else None,
            reviewer_feedback=state.get("reviewer_feedback") if iteration > 0 else None,
            missed_tasks=missed if iteration > 0 else None,
        )
        # NOTE: We do NOT merge/accumulate ops across rounds.
        # The generator is given the full context (previous_ops + missed_tasks)
        # and produces a COMPLETE replacement list each time.
        # Merging caused duplicate operations (e.g. add_col run twice) because
        # the LLM would regenerate ops with slightly different params, defeating
        # JSON-level deduplication.

        # Increment iteration counter
        new_iteration = iteration + 1

        # Check for needs_image signal
        needs_image_ops = [o for o in ops if o.get("op_type") == "needs_image"]
        if needs_image_ops:
            msg = needs_image_ops[0].get("parameters", {}).get("message", "")
            thought2 = "No image attached — asking user to provide one."
            await self._thought(state, thought2)
            return {
                "operations": ops,
                "needs_image": True,
                "needs_image_message": msg,
                "iteration": new_iteration,
                "thoughts": state["thoughts"] + [thought, thought2],
            }

        thought2 = f"Prepared {len(ops)} operation(s) to execute across {len(op_categories) or 1} operation type(s)."
        await self._thought(state, thought2)

        return {
            "operations": ops,
            "needs_image": False,
            "iteration": new_iteration,
            "thoughts": state["thoughts"] + [thought, thought2],
        }

    # ------------------------------------------------------------------
    # Node: enrich_operations  (content generation for new DOCX blocks)
    # ------------------------------------------------------------------

    async def _enrich_operations(self, state: AgentState) -> dict:
        """Fill in substantive content for insert_block ops and convert insert_toc
        into a visible table.  Runs after generate_operations, before review_operations.
        Only active for DOCX; all other document types pass through instantly.
        """
        ops = state.get("operations", [])
        doc_type = state.get("document_type", "")

        # Quick skip: non-DOCX or no generative operations
        generative_actions = {"insert_block", "insert_toc"}
        has_generative = any(
            op.get("op_type") == "layout_op"
            and op.get("parameters", {}).get("action") in generative_actions
            for op in ops
        )
        if doc_type != "docx" or not has_generative:
            return {}  # No state change needed

        thought = (
            "Generating substantive content for new document sections "
            "and building the Table of Contents from the document's headings…"
        )
        await self._thought(state, thought)

        enriched_ops = self.content_enricher.enrich(
            operations=ops,
            structure=state["structure"],
            document_type=doc_type,
            original_request=state["original_request"],
        )

        thought2 = f"Content enrichment complete — {len(enriched_ops)} operation(s) ready."
        await self._thought(state, thought2)

        return {
            "operations": enriched_ops,
            "thoughts": state["thoughts"] + [thought, thought2],
        }

    # ------------------------------------------------------------------
    # Node: review_operations
    # ------------------------------------------------------------------

    async def _review_operations(self, state: AgentState) -> dict:
        # If we need an image, just skip review
        if state.get("needs_image"):
            return {"reviewer_satisfied": True, "reviewer_feedback": "", "missed_tasks": []}

        thought = "Verifying that the generated operations fulfill ALL parts of your instructions."
        await self._thought(state, thought)

        review = self.reviewer.review_operations(
            request=state["original_request"],  # Always check against the ORIGINAL request
            ops=state["operations"],
        )

        satisfied = review["satisfied"]
        feedback = review["feedback"]
        missed = review.get("missed_tasks", [])

        if satisfied:
            thought2 = "✓ All requested operations are correctly implemented."
        else:
            next_iter = state["iteration"]
            if next_iter >= MAX_ITERATIONS:
                thought2 = (
                    f"Safety cap ({MAX_ITERATIONS} rounds) reached. "
                    f"Committing best available operations. Still missing: {', '.join(missed) if missed else 'see feedback'}."
                )
            else:
                missed_desc = f" Missing: {', '.join(missed)}." if missed else ""
                thought2 = f"Reviewer: {feedback or 'some operations are missing'}{missed_desc} Refining..."

        await self._thought(state, thought2)

        return {
            "reviewer_satisfied": satisfied,
            "reviewer_feedback": feedback,
            "missed_tasks": missed,
            "thoughts": state["thoughts"] + [thought, thought2],
        }

    # ------------------------------------------------------------------
    # Node: apply_operations  (operations mode, runs once at the end)
    # ------------------------------------------------------------------

    async def _apply_operations(self, state: AgentState) -> dict:
        workspace_id = state["workspace_id"]
        workspace = self.repo.get(workspace_id)

        # If we need an image, skip applying (finalise will send the message)
        if state.get("needs_image"):
            return {"thoughts": state["thoughts"]}

        ops = state.get("operations", [])
        if not ops:
            thought = "No operations to apply — the document is unchanged."
            await self._thought(state, thought)
            return {"thoughts": state["thoughts"] + [thought]}

        thought = f"Applying {len(ops)} operation(s) to the document."
        await self._thought(state, thought)

        new_version = state["latest_version_number"] + 1
        document_path = self.storage.version_document_path(
            workspace_id, new_version, state["document_type"]
        )
        from pathlib import Path
        changed, summaries = self.processor.apply_operations(
            source=Path(state["source_document_path"]),
            target=document_path,
            document_type=state["document_type"],
            operations=ops,
        )

        pdf_path = self.storage.version_pdf_path(workspace_id, new_version)
        await self.preview.convert_to_pdf(document_path, pdf_path)
        new_structure = self.processor.extract(document_path, state["document_type"])

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

        pdf_url = f"/api/files/{workspace_id}/v{new_version}.pdf"
        doc_url = f"/api/files/{workspace_id}/v{new_version}.{state['document_type']}"
        thought2 = f"Version {new_version} saved — {len(summaries)} operation(s) applied."
        await self._thought(state, thought2)

        run_store.push_event(state["run_id"], {
            "type": "version_created",
            "version_number": new_version,
            "pdf_url": pdf_url,
            "document_url": doc_url,
        })

        return {
            "new_version_number": new_version,
            "op_summaries": summaries,
            "thoughts": state["thoughts"] + [thought, thought2],
        }

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    async def _thought(self, state: AgentState, content: str) -> None:
        run_store.push_event(state["run_id"], {
            "type": "thought",
            "content": content,
            "iteration": state["iteration"],
        })

    # ------------------------------------------------------------------
    # TaskGraph Pipeline Nodes (Phase 1)
    # ------------------------------------------------------------------

    async def _resolve_references(self, state: AgentState) -> dict:
        thought = "Analyzing your request to resolve document references..."
        await self._thought(state, thought)
        
        task_graph = state.get("task_graph") or {}
        
        resolved_refs = self.reference_resolver.resolve(
            request=state["original_request"],
            structure=state["structure"]
        )
        
        task_graph["references"] = resolved_refs
        
        thought2 = f"Resolved {len(resolved_refs)} reference(s)."
        await self._thought(state, thought2)
        
        return {
            "task_graph": task_graph,
            "thoughts": state["thoughts"] + [thought, thought2]
        }

    async def _generate_operations_v2(self, state: AgentState) -> dict:
        iteration = state.get("iteration", 0)
        task_graph = state.get("task_graph", {})
        
        if iteration == 0:
            thought = "Generating operations using resolved references..."
        else:
            fails = task_graph.get("verification", [])
            thought = f"Refinement round {iteration}/{MAX_ITERATIONS}: Repairing {len(fails)} failed operations..."
            
        await self._thought(state, thought)
        
        ops = self.op_generator_v2.generate(
            request=state["original_request"],
            structure=state["structure"],
            document_type=state["document_type"],
            chat_history=state["chat_history"],
            intent=state["intent"],
            task_graph=task_graph,
            attached_image_path=state.get("attached_image_path"),
            previous_ops=state.get("operations") if iteration > 0 else None,
        )
        task_graph["operations"] = ops
        print("!!! GENERATED OPS:", ops)
        
        return {
            "operations": ops,
            "task_graph": task_graph,
            "thoughts": state["thoughts"] + [thought]
        }

    async def _validate_operations(self, state: AgentState) -> dict:
        ops = state.get("operations", [])
        task_graph = state.get("task_graph", {})
        blocks = state["structure"].get("blocks", [])
        valid_ids = {b["element_id"] for b in blocks}
        
        verification = []
        valid_ops = []
        
        for op in ops:
            params = op.get("parameters", {})
            invalid = False
            for key in ["start_id", "end_id", "before_id", "after_id", "target_id"]:
                ref_id = params.get(key)
                if ref_id and ref_id not in valid_ids:
                    verification.append(f"Operation {op.get('op_type')} failed validation: {key} '{ref_id}' does not exist in structure.")
                    invalid = True
            if not invalid:
                valid_ops.append(op)
                
        task_graph["verification"] = verification
        task_graph["operations"] = valid_ops
        
        if verification:
            thought = f"Validation failed for {len(ops) - len(valid_ops)} operation(s)."
            await self._thought(state, thought)
            return {"operations": valid_ops, "task_graph": task_graph, "thoughts": state["thoughts"] + [thought]}
            
        return {"operations": valid_ops, "task_graph": task_graph}

    async def _execute_operations(self, state: AgentState) -> dict:
        ops = state.get("operations", [])
        if not ops:
            return {}
            
        workspace_id = state["workspace_id"]
        from pathlib import Path
        
        # Write to a temporary iteration path
        temp_path = self.storage.workspace_dir(workspace_id) / f"temp_v2_{state.get('iteration', 0)}.{state['document_type']}"
        
        changed, summaries = self.processor.apply_operations(
            source=Path(state["source_document_path"]),
            target=temp_path,
            document_type=state["document_type"],
            operations=ops,
        )
        
        # Update source_document_path to the temp file so reread picks it up
        return {
            "source_document_path": str(temp_path),
            "op_summaries": state.get("op_summaries", []) + summaries
        }

    async def _reread_and_verify(self, state: AgentState) -> dict:
        task_graph = state.get("task_graph", {})
        ops = state.get("operations", [])
        iteration = state.get("iteration", 0)
        
        if not ops:
            return {"iteration": iteration + 1}
            
        from pathlib import Path
        new_structure = self.processor.extract(Path(state["source_document_path"]), state["document_type"])
        blocks = new_structure.get("blocks", [])
        
        verification = []
        
        # Verify expectations
        for ref in task_graph.get("references", []):
            expected_state = ref.get("expected_state", [])
            object_id = ref.get("object_id")
            if not object_id or not expected_state:
                continue
                
            # object_id is ephemeral and changes on re-extraction. Find block by exact text match (case-insensitive)
            expected_text = ref.get("text")
            block_idx = next((i for i, b in enumerate(blocks) if b.get("text", "").strip().lower() == expected_text.strip().lower()), -1)
            
            if block_idx == -1:
                verification.append(f"Element '{expected_text}' was lost during execution.")
                continue
                
            block = blocks[block_idx]
            
            for expectation in expected_state:
                if expectation.get("type") == "position":
                    expected_pos = expectation.get("expected")
                    if expected_pos == "last":
                        if block.get("metadata", {}).get("role") == "heading":
                            has_heading_after = any(b.get("metadata", {}).get("role") == "heading" for b in blocks[block_idx + 1:])
                            if has_heading_after:
                                verification.append(f"Element '{expected_text}' is not at the 'last' position.")
                        elif block_idx != len(blocks) - 1:
                            verification.append(f"Element '{expected_text}' is not at the 'last' position.")
                    elif expected_pos == "first":
                        if block.get("metadata", {}).get("role") == "heading":
                            has_heading_before = any(b.get("metadata", {}).get("role") == "heading" for b in blocks[:block_idx])
                            if has_heading_before:
                                verification.append(f"Element '{expected_text}' is not at the 'first' position.")
                        elif block_idx != 0:
                            verification.append(f"Element '{expected_text}' is not at the 'first' position.")
                elif expectation.get("type") == "property":
                    prop = expectation.get("property")
                    expected_val = expectation.get("value")
                    actual_val = block.get("metadata", {}).get(prop)
                    if actual_val != expected_val:
                        verification.append(f"Element '{expected_text}' property '{prop}' is {actual_val}, expected {expected_val}.")
        task_graph["verification"] = verification
        print("!!! VERIFICATION FAILURES:", verification)
        
        if verification:
            thought = "Verification failed. Generating repair operations..."
            await self._thought(state, thought)
        else:
            thought = "Verification passed! All expectations met."
            await self._thought(state, thought)
            
        return {
            "task_graph": task_graph,
            "structure": new_structure,
            "iteration": iteration + 1,
            "thoughts": state["thoughts"] + [thought]
        }
        
    def _should_continue_v2(self, state: AgentState) -> str:
        task_graph = state.get("task_graph", {})
        iteration = state.get("iteration", 0)
        
        if task_graph.get("verification") and iteration < MAX_ITERATIONS:
            return "repair"
        return "commit"

    async def _finalize_operations_v2(self, state: AgentState) -> dict:
        ops = state.get("task_graph", {}).get("operations", [])
        if not ops:
            return {}
            
        workspace_id = state["workspace_id"]
        new_version = state["latest_version_number"] + 1
        document_path = self.storage.version_document_path(workspace_id, new_version, state["document_type"])
        
        import shutil
        shutil.copy2(state["source_document_path"], document_path)
        
        pdf_path = self.storage.version_pdf_path(workspace_id, new_version)
        await self.preview.convert_to_pdf(document_path, pdf_path)
        
        workspace = self.repo.get(workspace_id)
        workspace.current_version = new_version
        
        from app.models import DocumentVersion, DocumentStructure
        self.db.add(DocumentVersion(
            workspace_id=workspace_id,
            version_number=new_version,
            document_path=str(document_path),
            pdf_path=str(pdf_path),
        ))
        self.db.add(DocumentStructure(
            workspace_id=workspace_id,
            version_number=new_version,
            structure_json=state["structure"],
        ))
        self.db.commit()
        
        pdf_url  = f"/api/files/{workspace_id}/v{new_version}.pdf"
        doc_url  = f"/api/files/{workspace_id}/v{new_version}.{state['document_type']}"
        
        run_store.push_event(state["run_id"], {
            "type": "version_created",
            "version_number": new_version,
            "pdf_url": pdf_url,
            "document_url": doc_url,
        })
        
        return {"new_version_number": new_version}

