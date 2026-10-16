"""Display facts for the report's charts, derived only from frozen, checked facts.

Nothing here collects, scores or judges. For a current (functional) packet the
charts are drawn from the exact originals the opportunity score itself read,
which the packet already freezes and ``validate_frozen_functional_market``
re-checks: the Google Trends chart, the Meta ads behind the advertiser counts,
the quick-commerce listings and the dropshipping-platform presence record.
The one fact the frozen market proof does not hold, which quick-commerce
places were asked for the type, is frozen with the selection as
``quick_commerce_checks`` (owner choice 4, 16 October 2026). Packets without
those originals fall back to the score's own sub-point facts (text only).
"""

from datetime import date, timedelta
from decimal import Decimal
import re

from .models import SafetyError, timestamp

QC_APPS = ("blinkit", "zepto", "instamart")
PLATFORMS = ("deodap", "baapstore", "dropdash")
ROLE_ORDER = ("bestseller", "trending", "high_demand", "winning", "state_bestseller", "new_arrival")
SCORED_LABELS = ("sweet_spot", "early_bet", "proven_but_crowded")
SCORED_LEVELS = ("low", "medium", "high")
QC_CHECKS_VERSION = "quick-commerce-checks-v1"
QC_FRESH_DAYS = 7  # the score reads quick-commerce listings seen in the last seven days


# Score ------------------------------------------------------------------------

def score_view(product):
    """The code-owned score facts for a scored product, or None for a packet
    format without the opportunity score. A scored record that could not have
    been admitted (too quiet, crowding not checked) fails closed."""
    from .opportunity_score import PARTS, POLICY_ID
    if "opportunity_policy_id" not in product:
        return None
    try:
        basis = product["opportunity_basis"]
        parts = basis["parts"]
        level = parts["room"]["items"]["crowding"]["facts"]["level"]
        if (product["opportunity_policy_id"] != POLICY_ID or basis.get("reason") != "opportunity_scored"
                or basis.get("eligible") is not True or product["opportunity_label"] not in SCORED_LABELS
                or level not in SCORED_LEVELS or set(parts) != set(PARTS)
                or product["opportunity_score"] != sum(parts[part]["points"] for part in PARTS)):
            raise SafetyError("LIVE_REPORT_SCORE_INVALID")
        return {"score": product["opportunity_score"], "label": product["opportunity_label"], "level": level,
                "points": {part: parts[part]["points"] for part in PARTS},
                "maxima": {part: parts[part]["max"] for part in PARTS},
                "items": {part: {name: row["points"] for name, row in parts[part]["items"].items()} for part in PARTS},
                "facts": {name: row["facts"] for part in PARTS for name, row in parts[part]["items"].items()}}
    except (KeyError, TypeError):
        raise SafetyError("LIVE_REPORT_SCORE_INVALID") from None


# The frozen market originals ----------------------------------------------------

def market_context(product):
    """The market signals recomputed from the packet's frozen originals (the
    same computation ``validate_frozen_functional_market`` checks), or None."""
    proof = product.get("functional_market_proof")
    if not isinstance(proof, dict):
        return None
    from .discovery import compute_market_signals, latest_observations
    from .frozen_records import read_original_json
    records = {table: {rid: read_original_json(value) for rid, value in values.items()}
               for table, values in proof["records"].items()}
    matches = proof["matches"]
    observations = {ref: records["observations"][ref] for ref in matches if ref in records["observations"]}
    relevance = {value["semantic_id"]: matches[ref]["relationship"] for ref, value in observations.items()}
    charts = [dict(id=ref, **records["trends_evidence"][ref]) for ref in matches if ref in records["trends_evidence"]]
    as_of = proof["as_of"]
    market = compute_market_signals(observations, relevance, as_of, charts)
    relevant = {ref: value for ref, value in latest_observations(observations, as_of).items()
                if relevance.get(value["semantic_id"]) in ("exact_product", "relevant_family")}
    presence = None
    frozen_presence = proof["score_facts"]["presence"]
    if frozen_presence is not None:
        presence = read_original_json(frozen_presence["record"])
    return {"as_of": as_of, "market": market, "relevant": relevant, "charts": charts, "presence": presence}


# Meta ---------------------------------------------------------------------------

def meta_view(score, context):
    """Advertiser and ad counts from the score; the timeline's start dates from the frozen ads."""
    ads_facts = score["facts"]["facebook_ads"]
    recent = score["facts"]["new_advertisers"]["recent_start_advertisers"]
    view = {"advertisers": ads_facts["advertisers"], "relevant": ads_facts["relevant_ads"], "active": None,
            "recent": recent, "starts": None, "end": None, "undated": 0, "observed": None, "unlinked": False}
    if context is None:
        return view
    from .meta_facts import advertiser_firsts, relevant_ads
    meta = [(ref, value) for ref, value in context["relevant"].items() if value["source"] == "meta"]
    ads, conflicts = relevant_ads(meta)
    valid = [value for ref, value in ads.items() if ref not in conflicts]
    depth = context["market"]["sources"]["meta"]["depth"]
    if (sorted(value["id"] for value in valid) != context["market"]["sources"]["meta"]["used_ids"]
            or len(valid) != view["relevant"]):
        raise SafetyError("LIVE_REPORT_VISUAL_FACTS_CHANGED")
    firsts = advertiser_firsts(valid)
    if (sum(row["recent"] for row in firsts) if firsts else None) != recent:
        raise SafetyError("LIVE_REPORT_VISUAL_FACTS_CHANGED")
    view.update(active=depth["active_ads"] if valid else None, undated=depth["ads_without_start_date"],
                starts=[(timestamp(row["started_at"]).date(), row["recent"]) for row in firsts],
                unlinked=bool(valid) and view["advertisers"] is not None and len(firsts) < view["advertisers"]
                and depth["ads_without_start_date"] == 0)
    if valid:
        observed = max(timestamp(value["observed_at"]) for value in valid)
        view["observed"] = observed.date()
        view["end"] = observed.date()
    return view


# Google Trends -------------------------------------------------------------------

def _settings():
    from .opportunity_score import load_settings
    return load_settings()


def _mean_text(low, high):
    def number(value):
        return format(Decimal(repr(value)).quantize(Decimal("0.01")).normalize(), "f")
    return number(low) if low == high else number(low) + "–" + number(high)


def trends_view(score, context):
    """The rising state from the score; the weekly series and the two compared windows from the frozen chart."""
    facts, rising = score["facts"]["google_searches"], score["facts"]["rising_searches"]
    points = score["items"]["early"]["rising_searches"]
    if not facts["usable"]:
        state = "none"
    elif not rising["rising"]:
        state = "steady"
    else:
        state = "rising_strongly" if points >= _settings()["early"]["rising_searches"]["strong"] else "rising"
    view = {"state": state, "points": None, "windows": None, "previous": None, "recent": None, "collected": None}
    if context is None or state == "none":
        return view
    from .trends import compute_trends_signal
    from .trends_related import chart_with_related_support
    used = context["market"]["sources"]["google-trends"]["used_ids"]
    chart = next((row for row in context["charts"] if used and row["id"] == used[0]), None)
    if chart is None:
        raise SafetyError("LIVE_REPORT_VISUAL_FACTS_CHANGED")
    parsed = chart_with_related_support(chart, context["charts"], context["as_of"])
    signal = compute_trends_signal(parsed, now=context["as_of"])
    from .report_charts import spark_window_indexes
    series = [{"start": point["start"], "state": point["state"], "value": point["value"]} for point in parsed["series"]]
    view.update(points=series, windows=spark_window_indexes(series, signal.get("comparison_intervals")),
                collected=timestamp(parsed["metadata"]["collected_at"]).date())
    if signal.get("previous_mean_bounds") and signal.get("recent_mean_bounds"):
        view["previous"] = _mean_text(*signal["previous_mean_bounds"])
        view["recent"] = _mean_text(*signal["recent_mean_bounds"])
    return view


# Dropshipping platforms -----------------------------------------------------------

_PLATFORM_STATES = {"relevant": "listed", "relevant_delisted": "delisted", "review_pending": "pending",
                    "no_relevant_items": "not_found", "no_relevant_items_in_sample": "not_found", "not_checked": "not_checked"}


def platform_view(score, context):
    """Per platform: listed / not found / not checked, the list roles and the lowest
    dropship price, from the presence record the score read (a record the score
    found older than a week counts as not checked, as in the score)."""
    listed = score["facts"]["platform_lists"]["listed"]
    view = {"rows": [], "checked": None, "stale": False, "basis_only": context is None}
    if context is None:
        for route in PLATFORMS:
            view["rows"].append({"route": route, "state": "listed" if route in listed else "unknown", "roles": [], "price": None})
        return view
    record = context["presence"]
    from .opportunity_score import presence_input
    current = presence_input(record, as_of=context["as_of"]) if record is not None else None
    if record is not None and current is None:
        view["stale"] = True
    for route in PLATFORMS:
        row = {"route": route, "state": "not_checked", "roles": [], "price": None}
        platform = record["platforms"].get(route) if current is not None else None
        if platform is not None:
            # "Listed" is exactly what the score counted; any other state is the record's own.
            row["state"] = ("listed" if route in listed else
                            "delisted" if platform["state"] == "relevant" else _PLATFORM_STATES.get(platform["state"], "not_checked"))
            items = [item for item in platform["items"] if item["listed"]]
            roles = {role for item in items for role in item["roles"]}
            row["roles"] = [role for role in ROLE_ORDER if role in roles]
            prices = [Decimal(item["price"]) for item in items if isinstance(item.get("price"), str) and item["price"]]
            if row["state"] == "listed" and prices:
                row["price"] = format(min(prices).normalize(), "f")
        if route in listed and row["state"] != "listed":
            raise SafetyError("LIVE_REPORT_VISUAL_FACTS_CHANGED")
        view["rows"].append(row)
    if current is not None:
        view["checked"] = timestamp(record["at"]).date()
    return view


# Quick commerce ----------------------------------------------------------------------

def quick_commerce_checks(state, concept_id, *, as_of):
    """Which places each quick-commerce app was asked for this type within the
    score's seven-day window (answered questions only), with the configured places."""
    from .quickcommerce_plan import answered_at, load_settings
    places = [place["name"] for place in load_settings()["places"]]
    now = timestamp(as_of)
    answered = {}
    for task in state.get("query_tasks", {}).values():
        if task.get("route") not in QC_APPS or concept_id not in task.get("targets", {}):
            continue
        at = answered_at(state, task)
        place = task.get("request", {}).get("location", {}).get("name")
        if at is None or place not in places or not timedelta(0) <= now - timestamp(at) <= timedelta(days=QC_FRESH_DAYS):
            continue
        app = answered.setdefault(task["route"], {})
        if place not in app or timestamp(at) > timestamp(app[place]):
            app[place] = at
    return {"version": QC_CHECKS_VERSION, "as_of": as_of, "places": places,
            "answered": {app: dict(sorted(answered[app].items())) for app in QC_APPS if app in answered}}


def validate_quick_commerce_checks(value):
    try:
        if (not isinstance(value, dict) or set(value) != {"version", "as_of", "places", "answered"}
                or value["version"] != QC_CHECKS_VERSION or not isinstance(value["places"], list)
                or not 1 <= len(value["places"]) <= 5 or len(set(value["places"])) != len(value["places"])
                or any(not isinstance(place, str) or not place.strip() for place in value["places"])
                or not isinstance(value["answered"], dict) or not set(value["answered"]) <= set(QC_APPS)
                or list(value["answered"]) != [app for app in QC_APPS if app in value["answered"]]):
            raise SafetyError("LIVE_QUICK_COMMERCE_CHECKS_INVALID")
        now = timestamp(value["as_of"])
        for places in value["answered"].values():
            if not isinstance(places, dict) or not places or list(places) != sorted(places):
                raise SafetyError("LIVE_QUICK_COMMERCE_CHECKS_INVALID")
            for place, at in places.items():
                if place not in value["places"] or not timedelta(0) <= now - timestamp(at) <= timedelta(days=QC_FRESH_DAYS):
                    raise SafetyError("LIVE_QUICK_COMMERCE_CHECKS_INVALID")
    except (TypeError, AttributeError, SafetyError):
        raise SafetyError("LIVE_QUICK_COMMERCE_CHECKS_INVALID") from None
    return value


def qc_view(score, context, checks):
    """App x place matrix: in stock / not in stock or not listed / not checked. A place
    with a seen listing is checked; one whose question was answered with nothing
    relevant is 'not in stock or not listed'; anything else is 'not checked'."""
    stock = score["facts"]["quick_commerce"]["stock_cities"]
    view = {"places": None, "rows": None, "in_stock": sorted(stock), "checked": None, "observed": None}
    if context is None or checks is None:
        return view
    if checks["as_of"] != context["as_of"]:
        raise SafetyError("LIVE_REPORT_VISUAL_FACTS_CHANGED")
    from .marketplace_browser import verified_list_observation
    seen = {}
    dates = []
    for value in context["relevant"].values():
        location = value["facts"].get("location") if isinstance(value.get("facts"), dict) else None
        name = location.get("name") if isinstance(location, dict) else None
        # The same listings the score's quick-commerce rows read (discovery.compute_market_signals).
        if (value["source"] not in QC_APPS or not isinstance(name, str) or not name.strip(" \t\r\n\f\v")
                or not (value["facts"].get("list_type") == "quickcommerce_context" or verified_list_observation(value))):
            continue
        cell = seen.setdefault((value["source"], name), set())
        cell.add(value["facts"].get("available") is True)
        dates.append(timestamp(value["observed_at"]))
    places = list(checks["places"]) + sorted({name for _, name in seen} - set(checks["places"]))
    rows = {}
    for app in QC_APPS:
        cells = []
        for place in places:
            if True in seen.get((app, place), set()):
                cells.append("in_stock")
            elif (app, place) in seen or place in checks["answered"].get(app, {}):
                cells.append("not_in_stock")
            else:
                cells.append("not_checked")
        rows[app] = cells
    in_stock = sorted({place for (app, place), states in seen.items() if True in states})
    if in_stock != sorted(stock):
        raise SafetyError("LIVE_REPORT_VISUAL_FACTS_CHANGED")
    checked = [place for index, place in enumerate(places) if any(rows[app][index] != "not_checked" for app in QC_APPS)]
    answered = [timestamp(at) for places_at in checks["answered"].values() for at in places_at.values()]
    view.update(places=places, rows=rows, checked=checked,
                observed=max(dates + answered).date() if dates or answered else None)
    return view


# Compliance ----------------------------------------------------------------------------

# A fixed, code-owned rule by product class (owner choice 5a). The class comes
# from the category (Baby Products) and from fixed words in the accepted
# product-type wording; the AI's free-text compliance note stays in the workbook.
COMPLIANCE_RULE = "compliance-classes-v1"
COMPLIANCE_CLASSES = ("baby", "ingestible", "topical", "medical", "electrical")
_WORDS = {
    "baby": r"bab(?:y|ies)|infants?|toddlers?|newborns?|kids?|child(?:ren)?|teethers?|teething|pacifiers?|soothers?|diapers?|nappies|nappy",
    "ingestible": r"supplements?|vitamins?|snacks?|cand(?:y|ies)|chocolates?|protein powders?|herbal|nutrition(?:al)?|health drinks?",
    "topical": (r"creams?|lotions?|serums?|ointments?|balms?|cosmetics?|make-?up|shampoos?|hair conditioners?|sunscreens?"
                r"|moisturi[sz]ers?|perfumes?|fragrances?|deodorants?|hair oils?|body oils?|massage oils?|face masks?|scrubs?"
                r"|soaps?(?!\s*(?:dispensers?|dish(?:es)?|holders?|trays?|stands?|savers?|boxe?s?|cases?|racks?))"),
    "medical": (r"medical|medicines?|therap(?:y|eutic)|orthop(?:a)?edic|first[- ]aid|thermometers?|oximeters?|blood pressure"
                r"|nebuli[sz]ers?|(?:knee|back|wrist|ankle|neck) braces?|posture correctors?|pain relief|wounds?|bandages?"
                r"|inhalers?|hearing aids?"),
    "electrical": (r"electric(?:al)?|electronic|battery|batteries|battery-(?:powered|operated)|rechargeable|usb|chargers?|charging"
                   r"|cordless|plug-?in|mains|led|lamps?|lights?|bulbs?|motori[sz]ed|motors?|heaters?|heating|heated|solar"
                   r"|power banks?|adapters?|massagers?"),
}
_PATTERNS = {name: re.compile(r"\b(?:" + words + r")\b", re.I) for name, words in _WORDS.items()}


def _type_wording(product):
    words = [product.get("title")]
    definition = product.get("functional_definition")
    if isinstance(definition, dict):
        for field in ("report_title", "primary_use", "mechanism", "use_context"):
            value = definition.get(field)
            words.extend(value if isinstance(value, list) else [value])
    return " ".join(word for word in words if isinstance(word, str))


def compliance_classes(product):
    """The product classes that need the client's own compliance check (fixed order)."""
    text = _type_wording(product)
    found = [name for name in COMPLIANCE_CLASSES if _PATTERNS[name].search(text)]
    if product.get("category") == "Baby Products" and "baby" not in found:
        found.insert(0, "baby")
    return found


def compliance_text(classes, language):
    from .report_copy import TEXT
    words = TEXT[language]
    return " ".join([words["compliance"][name] for name in classes] + [words["compliance_tail"]])


def iso_day(value):
    return value.isoformat() if isinstance(value, date) else None
