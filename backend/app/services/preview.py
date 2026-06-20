import shutil
import subprocess
from pathlib import Path

import httpx
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app.core.config import settings


class PreviewService:
    def convert_to_pdf(self, document_path: Path, pdf_path: Path) -> Path:
        if settings.converter_url:
            try:
                return self._convert_with_service(document_path, pdf_path)
            except Exception:
                pass

        office = shutil.which("soffice") or shutil.which("libreoffice")
        if office:
            subprocess.run(
                [
                    office,
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(pdf_path.parent),
                    str(document_path),
                ],
                check=True,
                capture_output=True,
            )
            generated = pdf_path.parent / f"{document_path.stem}.pdf"
            if generated != pdf_path and generated.exists():
                generated.replace(pdf_path)
            if pdf_path.exists():
                return pdf_path
        self._placeholder(document_path, pdf_path)
        return pdf_path

    def _convert_with_service(self, document_path: Path, pdf_path: Path) -> Path:
        endpoint = settings.converter_url.rstrip("/") + "/convert"
        with document_path.open("rb") as handle:
            response = httpx.post(
                endpoint,
                files={"file": (document_path.name, handle, self._content_type(document_path))},
                timeout=120,
            )
        response.raise_for_status()
        pdf_path.write_bytes(response.content)
        return pdf_path

    def _content_type(self, document_path: Path) -> str:
        if document_path.suffix.lower() == ".pptx":
            return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    def _placeholder(self, document_path: Path, pdf_path: Path) -> None:
        c = canvas.Canvas(str(pdf_path), pagesize=letter)
        width, height = letter
        c.setFont("Helvetica-Bold", 18)
        c.drawString(72, height - 96, "Preview placeholder")
        c.setFont("Helvetica", 11)
        c.drawString(72, height - 125, "Install LibreOffice to render live DOCX/PPTX previews.")
        c.drawString(72, height - 145, f"Document: {document_path.name}")
        c.showPage()
        c.save()
