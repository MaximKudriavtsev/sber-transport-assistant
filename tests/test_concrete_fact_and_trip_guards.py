"""Guardrails for numeric evidence and confirmed current-trip status."""
import json

from fastapi.testclient import TestClient

from app.agent_service import explicit_trip_status
from app.conversation import new_dialogue_state, reconcile_state
from app.fact_guard import unsupported_concrete_facts
from app.main import app, service


client = TestClient(app)


def _final(status, answer, severity="safety", pending=None):
    return {"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps({
        "status": status, "answer": answer, "task_mode": "safety", "issue_type": "unsafe_driver",
        "severity": severity, "state_patch": {"pending_clarification": pending, "slots": {}},
        "used_source_ids": []}, ensure_ascii=False)}}


def _price(answer: str):
    return {"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps({
        "status": "answered", "answer": answer, "task_mode": "information", "issue_type": "unknown",
        "severity": "normal", "state_patch": {"slots": {}}, "used_source_ids": []}, ensure_ascii=False)}}


def _answered(answer: str, source_ids: list[str] | None = None):
    return {"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps({
        "status": "answered", "answer": answer, "task_mode": "information", "issue_type": "unknown",
        "severity": "normal", "state_patch": {"slots": {}}, "used_source_ids": source_ids or []}, ensure_ascii=False)}}


def test_schedule_question_is_no_data_while_registry_empty(monkeypatch):
    from app.text_search import SearchHit

    hit = SearchHit(row={
        "id": "chunk-schedule", "source_id": "foreign-hours", "title": "Режим офиса",
        "url": "https://example.test/hours", "text": "Офис открыт в 07:40.",
    }, score=1.0)

    async def completion(messages, functions, function_call):
        if not any(item.get("role") == "function" for item in messages):
            return {"finish_reason": "function_call", "message": {
                "role": "assistant", "content": "",
                "function_call": {"name": "search_official_sources", "arguments": {"query": "расписание автобуса 27"}},
            }}
        return _answered("Автобус 27 отходит в 07:40.", ["chunk-schedule"])

    monkeypatch.setattr(service.search, "search", lambda *args, **kwargs: [hit])
    monkeypatch.setattr(service.gigachat, "chat_completion", completion)
    result = client.post("/api/chat", json={"message": "Во сколько отходит автобус 27?"}).json()
    assert result["status"] == "no_data"
    assert "07:40" not in result["answer"]
    assert "не загружено" in result["answer"]


def test_pass_price_without_fare_card_is_still_rejected(monkeypatch):
    async def completion(messages, functions, function_call):
        return _price("Льготный проездной стоит 750 рублей.")

    monkeypatch.setattr(service.gigachat, "chat_completion", completion)
    result = client.post("/api/chat", json={"message": "Сколько стоит льготный проездной?"}).json()
    assert result["status"] == "no_data"
    assert "750" not in result["answer"]


def test_high_risk_numeric_claims_need_exact_evidence():
    assert "10:minutes" in unsupported_concrete_facts("Восстановится через 10 минут.", "Проверьте стоп-лист.")
    assert not unsupported_concrete_facts("Восстановится через 24 часа.", "Восстановится через 24 часов.")
    assert not unsupported_concrete_facts("Телефон +7 (4872) 52-09-02.",
                                          '{"verified": true, "phone": "+7 (4872) 52-09-02"}')
    assert "38:rubles" in unsupported_concrete_facts("Стоимость 38 ₽.", "Банковская карта принимается.")
    assert "10-20:minutes" in unsupported_concrete_facts("Через 10–20 минут.", "Через 20 минут.")


def test_trip_status_requires_explicit_statement_or_pending_answer():
    assert explicit_trip_status("водитель пьяный") is None
    assert explicit_trip_status("водитель пьяный, мы сейчас едем") is True
    assert explicit_trip_status("я уже вышел, это было вчера") is False
    assert explicit_trip_status("да", "is_trip_ongoing") is True
    assert explicit_trip_status("нет", "is_trip_ongoing") is False
    assert explicit_trip_status("да") is None


def test_immediate_danger_requires_confirmed_trip():
    for ongoing, expected in ((None, "safety"), (False, "safety"), (True, "immediate_danger")):
        before = new_dialogue_state()
        before["slots"]["is_trip_ongoing"] = ongoing
        after = reconcile_state(before, {"task_mode": "safety", "issue_type": "unsafe_driver",
                                         "severity": "immediate_danger"}, [])
        assert after["severity"] == expected


def test_ambiguous_trip_blocks_emergency_tool_and_asks_when(monkeypatch):
    calls = 0

    async def completion(messages, functions, function_call):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"finish_reason": "function_call", "message": {"role": "assistant", "content": "",
                "function_call": {"name": "get_emergency_guidance", "arguments": {}}}}
        assert json.loads(messages[-1]["content"])["blocked"] is True
        return _final("answered", "Звоните 112.", "immediate_danger")

    monkeypatch.setattr(service.gigachat, "chat_completion", completion)
    result = client.post("/api/chat", json={"message": "водитель пьяный"}).json()
    trace = service.traces[result["conversation_id"]]
    assert calls == 2 and result["status"] == "clarify"
    assert result["severity"] == "safety" and result["dialogue_state"]["slots"]["is_trip_ongoing"] is None
    assert result["dialogue_state"]["pending_clarification"] == "is_trip_ongoing"
    assert trace["tool_calls"] == [] and trace["blocked_tool_calls"][0]["name"] == "get_emergency_guidance"
    assert "112" not in result["answer"]


def test_confirmed_trip_allows_emergency_tool(monkeypatch):
    calls = 0

    async def completion(messages, functions, function_call):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"finish_reason": "function_call", "message": {"role": "assistant", "content": "",
                "function_call": {"name": "get_emergency_guidance", "arguments": {}}}}
        assert json.loads(messages[-1]["content"])["verified"] is True
        return _final("answered", "Если есть непосредственная опасность, позвоните 112.", "immediate_danger")

    monkeypatch.setattr(service.gigachat, "chat_completion", completion)
    result = client.post("/api/chat", json={"message": "водитель пьяный, мы сейчас едем"}).json()
    trace = service.traces[result["conversation_id"]]
    assert result["severity"] == "immediate_danger"
    assert result["dialogue_state"]["slots"]["is_trip_ongoing"] is True
    assert trace["tool_calls"][0]["name"] == "get_emergency_guidance"
