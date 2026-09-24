import json
from pathlib import Path

from app.agent_service import SYSTEM_PROMPT
from app.config import get_settings
from app.ingest import (
    apply_source_freshness,
    chunk_record,
    classify_document_audience,
    discover_transport_documents,
    merge_discovered_catalog,
    warn_conflicting_fare_amounts,
)
from app.text_search import OfficialTextSearch


SOURCES_PATH = Path(__file__).resolve().parents[1] / "app" / "data" / "sources.json"
REGULATION_SOURCE_ID = "oeirc-discovered-862dacd7b3"
PASSENGER_RULES_SOURCE_ID = "oeirc-discovered-c3abfb76c9"


def test_only_current_enabled_sources_are_curated():
    sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    active = [s for s in sources if s.get("enabled", True)]
    assert active
    assert all(s.get("current", True) for s in active)


def test_stale_oeirc_social_card_page_is_excluded():
    sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    urls = {s["url"] for s in sources if s.get("enabled", True)}
    assert "https://oeirc.ru/transportnaya_karta/socialnaya-tk/" not in urls
    assert "https://oeirc.ru/?page=tk/stk.php" in urls


def test_document_discovery_keeps_only_transport_documents():
    html = """
    <html><body>
      <a href='/tk/docs/rules.pdf'>Правила транспортной системы Сбертройка</a>
      <a href='/docs/accounting.pdf'>Бухгалтерская отчетность</a>
      <a href='/tk/docs/change.docx'>Приказ о внесении изменений в правила ТКП</a>
    </body></html>
    """.encode("utf-8")
    parent = {
        "id": "oeirc-documents-index",
        "url": "https://oeirc.ru/?page=about/docs",
        "kind": "html",
        "authority": "АО ОЕИРЦ",
        "priority": 75,
    }
    docs = discover_transport_documents(html, "https://oeirc.ru/?page=about/docs", parent)
    urls = {d["url"] for d in docs}
    assert "https://oeirc.ru/tk/docs/rules.pdf" in urls
    assert "https://oeirc.ru/tk/docs/change.docx" in urls
    assert "https://oeirc.ru/docs/accounting.pdf" not in urls


def test_discovered_audience_follows_title():
    assert classify_document_audience("Технический регламент для участников АСОП ТО.") == "operator_technical"
    assert classify_document_audience("Правила транспортной системы «СберТройка»") == "passenger"
    html = """
    <html><body>
      <a href='/tk/docs/rules.pdf'>Правила транспортной системы СберТройка</a>
      <a href='/about/docs/reglament.pdf'>Технический регламент для участников АСОП ТО.</a>
    </body></html>
    """.encode("utf-8")
    parent = {"authority": "АО ОЕИРЦ", "priority": 75}
    docs = {d["url"]: d for d in discover_transport_documents(html, "https://oeirc.ru/?page=about/docs", parent)}
    rules = docs["https://oeirc.ru/tk/docs/rules.pdf"]
    regulation = docs["https://oeirc.ru/about/docs/reglament.pdf"]
    assert rules["audience"] == "passenger" and rules["enabled"] is True
    assert rules["scope"] == "region" and rules["priority"] <= 68 and rules["doc_type"] == "pdf"
    assert regulation["audience"] == "operator_technical" and regulation["enabled"] is False


def test_same_content_hash_stays_one_source_across_ingest():
    curated = [{"id": "oeirc-stk-current", "enabled": True, "current": True}]
    rules = {
        "id": PASSENGER_RULES_SOURCE_ID,
        "discovered": True,
        "content_hash": "same-pdf",
        "audience": "passenger",
        "enabled": True,
    }
    duplicate = {**rules, "id": "oeirc-discovered-5153be3a73", "url": "https://oeirc.ru/about/docs/same.pdf"}
    once = merge_discovered_catalog(curated, [rules, duplicate])
    twice = merge_discovered_catalog(once, [rules, duplicate])
    discovered = [row for row in twice if row.get("discovered")]
    assert [row["id"] for row in discovered] == [PASSENGER_RULES_SOURCE_ID]
    assert all(row["id"] != "oeirc-discovered-5153be3a73" for row in twice)


def test_technical_regulation_is_absent_from_passenger_fare_search():
    search = OfficialTextSearch(get_settings())
    hits = search.search("сколько стоит проездной", top_k=5)
    assert hits
    assert all(hit.row["source_id"] != REGULATION_SOURCE_ID for hit in hits)
    assert search.details(f"{REGULATION_SOURCE_ID}:0") == []


ROUTE_CATALOG_SOURCE_IDS = ("oeirc-routes-wallet", "oeirc-routes-subscription")


def test_route_catalogs_are_disabled_for_fulltext_search():
    sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    catalogs = [row for row in sources if row["id"] in ROUTE_CATALOG_SOURCE_IDS]
    assert len(catalogs) == 2
    assert all(row["enabled"] is False for row in catalogs)
    assert all("resolve_route" in row["note"] for row in catalogs)
    search = OfficialTextSearch(get_settings())
    hits = search.search("перевозчик маршрута 27", top_k=5, context={})
    found = {hit.row["source_id"] for hit in hits}
    assert ROUTE_CATALOG_SOURCE_IDS[0] not in found
    assert ROUTE_CATALOG_SOURCE_IDS[1] not in found
    assert "resolve_route, не search_official_sources" in SYSTEM_PROMPT


def test_chunk_and_source_keep_content_hash_and_fetched_at():
    loaded = {
        "resolved_url": "https://oeirc.ru/?page=tk/stk.php",
        "fetched_at": "2026-09-24T08:49:27+00:00",
        "content_hash": "abc123",
    }
    source = {
        "id": "oeirc-stk-current",
        "title": "СТК",
        "url": "https://oeirc.ru/?page=tk/stk.php",
        "category": "льготы_стк",
        "priority": 100,
        "current": True,
    }
    chunk = chunk_record(source, loaded, 0, "750 рублей на 1 месяц.")
    catalog = apply_source_freshness([source], {"oeirc-stk-current": {
        "content_hash": loaded["content_hash"],
        "fetched_at": loaded["fetched_at"],
    }})
    assert chunk["content_hash"] == "abc123"
    assert chunk["fetched_at"] == loaded["fetched_at"]
    assert catalog[0]["content_hash"] == "abc123"
    assert catalog[0]["fetched_at"] == loaded["fetched_at"]
    assert catalog[0]["priority"] == 100


def test_conflicting_current_fare_excerpts_warn_and_both_stay(capsys):
    sources = [
        {"id": "edition-a", "category": "льготы_стк", "current": True, "priority": 100},
        {"id": "edition-b", "category": "льготы_стк", "current": True, "priority": 60},
    ]
    cards = [
        {"id": "pass-a", "source_id": "edition-a", "amount": 750, "quoted_excerpt": "750 рублей на 1 месяц."},
        {"id": "pass-b", "source_id": "edition-b", "amount": 700, "quoted_excerpt": "700 рублей на 1 месяц."},
    ]
    kept = warn_conflicting_fare_amounts(sources, cards)
    warning = capsys.readouterr().out
    assert "[WARN]" in warning
    assert "льготы_стк" in warning
    assert "750" in warning and "700" in warning
    assert "priority unchanged" in warning
    assert kept == cards
    assert [row["priority"] for row in sources] == [100, 60]


def test_same_fare_amount_in_one_category_is_silent(capsys):
    sources = [
        {"id": "edition-a", "category": "льготы_стк", "current": True, "priority": 100},
        {"id": "edition-b", "category": "льготы_стк", "current": True, "priority": 60},
    ]
    cards = [
        {"id": "pass-a", "source_id": "edition-a", "amount": 750, "quoted_excerpt": "750 рублей."},
        {"id": "pass-b", "source_id": "edition-b", "amount": 750, "quoted_excerpt": "750 рублей."},
    ]
    kept = warn_conflicting_fare_amounts(sources, cards)
    assert capsys.readouterr().out == ""
    assert kept == cards


def test_passenger_rules_are_searchable_and_duplicate_pdf_is_not():
    search = OfficialTextSearch(get_settings())
    hits = search.search("правила транспортной системы сбертройка", top_k=5)
    found = [hit.row["source_id"] for hit in hits]
    assert PASSENGER_RULES_SOURCE_ID in found
    assert "oeirc-discovered-5153be3a73" not in found
