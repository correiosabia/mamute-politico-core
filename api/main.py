"""Aplicação FastAPI para exposição dos dados coletados."""

from contextlib import asynccontextmanager
import logging
import os
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

try:
    # Permite execução como pacote (api.main).
    from .routers import (
        admin,
        amendments,
        analysis,
        authors_proposition,
        candidacies,
        collection,
        email_settings,
        election_results,
        electoral_history,
        events,
        expenses,
        ghost_webhooks,
        projects,
        parliamentarians,
        propositions,
        roll_call_votes,
        settings,
        speeches_transcripts,
        speeches_transcripts_proposition,
    )
    from .db.engine import SessionLocal
    from .security import verify_token
    from .services.ghost_reconcile import run_ghost_reconciliation
except ImportError:
    # Permite execução dentro do diretório api/ (python main.py / uvicorn main:app).
    from routers import (
        admin,
        amendments,
        analysis,
        authors_proposition,
        candidacies,
        collection,
        email_settings,
        election_results,
        electoral_history,
        events,
        expenses,
        ghost_webhooks,
        projects,
        parliamentarians,
        propositions,
        roll_call_votes,
        settings,
        speeches_transcripts,
        speeches_transcripts_proposition,
    )
    from db.engine import SessionLocal
    from security import verify_token
    from services.ghost_reconcile import run_ghost_reconciliation


logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def _reconcile_ghost_on_startup() -> None:
    if not _env_bool("MAMUTE_GHOST_RECONCILE_ON_STARTUP", True):
        logger.info("Reconciliação Ghost no startup da API desabilitada.")
        return

    session = SessionLocal()
    try:
        result = run_ghost_reconciliation(session)
    except Exception:
        logger.warning(
            "Falha na reconciliação Ghost no startup da API; seguindo boot.",
            exc_info=True,
        )
    else:
        if result.action == "skipped":
            logger.info("Reconciliação Ghost no startup da API pulada: %s", result.reason)
    finally:
        session.close()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _reconcile_ghost_on_startup()
    yield


def create_app() -> FastAPI:
    """Cria e configura a aplicação FastAPI."""
    app = FastAPI(
        title="Mamute Político API",
        description="API para consulta das tabelas do projeto Mamute Político.",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url="/api/redoc",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    auth_dependencies = [Depends(verify_token)]

    api_router = APIRouter(prefix="/api")

    api_router.include_router(ghost_webhooks.router)
    api_router.include_router(admin.router)
    api_router.include_router(collection.admin_router)
    # Leitura pública de coleções publicadas: sem login, o link é compartilhável.
    api_router.include_router(collection.router)
    api_router.include_router(events.router)
    api_router.include_router(email_settings.admin_router)
    # Imagens do e-mail: o cliente de e-mail busca sem login.
    api_router.include_router(email_settings.public_router)

    api_router.include_router(analysis.router, dependencies=auth_dependencies)
    api_router.include_router(settings.router, dependencies=auth_dependencies)
    api_router.include_router(parliamentarians.router, dependencies=auth_dependencies)
    api_router.include_router(propositions.router, dependencies=auth_dependencies)
    api_router.include_router(projects.router, dependencies=auth_dependencies)
    api_router.include_router(authors_proposition.router, dependencies=auth_dependencies)
    api_router.include_router(amendments.router, dependencies=auth_dependencies)
    api_router.include_router(expenses.router, dependencies=auth_dependencies)
    api_router.include_router(candidacies.router, dependencies=auth_dependencies)
    api_router.include_router(election_results.router, dependencies=auth_dependencies)
    api_router.include_router(electoral_history.router, dependencies=auth_dependencies)
    api_router.include_router(roll_call_votes.router, dependencies=auth_dependencies)
    api_router.include_router(speeches_transcripts.router, dependencies=auth_dependencies)
    api_router.include_router(
        speeches_transcripts_proposition.router, dependencies=auth_dependencies
    )

    @api_router.get("", response_class=HTMLResponse, include_in_schema=False)
    @api_router.get("/", response_class=HTMLResponse, include_in_schema=False)
    def read_api_root() -> str:
        return """
        <!DOCTYPE html>
        <html lang="pt-BR">
            <head>
                <meta charset="utf-8" />
                <title>Mamute Político API</title>
                <style>
                    body {
                        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                        background: #0f172a;
                        color: #e2e8f0;
                        display: flex;
                        align-items: center;
                        justify-content: center;
                        min-height: 100vh;
                        margin: 0;
                    }
                    .card {
                        background: rgba(15, 23, 42, 0.75);
                        border: 1px solid rgba(148, 163, 184, 0.3);
                        border-radius: 16px;
                        padding: 32px;
                        max-width: 560px;
                        text-align: center;
                        box-shadow: 0 20px 50px rgba(15, 23, 42, 0.4);
                    }
                    h1 {
                        margin-top: 0;
                        font-size: 2.2rem;
                    }
                    p {
                        line-height: 1.6;
                        margin: 16px 0 24px;
                    }
                    a {
                        color: #38bdf8;
                        text-decoration: none;
                        font-weight: 600;
                    }
                    a:hover {
                        text-decoration: underline;
                    }
                </style>
            </head>
            <body>
                <main class="card">
                    <h1>Bem-vindo à Mamute Político API</h1>
                    <p>
                        Consulte a documentação interativa em
                        <a href="/api/docs">/api/docs</a> ou acesse o schema JSON em
                        <a href="/api/openapi.json">/api/openapi.json</a>.
                    </p>
                </main>
            </body>
        </html>
        """

    app.include_router(api_router)

    return app


app = create_app()


__all__ = ["app", "create_app"]
