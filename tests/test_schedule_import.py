"""Schedule registry: terminal tables and get_schedule filters."""
from pathlib import Path

from docx import Document

from app.agent_service import unconfirmed_departure_answer
from app.agent_tools import ToolRunContext, load_schedules, matching_schedules
from app.config import get_settings
from app.text_search import OfficialTextSearch
from build_schedules import parse_docx, route_number_from_label


def test_route_number_comes_from_caption_not_filename():
    assert route_number_from_label("Автобусный маршрут № 122") == "122"
    assert route_number_from_label("Автобусный маршрут № 5-Л") == "5Л"
    assert route_number_from_label("Автобусный маршрут № 52B") == "52В"
    assert route_number_from_label("Трамвайный маршрут №12") == "12"


def test_docx_terminal_table_keeps_notes(tmp_path: Path):
    path = tmp_path / "route.docx"
    document = Document()
    table = document.add_table(rows=3, cols=3)
    table.rows[0].cells[0].text = "Новомосковское шоссе"
    table.rows[0].cells[1].text = "№ выхода"
    table.rows[0].cells[2].text = "Баташи"
    table.rows[1].cells[0].text = "5:30 (Кауля); 7:10"
    table.rows[1].cells[1].text = "1"
    table.rows[1].cells[2].text = "6:15"
    table.rows[2].cells[0].text = "9:40 (Кауля)"
    table.rows[2].cells[1].text = "2"
    table.rows[2].cells[2].text = "14:30 (до Кауля)"
    document.save(path)

    rows = parse_docx(path)
    by_stop = {row["stop"]: row for row in rows}
    assert by_stop["Новомосковское шоссе"]["days"] == ["ежедневно"]
    assert by_stop["Новомосковское шоссе"]["times"] == ["05:30", "07:10", "09:40"]
    assert by_stop["Новомосковское шоссе"]["notes"] == ["Кауля", "", "Кауля"]
    assert by_stop["Баташи"]["times"] == ["06:15", "14:30"]
    assert by_stop["Баташи"]["notes"] == ["", "до Кауля"]
    assert all(len(row["times"]) == len(row["notes"]) for row in rows)


def test_get_schedule_filters_transport_and_does_not_return_the_city():
    rows = [
        {"route_number": "12", "transport_type": "bus", "municipality": "tula", "stop": "РТИ", "days": ["будни"], "times": ["05:54"], "notes": [""]},
        {"route_number": "12", "transport_type": "bus", "municipality": "tula", "stop": "Птицефабрики", "days": ["будни"], "times": ["06:10"], "notes": [""]},
        {"route_number": "12", "transport_type": "tram", "municipality": "tula", "stop": "Щегловская засека", "days": ["ежедневно"], "times": ["05:36"], "notes": [""]},
        {"route_number": "8", "transport_type": "bus", "municipality": "tula", "stop": "Баташи", "days": ["ежедневно"], "times": ["06:15"], "notes": [""]},
    ]
    assert matching_schedules(rows, {}) == []
    bus = matching_schedules(rows, {"route_number": "12", "transport_type": "bus"})
    assert [row["stop"] for row in bus] == ["РТИ", "Птицефабрики"]
    assert bus[0]["notes"] == [""]
    weekday = matching_schedules(rows, {"route_number": "12", "stop": "рти", "days": "будни"})
    assert weekday[0]["times"] == ["05:54"]
    city = matching_schedules(rows, {"route_number": "№ 12", "municipality": "Тула", "transport_type": "автобус"})
    assert {row["stop"] for row in city} == {"РТИ", "Птицефабрики"}
    terminal = matching_schedules(rows, {"route_number": "12", "stop": "Птицефабрика «Тульская»"})
    assert [row["stop"] for row in terminal] == ["Птицефабрики"]


def test_departure_answer_uses_schedule_when_model_omits_times():
    results = [{"ok": True, "schedules": [{
        "stop": "РТИ", "days": ["будни"], "times": ["05:54", "06:09"], "notes": ["", ""],
    }]}]
    refused = unconfirmed_departure_answer(
        "Какое расписание у автобуса 12?",
        {"status": "answered", "answer": "Расписание автобуса №12 сейчас недоступно в официальной базе."},
        results,
    )
    assert refused["status"] == "answered"
    assert "05:54" in refused["answer"]
    assert "РТИ" in refused["answer"]
    hidden = unconfirmed_departure_answer(
        "Подскажи расписание 12 маршрута в городе Тула",
        {"status": "no_data", "answer": "Расписание автобуса №12 в Туле сейчас недоступно в официальной базе."},
        results,
    )
    assert "05:54" in hidden["answer"]


def test_bus_12_rti_weekdays_come_from_registry():
    tools = ToolRunContext(OfficialTextSearch(get_settings()), schedules=load_schedules())
    result = tools.execute("get_schedule", {"route_number": "12", "stop": "РТИ", "transport_type": "bus", "days": "будни"})
    assert result["ok"] is True
    assert result["schedules"]
    row = result["schedules"][0]
    assert row["times"]
    assert len(row["notes"]) == len(row["times"])
    assert "рти" in row["stop"].lower().replace("ё", "е")
