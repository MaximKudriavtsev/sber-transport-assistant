from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import BASE_DIR, get_settings
from .agent_service import AgentAssistantService
from .conversation import ConversationStore
from .gigachat_client import GigaChatClient
from .text_search import OfficialTextSearch
from .schemas import ChatRequest, ChatResponse, HealthResponse


def api_docs_urls(app_env: str) -> dict[str, str | None]:
    if app_env.lower() in {"development", "dev"}:
        return {"docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json"}
    return {"docs_url": None, "redoc_url": None, "openapi_url": None}


class CachedStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=86400"
        return response


settings = get_settings()
search = OfficialTextSearch(settings)
conversations = ConversationStore(max_messages=12)
service = AgentAssistantService(settings, search, conversations)
gigachat_diagnostics = GigaChatClient(settings)

app = FastAPI(
    title=settings.app_name,
    description="ИИ-ассистент первой линии поддержки пассажиров общественного транспорта",
    version="0.4.0",
    **api_docs_urls(settings.app_env),
)

STATIC_DIR = BASE_DIR / "static"
app.mount("/static", CachedStaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        app=settings.app_name,
        demo_mode=settings.demo_mode,
        knowledge_items=0,
        rag_ready=search.ready,
        rag_chunks=len(search.chunks),
        agent_ready=bool(search.ready and not settings.demo_mode),
        retrieval_mode="agentic_text_search" if search.ready else "unavailable",
    )


@app.get("/api/diagnostics/gigachat")
async def diagnose_gigachat() -> dict:
    result = await gigachat_diagnostics.diagnose()
    result.update({
        "rag_chunks": len(search.chunks),
        "retrieval": {"mode": "agentic_text_search", "ready": search.ready},
        "agent": {"ready": bool(result.get("chat_ok") and result.get("function_calling", {}).get("ready") and search.ready)},
        "source_snapshots_ready": settings.source_snapshots_path.exists(),
        "sources_count": len([s for s in __import__('json').loads(settings.sources_path.read_text(encoding='utf-8')) if s.get('enabled', True)]),
        "embeddings_dependency": "none",
        "knowledge_items": 0,
    })
    return result


@app.post("/api/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest) -> ChatResponse:
    return await service.chat(payload.message.strip(), payload.conversation_id)


@app.delete("/api/conversations/{conversation_id}")
async def clear_conversation(conversation_id: str) -> dict:
    conversations.clear(conversation_id)
    return {"ok": True}


@app.get("/api/debug/conversations/{conversation_id}")
async def conversation_trace(conversation_id: str) -> dict:
    if not settings.is_development:
        return {"enabled": False}
    return {"enabled": True, "trace": service.traces.get(conversation_id, [])}
