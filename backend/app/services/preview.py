import asyncio
import logging
import uuid
from pathlib import Path

import httpx
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app.core.config import settings

logger = logging.getLogger(__name__)


class PreviewService:
    async def convert_to_pdf(self, document_path: Path, pdf_path: Path) -> Path:
        """Convert a .docx or .pptx to PDF using OnlyOffice Document Server."""
        if settings.converter_url:
            try:
                return await self._convert_with_onlyoffice(document_path, pdf_path)
            except Exception as exc:
                logger.warning("OnlyOffice conversion failed (%s), falling back to placeholder.", exc)

        # Run blocking reportlab code in a thread so the event loop stays free
        await asyncio.to_thread(self._placeholder, document_path, pdf_path)
        return pdf_path

    async def _convert_with_onlyoffice(self, document_path: Path, pdf_path: Path) -> Path:
        """
        Use the OnlyOffice Document Server Conversion API (async).

        Flow:
          1. Build a URL that OnlyOffice (running in Docker) can use to fetch
             the source file from this backend's /api/source-files/ endpoint.
          2. POST JSON to /ConvertService.ashx — OnlyOffice pulls the file,
             converts it, and returns a JSON payload with a fileUrl.
          3. Download the PDF from that fileUrl and write it to pdf_path.

        IMPORTANT: Must be async — if blocking httpx is used here, the event loop
        freezes and OnlyOffice's file-fetch request to this same server can never
        be handled, causing a deadlock and timeout.
        """
        rel_path = document_path.resolve().relative_to(settings.storage_root.resolve())
        source_url = settings.backend_url.rstrip("/") + "/api/source-files/" + rel_path.as_posix()

        filetype = document_path.suffix.lstrip(".").lower()  # "docx" or "pptx"
        key = uuid.uuid4().hex

        endpoint = settings.converter_url.rstrip("/") + "/ConvertService.ashx"

        payload = {
            "async": False,
            "filetype": filetype,
            "key": key,
            "outputtype": "pdf",
            "title": pdf_path.name,
            "url": source_url,
        }

        logger.debug("OnlyOffice request → %s  payload=%s", endpoint, payload)

        # Step 2 — POST to OnlyOffice; it downloads the source file and converts
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                endpoint,
                json=payload,
                headers={"Accept": "application/json"},
            )

        logger.debug("OnlyOffice HTTP %s: %s", response.status_code, response.text)
        response.raise_for_status()

        data = response.json()
        error_code = data.get("error")
        if error_code:
            raise RuntimeError(f"OnlyOffice conversion error code: {error_code}")

        file_url: str | None = data.get("fileUrl")
        if not file_url:
            raise RuntimeError(f"OnlyOffice did not return a fileUrl: {data}")

        # Step 3 — download the converted PDF (fileUrl points to OnlyOffice's own cache)
        # OnlyOffice returns URLs referencing itself, so no hostname rewriting needed
        async with httpx.AsyncClient(timeout=60) as client:
            pdf_response = await client.get(file_url)
        pdf_response.raise_for_status()

        pdf_path.write_bytes(pdf_response.content)
        logger.info("OnlyOffice converted %s → %s", document_path.name, pdf_path.name)
        return pdf_path

    def _placeholder(self, document_path: Path, pdf_path: Path) -> None:
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        c = canvas.Canvas(str(pdf_path), pagesize=letter)
        width, height = letter
        c.setFont("Helvetica-Bold", 18)
        c.drawString(72, height - 96, "Preview placeholder")
        c.setFont("Helvetica", 11)
        c.drawString(72, height - 125, "Start the OnlyOffice Document Server to render live DOCX/PPTX previews.")
        c.drawString(72, height - 145, f"Document: {document_path.name}")
        c.showPage()
        c.save()
