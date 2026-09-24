"""Build a checked route registry from the OEIRC route tables.

The public pages list one district and one transport type at a time. This
script requests each pair and keeps a row only when it has a number, a name
and a carrier. The registry is not written into the BM25 corpus.
"""
import html
import json
import re
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from app.municipalities import normalize_municipality

ROOT = Path(__file__).parent
ROUTE_PAGES = (
    "https://oeirc.ru/?page=%D0%9A%D0%BE%D1%88%D0%B5%D0%BB%D0%B5%D0%BA%24tk/routes",
    "https://oeirc.ru/?page=%D0%90%D0%B1%D0%BE%D0%BD%D0%B5%D0%BC%D0%B5%D0%BD%D1%82%24tk/routes",
)
TRANSPORT_TYPES = {
    "автобус": "bus",
    "троллейбус": "trolleybus",
    "трамвай": "tram",
}
# Confirmed by an official route document. Do not add a number here without its URL.
SCOPE_DOCUMENTS = {
    "208": ("Узловая", "Тула", "uzlovaya", "https://orgpn.ru/files/reestr_marshruta/megmunitspal/208_uzlovaya_tula.pdf"),
    "114": ("Щёкино", "Тула", "schekino", "https://orgpn.ru/files/reestr_marshruta/megmunitspal/114_Tula_Shekino_s_01052022.pdf"),
}
ROW_RE = re.compile(
    r"<tr>\s*<td>\s*([^<]*)</td>\s*<td>\s*([^<]*)</td>\s*<td>\s*([^<]*)</td>\s*<td[^>]*>\s*([^<]*)</td>\s*<td[^>]*>\s*([^<]*)</td>",
    re.IGNORECASE,
)
ENDPOINT_SPLIT = re.compile(r"\s*[-–—]\s*")


def _clean(value: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", value.replace("\xa0", " "))).strip()


def _normalize_number(number: str) -> str:
    return re.sub(r"\s+", "", number.lower().replace("ё", "е"))


def _endpoints(name: str) -> tuple[str | None, str | None]:
    parts = [part.strip() for part in ENDPOINT_SPLIT.split(name) if part.strip()]
    if len(parts) != 2:
        return None, None
    return parts[0], parts[1]


def _record(place: str, transport: str, number: str, name: str, operator: str, source_url: str, verified_at: str) -> dict | None:
    place, transport, number, name, operator = (_clean(value) for value in (place, transport, number, name, operator))
    transport_type = TRANSPORT_TYPES.get(transport.lower())
    municipality = normalize_municipality(place)
    if not transport_type or not municipality or not number or not name or not operator:
        return None
    origin, destination = _endpoints(name)
    endpoint_ids = []
    for endpoint in (origin, destination):
        found = normalize_municipality(endpoint or "")
        if found and found not in endpoint_ids:
            endpoint_ids.append(found)
    if len(endpoint_ids) >= 2:
        route_scope = "intermunicipal"
        row_municipality = None
        served = endpoint_ids
    else:
        route_scope = "municipal"
        row_municipality = municipality
        served = [municipality]
        for found in endpoint_ids:
            if found not in served:
                served.append(found)
    return {
        "route_number": number,
        "normalized_number": _normalize_number(number),
        "transport_type": transport_type,
        "municipality": row_municipality,
        "route_scope": route_scope,
        "name": name,
        "origin": origin,
        "destination": destination,
        "operator": operator,
        "organizer": None,
        "served_municipalities": served,
        "source_url": source_url,
        "source_type": "oeirc",
        "verified": True,
        "verified_at": verified_at,
        "valid_from": None,
        "valid_to": None,
    }


def parse_route_catalog(text: str, source_url: str, verified_at: str) -> list[dict]:
    """Parse OEIRC route rows from HTML or from plain lines (place, type, number, name, carrier)."""
    records = []
    if "<tr" in text.lower():
        for place, transport, number, name, operator in ROW_RE.findall(text):
            row = _record(place, transport, number, name, operator, source_url, verified_at)
            if row:
                records.append(row)
        return records
    lines = [_clean(line) for line in text.splitlines() if _clean(line)]
    index = 0
    while index < len(lines):
        if lines[index].lower() in TRANSPORT_TYPES and index >= 1 and index + 3 < len(lines):
            row = _record(lines[index - 1], lines[index], lines[index + 1], lines[index + 2], lines[index + 3], source_url, verified_at)
            if row:
                records.append(row)
            index += 4
            continue
        if "|" in lines[index]:
            cells = [_clean(cell) for cell in lines[index].split("|")]
            if len(cells) >= 5:
                row = _record(cells[0], cells[1], cells[2], cells[3], cells[4], source_url, verified_at)
                if row:
                    records.append(row)
        index += 1
    return records


def apply_scope_documents(records: list[dict]) -> None:
    for number, (origin, destination, other_municipality, url) in SCOPE_DOCUMENTS.items():
        matches = [row for row in records if row["normalized_number"] == number and all(
            point.lower().replace("ё", "е") in row["name"].lower().replace("ё", "е")
            for point in (origin, destination)
        )]
        if len(matches) != 1:
            raise AssertionError((number, [(row["name"], row["source_url"]) for row in matches]))
        row = matches[0]
        row["municipality"] = None
        row["route_scope"] = "intermunicipal"
        row["origin"], row["destination"] = origin, destination
        row["served_municipalities"] = ["tula", other_municipality]
        row["scope_source_url"] = url


def _fetch(url: str, form: dict | None = None) -> str:
    data = urllib.parse.urlencode(form).encode() if form else None
    request = urllib.request.Request(url, data=data, headers={"User-Agent": "sber-transport-assistant"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", "replace")


def _regions(page: str) -> list[str]:
    options = re.findall(r"<option value='([^']*)'>", page)
    return options or ["Тула"]


def build_records(verified_at: str | None = None) -> list[dict]:
    verified_at = verified_at or date.today().isoformat()
    records = []
    seen = set()
    for url in ROUTE_PAGES:
        page = _fetch(url)
        forms = [{"Region": region, "Vehicle": vehicle}
                 for region in _regions(page)
                 for vehicle in ("Автобус", "Троллейбус", "Трамвай")]
        for form in forms:
            table = page if form["Region"] == "Тула" and form["Vehicle"] == "Автобус" else _fetch(url, form)
            for row in parse_route_catalog(table, url, verified_at):
                identity = (row["normalized_number"], row["transport_type"], row["name"], row["operator"], tuple(row["served_municipalities"]))
                if identity in seen:
                    continue
                seen.add(identity)
                records.append(row)
    apply_scope_documents(records)
    records.sort(key=lambda row: (row["normalized_number"], row["transport_type"], row["name"]))
    return records


def main() -> None:
    records = build_records()
    if any(row["verified"] and not row["route_scope"] for row in records):
        raise SystemExit("verified route without route_scope")
    path = ROOT / "app/data/routes.json"
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(records)} verified route records")


if __name__ == "__main__":
    main()
