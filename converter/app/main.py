import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

app = FastAPI(title="LibreOffice Converter", version="0.1.0")

SUPPORTED_SUFFIXES = {".docx", ".pptx"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/convert")
async def convert(file: UploadFile = File(...)) -> FileResponse:
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise HTTPException(status_code=400, detail="Only DOCX and PPTX files are supported")

    office = shutil.which("soffice") or shutil.which("libreoffice")
    if not office:
        raise HTTPException(status_code=500, detail="LibreOffice is not installed in the converter image")

    workdir = Path(tempfile.mkdtemp(prefix="lo-convert-"))
    source = workdir / f"{uuid4()}{suffix}"
    try:
        with source.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                handle.write(chunk)

        result = subprocess.run(
            [
                office,
                "--headless",
                "--nologo",
                "--nofirststartwizard",
                "--convert-to",
                "pdf",
                "--outdir",
                str(workdir),
                str(source),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "LibreOffice conversion failed"
            raise HTTPException(status_code=500, detail=detail)

        pdf = source.with_suffix(".pdf")
        if not pdf.exists():
            raise HTTPException(status_code=500, detail="LibreOffice did not create a PDF")

        return FileResponse(
            pdf,
            media_type="application/pdf",
            filename=Path(filename).with_suffix(".pdf").name,
            background=BackgroundTask(shutil.rmtree, workdir, ignore_errors=True),
        )
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(status_code=504, detail="LibreOffice conversion timed out") from exc
    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="Unexpected conversion error") from exc
