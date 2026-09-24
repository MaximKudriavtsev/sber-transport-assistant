from fastapi.testclient import TestClient

from app.main import app, service


client = TestClient(app)


def test_production_hides_api_docs():
    from app.main import api_docs_urls

    assert api_docs_urls("production") == {
        "docs_url": None,
        "redoc_url": None,
        "openapi_url": None,
    }
    assert api_docs_urls("development")["docs_url"] == "/docs"


def test_health():
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["agent_ready"] is True
    assert data["retrieval_mode"] == "agentic_text_search"
    assert data["rag_chunks"] >= 45


def test_function_call_tool_result_and_final_answer(monkeypatch):
    calls = []
    async def fake_completion(messages, functions, function_call):
        calls.append(messages)
        if len(calls) == 1:
            return {"finish_reason": "function_call", "message": {
                "role": "assistant", "content": "", "functions_state_id": "state-1",
                "function_call": {"name": "search_official_sources", "arguments": {"query": "как вывести банковскую карту из стоп-листа"}},
            }}
        assert messages[-1]["role"] == "function"
        assert messages[-2]["functions_state_id"] == "state-1"
        return {"finish_reason": "stop", "message": {"role": "assistant", "content":
            '{"status":"answered","answer":"Порядок вывода карты из стоп-листа указан в официальном FAQ.","used_source_ids":["oeirc-faq-current"]}'}}
    monkeypatch.setattr(service.gigachat, "chat_completion", fake_completion)
    response = client.post("/api/chat", json={"message": "Сколько стоит проезд?"})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "answered"
    assert data["sources"]
    assert data["conversation_id"]
    assert len(calls) == 2


def test_multi_turn_history_is_sent(monkeypatch):
    observed = []
    async def fake_completion(messages, functions, function_call):
        observed.append(messages)
        return {"finish_reason": "stop", "message": {"role": "assistant", "content":
            '{"status":"clarify","answer":"Уточните категорию.","used_source_ids":[]}'}}
    monkeypatch.setattr(service.gigachat, "chat_completion", fake_completion)
    first = client.post("/api/chat", json={"message": "Какие льготы?"}).json()
    client.post("/api/chat", json={"message": "А студентам?", "conversation_id": first["conversation_id"]})
    second_messages = observed[1]
    assert any(row.get("content") == "Какие льготы?" for row in second_messages)
    assert any(row.get("content") == "А студентам?" for row in second_messages)


def test_no_data_has_no_sources(monkeypatch):
    async def fake_completion(messages, functions, function_call):
        return {"finish_reason": "stop", "message": {"role": "assistant", "content":
            '{"status":"no_data","answer":"Официального подтверждения нет.","used_source_ids":[]}'}}
    monkeypatch.setattr(service.gigachat, "chat_completion", fake_completion)
    response = client.post("/api/chat", json={"message": "Можно ли провозить альпаку бесплатно?"})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "no_data"
    assert data["sources"] == []


def test_service_error_is_not_no_data(monkeypatch):
    from app.gigachat_client import GigaChatError
    async def broken(*args, **kwargs):
        raise GigaChatError("temporary")
    monkeypatch.setattr(service.gigachat, "chat_completion", broken)
    response = client.post("/api/chat", json={"message": "Сколько стоит проезд?"})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "service_error"
