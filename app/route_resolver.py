"""Official-data-only route identification. No model or fuzzy carrier fallback."""
import json
import re
from datetime import date
from pathlib import Path

from .municipalities import REGISTRY, normalize_municipality

ROUTES = json.loads((Path(__file__).parent / 'data' / 'routes.json').read_text(encoding='utf-8'))
MUNICIPALITY_IDS = {row['id'] for row in REGISTRY}


def normalize_route_number(text: str) -> str | None:
    value = text.lower().replace('ё', 'е')
    match = re.search(r'(?<!\w)№?\s*(\d{1,4})\s*([а-яa-z]?)(?!\w)', value)
    return (match.group(1) + match.group(2)) if match else None


def _current(route: dict) -> bool:
    return bool(route.get('verified') and (not route.get('valid_from') or date.fromisoformat(route['valid_from']) <= date.today()) and (not route.get('valid_to') or date.fromisoformat(route['valid_to']) >= date.today()))


def _candidates(rows: list[dict]) -> list[dict]:
    # Unselected matches must not hand an LLM an unverified carrier choice.
    return [{key: row[key] for key in ('route_number', 'municipality', 'name', 'source_url')}
            for row in rows]


def _fold(text: str) -> str:
    value = str(text or '').lower().replace('ё', 'е')
    value = re.sub(r'[«»"\'`]', ' ', value)
    value = re.sub(r'[-–—−]', ' ', value)
    value = re.sub(r'[^\w\s]', ' ', value)
    return re.sub(r'\s+', ' ', value).strip()


def _endpoint_pair(origin: str, destination: str) -> tuple[str, str] | None:
    left, right = _fold(origin), _fold(destination)
    if not left or not right:
        return None
    return tuple(sorted((left, right)))


def _label_match(row: dict, origin: str, destination: str, name: str) -> bool:
    matched = False
    if name:
        if _fold(row.get('name') or '') != _fold(name):
            return False
        matched = True
    if origin and destination:
        asked = _endpoint_pair(origin, destination)
        have = _endpoint_pair(row.get('origin') or '', row.get('destination') or '')
        same_stops = asked is not None and asked == have
        same_title = _fold(row.get('name') or '') == _fold(f'{origin} {destination}')
        if not (same_stops or same_title):
            return False
        matched = True
    return matched


def _municipality_id(text: str) -> str | None:
    raw = text.strip()
    if raw in MUNICIPALITY_IDS:
        return raw
    return normalize_municipality(raw) if raw else None


def _clarify(field: str, rows: list[dict] | None = None) -> dict:
    return {'status': 'needs_clarification', 'matches': _candidates(rows or []), 'route': None,
            'missing_fields': [field]}


def resolve_route(facts: dict) -> dict:
    number = normalize_route_number(str(facts.get('route_number') or ''))
    raw_municipality = str(facts.get('municipality') or '').strip()
    municipality = _municipality_id(raw_municipality)
    unrecognized_municipality = bool(raw_municipality) and not municipality
    origin_text = str(facts.get('origin') or '').strip()
    destination_text = str(facts.get('destination') or '').strip()
    name_text = str(facts.get('name') or '').strip()
    origin_city = _municipality_id(origin_text)
    destination_city = _municipality_id(destination_text)
    has_label = bool(name_text or (origin_text and destination_text))
    transport_type = facts.get('transport_type') or None
    if not number and not has_label and not (origin_city and destination_city):
        return _clarify('route_number')
    matches = [row for row in ROUTES if _current(row) and (not number or row['normalized_number'] == number)]
    if transport_type:
        matches = [row for row in matches if row['transport_type'] == transport_type]
    if municipality:
        matches = [row for row in matches if municipality in row['served_municipalities']]
    label_hit = False
    if has_label:
        labeled = [row for row in matches if _label_match(row, origin_text, destination_text, name_text)]
        if labeled:
            matches = labeled
            label_hit = True
        elif origin_city and destination_city:
            matches = [row for row in matches if {origin_city, destination_city} <= set(row['served_municipalities'])
                       and row.get('origin') and row.get('destination')]
        else:
            matches = []
    elif origin_city and destination_city:
        matches = [row for row in matches if {origin_city, destination_city} <= set(row['served_municipalities'])
                   and row.get('origin') and row.get('destination')]
    if not matches:
        if unrecognized_municipality and (not number or any(
                _current(row) and row['normalized_number'] == number for row in ROUTES)):
            return _clarify('municipality')
        return {'status': 'not_found', 'matches': [], 'route': None, 'missing_fields': []}
    if label_hit and len(matches) == 1:
        return {'status': 'resolved', 'matches': matches, 'route': matches[0], 'missing_fields': []}
    if unrecognized_municipality:
        return _clarify('municipality', matches)
    if not number and not label_hit:
        return _clarify('route_number', matches)
    # The source tables cover a selected area, not every route in the region. A
    # number alone is therefore insufficient evidence of regional uniqueness.
    if not municipality and not label_hit and not (origin_city and destination_city):
        return _clarify('municipality', matches)
    if len(matches) > 1:
        return {'status': 'ambiguous', 'matches': _candidates(matches), 'route': None,
                'missing_fields': ['route_number'] if not number else ['origin', 'destination']}
    return {'status': 'resolved', 'matches': matches, 'route': matches[0], 'missing_fields': []}
