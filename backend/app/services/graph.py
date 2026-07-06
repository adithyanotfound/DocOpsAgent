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
from app.services.document_processor import DocumentProcessor
from app.services.editor import ContentEditor
from app.services.intent import IntentClassifier
from app.services.operation_generator import OperationGenerator
from app.services.preview import PreviewService
from app.services.retrieval import RetrievalService
from app.services.reviewer import Reviewer
from app.services.slide_planner import SlidePlanner
from app.services.storage import StorageService
from app.services.websocket_manager import manager


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    workspace_id: str
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
    needs_image: bool        # True when agent is asking for an image
    needs_image_message: str # Message to show when needs_image is True

    new_version_number: int | None
    thoughts: list[str]
    satisfied: bool
    error: str | None


MAX_ITERATIONS = 3


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

        # Operations mode nodes (new)
        workflow.add_node("generate_operations", self._generate_operations)
        workflow.add_node("review_operations",   self._review_operations)
        workflow.add_node("apply_operations",    self._apply_operations)

        workflow.set_entry_point("read_document")
        workflow.add_edge("read_document", "classify_retrieve")

        # Branch after classification based on mode
        workflow.add_conditional_edges(
            "classify_retrieve",
            self._route_by_mode,
            {"edit": "generate_edits", "generate": "plan_slides", "operations": "generate_operations"},
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

        # Operations mode flow
        workflow.add_edge("generate_operations", "review_operations")
        workflow.add_conditional_edges(
            "review_operations",
            self._should_continue_operations,
            {"refine": "generate_operations", "commit": "apply_operations"},
        )
        workflow.add_edge("apply_operations", END)

        return workflow.compile()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self, workspace_id: str, request: str, attached_image_path: str | None = None) -> AgentRun:
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
            await manager.send(workspace_id, {"type": "error", "message": error_msg})

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
            await manager.send(workspace_id, {"type": "completed", "version": None})
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

        await manager.send(workspace_id, {"type": "completed", "version": version_num})

    # ------------------------------------------------------------------
    # Routing edges
    # ------------------------------------------------------------------

    def _route_by_mode(self, state: AgentState) -> str:
        return state.get("mode", "edit")

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
        if state["iteration"] >= MAX_ITERATIONS:
            return "commit"
        if state.get("reviewer_satisfied", True):
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

        # Edit mode — existing behaviour
        if mode == "generate" and document_type != "pptx":
            thought2 = "Generation mode is only supported for PPTX files. Falling back to edit mode."
            await self._thought(state, thought2)
            mode = "edit"
        else:
            thought2 = "Identified edit mode — locating target content."
            await self._thought(state, thought2)

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
            f"No matching content found." if not targets
            else f"Found {len(targets)} text block(s) to edit ({target_desc})."
        )
        await self._thought(state, thought3)

        return {
            "mode": mode,
            "intent": intent,
            "targets": targets,
            "thoughts": state["thoughts"] + [thought, thought2, thought3],
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
        await manager.send(workspace_id, {
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

        await manager.send(workspace_id, {
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
        thought = "Analysing your request and building a list of document operations."
        await self._thought(state, thought)

        ops = self.op_generator.generate(
            request=state["request"],
            structure=state["structure"],
            document_type=state["document_type"],
            chat_history=state["chat_history"],
            intent=state["intent"],
            attached_image_path=state.get("attached_image_path"),
            previous_ops=state.get("operations"),
            reviewer_feedback=state.get("reviewer_feedback"),
        )

        # Increment iteration if operations were already generated previously
        iteration = state.get("iteration", 1)
        if state.get("operations"):
            iteration += 1

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
                "thoughts": state["thoughts"] + [thought, thought2],
            }

        thought2 = f"Prepared {len(ops)} operation(s) to execute."
        await self._thought(state, thought2)

        return {
            "operations": ops,
            "needs_image": False,
            "iteration": iteration,
            "thoughts": state["thoughts"] + [thought, thought2],
        }

    # ------------------------------------------------------------------
    # Node: review_operations
    # ------------------------------------------------------------------

    async def _review_operations(self, state: AgentState) -> dict:
        # If we need an image, just skip review
        if state.get("needs_image"):
            return {"reviewer_satisfied": True, "reviewer_feedback": ""}

        thought = "Verifying that the generated operations fulfill all your instructions."
        await self._thought(state, thought)

        review = self.reviewer.review_operations(
            request=state["request"],
            ops=state["operations"],
        )

        satisfied = review["satisfied"]
        feedback = review["feedback"]

        if satisfied:
            thought2 = "All requested operations appear correctly implemented."
        else:
            thought2 = f"I missed some details. Refining operations. Feedback: {feedback}"

        await self._thought(state, thought2)

        return {
            "reviewer_satisfied": satisfied,
            "reviewer_feedback": feedback,
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

        await manager.send(workspace_id, {
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
        await manager.send(state["workspace_id"], {
            "type": "thought",
            "content": content,
            "iteration": state["iteration"],
        })

