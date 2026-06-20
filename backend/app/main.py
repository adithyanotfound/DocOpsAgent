from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.config import settings
from app.db.session import Base, engine


def create_app() -> FastAPI:
    Base.metadata.create_all(bind=engine)

    app = FastAPI(title="AI Document Agent Platform", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin, "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    settings.storage_root.mkdir(parents=True, exist_ok=True)
    app.mount("/storage", StaticFiles(directory=settings.storage_root.parent), name="storage")
    app.include_router(router, prefix="/api")
    return app


app = create_app()
