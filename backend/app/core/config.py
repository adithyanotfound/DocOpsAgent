from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./document_agent.db"
    storage_root: Path = Path("../storage/workspaces")
    frontend_origin: str = "http://localhost:5173"
    converter_url: str | None = None
    # URL that the OnlyOffice Docker container can use to reach THIS backend.
    # On macOS/Windows Docker Desktop use http://host.docker.internal:<port>
    backend_url: str = "http://host.docker.internal:8000"
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    llm_model: str = "gpt-4o-mini"
    embedding_model: str = "text-embedding-3-small"
    qdrant_url: str | None = None
    qdrant_api_key: str | None = None
    max_review_iterations: int = 3


settings = Settings()
