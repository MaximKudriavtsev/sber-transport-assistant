import json
from pathlib import Path
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .municipalities import normalize_municipality
from .route_resolver import normalize_route_number
from .text_search import CONDITIONAL_OPERATOR_NOTE, OfficialTextSearch, source_applicability
from .responsibility import ResponsibilityRouter, resolve_route


FUNCTIONS = [
    {"name": "get_emergency_guidance", "description": "Проверенные действия и номер экстренной помощи ТОЛЬКО при явно продолжающейся сейчас опасной поездке, например «мы сейчас едем». Одна фраза «водитель пьяный» без времени не доказывает непосредственную угрозу: сначала уточни время. При явной текущей угрозе вызывай прежде формального маршрутизатора жалобы.", "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "resolve_responsibility", "description": "Определяет компетентный орган только по проверенным правилам. Обязателен для жалобы или вопроса куда обратиться.", "parameters": {"type": "object", "properties": {"issue_type": {"type": "string"}, "municipality": {"type": "string"}, "route_number": {"type": "string"}, "route_scope": {"type": "string"}, "operator": {"type": "string"}, "transport_type": {"type": "string"}, "card_type": {"type": "string"}, "safety_related": {"type": "boolean"}}, "required": ["issue_type"]}},
    {"name": "resolve_route", "description": "Определяет маршрут, перевозчика и территорию только по официальному реестру. Ищет по номеру, по паре конечных остановок или по полному названию. municipality — только город (Тула или tula), не слово из названия остановки. При неоднозначности требует уточнение.", "parameters": {"type": "object", "properties": {"municipality": {"type": "string", "description": "Город маршрута: Тула или tula. Не подставляй часть названия остановки, например «Тульская»."}, "route_number": {"type": "string", "description": "Номер маршрута, если пассажир его назвал"}, "origin": {"type": "string", "description": "Начальная конечная остановка, не город"}, "destination": {"type": "string", "description": "Конечная остановка, не город"}, "name": {"type": "string", "description": "Полное название маршрута, если пассажир назвал его целиком"}, "transport_type": {"type": "string", "description": "bus, tram или trolley, если вид транспорта известен"}}, "required": []}},
    {
        "name": "search_official_sources",
        "description": (
            "Ищет актуальные сведения в локальном корпусе официальных источников общественного "
            "транспорта Тульской области. Формулируй самостоятельный точный поисковый запрос; "
            "если результатов недостаточно, вызови функцию повторно с другими терминами."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Нормализованный поисковый запрос на русском языке"},
                "category": {"type": "string", "description": "Необязательная категория источника"},
                "top_k": {"type": "integer", "description": "Число результатов от 1 до 8"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_source_details",
        "description": "Возвращает полный найденный фрагмент и соседние фрагменты того же официального источника.",
        "parameters": {
            "type": "object",
            "properties": {
                "result_id": {"type": "string", "description": "result_id из search_official_sources"},
                "neighbor_count": {"type": "integer", "description": "Число соседних фрагментов: 0–2"},
            },
            "required": ["result_id"],
        },
    },
    {
        "name": "get_fare_card",
        "description": (
            "Возвращает карточку цены проездного или тарифа за километр и дословный excerpt. "
            "Цену проездного и километра называй только отсюда. Если карточки нет, это no_data: "
            "не бери сумму из текста поиска."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "pass или per_km"},
                "query": {"type": "string", "description": "Категория льготника, перевозчик или название билета"},
                "card_id": {"type": "string", "description": "Идентификатор карточки, если уже известен"},
            },
            "required": [],
        },
    },
    {
        "name": "get_schedule",
        "description": (
            "Возвращает официальное расписание отправления с конечных: times и пометки notes той же длины. "
            "На вопрос о времени рейса вызывай этот инструмент и передай transport_type, если вид транспорта известен. "
            "Без остановки вернёт конечные этого маршрута и типа дня, не весь город. "
            "Если reason=no_schedule_data, это no_data: расписание в официальной базе помощника пока не загружено. "
            "Не подставляй часы из других чанков."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "route_number": {"type": "string", "description": "Номер маршрута, если пассажир его назвал"},
                "stop": {"type": "string", "description": "Конечная, если пассажир её назвал"},
                "municipality": {"type": "string", "description": "Муниципалитет, если он известен"},
                "transport_type": {"type": "string", "description": "bus, tram или trolleybus, если вид транспорта известен"},
                "days": {"type": "string", "description": "будни, выходные или ежедневно, если пассажир назвал тип дня"},
            },
            "required": [],
        },
    },
    {
        "name": "calculate_fare",
        "description": (
            "Считает стоимость поездки по расстоянию и тарифу за км. fare_per_km бери только "
            "из карточки get_fare_card с kind=per_km, не из текста поиска."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "distance_km": {"type": "number", "description": "Подтвержденное расстояние в километрах"},
                "fare_per_km": {"type": "number", "description": "Тариф за километр из карточки per_km"},
            },
            "required": ["distance_km", "fare_per_km"],
        },
    },
]


FARE_CARDS_PATH = Path(__file__).parent / "data" / "fare_cards.json"
SCHEDULES_PATH = Path(__file__).parent / "data" / "schedules.json"


def load_fare_cards(path: Path | None = None) -> list[dict]:
    data = json.loads((path or FARE_CARDS_PATH).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("fare cards must be a list")
    return data


def _card_blob(card: dict) -> str:
    parts = (card.get(key) or "" for key in ("id", "title", "audience", "valid_note", "quoted_excerpt", "kind"))
    return " ".join(str(part) for part in parts).lower().replace("ё", "е")


def load_schedules(path: Path | None = None) -> list[dict]:
    data = json.loads((path or SCHEDULES_PATH).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("schedules must be a list")
    return data


def _fold_schedule(value: str) -> str:
    return str(value or "").strip().lower().replace("ё", "е")


TRANSPORT_ALIASES = {
    "bus": "bus", "автобус": "bus",
    "tram": "tram", "трамвай": "tram",
    "trolleybus": "trolleybus", "trolley": "trolleybus", "троллейбус": "trolleybus",
}
DAY_ALIASES = {
    "будни": "будни", "рабочие": "будни", "рабочий": "будни",
    "выходные": "выходные", "выходной": "выходные",
    "ежедневно": "ежедневно", "повседневно": "ежедневно",
}


def _route_key(value: str) -> str:
    folded = _fold_schedule(value)
    return normalize_route_number(folded) or folded


def _stop_matches(query: str, stop: str) -> bool:
    if not query:
        return True
    if query in stop or stop in query:
        return True
    for token in query.replace("«", " ").replace("»", " ").replace('"', " ").split():
        if len(token) < 4:
            continue
        if stop.startswith(token) or token.startswith(stop):
            return True
        stem = token[:-1]
        if len(stem) >= 4 and stop.startswith(stem):
            return True
    return False


def matching_schedules(rows: list[dict], arguments: dict) -> list[dict]:
    route_number = _route_key(str(arguments.get("route_number") or ""))
    stop = _fold_schedule(arguments.get("stop") or "")
    raw_municipality = str(arguments.get("municipality") or "").strip()
    municipality = normalize_municipality(raw_municipality) if raw_municipality else None
    transport = TRANSPORT_ALIASES.get(_fold_schedule(arguments.get("transport_type") or ""), "")
    if arguments.get("transport_type") and not transport:
        transport = _fold_schedule(arguments.get("transport_type"))
    day = DAY_ALIASES.get(_fold_schedule(arguments.get("days") or ""), "")
    if arguments.get("days") and not day:
        day = _fold_schedule(arguments.get("days"))
    if not route_number and not stop:
        return []
    found = []
    for row in rows:
        if route_number and _route_key(str(row.get("route_number") or "")) != route_number:
            continue
        if transport and _fold_schedule(row.get("transport_type") or "") != transport:
            continue
        if not _stop_matches(stop, _fold_schedule(row.get("stop") or "")):
            continue
        row_municipality = str(row.get("municipality") or "")
        if raw_municipality and municipality and row_municipality != municipality:
            continue
        if raw_municipality and not municipality and _fold_schedule(raw_municipality) not in _fold_schedule(row_municipality):
            continue
        if day and day not in {_fold_schedule(item) for item in (row.get("days") or [])}:
            continue
        found.append(row)
    return found


def matching_fare_cards(cards: list[dict], arguments: dict) -> list[dict]:
    kind = str(arguments.get("kind") or "").strip()
    card_id = str(arguments.get("card_id") or "").strip()
    query = str(arguments.get("query") or "").strip().lower().replace("ё", "е")
    found = []
    for card in cards:
        if kind and card.get("kind") != kind:
            continue
        if card_id and card.get("id") != card_id:
            continue
        if query and query not in _card_blob(card):
            continue
        found.append(card)
    return found


@dataclass
class ToolRunContext:
    search: OfficialTextSearch
    context: dict = field(default_factory=dict)
    result_rows: dict[str, dict] = field(default_factory=dict)
    source_rows: dict[str, dict] = field(default_factory=dict)
    fare_cards: list[dict] | None = None
    allowed_per_km_fares: set[Decimal] = field(default_factory=set)
    fare_results: list[dict] = field(default_factory=list)
    schedules: list[dict] | None = None
    schedule_results: list[dict] = field(default_factory=list)
    responsibility_result: dict | None = None
    route_result: dict | None = None
    emergency_result: dict | None = None

    def __post_init__(self) -> None:
        if self.fare_cards is None:
            self.fare_cards = load_fare_cards()
        if self.schedules is None:
            self.schedules = load_schedules()

    def public_row(self, row: dict, score: float | None = None, full: bool = False) -> dict:
        source = self.search.sources.get(row["source_id"], {})
        applicability = source_applicability(source, self.context)
        result = {
            "result_id": row["id"], "source_id": row["source_id"], "title": row.get("title"),
            "url": row.get("url"), "category": row.get("category"), "priority": row.get("priority"),
            "scope": source.get("scope", "unknown"),
            "municipality": source.get("municipality"), "operator": source.get("operator"),
            "applicability": applicability or "applicable",
            "fetched_at": row.get("fetched_at"),
            "text": row.get("text", "") if full else row.get("text", "")[:1400],
        }
        if applicability == "conditional":
            result["applicability_note"] = CONDITIONAL_OPERATOR_NOTE
        if score is not None:
            result["score"] = score
        return result

    def fetched_at_for_source(self, source_id: str) -> str | None:
        source = self.search.sources.get(source_id) or {}
        if source.get("fetched_at"):
            return source["fetched_at"]
        for row in self.search.by_source.get(source_id, []):
            if row.get("fetched_at"):
                return row["fetched_at"]
        return None

    def public_fare_card(self, card: dict) -> dict:
        published = dict(card)
        fetched_at = self.fetched_at_for_source(str(card.get("source_id") or ""))
        if fetched_at:
            published["fetched_at"] = fetched_at
        return published

    def _remember(self, row: dict) -> None:
        self.result_rows[row["id"]] = row
        self.source_rows[row["source_id"]] = row

    def _remember_per_km(self, cards: list[dict]) -> None:
        for card in cards:
            if card.get("kind") != "per_km":
                continue
            try:
                fare = Decimal(str(card["amount"]))
            except (KeyError, InvalidOperation):
                continue
            if fare > 0:
                self.allowed_per_km_fares.add(fare)

    def execute(self, name: str, arguments: dict) -> dict:
        if name == "get_emergency_guidance":
            data = json.loads((Path(__file__).parent / "data" / "emergency_guidance.json").read_text(encoding="utf-8"))
            self.emergency_result = data if data.get("verified") and data.get("source_url") else {"verified": False}
            return self.emergency_result
        if name == "resolve_responsibility":
            self.responsibility_result = ResponsibilityRouter().resolve(arguments)
            return self.responsibility_result
        if name == "resolve_route":
            self.route_result = resolve_route(arguments)
            if self.route_result.get("status") == "resolved":
                route = self.route_result.get("route") or {}
                for key in ("municipality", "route_number", "operator"):
                    if route.get(key):
                        self.context[key] = route[key]
            return self.route_result
        if name == "search_official_sources":
            query = str(arguments.get("query", "")).strip()
            if not query:
                return {"ok": False, "error": "query is required"}
            hits = self.search.search(query, arguments.get("category"), int(arguments.get("top_k", 5)), self.context)
            for hit in hits:
                self._remember(hit.row)
            return {"ok": True, "query": query, "results": [self.public_row(hit.row, hit.score) for hit in hits]}
        if name == "get_source_details":
            result_id = str(arguments.get("result_id", ""))
            rows = self.search.details(result_id, int(arguments.get("neighbor_count", 1)), self.context)
            for row in rows:
                self._remember(row)
            return {"ok": bool(rows), "result_id": result_id,
                    "results": [self.public_row(row, full=True) for row in rows],
                    **({} if rows else {"error": "result_id not found"})}
        if name == "get_fare_card":
            cards = matching_fare_cards(self.fare_cards or [], arguments)
            if not cards:
                result = {"ok": False, "reason": "no_fare_card"}
                self.fare_results.append(result)
                return result
            published = [self.public_fare_card(card) for card in cards]
            self._remember_per_km(published)
            result = {"ok": True, "cards": published}
            self.fare_results.append(result)
            return result
        if name == "get_schedule":
            rows = matching_schedules(self.schedules or [], arguments)
            if not rows:
                result = {"ok": False, "reason": "no_schedule_data"}
                self.schedule_results.append(result)
                return result
            result = {"ok": True, "schedules": rows}
            self.schedule_results.append(result)
            return result
        if name == "calculate_fare":
            try:
                distance = Decimal(str(arguments["distance_km"]))
                fare = Decimal(str(arguments["fare_per_km"]))
            except (KeyError, InvalidOperation):
                return {"ok": False, "error": "distance_km and fare_per_km must be numbers"}
            if distance <= 0 or fare <= 0:
                return {"ok": False, "error": "values must be positive"}
            if fare not in self.allowed_per_km_fares:
                return {"ok": False, "error": "fare_per_km was not found in previous official tool results"}
            total = (distance * fare).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            return {"ok": True, "distance_km": float(distance), "fare_per_km": float(fare),
                    "total": float(total), "currency": "RUB"}
        return {"ok": False, "error": f"unknown function: {name}"}
