"""FastAPI entry point for the Touch Grass Agent monolith."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.config import Settings, get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("touch-grass")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory (kept separate from `app` for testability)."""
    settings = settings or get_settings()

    application = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Turns screen-time alerts into concise, privacy-friendly outdoor "
            "plans using a Hermes agent and OpenStreetMap."
        ),
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    application.state.settings = settings

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @application.get("/", tags=["meta"])
    async def root() -> JSONResponse:
        return JSONResponse(
            {
                "service": settings.app_name,
                "version": __version__,
                "docs": "/docs",
                "trigger": f"{settings.api_prefix}/trigger",
            }
        )

    @application.get("/health", tags=["meta"])
    async def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "llm_configured": settings.llm_configured,
            "model": settings.llm_model,
        }

    return application


app = create_app()


def run() -> None:
    """Console entry point: `touch-grass`."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    run()
