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

    # ---------------------------------------------------------------------------
    # LLM provider — "gemini" (default) or "openai" (legacy fallback)
    # ---------------------------------------------------------------------------
    llm_provider: str = "openrouter"

    # Gemini (primary)
    gemini_api_key: str | None = None
    llm_model: str = "google/gemini-2.5-flash-lite"
    embedding_model: str = "openai/text-embedding-3-small"

    # OpenAI (legacy — kept so the OpenAI provider path still works during
    # any transition period; unused when llm_provider="gemini")
    openai_api_key: str | None = None
    openai_base_url: str | None = None

    open_router_api: str | None = None

    # ---------------------------------------------------------------------------
    # Vector store (Qdrant)
    # ---------------------------------------------------------------------------
    qdrant_url: str | None = None
    qdrant_api_key: str | None = None

    max_review_iterations: int = 3


settings = Settings()
