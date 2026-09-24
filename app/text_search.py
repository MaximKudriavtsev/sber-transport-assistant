import json
import math
import re
from collections import Counter
from dataclasses import dataclass

from rapidfuzz.fuzz import token_set_ratio

from .config import Settings
from .municipalities import normalize_municipality


def source_in_passenger_search(source: dict) -> bool:
    """Operator manuals and disabled documents stay out of search and details."""
    if source.get("enabled", True) is False:
        return False
    return source.get("audience", "passenger") == "passenger"


CONDITIONAL_OPERATOR_NOTE = "тариф этого перевозчика, не всей области"


def source_applicability(source: dict, context: dict | None = None) -> str | None:
    """Return applicable, conditional, or None when the source must be dropped.

    An operator page stays visible when the operator slot is empty, but only as
    conditional. A known operator that does not match is still dropped.
    """
    context = context or {}
    scope = source.get("scope", "unknown")
    if scope == "region":
        return "applicable"
    if scope == "municipality":
        city = str(context.get("municipality") or "")
        if city and source.get("municipality") == (normalize_municipality(city) or city):
            return "applicable"
        return None
    if scope == "operator":
        known = context.get("operator")
        if known and source.get("operator") == known:
            return "applicable"
        if not known:
            return "conditional"
        return None
    if scope == "route_specific":
        if source.get("route_number") and context.get("route_number") == source["route_number"]:
            return "applicable"
        return None
    return None


def source_applicable(source: dict, context: dict | None = None) -> bool:
    """True only for a confirmed scope match. Unknown operator is not confirmed."""
    return source_applicability(source, context) == "applicable"


STOPWORDS = {
    "и", "в", "во", "на", "не", "что", "как", "для", "ли", "а", "по", "с", "со",
    "у", "из", "к", "ко", "при", "это", "где", "можно", "могу", "сейчас", "щас",
}

# Query-only expansions. Documents stay on their own word forms.
QUERY_SYNONYM_GROUPS = (
    ("жалоба", "обращение", "пожаловаться"),
    ("билет", "проезд", "тариф"),
    ("стоит", "стоимость"),
    ("банковская карта", "стоп-лист"),
)

# Longest first. Relational adjective endings are listed whole so «банковской» meets «банк».
_ENDINGS = (
    "овскими", "евскими", "овского", "евского", "овскому", "евскому",
    "овская", "евская", "овское", "евское", "овские", "евские",
    "овскую", "евскую", "овской", "евской", "овский", "евский",
    "овским", "евским", "овских", "евских",
    "скими", "ского", "скому", "ская", "ское", "ские", "скую", "ской", "ский", "ским", "ских",
    "остью", "ости", "ость",
    "иями", "ями", "ами", "иях", "ого", "его", "ому", "ему", "ыми", "ими",
    "ах", "ях", "ов", "ев", "ам", "ям", "ом", "ем",
    "ая", "яя", "ое", "ее", "ые", "ие", "ую", "юю", "ою", "ею",
    "ий", "ый", "ой", "ей", "ым", "им", "ых", "их",
    "а", "я", "ы", "и", "е", "у", "ю", "о", "ь",
)
_MIN_STEM = 3


def _fold(text: str) -> str:
    return (
        text.lower()
        .replace("ё", "е")
        .replace("‑", "-")
        .replace("–", "-")
        .replace("—", "-")
    )


def _stem_word(word: str) -> str:
    """Strip a noun or adjective ending. Numbers, route ids, and Latin stay intact."""
    if any(char.isdigit() or "a" <= char <= "z" for char in word):
        return word
    for ending in _ENDINGS:
        if len(word) - len(ending) < _MIN_STEM or not word.endswith(ending):
            continue
        return word[: -len(ending)]
    return word


def _tokenize(text: str) -> list[str]:
    result = []
    for word in re.findall(r"[a-zа-яё0-9]+", _fold(text)):
        if word in STOPWORDS:
            continue
        result.append(_stem_word(word))
    return result


def _synonym_matches(folded: str, query_stems: set[str], member: str) -> bool:
    """A phrase synonym matches only when every word is present, not one leftover token."""
    if _term_in_text(folded, _fold(member)):
        return True
    stems = _tokenize(member)
    if not stems:
        return False
    if len(stems) == 1:
        return stems[0] in query_stems
    return set(stems) <= query_stems


def _term_in_text(text: str, term: str) -> bool:
    pattern = r"(?<![0-9a-zа-я])" + re.escape(term) + r"(?![0-9a-zа-я])"
    return re.search(pattern, text) is not None


def expand_query(text: str) -> str:
    """Append passenger synonyms to the query. Indexed documents are not expanded."""
    folded = _fold(text)
    query_stems = set(_tokenize(folded))
    extras: list[str] = []
    for group in QUERY_SYNONYM_GROUPS:
        if not any(_synonym_matches(folded, query_stems, member) for member in group):
            continue
        extras.extend(member for member in group if not _synonym_matches(folded, query_stems, member))
    if not extras:
        return text
    return f"{text} {' '.join(extras)}"


def normalize_tokens(text: str, *, expand_synonyms: bool = False) -> list[str]:
    source = expand_query(text) if expand_synonyms else text
    tokens = _tokenize(source)
    if not expand_synonyms:
        return tokens
    seen: set[str] = set()
    unique: list[str] = []
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        unique.append(token)
    return unique


@dataclass
class SearchHit:
    row: dict
    score: float
    applicability: str = "applicable"


class OfficialTextSearch:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.sources = {row["id"]: row for row in self._load(settings.sources_path)}
        self.chunks = self._load(settings.chunks_path)
        self.by_id = {row["id"]: row for row in self.chunks if row.get("current", True)}
        self.by_source: dict[str, list[dict]] = {}
        self.tokens: dict[str, list[str]] = {}
        document_frequency: Counter[str] = Counter()
        for row in self.by_id.values():
            self.by_source.setdefault(row["source_id"], []).append(row)
            tokens = normalize_tokens(
                f"{row.get('title', '')} {row.get('category', '')} {row.get('text', '')}"
            )
            self.tokens[row["id"]] = tokens
            document_frequency.update(set(tokens))
        count = max(1, len(self.by_id))
        self.idf = {term: math.log(1 + (count - freq + 0.5) / (freq + 0.5))
                    for term, freq in document_frequency.items()}
        lengths = [len(tokens) for tokens in self.tokens.values()]
        self.average_length = sum(lengths) / max(1, len(lengths))

    @staticmethod
    def _load(path) -> list[dict]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, list) else []
        except (OSError, ValueError):
            return []

    @property
    def ready(self) -> bool:
        return bool(self.chunks)

    def search(self, query: str, category: str | None = None, top_k: int = 5,
               context: dict | None = None) -> list[SearchHit]:
        query_tokens = normalize_tokens(query, expand_synonyms=True)
        if not query_tokens:
            return []
        query_text = " ".join(query_tokens)
        hits = []
        for row_id, row in self.by_id.items():
            source = self.sources.get(row["source_id"], {})
            applicability = source_applicability(source, context)
            if not source_in_passenger_search(source) or applicability is None:
                continue
            row_category = str(row.get("category", ""))
            if category and category.lower() not in row_category.lower():
                continue
            tokens = self.tokens[row_id]
            frequencies = Counter(tokens)
            length = len(tokens)
            bm25 = 0.0
            for term in query_tokens:
                frequency = frequencies.get(term, 0)
                if not frequency:
                    continue
                denominator = frequency + 1.2 * (1 - 0.75 + 0.75 * length / max(1, self.average_length))
                bm25 += self.idf.get(term, 0) * frequency * 2.2 / denominator
            fuzzy = token_set_ratio(query_text, " ".join(tokens)) / 100
            phrase = 1.0 if query.lower() in row.get("text", "").lower() else 0.0
            numbers = re.findall(r"\d+(?:[,.]\d+)?", query)
            number_bonus = 0.8 if numbers and all(number in row.get("text", "") for number in numbers) else 0
            priority = max(0, min(100, int(row.get("priority", 50)))) / 100
            score = bm25 + 1.4 * fuzzy + 1.2 * phrase + number_bonus + 0.18 * priority
            if score > 0.35:
                hits.append(SearchHit(row=row, score=round(score, 4), applicability=applicability))
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[:max(1, min(top_k, 8))]

    def details(self, result_id: str, neighbor_count: int = 1,
                context: dict | None = None) -> list[dict]:
        row = self.by_id.get(result_id)
        source = self.sources.get(row["source_id"], {}) if row else {}
        if not row or not source_in_passenger_search(source) or source_applicability(source, context) is None:
            return []
        source_rows = self.by_source.get(row["source_id"], [])
        try:
            index = next(i for i, candidate in enumerate(source_rows) if candidate["id"] == result_id)
        except StopIteration:
            return [row]
        start = max(0, index - max(0, min(neighbor_count, 2)))
        end = min(len(source_rows), index + max(0, min(neighbor_count, 2)) + 1)
        return source_rows[start:end]
