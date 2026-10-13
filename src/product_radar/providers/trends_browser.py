"""Pure Google Trends chart-export adapter; no browser or provider requests."""

import calendar
import csv
from datetime import date, timedelta
import hashlib
import io
import re

from ..models import SafetyError, timestamp

MAX_BYTES = 5 * 1024 * 1024
MAX_INTERVALS = 1000
PARSER_VERSION = "google-trends-csv-v1"


def _period(value, heading):
    week_headings = ("week", "woche", "semaine", "semana", "settimana")
    month_headings = ("month", "monat", "mois", "mes", "mese")
    if re.fullmatch(r"\d{4}-\d{2}", value):
        if heading not in month_headings:
            raise ValueError("interval_header_mismatch")
        start = date.fromisoformat(value + "-01")
        return start, date(start.year, start.month, calendar.monthrange(start.year, start.month)[1]), "monthly"
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s+-\s+(\d{4}-\d{2}-\d{2})", value)
    if match:
        if heading not in week_headings:
            raise ValueError("interval_header_mismatch")
        start, end = map(date.fromisoformat, match.groups())
        if (end - start).days != 6:
            raise ValueError("nonweekly_range")
        return start, end, "weekly"
    start = date.fromisoformat(value)
    if heading in week_headings:
        return start, start + timedelta(days=6), "weekly"
    raise ValueError("unsupported_interval")


def _value(raw):
    if not raw or raw in ("-", "—", "N/A"):
        return {"value": None, "lower": None, "upper": None, "state": "missing"}
    if raw == "<1":
        return {"value": None, "lower": 0, "upper": 1, "upper_exclusive": True, "state": "bounded_nonzero"}
    if not re.fullmatch(r"\d{1,3}", raw) or not 0 <= int(raw) <= 100:
        raise ValueError("invalid_interest_value")
    value = int(raw)
    return {"value": value, "lower": value, "upper": value, "state": "observed"}


def parse_trends_csv(csv_bytes, capture_metadata):
    """Capture receipt must verify scope; CSV alone cannot establish geography.

    Required receipt fields: term, geo, time_range, kind, property, scope_verified,
    collected_at and source_reference. A verified receipt comes from controlled
    capture/UI review, not merely the URL originally requested. Optional
    column_label pins the exact visible term/topic label in translated exports.
    """
    result = {"schema_version": 1, "source": "google_trends", "acquisition_route": "browser_csv",
              "parser_version": PARSER_VERSION, "state": "Failed", "reason": "Invalid CSV capture.",
              "series": [], "metadata": {}, "raw_sha256": None}
    if not isinstance(csv_bytes, bytes) or not isinstance(capture_metadata, dict):
        return result
    result["raw_sha256"] = hashlib.sha256(csv_bytes).hexdigest()
    if len(csv_bytes) > MAX_BYTES:
        return {**result, "reason": "CSV exceeds the five MiB capture limit."}
    required = ("term", "geo", "time_range", "kind", "property", "collected_at", "source_reference")
    if any(not isinstance(capture_metadata.get(k), str) or not capture_metadata[k] or len(capture_metadata[k]) > 4096 for k in required):
        return {**result, "reason": "Capture receipt is missing explicit scope or source reference."}
    result["metadata"] = {k: capture_metadata[k] for k in required}
    result["metadata"]["scope_verified"] = capture_metadata.get("scope_verified") is True
    try:
        observed = timestamp(capture_metadata["collected_at"]).date()
    except SafetyError:
        return {**result, "reason": "Capture time is invalid."}
    if (capture_metadata["geo"] != "IN" or capture_metadata["time_range"] != "today 12-m"
            or capture_metadata["kind"] not in ("search_term", "topic") or capture_metadata["property"] != "web"
            or capture_metadata.get("scope_verified") is not True):
        return {**result, "state": "Insufficient", "reason": "Verified India, twelve-month, Web Search scope is required."}
    try:
        text = csv_bytes.decode("utf-8-sig")
        if "<html" in text[:1024].casefold() or "<!doctype" in text[:1024].casefold() or "\x00" in text:
            raise ValueError("html_or_binary_download")
        # Google's export uses comma; translated spreadsheet exports can use
        # semicolon. Pick the separator from an explicit interval header only.
        headings = {"week", "month", "woche", "monat", "semaine", "mois", "semana", "mes", "settimana", "mese"}
        header_line = next((line for line in text.splitlines() if any(
            line.split(delimiter, 1)[0].strip().strip('"').casefold() in headings for delimiter in (",", ";"))), None)
        if header_line is None:
            raise ValueError("interval_header_missing")
        delimiter = ";" if header_line.split(";", 1)[0].strip().strip('"').casefold() in headings else ","
        reader = csv.reader(io.StringIO(text), delimiter=delimiter, strict=True)
        header = None
        for row in reader:
            if row and row[0].strip().casefold() in headings:
                header = [cell.strip() for cell in row]
                break
        if not header or len(header) != 2:
            raise ValueError("single_term_header_required")
        expected = capture_metadata.get("column_label", capture_metadata["term"] + ": (India)")
        if not isinstance(expected, str) or header[1].casefold() != expected.casefold():
            raise ValueError("export_label_does_not_match_capture")
        if not re.search(r"\((india|indien|inde|índia)\)$", header[1], re.IGNORECASE):
            raise ValueError("export_country_label_is_not_india")
        series = []
        for row in reader:
            if not row or all(not cell.strip() for cell in row):
                continue
            if len(row) != 2:
                raise ValueError("malformed_interval_row")
            raw_period, raw_value = (cell.strip() for cell in row)
            start, end, interval = _period(raw_period, header[0].casefold())
            if start > end or (series and start <= date.fromisoformat(series[-1]["start"])):
                raise ValueError("duplicate_or_out_of_order_interval")
            series.append({"start": start.isoformat(), "end": end.isoformat(), "interval": interval,
                           "raw_period": raw_period, "raw_value": raw_value, "partial": None,
                           "ongoing_at_capture": end >= observed, **_value(raw_value)})
            if len(series) > MAX_INTERVALS:
                raise ValueError("interval_limit_exceeded")
        if len({p["interval"] for p in series}) > 1:
            raise ValueError("mixed_intervals")
        result.update(series=series, state="Usable" if series else "Insufficient",
                      reason="Parsed chart export; no absolute search volume inferred." if series else "Export contains no intervals.")
        return result
    except (UnicodeError, csv.Error, ValueError, OverflowError) as exc:
        return {**result, "reason": "CSV parse failed: " + str(exc)[:160]}
