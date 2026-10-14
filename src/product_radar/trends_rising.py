"""Rising related queries from a signed-in Trends read, saved for discovery (W8).

The export expression keeps the rendered "Related queries" widget's own text
(``trends_controller._RELATED_JS``; nothing is clicked). This module reads the
Rising list as shown: rank, query, and Google's growth label ("Breakout" or
"+250%"). It never infers relevance; W8 decides which queries become
questions. The structured result is saved beside the read as
``rising_queries.json`` and can be re-derived from the saved ``scope.json``
(``rising_queries``) when the parser changes.

States: "read" (queries found), "none" (the widget says there is not enough
data), "top_selected" (the widget showed Top, not Rising), "not_rendered"
(no widget on the page) and "unreadable" (a widget this parser cannot read).
"""

from hashlib import sha256
import re

from .storage import atomic_json, load_json, safe_path

KIND = "trends-rising-queries-v1"
PARSER_VERSION = "trends-rising-dom-v1"
FILE = "rising_queries.json"
MAX_QUERIES = 25
_ICONS = {"help_outline", "file_download", "code", "share", "more_vert", "arrow_drop_down", "chevron_left",
          "chevron_right", "Related queries"}
_VALUE = re.compile(r"(Breakout)|\+?((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)%")
_SHOWING = re.compile(r"Showing \d+-\d+ of (\d+) quer(?:y|ies)", re.I)
_NONE = re.compile(r"(?:doesn't|does not) have enough data|not enough data|no data", re.I)


def parse_rising_queries(related):
    """The widget's Rising list as structured rows (see the module states)."""
    result = {"parser_version": PARSER_VERSION, "state": "not_rendered", "section": None, "queries": [], "total": None}
    if not isinstance(related, dict) or not isinstance(related.get("text"), str):
        return result
    lines = [line for line in (" ".join(raw.split()) for raw in related["text"][:8000].splitlines())
             if line and line not in _ICONS]
    sections = {line for line in lines if line in ("Rising", "Top")}
    if sections != {"Rising"}:
        return dict(result, state="top_selected" if sections == {"Top"} else "unreadable")
    queries, index = [], 0
    while index + 2 < len(lines) and len(queries) < MAX_QUERIES:
        rank, query, value = lines[index:index + 3]
        found = _VALUE.fullmatch(value)
        if (rank == str(len(queries) + 1) and found and 0 < len(query) <= 120 and query not in ("Rising", "Top")
                and not _VALUE.fullmatch(query) and not re.search(r"[\x00-\x1f\x7f]", query)):
            percent = None if found[1] else float(found[2].replace(",", ""))
            queries.append({"rank": len(queries) + 1, "query": query, "raw_value": value,
                            "growth_kind": "breakout" if found[1] else "percentage", "growth_percent": percent})
            index += 3
        else:
            index += 1
    total = next((int(found[1]) for found in map(_SHOWING.search, lines) if found), None)
    state = "read" if queries else "none" if any(_NONE.search(line) for line in lines) else "unreadable"
    return dict(result, state=state, section="Rising", queries=queries, total=total)


def _record(folder, receipt):
    from .trends_controller import MAX_JSON
    snapshot = load_json(folder / receipt.get("scope_file", "scope.json"), MAX_JSON)
    scope_name = receipt.get("scope_file", "scope.json")
    return {"kind": KIND, "acquisition_id": receipt["acquisition_id"], "term": receipt["request"]["query"],
            "country": "IN", "time_range": "today 12-m", "observed_at": receipt["observed_at"],
            "source_reference": snapshot.get("url") if isinstance(snapshot, dict) else None,
            "scope_sha256": receipt["artifacts"][scope_name],
            **parse_rising_queries(snapshot.get("related") if isinstance(snapshot, dict) else None)}


def save_rising_queries(folder, receipt):
    """Save the read's rising queries when its India/year/Web Search scope was
    verified; returns the record, or None for an unverified page."""
    folder = safe_path(folder)
    if receipt.get("scope_verified") is not True or receipt.get("scope_file", "scope.json") not in receipt.get("artifacts", {}):
        return None
    record = _record(folder, receipt)
    atomic_json(folder / FILE, record)
    return record


def rising_queries(folder):
    """The saved rising queries of one Trends read (W8 reads this). The saved
    record is reused only when it belongs to the read's current saved scope
    and this parser version; otherwise it is derived again and saved."""
    from .trends_controller import reconcile_direct_trends
    folder = safe_path(folder)
    receipt = reconcile_direct_trends(folder)
    scope_name = receipt.get("scope_file", "scope.json")
    if receipt.get("scope_verified") is not True or scope_name not in receipt["artifacts"]:
        return None
    path = folder / FILE
    saved = load_json(path, 262144) if path.is_file() else None
    if (isinstance(saved, dict) and saved.get("kind") == KIND and saved.get("parser_version") == PARSER_VERSION
            and saved.get("acquisition_id") == receipt["acquisition_id"]
            and saved.get("scope_sha256") == receipt["artifacts"][scope_name]
            == sha256((folder / scope_name).read_bytes()).hexdigest()):
        return saved
    return save_rising_queries(folder, receipt)
