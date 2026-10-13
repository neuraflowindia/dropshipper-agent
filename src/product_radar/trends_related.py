"""Public related-query CSV support, explicitly reviewed against one saved chart.

No collection, model calls, inferred relevance or alteration of the original
time series occurs here. TOP popularity values are not growth percentages.
"""

from copy import deepcopy
import csv
from datetime import datetime, timedelta
import hashlib
import io
import re

from .models import SafetyError, digest, identifier, timestamp
from .providers.trends_browser import MAX_BYTES
from .research_memory import key

PARSER_VERSION = "google-trends-related-csv-v2"
MAX_QUERIES = 1000


def _scope_label(label, metadata):
    """Validate the actual English export's term, India and dated annual range.

    Undated labels remain accepted only with the already-verified capture
    receipt. A dated label must independently agree with the annual scope;
    one day accommodates Google's display timezone versus the UTC receipt.
    """
    if label in (metadata["term"] + ": (India)", metadata["term"] + " (India)"):
        return {}
    date_pattern = r"[0-9]{1,2}/[0-9]{1,2}/(?:[0-9]{4}|[0-9]{2})"
    pattern = re.escape(metadata["term"]) + r":? \((" + date_pattern + r")\s+-\s+(" + date_pattern + r"), India\)"
    match = re.fullmatch(pattern, label)
    if not match:
        return None
    try:
        start, end = [datetime.strptime(value, "%m/%d/%Y" if len(value.rsplit("/", 1)[1]) == 4 else "%m/%d/%y").date()
                      for value in match.groups()]
        captured = timestamp(metadata["collected_at"]).date()
        try:
            annual_start = end.replace(year=end.year - 1)
        except ValueError:
            # Leap-day annual ranges have the usual February-end boundary.
            annual_start = end.replace(year=end.year - 1, day=28)
        if start >= end or abs((end - captured).days) > 1 or abs((start - annual_start).days) > 1:
            return None
        return {"export_date_range": {"start": start.isoformat(), "end": end.isoformat()},
                "export_date_format": "month/day/year"}
    except (ValueError, OverflowError):
        return None


def parse_related_queries_csv(raw, metadata):
    result = {"state": "Failed", "reason": "Invalid related-query CSV.",
              "parser_version": PARSER_VERSION, "metadata": {}, "queries": [],
              "top_queries": [], "raw_sha256": None}
    if not isinstance(raw, bytes) or not isinstance(metadata, dict):
        return result
    result["raw_sha256"] = hashlib.sha256(raw).hexdigest()
    required = ("term", "geo", "time_range", "kind", "property", "collected_at", "source_reference")
    if len(raw) > MAX_BYTES:
        return {**result, "reason": "CSV exceeds the five MiB capture limit."}
    if any(not isinstance(metadata.get(k), str) or not metadata[k] or len(metadata[k]) > 4096 for k in required):
        return {**result, "reason": "Related-query capture receipt is incomplete."}
    result["metadata"] = {k: metadata[k] for k in required}
    result["metadata"].update(scope_verified=metadata.get("scope_verified") is True,
                              widget=metadata.get("widget"), displayed_section=metadata.get("displayed_section"))
    try:
        timestamp(metadata["collected_at"])
    except SafetyError:
        return {**result, "reason": "Capture time is invalid."}
    if (metadata["geo"] != "IN" or metadata["time_range"] != "today 12-m"
            or metadata["property"] != "web" or metadata["kind"] != "search_term"
            or metadata.get("scope_verified") is not True or metadata.get("widget") != "related_queries"
            or metadata.get("displayed_section") != "Rising"):
        return {**result, "state": "Insufficient", "reason": "Verified same-term India/twelve-month/Web Search Rising-query scope is required."}
    try:
        text = raw.decode("utf-8-sig")
        if "\x00" in text or re.search(r"<!doctype|<html", text[:1024], re.I):
            raise ValueError("html_or_binary_download")
        # The export's explicit scope label must match. A requested URL alone
        # cannot identify the actual widget or country of its download.
        delimiters = (",", ";")
        rows = None
        for delimiter in delimiters:
            candidate = list(csv.reader(io.StringIO(text), delimiter=delimiter, strict=True))
            labels = [row[0].strip() for row in candidate if len(row) == 1
                      and re.search(r"\([^()]+\)$", row[0].strip())]
            if len(labels) == 1 and any(len(row) == 2 for row in candidate):
                scope = _scope_label(labels[0], metadata)
                if scope is not None:
                    result["metadata"].update(scope)
                    rows = candidate
                    break
        if rows is None:
            raise ValueError("export_label_or_columns_do_not_match_capture")
        section = None
        sections = set()
        seen = set()
        for row in rows:
            row = [cell.strip() for cell in row]
            if not row or not any(row):
                continue
            if len(row) == 1 and row[0].upper() in ("TOP", "RISING"):
                section = row[0].upper()
                if section in sections:
                    raise ValueError("duplicate_query_section")
                sections.add(section)
                continue
            if section is None:
                continue
            if len(row) != 2 or not row[0] or len(row[0]) > 512 or re.search(r"[\x00-\x1f\x7f]", row[0]):
                raise ValueError("malformed_query_row")
            if [cell.casefold() for cell in row] in (["query", "value"], ["query", "score"]):
                continue
            identity = (section, row[0].casefold())
            if identity in seen:
                raise ValueError("duplicate_query_row")
            seen.add(identity)
            if len(seen) > MAX_QUERIES:
                raise ValueError("query_limit_exceeded")
            value = {"query": row[0], "raw_value": row[1], "rising": False,
                     "growth_percent": None, "growth_kind": "unavailable"}
            if section == "TOP":
                # Retained only for audit; even 100 is not a rising signal.
                result["top_queries"].append(value)
                continue
            if row[1].casefold() == "breakout":
                value.update(rising=True, growth_kind="breakout")
            elif re.fullmatch(r"\+?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?%", row[1]):
                number = float(row[1].rstrip("%").replace(",", ""))
                if number > 1_000_000_000:
                    raise ValueError("growth_value_out_of_range")
                value.update(rising=number > 0, growth_percent=number, growth_kind="percentage")
            elif row[1] not in ("", "-", "—", "N/A"):
                raise ValueError("rising_growth_value_invalid")
            result["queries"].append(value)
        usable = any(row["rising"] for row in result["queries"])
        return {**result, "state": "Usable" if usable else "Insufficient",
                "reason": "Parsed Rising-query export; product relevance still requires explicit review."
                          if usable else "No usable positive Rising-query growth is available."}
    except (UnicodeError, csv.Error, ValueError, OverflowError) as error:
        return {**result, "state": "Failed", "queries": [], "top_queries": [],
                "reason": "Related-query CSV parse failed: " + str(error)[:160]}


def import_related_queries_evidence(memory, *, base_evidence_id, acquisition_id,
                                    csv_bytes, metadata, reviews, description_hash, reviewer):
    """Save actual exported growth plus named, explicit product-scope judgments.

    Reviews contain query, relationship (same_product/not_same_product/unclear),
    reason and reference equal to this receipt's source_reference. Replaying an
    exact saved acquisition returns its original binding, even after a later
    product edit; it never rebinds old evidence to the edited description.
    """
    from .research_flow import trends_description_current
    identifier(base_evidence_id); identifier(acquisition_id)
    state = memory.snapshot()
    base = state["trends_evidence"].get(base_evidence_id)
    if not base or base.get("evidence_kind") == "related_queries" or base.get("parsed", {}).get("state") != "Usable":
        raise SafetyError("RELATED_QUERIES_BASE_CHART_REQUIRED")
    bound_hash = base.get("description_hash")
    if not bound_hash or description_hash != bound_hash:
        raise SafetyError("RELATED_QUERIES_DESCRIPTION_MISMATCH")
    parsed = parse_related_queries_csv(csv_bytes, metadata)
    base_metadata = base["parsed"]["metadata"]
    if (any(parsed["metadata"].get(k) != base_metadata.get(k)
            for k in ("term", "geo", "time_range", "kind", "property"))
            or not parsed["metadata"].get("collected_at")):
        raise SafetyError("RELATED_QUERIES_SCOPE_MISMATCH")
    # Retain independently dated captures even when stale. Composition applies
    # each capture's existing seven-day freshness; importing never resets dates.
    timestamp(parsed["metadata"]["collected_at"])
    if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 120:
        raise SafetyError("RELATED_QUERIES_REVIEWER_REQUIRED")
    if not isinstance(reviews, list) or len(reviews) > 25:
        raise SafetyError("RELATED_QUERIES_REVIEWS_INVALID")
    actual_queries = {row["query"]: row for row in parsed["queries"]}
    reviewed = set()
    for review in reviews:
        if (not isinstance(review, dict) or set(review) != {"query", "relationship", "reason", "reference"}
                or not isinstance(review.get("query"), str)
                or review.get("query") not in actual_queries or review["query"] in reviewed
                or review.get("relationship") not in ("same_product", "not_same_product", "unclear")
                or not isinstance(review.get("reason"), str) or not review["reason"].strip() or len(review["reason"]) > 2000
                or review.get("reference") != parsed["metadata"]["source_reference"]):
            raise SafetyError("RELATED_QUERIES_REVIEWS_INVALID")
        reviewed.add(review["query"])
        actual_queries[review["query"]].update(same_product=review["relationship"] == "same_product",
                                               reference=review["reference"], review=deepcopy(review), reviewer=reviewer)
    value = {"concept_id": base["concept_id"], "acquisition_id": acquisition_id,
             "evidence_kind": "related_queries", "base_evidence_id": base_evidence_id,
             "base_parsed_hash": digest(base["parsed"]), "description_hash": bound_hash,
             "parsed": parsed, "reviewer": reviewer}
    evidence_id = key("trends-related", [base_evidence_id, acquisition_id])
    previous = state["trends_evidence"].get(evidence_id)
    if previous is not None:
        if previous != value:
            raise SafetyError("RELATED_QUERIES_CAPTURE_CONFLICT")
        return dict(id=evidence_id, **previous)
    if not trends_description_current(state, base["concept_id"], base):
        raise SafetyError("RELATED_QUERIES_PRODUCT_CHANGED")
    if state["concepts"][base["concept_id"]].get("status") in ("excluded", "retired", "merged"):
        raise SafetyError("RELATED_QUERIES_PRODUCT_INACTIVE")
    memory.put("trends_evidence", evidence_id, value)
    return dict(id=evidence_id, **value)


def current_related_support(base, evidence, as_of=None):
    """Newest fresh valid capture governs; failed downloads are not negatives.

    With no as_of, this returns the factual latest dependency for reservation
    signatures, which intentionally do not change just because time passes.
    """
    candidates = []
    for row in evidence:
        parsed = row.get("parsed", {})
        metadata = parsed.get("metadata", {})
        if (row.get("evidence_kind") == "related_queries" and row.get("base_evidence_id") == base["id"]
                and row.get("base_parsed_hash") == digest(base["parsed"])
                and row.get("description_hash") == base.get("description_hash")
                and parsed.get("state") != "Failed" and metadata.get("scope_verified") is True
                and metadata.get("widget") == "related_queries" and metadata.get("displayed_section") == "Rising"):
            at = metadata.get("collected_at")
            if at is not None and (as_of is None or timestamp(at) <= timestamp(as_of)
                                   and timestamp(as_of) - timestamp(at) <= timedelta(days=7)):
                candidates.append(row)
    return max(candidates, key=lambda row: (timestamp(row["parsed"]["metadata"]["collected_at"]), row["id"])) if candidates else None


def chart_with_related_support(base, evidence, as_of):
    """A derived view only: original chart bytes, arithmetic and time stay intact."""
    parsed = deepcopy(base["parsed"])
    support = current_related_support(base, evidence, as_of)
    if support is not None:
        parsed["rising_related_queries"] = (deepcopy(support["parsed"]["queries"])
                                            if support["parsed"]["state"] == "Usable" else [])
    return parsed
