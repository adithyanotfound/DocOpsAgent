import shutil
from pathlib import Path

from app.core.config import settings


class StorageService:
    def workspace_dir(self, workspace_id: str) -> Path:
        path = settings.storage_root / workspace_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def version_document_path(self, workspace_id: str, version: int, document_type: str) -> Path:
        return self.workspace_dir(workspace_id) / f"v{version}.{document_type}"

    def version_pdf_path(self, workspace_id: str, version: int) -> Path:
        return self.workspace_dir(workspace_id) / f"v{version}.pdf"

    def copy_version(self, source: str, workspace_id: str, version: int, document_type: str) -> Path:
        target = self.version_document_path(workspace_id, version, document_type)
        shutil.copyfile(source, target)
        return target
