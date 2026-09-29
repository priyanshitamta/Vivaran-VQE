"""
Stage E - application assembly.

Builds the FastAPI app: loads the four datasets once at process start, wires
the repositories and services, mounts CORS, and attaches the API router.

Run locally with::

    python -m uvicorn app.main:app --reload

or simply::

    python -m app.main
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

from app import config
from app.api.routes import analytics_router, meta_router, router
from app.data.loader import load_all, validate_references
from app.data.repositories import Repositories
from app.services.analytics_service import AnalyticsService
# DORMANT NLP import (Tier 2 semantic embeddings) - unused while
# config.ENABLE_EMBEDDINGS is off. Kept for future use.
from app.services.embeddings import EmbeddingEngine
from app.services.gap_service import GapService
from app.services.recommendation_service import RecommendationService
from app.services.registration_service import RegistrationService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


def create_app(enable_embeddings: Optional[bool] = None) -> FastAPI:
    """
    Build the application. Datasets load once here; repositories and services
    are stored on ``app.state`` for the routes to reach.

    ``enable_embeddings`` overrides ``config.ENABLE_EMBEDDINGS`` (used by
    tests to force Tier 1). If embeddings are requested but fail to build
    (e.g. sentence-transformers not installed), the app degrades gracefully
    to Tier 1 with a warning instead of crashing.
    """
    loaded = load_all()
    validate_references(loaded)
    repos = Repositories.build(loaded)

    # DORMANT NLP wiring (Tier 2 semantic embeddings). Skipped entirely while
    # config.ENABLE_EMBEDDINGS is off (the default). Kept for future use.
    use_embeddings = (
        config.ENABLE_EMBEDDINGS if enable_embeddings is None else enable_embeddings
    )
    embeddings = None
    if use_embeddings:
        try:
            embeddings = EmbeddingEngine(config.EMBEDDING_MODEL)
            embeddings.build(repos.courses.all())
        except Exception as exc:  # noqa: BLE001 - degrade, never crash
            logger.warning(
                "Tier 2 embeddings unavailable (%s). Falling back to Tier 1 "
                "tag-overlap ranking. Install requirements-tier2.txt to enable.",
                exc,
            )
            embeddings = None

    app = FastAPI(
        title="System 1 - Skill-Gap-to-Course Recommendation Engine",
        version="1.0.0",
        description=(
            "Computes skill gaps for officials against their role requirements "
            "and recommends training courses from the catalogue to close them. "
            "See /docs for interactive API documentation."
        ),
    )

    # CORS: enabled from day one since the full-stack frontend will call this
    # module from a different origin. Lock down via S1_CORS_ORIGINS in prod.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_methods=["GET", "POST"],  # POST: on-the-fly /employees/compute
        allow_headers=["*"],
        allow_credentials=False,  # "*" origins must not combine with credentials
    )

    # Shared state - constructed once, reused across requests.
    app.state.repos = repos
    app.state.embeddings = embeddings  # DORMANT NLP - None unless Tier 2 enabled
    app.state.gap_service = GapService(repos)
    app.state.recommendation_service = RecommendationService(
        repos, embeddings  # DORMANT NLP arg - None unless Tier 2 enabled
    )
    app.state.registration_service = RegistrationService(
        repos, app.state.gap_service, app.state.recommendation_service
    )
    app.state.analytics_service = AnalyticsService(repos, app.state.gap_service)

    app.include_router(router)
    app.include_router(analytics_router)
    app.include_router(meta_router)

    @app.get("/health", tags=["meta"], summary="Service health check")
    def health() -> dict:
        """Lightweight liveness probe (useful for the wider platform's
        orchestration and for the frontend to detect the service is up)."""
        return {
            "status": "ok",
            "employees_loaded": len(repos.employees.all()),  # synthetic + real
            "employees_synthetic": len(repos.employees.all_synthetic()),
            "employees_real": len(repos.employees.all_real()),
            "courses_loaded": len(repos.courses._by_id),  # noqa: SLF001
            "tier2_embeddings": embeddings.is_ready if embeddings else False,
        }

    @app.get("/", include_in_schema=False, summary="Frontend")
    def index() -> Any:
        """
        Serve the local frontend page from the same origin as the API.

        With this in place, http://localhost:8000/ is the whole app - the
        page and the endpoints it calls share one URL, so there is no CORS
        and no hardcoded port in the frontend. Start the server with
        ``python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`` and
        open http://localhost:8000/ in a browser to use the frontend.
        """
        frontend = Path(__file__).resolve().parent.parent / "frontend" / "index.html"
        if frontend.exists():
            return FileResponse(frontend, media_type="text/html")
        return HTMLResponse(
            "<h1>System 1 API is running</h1>"
            "<p>frontend/index.html not found - the website cannot be served.</p>"
        )

    return app


# Module-level instance used by ``uvicorn app.main:app``.
app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
