# AI Document Agent Platform MVP

Production-oriented MVP for editing PPTX and DOCX documents with a chat-driven AI agent, version history, live PDF previews, WebSocket progress updates, and semantic-ish retrieval with a local fallback.

## Stack

- Frontend: React, TypeScript, Tailwind CSS, Zustand, React Query, pdf.js
- Backend: FastAPI, SQLAlchemy, local filesystem storage
- Document processing: `python-pptx`, `python-docx`
- Preview conversion: LibreOffice converter microservice, local LibreOffice fallback, placeholder fallback
- Retrieval: local lexical retriever by default, Qdrant-ready service boundary

## Quick Start

Backend:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

For real DOCX/PPTX-to-PDF previews without installing LibreOffice on your system:

```bash
docker compose up --build converter
```

Then set the backend environment variable before starting FastAPI:

```bash
CONVERTER_URL=http://127.0.0.1:8081
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`.

## Environment

Backend settings are read from environment variables:

```bash
DATABASE_URL=sqlite:///./document_agent.db
STORAGE_ROOT=../storage/workspaces
FRONTEND_ORIGIN=http://localhost:5173
CONVERTER_URL=http://127.0.0.1:8081
OPENAI_API_KEY=
OPENAI_BASE_URL=
EMBEDDING_MODEL=text-embedding-3-small
QDRANT_URL=
QDRANT_API_KEY=
```

The app works without OpenAI or Qdrant. In that mode it uses deterministic editing heuristics and local retrieval so document upload, chat, versioning, and preview flows remain usable.

If `CONVERTER_URL` is set, the backend sends DOCX/PPTX files to the converter service. If it is not set, the backend tries local `soffice`/`libreoffice`. If neither is available, it creates a placeholder PDF.

## Notes

- The agent only edits text runs/paragraphs and does not modify layout, theme, images, colors, or positioning.
- Each edit creates a new immutable version under `storage/workspaces/{workspace_id}`.
- PDF rendering uses pdf.js in the browser, not iframes.
