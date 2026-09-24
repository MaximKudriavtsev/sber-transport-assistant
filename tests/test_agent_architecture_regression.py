"""Regression contract for the semantic-first agent path."""
from fastapi.testclient import TestClient

from app.main import app, service
from app.conversation import ConversationStore
from app.agent_tools import ToolRunContext
from app.config import get_settings
from app.text_search import OfficialTextSearch
import json

client = TestClient(app)


def test_every_message_reaches_gigachat_before_a_reply(monkeypatch):
    seen = []

    async def completion(messages, functions, function_call):
        seen.append(messages[-1]["content"])
        return {"finish_reason": "stop", "message": {"role": "assistant", "content":
            '{"status":"clarify","answer":"Чем вы пытаетесь оплатить: банковской картой, «Тройкой» или социальной картой?","task_mode":"troubleshooting","issue_type":"payment_problem","severity":"normal","state_patch":{"pending_clarification":"card_type"},"used_source_ids":[]}'}}

    monkeypatch.setattr(service.gigachat, "chat_completion", completion)
    result = client.post("/api/chat", json={"message": "Не проходит оплата, что делать?"}).json()
    assert seen == ["Не проходит оплата, что делать?"]
    assert result["status"] == "clarify"
    assert result["task_mode"] == "troubleshooting"
    assert "Тип проблемы недостаточно" not in result["answer"]


def test_plain_language_final_is_repaired_by_model_not_regex(monkeypatch):
    calls = 0

    async def completion(messages, functions, function_call):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"finish_reason": "stop", "message": {"role": "assistant", "content": "Уточните, пожалуйста, чем вы пытаетесь оплатить?"}}
        assert "Поле answer обязательно" in messages[-1]["content"]
        return _final("Уточните, пожалуйста, чем вы пытаетесь оплатить?", "clarify", "troubleshooting", "payment_problem", pending="card_type")

    monkeypatch.setattr(service.gigachat, "chat_completion", completion)
    result = client.post("/api/chat", json={"message": "Не проходит оплата"}).json()
    assert calls == 2
    assert result["status"] == "clarify"
    assert result["answer"] == "Уточните, пожалуйста, чем вы пытаетесь оплатить?"


def test_two_invalid_final_turns_fail_closed_after_one_repair(monkeypatch):
    calls = 0

    async def completion(messages, functions, function_call):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"finish_reason": "stop", "message": {"role": "assistant", "content": "Чем вы пытаетесь оплатить?"}}
        if calls == 2:
            return {"finish_reason": "stop", "message": {"role": "assistant", "content":
                '{"status":"clarify","task_mode":"troubleshooting","issue_type":"payment_problem","severity":"normal","state_patch":{"pending_clarification":"card_type"},"used_source_ids":[]}'}}

    monkeypatch.setattr(service.gigachat, "chat_completion", completion)
    result = client.post("/api/chat", json={"message": "Не могу оплатить"}).json()
    assert calls == 2
    assert result["status"] == "service_error"
    assert "tool_calls" not in result["answer"]


def test_tool_markup_never_becomes_final_answer_and_repairs_once(monkeypatch):
    marker = '< Владимиtool_calls>\n< Владимиinvoke name="search_official_sources">'
    calls = 0

    async def completion(messages, functions, function_call):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"finish_reason": "function_call", "message": {"role": "assistant", "content": marker,
                "function_call": {"name": "search_official_sources", "arguments": {"query": "банковская карта стоп-лист"}}}}
        if calls == 2:
            assert messages[-1]["role"] == "function"
            return {"finish_reason": "stop", "message": {"role": "assistant", "content":
                '{"status":"answered","task_mode":"troubleshooting","issue_type":"payment_problem","state_patch":{"slots":{"card_type":"bank"}}}'}}
        assert "Поле answer обязательно" in messages[-1]["content"]
        return _final("Проверьте банковскую карту в личном кабинете пассажира.", "answered",
                      "troubleshooting", "payment_problem", slots={"card_type": "bank"})

    monkeypatch.setattr(service.gigachat, "chat_completion", completion)
    result = client.post("/api/chat", json={"message": "Банковская"}).json()
    assert calls == 3
    assert result["status"] == "answered"
    assert result["answer"] == "Проверьте банковскую карту в личном кабинете пассажира."
    assert "tool_calls" not in result["answer"] and "invoke" not in result["answer"]


def test_failed_finalization_repair_fails_closed_without_markup(monkeypatch):
    calls = 0

    async def completion(messages, functions, function_call):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _tool("search_official_sources", {"query": "банковская карта стоп-лист"})
        return {"finish_reason": "stop", "message": {"role": "assistant", "content":
            '{"status":"answered","answer":"< Владимиtool_calls>"}' if calls == 2 else '{"status":"answered"}'}}

    monkeypatch.setattr(service.gigachat, "chat_completion", completion)
    result = client.post("/api/chat", json={"message": "Банковская"}).json()
    assert calls == 3
    assert result["status"] == "service_error"
    assert "tool_calls" not in result["answer"]


def test_valid_final_needs_no_repair(monkeypatch):
    calls = 0

    async def completion(messages, functions, function_call):
        nonlocal calls
        calls += 1
        return _final("Уточните город.", "clarify", "complaint", "missed_trip", pending="municipality")

    monkeypatch.setattr(service.gigachat, "chat_completion", completion)
    result = client.post("/api/chat", json={"message": "Автобус не приехал"}).json()
    assert calls == 1 and result["answer"] == "Уточните город."


def test_responsibility_tool_does_not_overwrite_natural_answer(monkeypatch):
    calls = 0

    async def completion(messages, functions, function_call):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"finish_reason": "function_call", "message": {"role": "assistant", "content": "", "function_call": {"name": "resolve_responsibility", "arguments": {"issue_type": "driver_behavior", "municipality": "tula"}}}}
        return {"finish_reason": "stop", "message": {"role": "assistant", "content":
            '{"status":"answered","answer":"На поведение водителя можно пожаловаться в администрацию Тулы.","task_mode":"complaint","issue_type":"driver_behavior","severity":"normal","used_source_ids":[]}'}}

    monkeypatch.setattr(service.gigachat, "chat_completion", completion)
    result = client.post("/api/chat", json={"message": "водитель нахамил в Туле"}).json()
    assert calls == 2
    assert result["answer"] == "На поведение водителя можно пожаловаться в администрацию Тулы."
    assert result["authority"]["name"] == "Администрация города Тулы"


def test_state_merge_preserves_known_fields_and_accepts_corrections():
    store = ConversationStore()
    cid = store.create()
    store.merge_state(cid, {"task_mode": "troubleshooting", "issue_type": "payment_problem", "slots": {"municipality": "uzlovaya", "card_type": "bank"}, "severity": "safety"})
    state = store.merge_state(cid, {"issue_type": "unknown", "slots": {"card_type": None, "municipality": "novomoskovsk"}, "severity": "normal"})
    assert state["issue_type"] == "payment_problem"
    assert state["slots"]["card_type"] == "bank"
    assert state["slots"]["municipality"] == "novomoskovsk"
    assert state["severity"] == "normal"
    assert store.merge_state(cid, {"slots": {"card_type": "troika"}})["slots"]["card_type"] == "troika"


def test_legacy_safety_mode_migrates_on_read():
    store = ConversationStore()
    cid = store.create()
    store.merge_state(cid, {"task_mode": "safety_emergency", "issue_type": "unsafe_driver", "severity": "safety"})
    state = store.dialogue_state(cid)
    assert state["task_mode"] == "safety"
    assert state["issue_type"] == "unsafe_driver"
    assert state["severity"] == "safety"


def test_emergency_guidance_is_verified_source_data():
    tool = ToolRunContext(OfficialTextSearch(get_settings()))
    result = tool.execute("get_emergency_guidance", {})
    assert result["verified"] is True
    assert result["emergency_number"] == "112"
    assert result["source_url"].startswith("https://")


def test_unknown_route_and_safety_competence_fail_closed():
    tool = ToolRunContext(OfficialTextSearch(get_settings()))
    assert tool.execute("resolve_route", {"route_number": "9999", "municipality": "tula"})["status"] == "not_found"
    for issue in ("unsafe_driver", "vehicle_defect_hazard"):
        result = tool.execute("resolve_responsibility", {"issue_type": issue})
        assert result["status"] == "resolved"
        assert result["primary_authority"]["id"] == "rostransnadzor_tula"


def _final(answer, status, mode, issue, severity="normal", slots=None, pending=None, sources=None):
    return {"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps({
        "status": status, "answer": answer, "task_mode": mode, "issue_type": issue,
        "severity": severity, "state_patch": {"slots": slots or {}, "pending_clarification": pending},
        "used_source_ids": sources or []}, ensure_ascii=False)}}


def _tool(name, args):
    return {"finish_reason": "function_call", "message": {"role": "assistant", "content": "",
            "function_call": {"name": name, "arguments": args}}}


def test_reconcile_state_invariants():
    from app.conversation import new_dialogue_state, reconcile_state
    base = new_dialogue_state()
    for key, value in (("card_type", "bank"), ("payment_method", "bank"),
                       ("benefit_category", "student"), ("route_number", "208")):
        previous = new_dialogue_state()
        previous["pending_clarification"] = key
        after = reconcile_state(previous, {"slots": {key: value}}, [])
        assert after["slots"][key] == value and after["pending_clarification"] is None
    for city, canonical in (("Узловая", "uzlovaya"), ("Новомосковск", "novomoskovsk")):
        previous = new_dialogue_state()
        previous["pending_clarification"] = "municipality"
        after = reconcile_state(previous, {}, [{"name": "resolve_responsibility",
            "arguments": {"municipality": city}, "result": {"status": "resolved"}}])
        assert after["slots"]["municipality"] == canonical and after["pending_clarification"] is None
    previous = new_dialogue_state()
    previous["slots"]["municipality"] = "uzlovaya"
    previous["slots"]["card_type"] = "bank"
    after = reconcile_state(previous, {"slots": {"card_type": None}}, [], {"municipality": "novomoskovsk"})
    assert after["slots"]["municipality"] == "novomoskovsk" and after["slots"]["card_type"] == "bank"
    for mode, issue, severity, expected in (("safety", "unsafe_driver", "normal", "safety"),
        ("safety", "unsafe_driver", "immediate_danger", "safety"),
        ("complaint", "driver_behavior", "normal", "normal")):
        after = reconcile_state(base, {"task_mode": mode, "issue_type": issue, "severity": severity}, [])
        assert after["severity"] == expected


def test_source_applicability_requires_confirmed_scope():
    from app.text_search import source_applicable
    local = {"scope": "municipality", "municipality": "tula"}
    assert not source_applicable(local, {"municipality": None, "operator": None})
    assert source_applicable(local, {"municipality": "tula"})
    assert source_applicable({"scope": "region"}, {"municipality": None})
    operator = {"scope": "operator", "operator": 'МКП "Тулгорэлектротранс"'}
    assert not source_applicable(operator, {"operator": None})
    assert source_applicable(operator, {"operator": 'МКП "Тулгорэлектротранс"'})
    assert not source_applicable({"scope": "unknown"}, {"municipality": "tula"})


def test_generic_payment_search_labels_unknown_operator_source():
    from app.agent_tools import ToolRunContext
    from app.text_search import CONDITIONAL_OPERATOR_NOTE

    search = OfficialTextSearch(get_settings())
    context = {"municipality": None, "operator": None}
    hits = search.search("как вывести банковскую карту из стоп-листа", top_k=8, context=context)
    assert hits
    operator_hits = [hit for hit in hits if hit.row["source_id"] == "tulatrans-payment-current"]
    assert operator_hits
    assert all(hit.applicability == "conditional" for hit in operator_hits)
    assert any(hit.row["source_id"] == "oeirc-faq-current" for hit in hits)
    published = ToolRunContext(search, context).public_row(operator_hits[0].row, operator_hits[0].score)
    assert published["applicability"] == "conditional"
    assert published["scope"] == "operator"
    assert published["operator"] == 'МКП "Тулгорэлектротранс"'
    assert published["applicability_note"] == CONDITIONAL_OPERATOR_NOTE
    assert search.details(operator_hits[0].row["id"], context=context)
    assert search.details(operator_hits[0].row["id"], context={"operator": "ООО «ИРБИС»"}) == []


def test_foreign_operator_hides_local_payment_source():
    search = OfficialTextSearch(get_settings())
    hits = search.search("как вывести банковскую карту из стоп-листа", top_k=8,
                         context={"operator": "ООО «ИРБИС»"})
    assert hits
    assert all(hit.row["source_id"] != "tulatrans-payment-current" for hit in hits)
    assert any(hit.row["source_id"] == "oeirc-faq-current" for hit in hits)


def test_payment_clarification_then_official_troubleshooting(monkeypatch):
    calls = 0

    async def completion(messages, functions, function_call):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _final("Чем вы пытаетесь оплатить: банковской картой, «Тройкой» или социальной картой?",
                          "clarify", "troubleshooting", "payment_problem", pending="card_type")
        if calls == 2:
            assert messages[-1]["content"] == "Банковская"
            assert '"issue_type": "payment_problem"' in messages[0]["content"]
            return _tool("search_official_sources", {"query": "банковская карта не проходит оплата проезда стоп-лист"})
        rows = json.loads(messages[-1]["content"])["results"]
        return _final("Проверьте, не находится ли карта в стоп-листе; порядок действий указан в официальном источнике.",
                      "answered", "troubleshooting", "payment_problem",
                      sources=[rows[0]["source_id"]])

    monkeypatch.setattr(service.gigachat, "chat_completion", completion)
    first = client.post("/api/chat", json={"message": "Не проходит оплата"}).json()
    second = client.post("/api/chat", json={"message": "Банковская", "conversation_id": first["conversation_id"]}).json()
    assert calls == 3
    assert first["status"] == "clarify" and first["authority"] is None
    assert second["status"] == "answered" and second["sources"]
    assert second["dialogue_state"]["slots"]["card_type"] == "bank"
    assert second["dialogue_state"]["pending_clarification"] is None
    assert second["authority"] is None


def test_immediate_danger_calls_verified_safety_tool_first(monkeypatch):
    calls = 0

    async def completion(messages, functions, function_call):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _tool("get_emergency_guidance", {})
        data = json.loads(messages[-1]["content"])
        assert data["verified"] is True
        return _final(f"Если поездка сейчас опасна, сообщите о ситуации по номеру {data['emergency_number']}.",
                      "answered", "safety", "unsafe_driver", "immediate_danger")

    monkeypatch.setattr(service.gigachat, "chat_completion", completion)
    result = client.post("/api/chat", json={"message": "водитель пьяный, мы сейчас едем"}).json()
    assert calls == 2
    assert result["severity"] == "immediate_danger"
    assert result["task_mode"] == "safety"
    assert result["authority"] is None
    assert result["sources"][0]["url"].startswith("https://")


def test_route_208_then_uzlovaya_uses_route_scope(monkeypatch):
    calls = 0

    async def completion(messages, functions, function_call):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _final("В каком городе или между какими населёнными пунктами ходит маршрут 208?",
                          "clarify", "complaint", "missed_trip", slots={"route_number": "208"}, pending="municipality")
        if calls == 2:
            return _tool("resolve_route", {"route_number": "208", "municipality": "uzlovaya"})
        if calls == 3:
            route = json.loads(messages[-1]["content"])["route"]
            assert route["route_scope"] == "intermunicipal"
            return _tool("resolve_responsibility", {"issue_type": "missed_trip", "route_scope": route["route_scope"]})
        return _final("По межмуниципальному маршруту можно обратиться к Организатору перевозок.",
                      "answered", "complaint", "missed_trip", slots={"municipality": "uzlovaya", "route_scope": "intermunicipal"})

    monkeypatch.setattr(service.gigachat, "chat_completion", completion)
    first = client.post("/api/chat", json={"message": "208 опять не приехал"}).json()
    second = client.post("/api/chat", json={"message": "Узловая", "conversation_id": first["conversation_id"]}).json()
    assert calls == 4
    assert second["route_resolution"]["route"]["route_scope"] == "intermunicipal"
    assert "Организатор перевозок" in second["authority"]["name"]


def test_unknown_route_is_no_data_after_resolver(monkeypatch):
    calls = 0

    async def completion(messages, functions, function_call):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _tool("resolve_route", {"route_number": "9999"})
        assert json.loads(messages[-1]["content"])["status"] == "not_found"
        return _final("Подтверждённого перевозчика маршрута 9999 я не нашёл.",
                      "no_data", "route_help", "route_operator")

    monkeypatch.setattr(service.gigachat, "chat_completion", completion)
    result = client.post("/api/chat", json={"message": "9999 маршрут кто возит?"}).json()
    assert result["status"] == "no_data"
    assert result["authority"] is None and result["sources"] == []


def test_missed_trip_clarification_uses_or_slot_policy(monkeypatch):
    calls = 0

    async def completion(messages, functions, function_call):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _final("Укажите город и номер маршрута.", "clarify", "complaint", "missed_trip", pending="municipality, route_number")
        assert "альтернативы" in messages[-1]["content"]
        return _final("Подскажите, в каком городе это произошло?", "clarify", "complaint", "missed_trip", pending="municipality")

    monkeypatch.setattr(service.gigachat, "chat_completion", completion)
    first = client.post("/api/chat", json={"message": "Автобус опять не приехал"}).json()
    assert calls == 2
    assert first["answer"] == "Подскажите, в каком городе это произошло?"
    assert first["dialogue_state"]["pending_clarification"] == "municipality"
    from app.agent_service import SYSTEM_PROMPT
    assert "При status=clarify за один ход спрашивай только поле pending_clarification" in SYSTEM_PROMPT
    assert "номер маршрута" not in first["answer"]


def test_missed_trip_city_resolves_novomoskovsk_without_route(monkeypatch):
    calls = 0

    async def completion(messages, functions, function_call):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _final("Подскажите, в каком городе это произошло?", "clarify", "complaint", "missed_trip", pending="municipality")
        if calls == 2:
            return _tool("resolve_responsibility", {"issue_type": "missed_trip", "municipality": "novomoskovsk"})
        return _final("Обращение по городскому маршруту принимает администрация Новомосковска.", "answered", "complaint", "missed_trip", slots={"municipality": "novomoskovsk"})

    monkeypatch.setattr(service.gigachat, "chat_completion", completion)
    first = client.post("/api/chat", json={"message": "Автобус опять не приехал"}).json()
    second = client.post("/api/chat", json={"message": "Новомосковск", "conversation_id": first["conversation_id"]}).json()
    assert calls == 3
    assert "Новомосковск" in second["authority"]["name"]
    assert second["dialogue_state"]["slots"]["route_number"] is None


def test_safety_ambiguity_then_past_incident(monkeypatch):
    calls = 0

    async def completion(messages, functions, function_call):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _final("Ситуация происходит сейчас или уже закончилась?", "clarify", "safety", "unsafe_driver", "safety", pending="immediacy")
        if calls == 2:
            return _tool("resolve_responsibility", {"issue_type": "unsafe_driver"})
        return _final("Подайте обращение в Ространснадзор по Тульской области.", "answered", "complaint", "unsafe_driver", "safety")

    monkeypatch.setattr(service.gigachat, "chat_completion", completion)
    first = client.post("/api/chat", json={"message": "водитель пьяный"}).json()
    second = client.post("/api/chat", json={"message": "я уже вышел, это было вчера", "conversation_id": first["conversation_id"]}).json()
    assert first["task_mode"] == "safety" and first["severity"] == "safety"
    assert second["task_mode"] == "complaint" and second["severity"] == "safety"
    assert second["authority"]["name"].startswith("Территориальный отдел")


def test_final_parser_recovers_harmless_trailing_brace():
    raw = '{"status":"answered","answer":"OK"}}'
    parsed = service._parse_final(raw)
    assert parsed["status"] == "answered"
    assert parsed["answer"] == "OK"


def test_final_parser_recovers_at_most_two_trailing_braces():
    raw = '{"status":"answered","answer":"OK"}}}'
    parsed = service._parse_final(raw)
    assert parsed["status"] == "answered"
    assert parsed["answer"] == "OK"


def test_final_parser_rejects_text_or_second_json_after_object():
    for raw in (
        '{"status":"answered","answer":"OK"} SECOND',
        '{"status":"answered","answer":"OK"} {"status":"answered","answer":"OTHER"}',
    ):
        assert service._parse_final(raw)["status"] == "service_error"


def test_final_parser_still_requires_nonempty_answer():
    assert service._parse_final('{"status":"answered"}}')["status"] == "service_error"


def test_final_parser_rejects_tool_markup_in_answer():
    raw = '{"status":"answered","answer":"<tool_calls>search_official_sources</tool_calls>"}'
    assert service._parse_final(raw)["status"] == "service_error"


def test_final_parser_accepts_normal_valid_json_without_repair():
    raw = '{"status":"clarify","answer":"Уточните город."}'
    parsed = service._parse_final(raw)
    assert parsed == {"status": "clarify", "answer": "Уточните город."}
