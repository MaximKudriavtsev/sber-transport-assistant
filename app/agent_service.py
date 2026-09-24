"""Semantic-first agent orchestration; factual decisions remain in tools."""
import json
import logging
import re

from .agent_tools import FUNCTIONS, ToolRunContext
from .config import Settings
from .conversation import ConversationStore, SLOTS, canonical_card_type
from .gigachat_client import GigaChatClient, GigaChatError
from .fact_guard import unsupported_concrete_facts
from .responsibility import ISSUE_TYPES, ResponsibilityRouter
from .schemas import ChatResponse, SourceItem
from .text_search import OfficialTextSearch

logger = logging.getLogger(__name__)
TASK_MODES = {"information", "troubleshooting", "complaint", "safety", "benefit_help", "route_help", "unknown"}
TASK_MODE_ALIASES = {"safety_emergency": "safety"}
SEVERITIES = {"normal", "safety", "immediate_danger"}
VALID_ISSUES = ISSUE_TYPES | {"benefit_eligibility", "benefit_issuance", "route_identity", "route_scope", "route_operator", "accident"}
ISSUE_ALIASES = {"payment_failure": "payment_problem", "bank_card_failure": "payment_problem", "missed_bus": "missed_trip", "unsafe_vehicle": "vehicle_defect_hazard"}
INTERNAL_REASONS = {"Тип проблемы недостаточно определён для адресации.", "Подтверждённый адресат не найден."}
TOOL_MARKUP = re.compile(r"<\s*/?\s*[\w|]*(?:tool_calls|function_call|invoke|parameter)\b", re.I)
CLOCK_TIME = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
NO_SCHEDULE_ANSWER = "Расписание в официальной базе помощника пока не загружено."
DEPARTURE_MARKERS = (
    "расписан", "во сколько", "время отправлен", "время прибыт",
    "когда отход", "когда отправ", "когда приход", "когда прибы",
)
TRIP_PENDING = {"is_trip_ongoing", "is_now", "is_current_trip", "is_trip_ongoing", "is_trip_ongoing_now"}


def asks_departure_time(message: str) -> bool:
    text = message.lower().replace("ё", "е")
    return any(marker in text for marker in DEPARTURE_MARKERS)


def clock_times(text: str) -> set[str]:
    found = set()
    for match in CLOCK_TIME.finditer(text):
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour <= 23 and minute <= 59:
            found.add(f"{hour:02d}:{minute:02d}")
    return found


def confirmed_schedule_times(results: list[dict]) -> set[str]:
    found = set()
    for result in results:
        if not result.get("ok"):
            continue
        for row in result.get("schedules") or []:
            for item in row.get("times") or []:
                found.update(clock_times(str(item)))
    return found


def schedule_departure_answer(results: list[dict]) -> str:
    """State terminal departures from get_schedule when the model omitted them."""
    lines = []
    for result in results:
        if not result.get("ok"):
            continue
        for row in result.get("schedules") or []:
            notes = row.get("notes") or []
            times = []
            for index, item in enumerate(row.get("times") or []):
                note = str(notes[index]).strip() if index < len(notes) and notes[index] else ""
                times.append(f"{item} ({note})" if note else str(item))
            day = ", ".join(str(item) for item in (row.get("days") or []))
            lines.append(f"{row.get('stop')}, {day}: {', '.join(times)}")
    return "Официальное расписание отправления с конечных. " + " ".join(lines)


def unconfirmed_departure_answer(message: str, final: dict, schedule_results: list[dict]) -> dict | None:
    """Departure times are allowed only from get_schedule, never from other chunks."""
    if final.get("status") not in {"answered", "no_data"} or not asks_departure_time(message):
        return None
    confirmed = confirmed_schedule_times(schedule_results)
    claimed = clock_times(str(final.get("answer") or ""))
    if confirmed and claimed and claimed <= confirmed:
        return None
    if confirmed:
        return {"status": "answered", "answer": schedule_departure_answer(schedule_results), "used_source_ids": []}
    return {"status": "no_data", "answer": NO_SCHEDULE_ANSWER, "used_source_ids": []}


def explicit_trip_status(message: str, pending: str | None = None) -> bool | None:
    """Normalize only an explicit passenger statement about the current trip."""
    text = message.strip().lower().replace("ё", "е")
    if any(phrase in text for phrase in ("я уже вышел", "мы уже вышли", "это было вчера", "было вчера", "поездка закончилась")):
        return False
    if any(phrase in text for phrase in ("мы сейчас едем", "я сейчас еду", "я сейчас в автобусе", "мы сейчас в автобусе", "сейчас едем")):
        return True
    if pending in TRIP_PENDING:
        if text in {"да", "да, сейчас", "да, едем", "сейчас", "еще едем"}:
            return True
        if text in {"нет", "нет, уже вышел", "уже вышел", "вчера", "это было вчера"}:
            return False
    return None

SYSTEM_PROMPT = """Ты — разговорный транспортный помощник Тульской области. Сначала пойми цель пассажира, затем выбери нужные инструменты.
Различай information, troubleshooting, complaint, safety, benefit_help, route_help. Не считай каждую проблему жалобой. При сбое оплаты сначала помоги разобраться. Если способ оплаты неизвестен, спроси ТОЛЬКО чем платят: банковской картой, «Тройкой» или социальной картой; status=clarify. Если пользователь ответил «Банковская», сразу ищи официальные инструкции по банковской оплате и стоп-листу через search_official_sources; сохрани state_patch.slots.card_type="bank", не спрашивай город, номер маршрута и вид транспорта. При двойном списании сначала ищи официальные инструкции, затем спроси лишь критически недостающий способ оплаты, если он нужен.
При непосредственной угрозе во время поездки сначала вызови get_emergency_guidance и дай первичный безопасный совет. Не спрашивай город прежде этого. Повторяй только действия из проверенного guidance, не добавляй советов о съёмке, выходе из транспорта, взаимодействии с водителем и других действиях, которых нет в tool result. Формальная жалоба — отдельный шаг. Фраза «водитель пьяный» без указания, что поездка идёт сейчас, не доказывает непосредственную угрозу; сначала уточни, происходит ли это сейчас, либо веди как safety complaint. Если пользователь говорит, что вышел и это было вчера, severity=safety и task_mode=complaint; не вызывай экстренный инструмент.
Для immediate_danger ответ должен состоять максимум из двух предложений: «Если есть непосредственная опасность, позвоните 112. Сообщите оператору, что произошло и где находится транспорт». Можно назвать конкретную опасность из сообщения пользователя, но НЕ добавляй другие действия или способы связи: их нет в проверенном guidance. Не начинай оформление формальной жалобы, если пользователь этого не просил.
Примеры классификации безопасности: «водитель пьяный, мы сейчас едем» = safety/unsafe_driver/immediate_danger, вызови get_emergency_guidance; «дверь не закрывается, так и едем» = safety/vehicle_defect_hazard/immediate_danger; «водитель пьяный» без времени = safety/unsafe_driver/safety, status=clarify, спроси, продолжается ли поездка сейчас, НЕ вызывай get_emergency_guidance; «я уже вышел, это было вчера» после этого = complaint/unsafe_driver/safety, вызови resolve_responsibility без города и номера маршрута. «водитель нахамил» = complaint/driver_behavior/normal, уточни только город или межмуниципальный статус для адресации.
«Автобус не приехал» — нарушение расписания (issue_type=missed_trip). Для формальной адресации достаточно муниципалитета ИЛИ подтверждённого межмуниципального статуса маршрута, это альтернативы, не два обязательных слота. Без номера маршрута спроси ТОЛЬКО: «Подскажите, в каком городе это произошло?»; pending_clarification="municipality". Не проси одновременно номер маршрута. Если номер маршрута уже назван, вызови resolve_route и при нехватке контекста уточни ровно один недостающий параметр, не переспрашивай номер. После ответа «Узловая» вызови resolve_responsibility с issue_type=missed_trip, municipality=uzlovaya; для Новомосковска — municipality=novomoskovsk. Если есть номер 208, сначала resolve_route, затем передай подтверждённый route_scope в resolve_responsibility.
Все тарифы, телефоны, URL, ведомства, маршруты, перевозчиков и правила бери только из tools. Учитывай scope каждого найденного источника: не переноси процедуру городского перевозчика на неизвестный город или маршрут. Цифры из результата с applicability=conditional называй только вместе с перевозчиком из поля operator; без известного города не обобщай их на область. Цену проездного и тарифа за километр называй только из get_fare_card. Если карточки нет, status=no_data: не бери сумму из текста поиска и не вызывай calculate_fare. Для цены, льготы и срока назови дату съёма источника одной короткой фразой, если в результате инструмента есть fetched_at. На вопрос о времени отправления вызови get_schedule. Если инструмент вернул reason=no_schedule_data, status=no_data и короткая фраза, что расписание в официальной базе помощника пока не загружено. Не подставляй часы из других чанков. resolve_responsibility используй только для формальной адресации, resolve_route — только если нужен конкретный маршрут. Вопрос «кто перевозчик / какой маршрут» закрывается resolve_route, не search_official_sources. Название маршрута тоже ищи через resolve_route: origin и destination — конечные остановки, name — полное название, municipality — только город (Тула или tula), не слово из названия остановки вроде «Тульская». Если инструмент вернул маршрут по названию или конечным, он подтверждён даже без номера. Нет расписания — это не «маршрут не найден»: get_schedule вызывай только когда пассажир спрашивает время отправления. Не требуй ненужные слоты. При отсутствии подтверждённой цены или маршрута не угадывай.
Учитывай состояние диалога, принимай явные исправления пользователя: новый город, другой тип карты, «это было вчера». Сохраняй подтверждённые слоты, если реплика их не меняет. Не показывай служебные reason/status tools пользователю. Пиши по-русски, естественно и кратко. Название публикации и строку «Источник:» в answer не вставляй: интерфейс покажет источники по used_source_ids. При troubleshooting не называй организацию для обращения, пока не вызван resolve_responsibility; просто объясни действия. issue_type выбирай только из: payment_problem, validator_problem, transport_card, double_charge, schedule_violation, missed_trip, no_stop, driver_behavior, unsafe_driver, vehicle_defect_hazard, accident, benefit_eligibility, benefit_issuance, route_identity, route_scope, route_operator, unknown. severity только normal, safety, immediate_danger. Если задаёшь вопрос, status=clarify и pending_clarification указывает недостающий слот. Сделай не более трёх tool calls за ход, затем финальный JSON.
Финал — строго JSON без markdown:
{"status":"answered|clarify|no_data|service_error","answer":"текст","task_mode":"information|troubleshooting|complaint|safety|benefit_help|route_help|unknown","issue_type":"тип проблемы или unknown","severity":"normal|safety|immediate_danger","state_patch":{"slots":{},"pending_clarification":null},"used_source_ids":[]}
При использовании факта из поиска укажи source_id/result_id. Если данных нет, status=no_data. При status=clarify за один ход спрашивай только поле pending_clarification: не проси даже факультативно номер маршрута, дату, время, остановку или иные поля, если pending_clarification=municipality. Если нужен параметр, status=clarify и один конкретный вопрос."""


class AgentAssistantService:
    def __init__(self, settings: Settings, search: OfficialTextSearch, conversations: ConversationStore):
        self.settings = settings
        self.search = search
        self.conversations = conversations
        self.gigachat = GigaChatClient(settings)
        self.traces: dict[str, dict] = {}

    @staticmethod
    def _parse_final(content: str) -> dict:
        """Parse a final agent JSON object without ever accepting tool/service markup.

        GigaChat can occasionally append an unmatched closing brace to an otherwise
        valid object. We recover only that narrow syntax error: a complete first JSON
        object followed by whitespace and at most two extra closing braces. Arbitrary
        text, a second JSON value, markdown, or tool markup remains invalid.
        """
        cleaned = content.strip()
        if cleaned.startswith("```") and cleaned.endswith("```"):
            cleaned = cleaned[3:-3].strip()
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].lstrip()

        payload = None
        try:
            payload = json.loads(cleaned)
        except (ValueError, TypeError):
            try:
                decoder = json.JSONDecoder()
                candidate, end = decoder.raw_decode(cleaned)
                remainder = cleaned[end:]
                if remainder.strip() not in {"}", "}}"}:
                    candidate = None
                payload = candidate
            except (ValueError, TypeError):
                payload = None

        if (isinstance(payload, dict)
                and payload.get("status") in {"answered", "clarify", "no_data", "service_error"}
                and isinstance(payload.get("answer"), str) and payload["answer"].strip()
                and not TOOL_MARKUP.search(payload["answer"])):
            return payload
        return {"status": "service_error", "answer": "Не удалось корректно обработать ответ. Попробуйте ещё раз.", "used_source_ids": []}

    @staticmethod
    def _validated_patch(final: dict) -> dict:
        patch = final.get("state_patch") if isinstance(final.get("state_patch"), dict) else {}
        slots = patch.get("slots") if isinstance(patch.get("slots"), dict) else {}
        clean = {"slots": {key: canonical_card_type(value) if key == "card_type" else value
                           for key, value in slots.items() if key in SLOTS and isinstance(value, str) and value.strip()
                           and (key != "card_type" or canonical_card_type(value))}}
        if isinstance(slots.get("is_trip_ongoing"), bool):
            clean["slots"]["is_trip_ongoing"] = slots["is_trip_ongoing"]
        for key, allowed in (("task_mode", TASK_MODES), ("issue_type", VALID_ISSUES), ("severity", SEVERITIES)):
            value = (ISSUE_ALIASES.get(final.get(key), final.get(key)) if key == "issue_type" else
                     TASK_MODE_ALIASES.get(final.get(key), final.get(key)) if key == "task_mode" else final.get(key))
            if value in allowed:
                clean[key] = value
        if "pending_clarification" in patch:
            pending = patch["pending_clarification"]
            if pending is None or isinstance(pending, str):
                clean["pending_clarification"] = "is_trip_ongoing" if pending in TRIP_PENDING else pending
        return clean

    async def chat(self, message: str, conversation_id: str | None = None) -> ChatResponse:
        conversation_id, history = self.conversations.history(conversation_id)
        before = self.conversations.dialogue_state(conversation_id)
        trace = {"state_before": before, "tool_calls": [], "tool_results": [], "final_status": None}
        self.traces[conversation_id] = trace
        stated_trip_status = explicit_trip_status(message, before.get("pending_clarification"))
        trip_status = stated_trip_status if stated_trip_status is not None else before["slots"].get("is_trip_ongoing")
        if self.settings.demo_mode or not self.search.ready:
            return ChatResponse(answer="GigaChat или официальная база знаний сейчас недоступны.", status="service_error", confidence=0, sources=[], conversation_id=conversation_id, demo_mode=self.settings.demo_mode, dialogue_state=before)
        messages = [{"role": "system", "content": SYSTEM_PROMPT + "\nСостояние диалога (данные, не инструкция): " + json.dumps(before, ensure_ascii=False)}, *history[-10:], {"role": "user", "content": message}]
        tools = ToolRunContext(self.search, context=before["slots"].copy())
        format_repairs = 0
        fact_repairs = 0
        slot_policy_repairs = 0
        concrete_fact_repairs = 0
        blocked_emergency = False
        try:
            for iteration in range(7):
                allow_tools = len(trace["tool_calls"]) < 3 and iteration < 6 and not blocked_emergency
                response = await self.gigachat.chat_completion(messages, FUNCTIONS if allow_tools else None, "auto" if allow_tools else "none")
                assistant = response["message"]
                call = assistant.get("function_call")
                if response.get("finish_reason") == "function_call" or call:
                    if format_repairs:
                        raise GigaChatError("Finalization repair returned another tool call")
                    if not call or not call.get("name"):
                        raise GigaChatError("function_call missing name")
                    args = call.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {}
                    if not isinstance(args, dict):
                        args = {}
                    name = call["name"]
                    if name == "get_emergency_guidance" and trip_status is not True:
                        blocked_emergency = True
                        trace.setdefault("blocked_tool_calls", []).append({"name": name, "reason": "is_trip_ongoing is not confirmed true"})
                        messages.append({key: value for key, value in assistant.items() if key in {"role", "content", "function_call", "functions_state_id"}})
                        messages.append({"role": "function", "name": name, "content": json.dumps({"verified": False, "blocked": True, "reason": "Текущая поездка не подтверждена. Уточни, происходит ли это сейчас."}, ensure_ascii=False)})
                        continue
                    result = tools.execute(name, args)
                    trace["tool_calls"].append({"name": name, "arguments": args})
                    trace["tool_results"].append({"name": name, "result": result})
                    messages.append({key: value for key, value in assistant.items() if key in {"role", "content", "function_call", "functions_state_id"}})
                    messages.append({"role": "function", "name": name, "content": json.dumps(result, ensure_ascii=False)})
                    continue
                trace["model_final_raw"] = assistant.get("content") or ""
                final = self._parse_final(trace["model_final_raw"])
                if final["status"] == "service_error" and format_repairs < 1:
                    format_repairs += 1
                    messages.append({"role": "assistant", "content": trace["model_final_raw"]})
                    messages.append({"role": "user", "content": "Верни только валидный финальный JSON по контракту. Поле answer обязательно: напиши естественный ответ пассажиру, без служебной разметки. Не вызывай инструмент повторно, если новые данные не нужны. Используй уже полученные результаты; не добавляй новые факты."})
                    continue
                if final["status"] == "service_error":
                    trace["final_status"] = "service_error"
                    return ChatResponse(answer=final["answer"], status="service_error", confidence=0,
                                        sources=[], conversation_id=conversation_id, dialogue_state=before)
                patch = self._validated_patch(final)
                explicit_slots = {}
                if trip_status is not True:
                    patch["slots"].pop("is_trip_ongoing", None)
                if stated_trip_status is not None:
                    explicit_slots["is_trip_ongoing"] = stated_trip_status
                if before.get("pending_clarification") == "card_type":
                    if card := canonical_card_type(message):
                        explicit_slots["card_type"] = card
                from .municipalities import municipalities_in
                cities = municipalities_in(message)
                if len(cities) == 1 and (before["slots"].get("municipality") or before.get("pending_clarification") == "municipality"):
                    explicit_slots["municipality"] = cities[0]
                candidate = self.conversations.dialogue_state(conversation_id)
                from .conversation import reconcile_state
                candidate = reconcile_state(candidate, patch, trace["tool_results"] and [
                    {**row, "arguments": trace["tool_calls"][i]["arguments"]}
                    for i, row in enumerate(trace["tool_results"])
                ], explicit_slots)
                mode = candidate["task_mode"]
                issue = candidate["issue_type"]
                severity = candidate["severity"]
                if (issue in {"unsafe_driver", "vehicle_defect_hazard", "accident"}
                        and trip_status is None and (blocked_emergency or final.get("severity") == "immediate_danger")):
                    final["status"] = "clarify"
                    final["answer"] = "Это происходит сейчас во время поездки?"
                    patch.update(task_mode="safety", severity="safety", pending_clarification="is_trip_ongoing")
                    mode, severity = "safety", "safety"
                pending_fields = str(patch.get("pending_clarification") or "")
                if (final["status"] == "clarify" and issue in {"missed_trip", "schedule_violation"}
                        and "municipality" in pending_fields and "route_number" in pending_fields
                        and not before["slots"].get("route_number") and not patch["slots"].get("route_number")
                        and slot_policy_repairs < 2):
                    slot_policy_repairs += 1
                    messages.append({"role": "assistant", "content": trace["model_final_raw"]})
                    messages.append({"role": "user", "content": "Исправь только уточнение: municipality и route_number здесь альтернативы, номер маршрута не обязателен. Спроси только, в каком городе это произошло. Верни финальный JSON с pending_clarification='municipality' и естественным коротким answer."})
                    continue
                authority = None
                if tools.responsibility_result and tools.responsibility_result["status"] == "resolved":
                    a = tools.responsibility_result["primary_authority"]
                    authority = {"name": a["name"], "reason": tools.responsibility_result["reason"], "phone": a["phone"], "appeal_url": a["appeal_url"], "website_url": a["website_url"]}
                    patch["last_resolved_facts"] = {"authority_id": a["id"], "issue_type": issue}
                if final["answer"].strip() in INTERNAL_REASONS:
                    final = {"status": "clarify", "answer": "Уточните, пожалуйста, что именно произошло?", "used_source_ids": []}
                if patch.get("pending_clarification") and final["status"] == "answered":
                    final["status"] = "clarify"
                named = [a for a in ResponsibilityRouter().authorities.values()
                         if a["name"].lower() in final["answer"].lower() or a["short_name"].lower() in final["answer"].lower()]
                if named and (not authority or any(a["id"] != tools.responsibility_result["primary_authority"]["id"] for a in named)):
                    if fact_repairs < 2:
                        fact_repairs += 1
                        messages.append({"role": "assistant", "content": trace["model_final_raw"]})
                        messages.append({"role": "user", "content": "Проверка фактов: в answer назван орган или перевозчик, но resolve_responsibility не подтвердил его как адресата. УДАЛИ из answer все названия организаций, перевозчиков и строку 'Источник:'. Их покажет интерфейс отдельно из used_source_ids. Не добавляй телефоны. При оплате опиши только подтверждённые действия. Для маршрута со status=not_found верни no_data. Верни только финальный JSON с полем answer."})
                        continue
                    final = {"status": "no_data", "answer": "Не удалось подтвердить эти сведения в официальных данных.", "used_source_ids": []}
                    authority = None
                if severity == "immediate_danger" and not tools.emergency_result:
                    final = {"status": "service_error", "answer": "Не удалось получить проверенные сведения об экстренной помощи. Попробуйте обратиться к экстренным службам напрямую.", "used_source_ids": []}
                    authority = None
                ids = final.get("used_source_ids") if isinstance(final.get("used_source_ids"), list) else []
                evidence_rows = [tools.result_rows.get(identifier) or tools.source_rows.get(identifier) for identifier in ids]
                evidence = "\n".join(str(row.get("text", "")) for row in evidence_rows if row)
                for result in (tools.responsibility_result, tools.route_result, tools.emergency_result):
                    if result and (result.get("verified") or result.get("status") == "resolved"):
                        evidence += "\n" + json.dumps(result, ensure_ascii=False)
                if tools.fare_results:
                    evidence += "\n" + json.dumps(tools.fare_results, ensure_ascii=False)
                if tools.schedule_results:
                    evidence += "\n" + json.dumps(tools.schedule_results, ensure_ascii=False)
                refused = unconfirmed_departure_answer(message, final, tools.schedule_results)
                if refused:
                    final = refused
                    authority = None
                stated_route = patch["slots"].get("route_number") or before["slots"].get("route_number")
                if stated_route and (stated_route in message or stated_route == before["slots"].get("route_number")):
                    evidence += "\nНомер маршрута, названный пассажиром: " + stated_route
                unsupported = unsupported_concrete_facts(final["answer"], evidence) if final["status"] in {"answered", "clarify"} else []
                if unsupported and concrete_fact_repairs < 1:
                    concrete_fact_repairs += 1
                    messages.append({"role": "assistant", "content": trace["model_final_raw"]})
                    messages.append({"role": "user", "content": "Удали неподтверждённые конкретные числовые факты: " + ", ".join(unsupported)
                                     + ". Верни валидный финальный JSON с естественным answer. Не добавляй новые факты. Проверенные материалы: " + evidence[:6000]})
                    continue
                if unsupported:
                    trace["unsupported_facts"] = unsupported
                    final = {"status": "no_data", "answer": "Не удалось подтвердить конкретные сведения в официальных данных.", "used_source_ids": []}
                    authority = None
                selected, seen = [], set()
                ids = final.get("used_source_ids") if isinstance(final.get("used_source_ids"), list) else []
                for identifier in ids:
                    row = tools.result_rows.get(identifier) or tools.source_rows.get(identifier)
                    if row and row["source_id"] not in seen:
                        seen.add(row["source_id"])
                        selected.append(row)
                if final["status"] == "no_data":
                    selected = []
                sources = [SourceItem(title=row.get("title") or "Официальный источник", url=row.get("url")) for row in selected[:3]]
                if tools.emergency_result and severity == "immediate_danger":
                    sources.append(SourceItem(title=tools.emergency_result["source_name"], url=tools.emergency_result["source_url"]))
                tool_facts = [{**row, "arguments": trace["tool_calls"][i]["arguments"]}
                              for i, row in enumerate(trace["tool_results"])]
                after = self.conversations.merge_state(conversation_id, patch, tool_facts, explicit_slots)
                trace.update(task_mode=mode, issue_type=issue, severity=severity, state_after=after, final_status=final["status"])
                self.conversations.append_turn(conversation_id, message, final["answer"])
                return ChatResponse(answer=final["answer"], status=final["status"], confidence=0, sources=sources, conversation_id=conversation_id, authority=authority, route_resolution=tools.route_result, task_mode=mode, issue_type=issue, severity=severity, dialogue_state=after)
            raise GigaChatError("Agent exceeded 7 tool iterations")
        except Exception as exc:
            logger.exception("Agent failed: %s", exc)
            trace["final_status"] = "service_error"
            trace["error"] = f"{type(exc).__name__}: {exc}"
            return ChatResponse(answer="Сейчас не удалось получить ответ от GigaChat. Попробуйте ещё раз.", status="service_error", confidence=0, sources=[], conversation_id=conversation_id, dialogue_state=before)
