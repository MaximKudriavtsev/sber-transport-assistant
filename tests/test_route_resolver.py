from build_routes import parse_route_catalog
from app.route_resolver import ROUTES, _current, normalize_route_number, resolve_route

CATALOG_FIXTURE = """
Тула
Автобус
27
пос. Северный - пос. Западный - пос. Угольный
ИП Макарова Н.Е.
Новомосковск
Трамвай
5
Новомосковск - Донской
ООО «Пример»
Тула
Автобус
39
Северная Мыза
ООО «ИРБИС»
Тула
Автобус
39А
Северная Мыза – мкр. Новая Тула
ООО «ИРБИС»
Алексин | Автобус | 10 | Бор-Кладбище |
"""


def test_registry_and_normalization():
    assert len(ROUTES) >= 90
    assert all(row["route_scope"] in {"municipal", "intermunicipal"} for row in ROUTES if row["verified"])
    assert any(row["transport_type"] == "tram" for row in ROUTES)
    assert any(row["municipality"] not in {None, "tula"} for row in ROUTES)
    for text in ("10Л", "10л", "10 л", "маршрут 10л", "автобус №10Л"):
        assert normalize_route_number(text) == "10л"


def test_catalog_parser_two_cities_and_tram():
    rows = parse_route_catalog(CATALOG_FIXTURE, "https://example.test/routes", "2026-09-24")
    by_key = {(row["normalized_number"], row["transport_type"]): row for row in rows}
    assert by_key[("27", "bus")]["route_scope"] == "municipal"
    assert by_key[("27", "bus")]["municipality"] == "tula"
    tram = by_key[("5", "tram")]
    assert tram["municipality"] is None
    assert tram["route_scope"] == "intermunicipal"
    assert set(tram["served_municipalities"]) == {"novomoskovsk", "donskoy"}
    assert by_key[("39", "bus")]["normalized_number"] != by_key[("39а", "bus")]["normalized_number"]
    assert ("10", "bus") not in by_key


def test_verified_operators_and_no_guessing():
    ten = resolve_route({"route_number": "10Л", "municipality": "tula"})
    assert ten["status"] == "resolved"
    assert ten["route"]["operator"] == "ООО «ИРБИС»"
    assert resolve_route({"route_number": "208"})["status"] == "needs_clarification"
    assert resolve_route({"route_number": "39", "municipality": "tula"})["status"] == "not_found"
    assert resolve_route({"route_number": "39А", "municipality": "tula"})["status"] == "resolved"
    assert resolve_route({"route_number": "9999", "municipality": "tula"})["status"] == "not_found"


def test_route_27_still_resolves_from_registry():
    route = resolve_route({"route_number": "27", "municipality": "tula"})
    assert route["status"] == "resolved"
    assert route["route"]["operator"] == "ИП Макарова Н.Е."
    assert route["route"]["route_number"] == "27"


def test_route_found_by_endpoint_names():
    route = resolve_route({
        "origin": "Завод РТИ",
        "destination": "Птицефабрика «Тульская»",
        "transport_type": "bus",
    })
    assert route["status"] == "resolved"
    assert route["route"]["route_number"] == "12"
    assert route["route"]["municipality"] == "tula"
    assert route["route"]["name"] == "Завод РТИ – Птицефабрика «Тульская»"


def test_full_route_name_resolves_without_number():
    route = resolve_route({"name": "Завод РТИ - Птицефабрика Тульская"})
    assert route["status"] == "resolved"
    assert route["route"]["normalized_number"] == "12"
    assert route["route"]["transport_type"] == "bus"


def test_unrecognized_municipality_is_not_false_not_found():
    route = resolve_route({"route_number": "12", "municipality": "Тульская", "transport_type": "bus"})
    assert route["status"] == "needs_clarification"
    assert route["missing_fields"] == ["municipality"]
    found = resolve_route({
        "route_number": "12",
        "municipality": "Тульская",
        "origin": "Завод РТИ",
        "destination": "Птицефабрика «Тульская»",
        "transport_type": "bus",
    })
    assert found["status"] == "resolved"
    assert found["route"]["name"] == "Завод РТИ – Птицефабрика «Тульская»"


def test_intermunicipal_and_ambiguity():
    route = resolve_route({"route_number": "208", "municipality": "uzlovaya"})
    assert route["route"]["route_scope"] == "intermunicipal"
    assert "orgpn.ru" in route["route"]["scope_source_url"]
    assert resolve_route({"route_number": "181", "municipality": "tula"})["status"] == "ambiguous"
    assert _current({**route["route"], "valid_to": "2020-01-01"}) is False
