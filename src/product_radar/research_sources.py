"""Two explicitly reviewed source contracts and pure, conservative mappings."""

from datetime import datetime, timezone
import math
import re
from urllib.parse import urlsplit, parse_qs

from .models import SafetyError, PreparedResearch, digest, identifier, timestamp, usd


SOURCES = {
    "amazon": {
        "actor": "amazon-scraper~amazon-bestsellers-scraper", "actor_id": "VeOVRxH4dtBxv1td1",
        "build": "0.1.10", "build_id": "RbzdpSITtmtqE1dhk", "cap": "0.08",
        "schema_hash": "e90a635213df61d90600209091d2d6f2032972a01bb0c12e52106e3cc5a86de3",
        "metadata_hash": "5596de720f1332aa0a7b2d941f4e69e5b06d3fe5752483eb556fdca323b5d3c3",
        "prices": {"apify-default-dataset-item": "0.002", "apify-actor-start": "0.00005"},
    },
    "trends": {
        "actor": "sourabhbgp~google-trends-scraper", "actor_id": "xeDvp8Y8h5CF6a7y2",
        "build": "0.0.14", "build_id": "c69GkRZWfzoTTwJmt", "cap": "0.01",
        "schema_hash": "132bbdd9a0b92faa1db7a50c9007ccdf55a32651fe0b461abc9bcf26f9b57c02",
        "metadata_hash": "727245e3ea434319a267f23ee1759a06957d6c133c4803c3aa8265c2065a462d",
        "prices": {"trending-result": "0.001", "explore-result": "0.002", "result": "0.002"},
    },
}
AMAZON_CASES = {category + "-" + kind: "https://www.amazon.in/gp/" + kind + "/" + category
                for category in ("kitchen", "baby") for kind in ("bestsellers", "movers-and-shakers")}
TREND_TERMS = ("sink organiser", "packing cubes", "napkin holder")
TREND_INPUT = {"mode": "explore", "keywords": ", ".join(TREND_TERMS), "geo": "IN",
               "timeRange": "today 12-m", "category": "0", "googleProperty": "",
               "language": "en-US", "timezone": "UTC", "exploreConcurrency": 1}


def source_spec(actor):
    for name, spec in SOURCES.items():
        if actor == spec["actor"]:
            return name, spec
    raise SafetyError("UNREVIEWED_ACTOR_BUILD")


def validate_request(request):
    identifier(request.attempt_id)
    name, spec = source_spec(request.actor)
    timestamp(request.reviewed_at)
    if (request.build != spec["build"] or request.expected_build_id != spec["build_id"]
            or request.public_metadata_hash != spec["metadata_hash"]):
        raise SafetyError("UNREVIEWED_ACTOR_BUILD")
    if request.reserved_usd != spec["cap"]:
        raise SafetyError("UNREVIEWED_ATTEMPT_CAP")
    expected = {"build": request.build, "timeout": 300, "maxTotalChargeUsd": request.reserved_usd, "restartOnError": "false"}
    if request.options != expected or type(request.options.get("timeout")) is not int:
        raise SafetyError("UNSAFE_LAUNCH_OPTIONS")
    body = request.input
    if name == "amazon":
        if (not isinstance(body, dict) or set(body) != {"urls", "subcategoryLevel", "nextPage", "retries"}
                or body["urls"] not in [[url] for url in AMAZON_CASES.values()]
                or type(body["subcategoryLevel"]) is not int or body["subcategoryLevel"] != 0
                or body["nextPage"] is not False or type(body["retries"]) is not int or body["retries"] != 3
                or request.purpose != body["urls"][0].split("/")[4]):
            raise SafetyError("UNREVIEWED_INPUT")
    elif (body != TREND_INPUT or type(body.get("exploreConcurrency")) is not int or request.purpose != "explore"):
        raise SafetyError("UNREVIEWED_INPUT")


def prepare_source(source, case, attempt_id, reviewed_at):
    if source not in SOURCES:
        raise SafetyError("UNREVIEWED_SOURCE")
    spec = SOURCES[source]
    if source == "amazon":
        if case not in AMAZON_CASES:
            raise SafetyError("AMAZON_CASE_REQUIRED")
        body = {"urls": [AMAZON_CASES[case]], "subcategoryLevel": 0, "nextPage": False, "retries": 3}
        purpose = body["urls"][0].split("/")[4]
    else:
        if case is not None:
            raise SafetyError("TRENDS_CASE_NOT_ALLOWED")
        body, purpose = dict(TREND_INPUT), "explore"
    request = PreparedResearch(attempt_id, spec["actor"], spec["build"], spec["build_id"], purpose, body,
        {"build": spec["build"], "timeout": 300, "maxTotalChargeUsd": spec["cap"], "restartOnError": "false"},
        spec["cap"], reviewed_at, spec["metadata_hash"])
    return {"schema_version": 1, "execution_mode": "offline_preparation", "request": request.to_dict(),
            "config_hash": request.config_hash, "remote_attempt_created": False, "credits_used": False,
            "ready_for_live": False, "source": source, "case": case}


def checked_source_pricing(actor, build, prepared, now=None):
    _, spec = source_spec(prepared.actor)
    try:
        data, definition = actor["data"], build["data"]
        if (data["id"] != spec["actor_id"] or data.get("isDeprecated") is True
                or data["actorPermissionLevel"] != "LIMITED_PERMISSIONS"
                or data.get("isPPEPlatformUsagePaidByUser") is True
                or definition["id"] != spec["build_id"] or definition["buildNumber"] != spec["build"]
                or definition["status"] != "SUCCEEDED"
                or digest(definition["actorDefinition"]["input"]) != spec["schema_hash"]):
            raise ValueError
        instant = now or datetime.now(timezone.utc)
        active = [p for p in data["pricingInfos"] if timestamp(p["startedAt"]) <= instant]
        price = max(active, key=lambda p: timestamp(p["startedAt"]))
        if (price["pricingModel"] != "PAY_PER_EVENT"
                or usd(str(price.get("minimalMaxTotalChargeUsd") or "0")) > usd(prepared.reserved_usd)):
            raise ValueError
        events = price["pricingPerEvent"]["actorChargeEvents"]
        if set(events) != set(spec["prices"]):
            raise ValueError
        for key, expected in spec["prices"].items():
            event = events[key]
            value = (event["eventTieredPricingUsd"]["FREE"]["tieredEventPriceUsd"]
                     if "eventTieredPricingUsd" in event else event["eventPriceUsd"])
            if usd(str(value)) != usd(expected):
                raise ValueError
        properties = definition["actorDefinition"]["input"]["properties"]
        return {"pricing_model": "PAY_PER_EVENT", "event_prices": spec["prices"], "plan": "Free",
                "input_defaults": {k: v["default"] for k, v in properties.items() if "default" in v},
                "build_id": spec["build_id"], "build": spec["build"]}
    except (KeyError, TypeError, ValueError, SafetyError):
        raise SafetyError("PUBLIC_METADATA_CHANGED_REVIEW_REQUIRED") from None


def public_url(value, host):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme == "https" and parsed.netloc == host else None
    except ValueError:
        return None


def amazon_scope(value):
    if not public_url(value, "www.amazon.in"):
        return None
    path = urlsplit(value).path
    match = re.match(r"^/gp/(bestsellers|movers-and-shakers)/(kitchen|baby)(?:/ref=[^/]*)?/?$", path)
    if match:
        return match.groups()
    match = re.match(r"^/(?:[^/]+/)?zgbs/(kitchen|baby)(?:/ref=[^/]*)?/?$", path)
    return ("bestsellers", match[1]) if match else None


def map_research_rows(rows, prepared):
    if not isinstance(rows, list) or len(rows) > 1000:
        raise SafetyError("INVALID_MAPPING_CONTEXT")
    source, _ = source_spec(prepared.actor)
    records, rejected, seen, conflicts = [], [], {}, set()
    for index, row in enumerate(rows):
        reference = {"query": prepared.attempt_id, "row": index}
        if not isinstance(row, dict):
            rejected.append({"reference": reference, "reason": "not_an_object"})
            continue
        result = map_amazon_row(row, prepared) if source == "amazon" else map_trends_row(row, prepared)
        if isinstance(result, str):
            rejected.append({"reference": reference, "reason": result})
            continue
        key = result["identity"]
        if key in seen:
            # Duplicate rows remain traceable; conflicting identities are quarantined.
            if result != seen[key][0]:
                conflicts.add(key)
            seen[key][1].append(reference)
        else:
            seen[key] = (result, [reference])
    for key, (result, references) in seen.items():
        if key in conflicts:
            rejected.extend({"reference": ref, "reason": "conflicting_duplicate_identity"} for ref in references)
        else:
            result["raw_references"] = references
            records.append(result)
    missing = [term for term in TREND_TERMS if term not in {r["identity"] for r in records}] if source == "trends" else []
    return {"schema_version": 1, "source": source, "purpose": prepared.purpose,
            "records": records, "rejected": rejected, "missing_keywords": missing,
            "status": "partial" if rejected or missing or not records or any(r["defects"] for r in records) else "usable",
            "empty_scope_observation": not rows, "production_assessment": False}


def map_amazon_row(row, prepared):
    asin, title, rank = row.get("asin"), row.get("title"), row.get("position")
    if not isinstance(asin, str) or not re.fullmatch(r"[A-Z0-9]{10}", asin):
        return "missing_or_invalid_asin"
    if not isinstance(title, str) or not title.strip() or type(rank) is not int or rank < 1:
        return "missing_or_invalid_title_or_position"
    defects = []
    link = public_url(row.get("link"), "www.amazon.in")
    linked = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", link or "")
    if linked is None or linked[1] != asin:
        defects.append("product_url_identity_or_country_unverified")
    requested = prepared.input["urls"][0]
    if amazon_scope(row.get("categoryUrl")) != amazon_scope(requested):
        defects.append("list_scope_mismatch_or_unverified")
    if row.get("status") != 200:
        defects.append("source_status_unverified")
    price = row.get("price")
    # Preserve display values; no currency/pack/discount inference or missing->zero.
    if price is not None and (isinstance(price, bool) or not isinstance(price, (str, int, float))):
        defects.append("price_type_unverified")
        price = None
    if isinstance(price, str) and not price.strip():
        price = None
    if type(price) in (int, float) and (not math.isfinite(price) or price < 0):
        defects.append("price_value_invalid")
        price = None
    if price is not None and row.get("currency") != "INR":
        defects.append("price_currency_unverified")
    return {"identity": asin, "title": title.strip()[:4000], "position": rank,
            "position_kind": "list_position_not_unit_sales", "list_type": prepared.purpose,
            "requested_list_url": requested, "observed_list_url": row.get("categoryUrl"),
            "product_url": link, "category": row.get("category"),
            "price_observation": {"displayed": price, "alternate_displayed": row.get("price2"),
                "currency": row.get("currency"), "basis": "retail_listing", "pack_basis": "not_verified",
                "tax_shipping": "not_verified", "source_url": link},
            "defects": defects, "limitations": ["listing_not_supplier_quote", "list_not_market_coverage"]}


def map_trends_row(row, prepared):
    term = row.get("keyword")
    if term not in TREND_TERMS:
        return "unrequested_or_missing_keyword"
    defects, limitations = [], []
    if (row.get("geo") != "IN" or row.get("timeRange") != "today 12-m"
            or row.get("category") not in ("0", "All categories")
            or row.get("googleProperty") not in ("", "Web Search")):
        defects.append("scope_mismatch_or_unverified")
    url = public_url(row.get("trendsUrl"), "trends.google.com")
    query = parse_qs(urlsplit(url).query) if url else {}
    if query.get("q") != [term] or query.get("geo") != ["IN"] or query.get("date") != ["today 12-m"]:
        defects.append("trends_url_scope_unverified")
    series = row.get("interestOverTime")
    points, dates = [], []
    if not isinstance(series, list) or len(series) > 1000:
        defects.append("series_missing_or_invalid")
        series = []
    for position, point in enumerate(series):
        if not isinstance(point, dict):
            defects.append("invalid_interval")
            continue
        stamp, value, partial = point.get("date"), point.get("value"), point.get("isPartial")
        valid_date = isinstance(stamp, str) and re.fullmatch(r"[0-9]{9,10}", stamp) and 0 < int(stamp) < 4102444800
        valid_value = type(value) in (int, float) and 0 <= value <= 100
        if not valid_date or not valid_value or (partial is not None and type(partial) is not bool):
            defects.append("invalid_interval")
        if valid_date:
            dates.append(int(stamp))
        points.append({"raw_interval": position, "timestamp": stamp if valid_date else None,
                       "value": value if valid_value else None, "formatted_date": point.get("formattedDate"),
                       "is_partial": partial if type(partial) is bool else None})
    if len(dates) != len(set(dates)) or dates != sorted(dates):
        defects.append("duplicate_or_unordered_intervals")
    requested_at = timestamp(prepared.reviewed_at).timestamp()
    if dates and (min(dates) < requested_at - 380 * 86400 or max(dates) > requested_at + 86400):
        defects.append("interval_dates_outside_requested_year")
    if dates and max(dates) < requested_at - 14 * 86400:
        defects.append("series_endpoint_stale_or_incomplete")
    spacing = sorted({b-a for a, b in zip(dates, dates[1:])})
    if len(spacing) > 1:
        limitations.append("irregular_intervals")
    if any(p["is_partial"] is None for p in points):
        limitations.append("partial_interval_flags_unavailable")
    state = "unavailable" if not points else "all_zero_low_volume_or_no_measurable_interest" if all(p["value"] == 0 for p in points) else "observed"
    if not points:
        limitations.append("empty_series_not_zero_demand")
    return {"identity": term, "keyword": term, "requested_geo": "IN", "observed_geo": row.get("geo"),
            "time_range": row.get("timeRange"), "trends_url": url, "series": points,
            "interval_seconds_observed": spacing, "series_state": state,
            "scale": "relative_0_100_not_search_count", "cross_keyword_magnitude_comparable": False,
            "defects": sorted(set(defects)), "limitations": limitations}
