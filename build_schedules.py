"""Download Tula administration schedules and keep terminal departures only.

Raw files stay in a temporary directory. The registry written to git is
app/data/schedules.json. A route is skipped, not invented, when a file has
no terminal time table.
"""
import io
import json
import re
import ssl
import tempfile
import urllib.parse
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

import xlrd
from docx import Document
from pypdf import PdfReader
from xlrd import xldate_as_datetime

ROOT = Path(__file__).parent
OUTPUT = ROOT / "app/data/schedules.json"
BASE = "https://tula-r71.gosweb.gosuslugi.ru"
CATALOGS = (
    ("/deyatelnost/napravleniya-deyatelnosti/dorogi-obschestvennyy-transport/raspisanie-dvizheniya-munitsipalnogo-i-kommercheskogo-transporta/raspisanie-dvizheniya-avtobusov/", "bus"),
    ("/deyatelnost/napravleniya-deyatelnosti/dorogi-obschestvennyy-transport/raspisanie-dvizheniya-munitsipalnogo-i-kommercheskogo-transporta/raspisanie-dvizheniya-tramvaev/", "tram"),
    ("/deyatelnost/napravleniya-deyatelnosti/dorogi-obschestvennyy-transport/raspisanie-dvizheniya-munitsipalnogo-i-kommercheskogo-transporta/raspisanie-dvizheniya-trolleybusov/", "trolleybus"),
    ("/deyatelnost/napravleniya-deyatelnosti/dorogi-obschestvennyy-transport/raspisanie-dvizheniya-munitsipalnogo-i-kommercheskogo-transporta/po-nereguliruemomu-tarifu/", "bus"),
)
ANCHOR_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
FILE_RE = re.compile(r"\.(docx|xls|xlsx|pdf|zip)(?:$|\?)", re.IGNORECASE)
CLOCK_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)\s*(?:\(([^)]*)\))?")
ROUTE_RE = re.compile(r"№\s*([0-9]+\s*[-–—]?\s*[0-9A-Za-zА-Яа-яЁё]*)")
DEPARTURE_RE = re.compile(r"отправлени\w*\s+(?:из|с|от)\s+(.+)", re.IGNORECASE)
LATIN_TO_CYRILLIC = str.maketrans({
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
    "O": "О", "P": "Р", "T": "Т", "X": "Х", "a": "а", "c": "с", "e": "е",
    "o": "о", "p": "р", "x": "х", "y": "у",
})


def _fold(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower().replace("ё", "е")).strip()


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def route_number_from_label(label: str) -> str | None:
    match = ROUTE_RE.search(_clean(label))
    if not match:
        return None
    token = match.group(1).translate(LATIN_TO_CYRILLIC)
    token = re.sub(r"[\s\-–—]+", "", token)
    digits = re.match(r"(\d+)", token)
    if not digits:
        return None
    suffix = token[digits.end():]
    suffix = re.sub(r"[^0-9A-Za-zА-Яа-яЁё]", "", suffix).upper().translate(LATIN_TO_CYRILLIC)
    return digits.group(1) + suffix


def day_from_name(name: str) -> str | None:
    folded = _fold(name)
    if "выход" in folded:
        return "выходные"
    if "рабоч" in folded or "будн" in folded:
        return "будни"
    return None


def classify_day(text: str) -> str | None:
    if CLOCK_RE.search(text):
        return None
    folded = _fold(text)
    if "выход" in folded:
        return "выходные"
    if "рабоч" in folded or re.search(r"\bбудн", folded):
        return "будни"
    if "повседнев" in folded:
        return "ежедневно"
    return None


def stop_from_header(text: str) -> str | None:
    cleaned = _clean(text).strip(" .")
    if not cleaned or CLOCK_RE.search(cleaned) or classify_day(cleaned):
        return None
    folded = _fold(cleaned)
    if re.fullmatch(r"\d+", folded):
        return None
    if re.search(r"график", folded) or re.search(r"№?\s*вых", folded):
        return None
    departure = DEPARTURE_RE.search(cleaned)
    if departure:
        return _clean(departure.group(1)).strip(" .")
    if "время" in folded:
        return None
    return cleaned


def parse_clock_cells(text: str) -> list[tuple[str, str]]:
    found = []
    for match in CLOCK_RE.finditer(str(text or "")):
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            continue
        note = _clean(match.group(3) or "")
        found.append((f"{hour:02d}:{minute:02d}", note))
    return found


def _add_pairs(bucket: dict[tuple[str, str], list[tuple[str, str]]], stop: str, day: str, pairs: list[tuple[str, str]]) -> None:
    if not stop or not pairs:
        return
    bucket.setdefault((stop, day), []).extend(pairs)


def parse_grid(grid: list[list[str]], default_day: str | None) -> list[dict]:
    """Read a terminal table: stop headers, a day banner, and clock cells."""
    if not grid:
        return []
    width = max(len(row) for row in grid)
    days = [default_day or "ежедневно"] * width
    stops = [None] * width
    bucket: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for row in grid:
        cells = [_clean(row[index] if index < len(row) else "") for index in range(width)]
        for index, cell in enumerate(cells):
            labeled = classify_day(cell)
            if labeled and not parse_clock_cells(cell):
                days[index] = labeled
                continue
            header = stop_from_header(cell)
            if header:
                stops[index] = header
                continue
            if stops[index]:
                _add_pairs(bucket, stops[index], days[index], parse_clock_cells(cell))
    return _records_from_bucket(bucket)


def _records_from_bucket(bucket: dict[tuple[str, str], list[tuple[str, str]]]) -> list[dict]:
    records = []
    for (stop, day), pairs in bucket.items():
        unique = list(dict.fromkeys(pairs))
        unique.sort(key=lambda item: item[0])
        if not unique:
            continue
        records.append({
            "stop": stop,
            "days": [day],
            "times": [item[0] for item in unique],
            "notes": [item[1] for item in unique],
        })
    return records


def _cell_text(cell: xlrd.sheet.Cell, datemode: int) -> str:
    if cell.ctype == xlrd.XL_CELL_DATE or (cell.ctype == xlrd.XL_CELL_NUMBER and 0 < cell.value < 1):
        try:
            stamp = xldate_as_datetime(cell.value, datemode)
        except Exception:
            return ""
        return stamp.strftime("%H:%M")
    if cell.ctype == xlrd.XL_CELL_NUMBER:
        if cell.value == int(cell.value):
            return str(int(cell.value))
        return str(cell.value)
    return _clean(cell.value)


def parse_interval_sheet(sheet: xlrd.sheet.Sheet, datemode: int, default_day: str | None) -> list[dict]:
    """Terminal departures live under «отправление от …», not in the run grid."""
    grid = [[_cell_text(sheet.cell(row, col), datemode) for col in range(sheet.ncols)] for row in range(sheet.nrows)]
    day = default_day or "ежедневно"
    bucket: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for row, line in enumerate(grid):
        for col, text in enumerate(line):
            match = DEPARTURE_RE.search(text)
            if not match:
                continue
            stop = _clean(match.group(1)).strip(" .")
            time_col = None
            for look_row in range(row + 1, min(row + 8, len(grid))):
                for look_col in range(col, min(col + 6, len(grid[look_row]))):
                    label = _fold(grid[look_row][look_col])
                    if "отправ" in label and "интер" not in label and "отправлен" not in label:
                        time_col = look_col
            if time_col is None:
                continue
            started = False
            blanks = 0
            for look_row in range(row + 1, len(grid)):
                pairs = parse_clock_cells(grid[look_row][time_col])
                if pairs:
                    started = True
                    blanks = 0
                    _add_pairs(bucket, stop, day, pairs)
                    continue
                if not started:
                    continue
                blanks += 1
                if blanks >= 3:
                    break
    return _records_from_bucket(bucket)


def parse_docx(path: Path, default_day: str | None = None) -> list[dict]:
    document = Document(path)
    records = []
    for table in document.tables:
        grid = [[cell.text for cell in row.cells] for row in table.rows]
        records.extend(parse_grid(grid, default_day))
    return records


def parse_xls(data: bytes, default_day: str | None) -> list[dict]:
    book = xlrd.open_workbook(file_contents=data)
    records = []
    for sheet in book.sheets():
        records.extend(parse_interval_sheet(sheet, book.datemode, default_day))
    return records


def parse_pdf(data: bytes, default_day: str | None) -> list[dict]:
    reader = PdfReader(io.BytesIO(data))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    lines = [_clean(line) for line in text.splitlines() if _clean(line)]
    if not any(";" in line and len(parse_clock_cells(line)) >= 3 for line in lines):
        return []
    return parse_grid([[line] for line in lines], default_day)


def parse_payload(name: str, data: bytes, default_day: str | None) -> tuple[list[dict], str | None]:
    suffix = Path(name).suffix.lower()
    day = day_from_name(name) or default_day
    try:
        if suffix == ".docx":
            with tempfile.NamedTemporaryFile(suffix=".docx") as handle:
                handle.write(data)
                handle.flush()
                return parse_docx(Path(handle.name), day), None
        if suffix == ".xls":
            return parse_xls(data, day), None
        if suffix == ".pdf":
            parsed = parse_pdf(data, day)
            return parsed, None if parsed else "pdf без таблицы конечных"
        if suffix == ".xlsx":
            return [], "xlsx не разбирается"
    except Exception as error:
        return [], f"{suffix or 'file'}: {error}"
    return [], f"неизвестный формат {suffix}"


def parse_download(name: str, data: bytes) -> tuple[list[dict], list[str]]:
    if name.lower().endswith(".zip"):
        parsed, skipped = [], []
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                rows, reason = parse_payload(info.filename, archive.read(info), day_from_name(info.filename))
                if rows:
                    parsed.extend(rows)
                elif reason:
                    skipped.append(f"{info.filename}: {reason}")
                else:
                    skipped.append(info.filename)
        return parsed, skipped
    rows, reason = parse_payload(name, data, day_from_name(name))
    if rows:
        return rows, []
    return [], [f"{name}: {reason or 'нет времён'}"]


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "sber-transport-assistant"})
    with urllib.request.urlopen(request, timeout=90, context=_ssl_context()) as response:
        return response.read()


def catalog_links(html: str) -> list[tuple[str, str]]:
    found = []
    for href, label in ANCHOR_RE.findall(html):
        if not FILE_RE.search(href):
            continue
        label = re.sub(r"<[^>]+>", " ", label)
        label = _clean(label)
        found.append((urllib.parse.urljoin(BASE, href), label))
    return found


def _published(route_number: str, transport_type: str, source_url: str, fetched_at: str, row: dict) -> dict:
    return {
        "route_number": route_number,
        "municipality": "tula",
        "transport_type": transport_type,
        "stop": row["stop"],
        "days": row["days"],
        "times": row["times"],
        "notes": row["notes"],
        "source_url": source_url,
        "fetched_at": fetched_at,
    }


def require_bus_12(records: list[dict]) -> None:
    rows = [row for row in records if row["route_number"] == "12" and row["transport_type"] == "bus" and "рти" in _fold(row["stop"])]
    days = {row["days"][0] for row in rows}
    if "будни" not in days or "выходные" not in days:
        raise SystemExit(f"автобус 12 без конечной РТИ на будни и выходные: {sorted(days) or 'нет строк'}")


def build_records(fetched_at: str | None = None) -> tuple[list[dict], list[str]]:
    fetched_at = fetched_at or date.today().isoformat()
    records = []
    skipped = []
    seen = set()
    for path, transport_type in CATALOGS:
        page = _fetch(BASE + path).decode("utf-8", "replace")
        for url, label in catalog_links(page):
            number = route_number_from_label(label)
            if not number:
                skipped.append(f"{label or url}: нет номера в подписи")
                continue
            try:
                payload = _fetch(url)
            except Exception as error:
                skipped.append(f"{label}: {error}")
                continue
            parsed, reasons = parse_download(Path(urllib.parse.urlparse(url).path).name, payload)
            if not parsed:
                skipped.append(f"{label}: {'; '.join(reasons) or 'нет таблицы конечных'}")
                continue
            for row in parsed:
                if len(row["notes"]) != len(row["times"]):
                    raise SystemExit(f"notes и times разной длины у {number} {row['stop']}")
                item = _published(number, transport_type, url, fetched_at, row)
                identity = (item["route_number"], item["transport_type"], _fold(item["stop"]), tuple(item["days"]), tuple(item["times"]), tuple(item["notes"]))
                if identity in seen:
                    continue
                seen.add(identity)
                records.append(item)
            if reasons:
                skipped.append(f"{label}: часть файлов пропущена ({'; '.join(reasons)})")
    records.sort(key=lambda row: (row["transport_type"], row["route_number"], row["days"][0], _fold(row["stop"])))
    require_bus_12(records)
    return records, skipped


def main() -> None:
    records, skipped = build_records()
    OUTPUT.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(records)} schedule rows")
    if skipped:
        print(f"{len(skipped)} skipped")
        for line in skipped:
            print(f"  - {line}")


if __name__ == "__main__":
    main()
