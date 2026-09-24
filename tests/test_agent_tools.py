import json
from pathlib import Path

from app.agent_service import SYSTEM_PROMPT
from app.agent_tools import SCHEDULES_PATH, ToolRunContext, load_fare_cards, load_schedules
from app.config import get_settings
from app.text_search import OfficialTextSearch

CHUNKS_PATH = Path(__file__).resolve().parents[1] / "app" / "data" / "chunks.json"


def test_search_tool_returns_traceable_official_results():
    tools = ToolRunContext(OfficialTextSearch(get_settings()))
    result = tools.execute("search_official_sources", {"query": "банковская карта стоп-лист задолженность", "top_k": 4})
    assert result["ok"] is True
    assert result["results"]
    assert all(row["result_id"] and row["source_id"] and row["url"] for row in result["results"])
    assert all(row.get("fetched_at") for row in result["results"])


def test_get_source_details_returns_neighbors():
    tools = ToolRunContext(OfficialTextSearch(get_settings()))
    search = tools.execute("search_official_sources", {"query": "утеря социальной транспортной карты"})
    detail = tools.execute("get_source_details", {"result_id": search["results"][0]["result_id"], "neighbor_count": 1})
    assert detail["ok"] is True
    assert detail["results"]


def test_fare_calculator_rejects_unverified_rate():
    tools = ToolRunContext(OfficialTextSearch(get_settings()))
    tools._remember({"id": "chunk-1", "source_id": "local", "text": "Тариф 4.46 руб за км."})
    result = tools.execute("calculate_fare", {"distance_km": 25, "fare_per_km": 4.46})
    assert result["ok"] is False
    assert "not found" in result["error"]
    assert tools.allowed_per_km_fares == set()


def test_fare_cards_quote_existing_chunks_and_have_no_per_km_rate():
    cards = load_fare_cards()
    chunks = json.loads(CHUNKS_PATH.read_text(encoding="utf-8"))
    texts = {row["source_id"]: [] for row in chunks}
    for row in chunks:
        texts.setdefault(row["source_id"], []).append(row["text"])
    assert cards
    assert all(card["kind"] == "pass" for card in cards)
    assert not any(card["kind"] == "per_km" for card in cards)
    for card in cards:
        blob = "\n".join(texts[card["source_id"]])
        assert card["quoted_excerpt"] in blob
        assert str(card["amount"]) in card["quoted_excerpt"].replace(" ", "")


def test_get_fare_card_returns_pass_excerpt_and_empty_per_km():
    tools = ToolRunContext(OfficialTextSearch(get_settings()))
    pension = tools.execute("get_fare_card", {"kind": "pass", "query": "пенсионер"})
    assert pension["ok"] is True
    assert pension["cards"][0]["amount"] == 750
    assert "750 рублей" in pension["cards"][0]["quoted_excerpt"]
    assert pension["cards"][0]["fetched_at"]
    per_km = tools.execute("get_fare_card", {"kind": "per_km"})
    assert per_km == {"ok": False, "reason": "no_fare_card"}
    assert "get_fare_card" in SYSTEM_PROMPT
    assert "fetched_at" in SYSTEM_PROMPT


def test_get_schedule_missing_route_is_no_data():
    tools = ToolRunContext(OfficialTextSearch(get_settings()))
    result = tools.execute("get_schedule", {"route_number": "9999"})
    assert result == {"ok": False, "reason": "no_schedule_data"}
    assert isinstance(load_schedules(), list)
    assert SCHEDULES_PATH.name == "schedules.json"
    assert "get_schedule" in SYSTEM_PROMPT
    assert "no_schedule_data" in SYSTEM_PROMPT


def test_calculate_fare_uses_only_per_km_card():
    search = OfficialTextSearch(get_settings())
    card = {
        "id": "fixture-per-km",
        "kind": "per_km",
        "title": "Тариф за километр",
        "amount": "4.46",
        "currency": "RUB",
        "unit": "km",
        "audience": "перевозчик из фикстуры",
        "valid_note": "Только для теста.",
        "source_id": "fixture",
        "source_url": "https://example.test/fare",
        "quoted_excerpt": "Тариф 4.46 руб за км.",
    }
    tools = ToolRunContext(search, fare_cards=[card])
    denied = tools.execute("calculate_fare", {"distance_km": 25, "fare_per_km": 4.46})
    assert denied["ok"] is False
    found = tools.execute("get_fare_card", {"kind": "per_km"})
    assert found["ok"] is True
    assert found["cards"][0]["quoted_excerpt"] == card["quoted_excerpt"]
    priced = tools.execute("calculate_fare", {"distance_km": 25, "fare_per_km": 4.46})
    assert priced == {"ok": True, "distance_km": 25.0, "fare_per_km": 4.46, "total": 111.5, "currency": "RUB"}
