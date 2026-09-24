import json
from pathlib import Path

import pytest

from app.config import get_settings
from app.text_search import OfficialTextSearch, normalize_tokens


CASES_PATH = Path(__file__).with_name("retrieval_cases.json")
REQUIRED_TOPICS = {"pass": 4, "payment": 4, "complaint": 4, "route": 4}


def _load_cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


WORD_FORM_PAIRS = (
    ("стоит", "стоимость"),
    ("тариф", "тарифы"),
    ("банк", "банковской"),
    ("жалобу", "пожаловаться"),
)


def test_word_forms_share_tokens():
    for left, right in WORD_FORM_PAIRS:
        left_query = set(normalize_tokens(left, expand_synonyms=True))
        right_query = set(normalize_tokens(right, expand_synonyms=True))
        assert left_query & set(normalize_tokens(right)), (left, right)
        assert right_query & set(normalize_tokens(left)), (right, left)


@pytest.fixture(scope="module")
def search() -> OfficialTextSearch:
    return OfficialTextSearch(get_settings())


def test_retrieval_cases_cover_resident_topics():
    cases = _load_cases()
    assert 30 <= len(cases) <= 40
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))
    topics: dict[str, int] = {}
    for case in cases:
        assert case["query"].strip()
        assert isinstance(case["context"], dict)
        expect_empty = case.get("expect_empty") is True
        expect_ids = case.get("expect_source_ids") or []
        assert expect_empty or expect_ids
        assert not (expect_empty and expect_ids)
        if "xfail" in case:
            assert isinstance(case["xfail"], str) and case["xfail"].strip()
        topics[case["topic"]] = topics.get(case["topic"], 0) + 1
    for topic, minimum in REQUIRED_TOPICS.items():
        assert topics.get(topic, 0) >= minimum, topic


@pytest.mark.parametrize("case", _load_cases(), ids=lambda case: case["id"])
def test_retrieval_case(search: OfficialTextSearch, case: dict):
    hits = search.search(case["query"], top_k=5, context=case["context"])
    found = [hit.row["source_id"] for hit in hits]
    if case.get("expect_empty"):
        matched = not found
    else:
        matched = any(source_id in found for source_id in case["expect_source_ids"])
    if case.get("xfail"):
        if matched:
            pytest.fail(
                f"{case['id']} больше не проваливается, обновите кейс: {case['xfail']}; top5={found}"
            )
        print(f"XFAIL {case['id']}: {case['xfail']}; top5={found}")
        pytest.xfail(f"{case['id']}: {case['xfail']}")
    assert matched, f"{case['id']}: top5={found}"
