"""HTTP surface: chat routes, admin routes, and the two static pages.

    uvicorn app.api:app --port 8000

The agent and the MCP subprocesses are started once in the lifespan handler and
shared, because connecting MCP servers and loading the embedding model cost
seconds. Startup is tolerant: a missing index or an unreachable MCP server
leaves the app serving with reduced capability rather than refusing to boot,
which is what `GET /health` is for.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import agent as agent_module
from app import llm, mcp_client, observability
from app.config import dotenv_keys_shadowed_by_environment, settings
from app.ingest.registry import SourceRegistry
from app.ingest.service import IngestionError, service as ingestion
from app.rag import retriever

logger = logging.getLogger(__name__)

#: Populated during startup. None means the agent could not be built, which is
#: reported by /health and by a clear error on /chat rather than a stack trace.
_agent: agent_module.TravelAgent | None = None
_startup_error: str | None = None
#: Keeps the SQLite checkpointer's connection open for the process lifetime.
_resources = AsyncExitStack()


async def _make_checkpointer():
    """Conversation store for the agent.

    SQLite by default so a conversation survives a restart. `from_conn_string`
    is an async context manager, so it is entered on the process-wide exit
    stack rather than with `async with` -- the connection has to outlive this
    function. If it cannot be opened, fall back to memory and say so: losing
    history is much better than refusing to start.
    """
    if settings.conversation_store == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        logger.info("Conversations are in memory and will not survive a restart")
        return InMemorySaver()

    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        settings.conversation_db.parent.mkdir(parents=True, exist_ok=True)
        saver = await _resources.enter_async_context(
            AsyncSqliteSaver.from_conn_string(str(settings.conversation_db))
        )
        logger.info("Conversations persisted to %s", settings.conversation_db)
        return saver
    except Exception as exc:
        from langgraph.checkpoint.memory import InMemorySaver

        logger.warning(
            "Could not open the conversation database (%s); falling back to "
            "in-memory history for this run", exc
        )
        return InMemorySaver()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent, _startup_error
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    tracing = observability.configure()
    if tracing["enabled"]:
        logger.info(
            "LangSmith tracing on, project %r -- every turn is traced",
            tracing["project"],
        )
    else:
        logger.info("LangSmith tracing off: %s", tracing["reason"])

    shadowed = dotenv_keys_shadowed_by_environment()
    if shadowed:
        logger.warning(
            "These keys are set in .env but OVERRIDDEN by real environment "
            "variables, which take precedence: %s. Unset them in your shell "
            "if you meant the .env values to apply.",
            ", ".join(shadowed),
        )

    # The retriever needs to know when a rebuild lands so chat picks it up
    # without a restart.
    ingestion.on_index_rebuilt = retriever.reload

    if not retriever.is_ready():
        logger.warning(
            "No FAISS index found. Chat will report the knowledge base as "
            "unavailable until you build one: python -m app.ingest.build_index"
        )

    try:
        _agent = await agent_module.build_agent(await _make_checkpointer())
        logger.info("Agent ready: %s", _agent.describe())
    except Exception as exc:
        _startup_error = f"{type(exc).__name__}: {exc}"
        logger.exception("Could not build the agent")

    yield

    _agent = None
    await _resources.aclose()


app = FastAPI(
    title="NorthStar",
    description=(
        "Travel assistant combining a per-destination RAG knowledge base with "
        "live information from MCP tools."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str = Field(default="", max_length=64)


class ChatResponse(BaseModel):
    session_id: str
    answer: str
    kb_sources: list[dict]
    tool_calls: list[dict]
    degraded_tools: list[dict]
    trace_url: str | None = None


class ResetRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)


class UrlRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


class UpdateSourceRequest(BaseModel):
    """Fields a user may correct on an existing source. Omitted means unchanged."""

    destination: str | None = Field(default=None, max_length=120)
    title: str | None = Field(default=None, max_length=300)


class ConfirmRequest(BaseModel):
    preview_token: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=300)
    destination: str | None = Field(default=None, max_length=120)
    license: str | None = Field(default=None, max_length=200)
    publisher: str | None = Field(default=None, max_length=200)
    source_url: str | None = Field(default=None, max_length=2000)


def _require_agent() -> agent_module.TravelAgent:
    if _agent is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "The assistant is not available: "
                + (_startup_error or "it failed to start.")
                + " Check GET /health."
            ),
        )
    return _agent


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """One conversation turn."""
    travel_agent = _require_agent()
    session_id = request.session_id.strip() or uuid.uuid4().hex[:12]

    answer, provenance = await agent_module.ask(
        travel_agent, session_id, request.message.strip()
    )
    return ChatResponse(
        session_id=session_id,
        answer=answer,
        kb_sources=provenance.kb_sources,
        tool_calls=provenance.tool_calls,
        degraded_tools=travel_agent.toolset.degraded_summary(),
        trace_url=provenance.trace_url,
    )


@app.post("/reset")
async def reset(request: ResetRequest) -> dict:
    """Forget one conversation, in the store as well as in memory."""
    await agent_module.reset(_require_agent(), request.session_id)
    return {"session_id": request.session_id, "reset": True}


@app.get("/conversations")
async def conversations(limit: int = 40) -> dict:
    """Past conversations, newest first, so one can be resumed."""
    travel_agent = _require_agent()
    return {
        "conversations": await agent_module.list_conversations(
            travel_agent, limit=max(1, min(limit, 200))
        ),
        "store": settings.conversation_store,
        "persistent": settings.conversation_store == "sqlite",
    }


@app.get("/history/{session_id}")
async def history(session_id: str) -> dict:
    """The stored turns for a session, for debugging retained context."""
    return {
        "session_id": session_id,
        "messages": agent_module.history(_require_agent(), session_id),
    }


@app.get("/health")
async def health() -> dict:
    """Everything a reviewer needs to see whether the system is wired up."""
    index = retriever.status()
    return {
        "status": "ok" if (_agent is not None and index["ready"]) else "degraded",
        "startup_error": _startup_error,
        "destination": settings.destination,
        "dotenv_keys_shadowed_by_environment": (
            dotenv_keys_shadowed_by_environment()
        ),
        "llm": llm.describe(),
        "conversations": {
            "store": settings.conversation_store,
            "persistent": settings.conversation_store == "sqlite",
            "database": (
                str(settings.conversation_db)
                if settings.conversation_store == "sqlite"
                else None
            ),
        },
        "tracing": observability.describe(),
        "knowledge_base": {
            "ready": index["ready"],
            "destinations": index["destinations"],
            "loaded": index["loaded"],
            "relevance_floor": index["relevance_floor"],
            "retrieval_k": index["retrieval_k"],
            "manifest": index["manifest"],
        },
        "mcp": {
            "servers": (
                _agent.toolset.tools_by_server if _agent else {}
            ),
            "degraded": _agent.toolset.degraded_summary() if _agent else [],
            "expected_servers": sorted(mcp_client.SERVER_MODULES),
        },
        "agent": _agent.describe() if _agent else None,
    }


@app.get("/destinations")
async def destinations() -> dict:
    """Which places the knowledge base can actually answer about.

    The chat page reads this to build its example prompts, so the UI never
    suggests a destination the index cannot support.
    """
    summaries = retriever.destinations()
    return {
        "destinations": [s.model_dump() for s in summaries],
        "names": [s.destination for s in summaries],
        "default": settings.destination,
    }


@app.get("/sources")
async def sources() -> dict:
    """The knowledge-base source registry, with licences."""
    registry = SourceRegistry.load()
    registry.reconcile()
    return {
        "sources": [record.model_dump() for record in registry.sources],
        "available": len(registry.available()),
        "total_chunks": registry.total_chunks(),
    }


# ---------------------------------------------------------------------------
# Admin / ingestion
# ---------------------------------------------------------------------------
@app.get("/admin/status")
async def admin_status() -> dict:
    return ingestion.status()


@app.post("/admin/sources/url")
async def admin_preview_url(request: UrlRequest) -> dict:
    return ingestion.preview_url(request.url)


@app.post("/admin/sources/upload")
async def admin_preview_upload(file: UploadFile = File(...)) -> dict:
    data = await file.read()
    return ingestion.preview_upload(data, file.filename or "upload")


@app.post("/admin/sources/confirm")
async def admin_confirm(request: ConfirmRequest) -> dict:
    return ingestion.confirm(
        request.preview_token,
        title=request.title,
        destination=request.destination,
        license_=request.license,
        publisher=request.publisher,
        source_url=request.source_url,
    )


@app.patch("/admin/sources/{source_id}")
async def admin_update_source(
    source_id: str, request: UpdateSourceRequest
) -> dict:
    """Correct a source's destination and/or title, then re-index."""
    if request.destination is None and request.title is None:
        raise HTTPException(
            status_code=400,
            detail="Provide a destination, a title, or both.",
        )
    return ingestion.update_source(
        source_id, destination=request.destination, title=request.title
    )


@app.post("/admin/sources/{source_id}/refresh")
async def admin_refresh_source(source_id: str) -> dict:
    """Re-fetch one document from its source URL, then re-index."""
    return ingestion.refresh_source(source_id)


@app.post("/admin/destinations/{destination}/refresh")
async def admin_refresh_destination(destination: str) -> dict:
    """Re-fetch every URL-backed document for one place, then re-index once."""
    return ingestion.refresh_destination(destination)


@app.delete("/admin/sources/{source_id}")
async def admin_remove(source_id: str) -> dict:
    return ingestion.remove_source(source_id)


@app.delete("/admin/destinations/{destination}")
async def admin_remove_destination(destination: str) -> dict:
    """Remove every document for one place, then rebuild."""
    return ingestion.remove_destination(destination)


@app.post("/admin/rebuild")
async def admin_rebuild() -> dict:
    return ingestion.start_rebuild(reason="requested from /admin")


@app.get("/admin/jobs/{job_id}")
async def admin_job(job_id: str) -> dict:
    return ingestion.job(job_id)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
@app.exception_handler(IngestionError)
async def ingestion_error_handler(request: Request, exc: IngestionError):
    """Ingestion problems are user input problems, not server faults."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    """Return something actionable instead of leaking a stack trace."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "detail": (
                f"{type(exc).__name__}: {exc}. "
                "See the server log for the full traceback."
            )
        },
    )


# ---------------------------------------------------------------------------
# Static pages (mounted last so they cannot shadow the API routes)
# ---------------------------------------------------------------------------
#: The pages are edited during development and served from disk, so a cached
#: copy shows stale markup against a freshly restarted backend -- which looks
#: exactly like a broken deployment. They are small; do not cache them.
_NO_CACHE = {"Cache-Control": "no-store, must-revalidate"}


@app.get("/")
async def chat_page() -> FileResponse:
    return FileResponse(settings.static_dir / "index.html", headers=_NO_CACHE)


@app.get("/admin")
async def admin_page() -> FileResponse:
    return FileResponse(settings.static_dir / "admin.html", headers=_NO_CACHE)


app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")
