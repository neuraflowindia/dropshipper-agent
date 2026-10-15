"""The opportunity score out of 100 (owner's weights, 15 October 2026; Plan R10 W6/W7).

Four parts, measured by code from saved facts only (no model opinion):

* Do people want it? (20): Facebook ads, Google searches, the dropshipping
  platforms' bestseller / trending / high-demand lists, quick-commerce stock.
* Is it early? (30): rising searches, newly started advertisers, the
  platforms' new-arrival lists.
* Room for us? (25): the Amazon/Flipkart crowding level (``crowding``).
* Good product? (25): Meets on the ten-point checklist.

The owner set the four part weights (they sum to 100); the inner points live
in ``config/research/opportunity_score.json`` (versioned). Amazon and Flipkart
give no want points; they only decide room. A type can go into a report only
with at least 5 Meets, 5 want points and a known crowding level. Labels: Too
quiet (under 5 want points), Proven but crowded (High crowding), Early bet
(Low/Medium, early 15+ and want under 10), Sweet spot (every other Low/Medium
type). The record keeps every sub-point with the facts and evidence ids behind
it; ``validate_opportunity`` recomputes the arithmetic and the label from it.
"""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re

from .fit_policy import FIT_POLICY_ID
from .models import SafetyError, digest, identifier, timestamp

POLICY_ID = "opportunity-score-v3"
OPPORTUNITY_POLICY_IDS = (POLICY_ID,)
SETTINGS = Path(__file__).resolve().parents[2] / "config/research/opportunity_score.json"
OPPORTUNITY_FIELDS = ("opportunity_policy_id", "opportunity_score", "opportunity_label", "opportunity_basis")
PARTS = ("want", "early", "room", "product")
ITEMS = {"want": ("facebook_ads", "google_searches", "platform_lists", "quick_commerce"),
         "early": ("rising_searches", "new_advertisers", "new_arrivals"),
         "room": ("crowding",), "product": ("checklist",)}
LABELS = ("too_quiet", "proven_but_crowded", "early_bet", "sweet_spot")
LABEL_TEXT = {"en": {"too_quiet": "Too quiet", "proven_but_crowded": "Proven but crowded",
                     "early_bet": "Early bet", "sweet_spot": "Sweet spot"},
              "hi-Latn": {"too_quiet": "Abhi bahut shaant", "proven_but_crowded": "Bikta hai, par bheed zyada",
                          "early_bet": "Jaldi ka daav", "sweet_spot": "Sahi mauka"}}
REASONS = ("fit_information_pending", "crowding_pending", "demand_too_quiet", "opportunity_scored")
CROWDING_LEVELS = ("low", "medium", "high", "not_checked")
QUICK_COMMERCE = ("blinkit", "zepto", "instamart")
# Sub-points measured from market sources: their evidence is cited in the report.
MARKET_ITEMS = {"facebook_ads": "meta", "new_advertisers": "meta", "google_searches": "google-trends",
                "rising_searches": "google-trends", "quick_commerce": None}
PRESENCE_DAYS = 7  # a platform presence fact is renewed weekly (platform_roles)
CALIBRATION_KIND = "opportunity-score-calibration-v2"
SUMMARY_KIND = "opportunity-score-summary-v1"
_CACHE = {}


def _int(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise SafetyError("OPPORTUNITY_SETTINGS_INVALID")
    return value


def _share(value):
    try:
        number = Decimal(value) if isinstance(value, str) else None
    except InvalidOperation:
        number = None
    if number is None or not number.is_finite() or not 0 <= number <= 1:
        raise SafetyError("OPPORTUNITY_SETTINGS_INVALID")
    return number


def _ladder(value, maximum):
    """[[minimum count, points], ...] highest first; returns its top points."""
    if (not isinstance(value, list) or not value
            or any(not isinstance(row, list) or len(row) != 2 for row in value)):
        raise SafetyError("OPPORTUNITY_SETTINGS_INVALID")
    minimums = [_int(row[0], 1, 1000) for row in value]
    points = [_int(row[1], 1, maximum) for row in value]
    if minimums != sorted(set(minimums), reverse=True) or points != sorted(set(points), reverse=True):
        raise SafetyError("OPPORTUNITY_SETTINGS_INVALID")
    return points[0]


def _keys(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise SafetyError("OPPORTUNITY_SETTINGS_INVALID")
    return value


def validate_settings(value):
    """Every number is checked; each part's inner maxima add up to its weight."""
    try:
        _keys(value, ("schema_version", "kind", "version", "note", "parts", "want", "early", "room", "product",
                      "eligibility", "labels", "selection", "calibration"))
        if (value["schema_version"] != 1 or value["kind"] != "opportunity-score-settings" or not isinstance(value["note"], str)
                or not isinstance(value["version"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value["version"])):
            raise SafetyError("OPPORTUNITY_SETTINGS_INVALID")
        parts = _keys(value["parts"], PARTS)
        if sum(_int(parts[name], 1, 100) for name in PARTS) != 100:
            raise SafetyError("OPPORTUNITY_SETTINGS_INVALID")  # the owner's four weights total 100
        want = _keys(value["want"], ITEMS["want"])
        ads = _keys(want["facebook_ads"], ("positive_with_advertisers", "positive", "relevant_ads"))
        many = _keys(ads["positive_with_advertisers"], ("min_advertisers", "points"))
        _int(many["min_advertisers"], 1, 1000)
        if not _int(many["points"], 1, 100) > _int(ads["positive"], 1, 100) > _int(ads["relevant_ads"], 1, 100):
            raise SafetyError("OPPORTUNITY_SETTINGS_INVALID")
        searches = _ladder(_keys(want["google_searches"], ("nonzero_points",))["nonzero_points"], 100)
        lists = _keys(want["platform_lists"], ("on_list_platforms", "listed_platforms"))
        platforms = max(_ladder(lists["on_list_platforms"], 100), _ladder(lists["listed_platforms"], 100))
        stock = _ladder(_keys(want["quick_commerce"], ("stock_cities",))["stock_cities"], 100)
        if many["points"] + searches + platforms + stock != parts["want"]:
            raise SafetyError("OPPORTUNITY_SETTINGS_INVALID")
        early = _keys(value["early"], ITEMS["early"])
        rising = _keys(early["rising_searches"], ("rising", "strong", "strong_rise_percent"))
        _int(rising["strong_rise_percent"], 1, 1000)
        if not _int(rising["strong"], 1, 100) > _int(rising["rising"], 1, 100):
            raise SafetyError("OPPORTUNITY_SETTINGS_INVALID")
        advertisers = _ladder(_keys(early["new_advertisers"], ("recent_start_advertisers",))["recent_start_advertisers"], 100)
        arrivals = _ladder(_keys(early["new_arrivals"], ("new_arrival_platforms",))["new_arrival_platforms"], 100)
        if rising["strong"] + advertisers + arrivals != parts["early"]:
            raise SafetyError("OPPORTUNITY_SETTINGS_INVALID")
        room = _keys(value["room"], ("low", "medium", "high"))
        if not parts["room"] == _int(room["low"], 0, 100) >= _int(room["medium"], 0, 100) >= _int(room["high"], 0, 100):
            raise SafetyError("OPPORTUNITY_SETTINGS_INVALID")
        product = _keys(value["product"], ("base_meets", "base_points", "per_extra_meet"))
        base = _int(product["base_meets"], 1, 10)
        if _int(product["base_points"], 0, 100) + _int(product["per_extra_meet"], 0, 100) * (10 - base) != parts["product"]:
            raise SafetyError("OPPORTUNITY_SETTINGS_INVALID")
        rules = _keys(value["eligibility"], ("min_meets", "min_want"))
        _int(rules["min_meets"], 1, 10); _int(rules["min_want"], 1, parts["want"])
        labels = _keys(value["labels"], ("early_bet_min_early", "early_bet_want_below"))
        _int(labels["early_bet_min_early"], 1, parts["early"]); _int(labels["early_bet_want_below"], 1, parts["want"])
        _int(_keys(value["selection"], ("high_crowding_only_below",))["high_crowding_only_below"], 1, 100)
        calibration = _keys(value["calibration"], ("after_scored_types", "too_quiet_share_above", "too_quiet_share_below"))
        _int(calibration["after_scored_types"], 1, 100000)
        if _share(calibration["too_quiet_share_below"]) >= _share(calibration["too_quiet_share_above"]):
            raise SafetyError("OPPORTUNITY_SETTINGS_INVALID")
    except (KeyError, TypeError):
        raise SafetyError("OPPORTUNITY_SETTINGS_INVALID") from None
    return value


def load_settings(path=SETTINGS):
    """The settings file, read again only when it changes on disk."""
    from .storage import load_json
    path = Path(path)
    try:
        info = path.stat()
    except OSError:
        raise SafetyError("OPPORTUNITY_SETTINGS_INVALID") from None
    stamp = (str(path), info.st_mtime_ns, info.st_size)
    if _CACHE.get("stamp") != stamp:
        _CACHE.update(stamp=stamp, value=validate_settings(load_json(path, 16384)))
    return deepcopy(_CACHE["value"])


def _points(ladder, count):
    return next((points for minimum, points in ladder if count >= minimum), 0)


# Facts per sub-point --------------------------------------------------------

def _ids(values, code="OPPORTUNITY_SOURCE_REFERENCE_INVALID"):
    if (not isinstance(values, list) or any(not isinstance(value, str) for value in values)
            or values != sorted(set(values))):
        raise SafetyError(code)
    for value in values:
        try:
            identifier(value)
        except SafetyError:
            raise SafetyError(code) from None
    return values


def _routes(value, code="OPPORTUNITY_BASIS_INVALID"):
    """{name: [evidence ids]} with no empty entries."""
    if not isinstance(value, dict) or any(not isinstance(name, str) or not name or not ids for name, ids in value.items()):
        raise SafetyError(code)
    for ids in value.values():
        _ids(ids, code)
    return value


def _union(value):
    return sorted({ref for ids in value.values() for ref in ids})


def _source(sources, name):
    result = sources.get(name)
    if (not isinstance(result, dict) or type(result.get("available")) is not bool
            or result.get("positive") not in (True, False, None)):
        raise SafetyError("OPPORTUNITY_SOURCE_INVALID")
    refs = result.get("used_ids", [])
    if (not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs)
            or len(set(refs)) != len(refs) or result["available"] and not refs):
        raise SafetyError("OPPORTUNITY_SOURCE_REFERENCE_INVALID")
    return result


def _count(value):
    if value is not None and (type(value) is not int or value < 0):
        raise SafetyError("OPPORTUNITY_SOURCE_INVALID")
    return value


def _mean(value):
    """A chart's mean bound as exact text (means are quarters of whole points)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SafetyError("OPPORTUNITY_SOURCE_INVALID")
    number = Decimal(repr(value))
    if not number.is_finite() or number < 0 or number * 4 != (number * 4).to_integral_value():
        raise SafetyError("OPPORTUNITY_SOURCE_INVALID")
    return format(number.normalize(), "f")


def market_facts(market):
    """The market sub-point facts and their evidence, from ``compute_market_signals``."""
    sources = market.get("sources") if isinstance(market, dict) else None
    if not isinstance(sources, dict):
        raise SafetyError("OPPORTUNITY_MARKET_INVALID")
    meta, trends = _source(sources, "meta"), _source(sources, "google-trends")
    depth = meta.get("depth") if isinstance(meta.get("depth"), dict) else {}
    meta_ids = sorted(meta["used_ids"]) if meta["available"] else []
    trend_ids = sorted(trends["used_ids"]) if trends["available"] else []
    usable = trends["available"]
    previous = trends.get("previous_mean_bounds") if usable else None
    recent = trends.get("recent_mean_bounds") if usable else None
    for bounds in (previous, recent):
        if bounds is not None and (not isinstance(bounds, list) or len(bounds) != 2 or any(_mean(value) is None for value in bounds)):
            raise SafetyError("OPPORTUNITY_SOURCE_INVALID")
    stock = {}
    for name in QUICK_COMMERCE:
        row = _source(sources, name)
        cities = row.get("stock_locations", {}) if row["available"] else {}
        _routes(cities, "OPPORTUNITY_SOURCE_INVALID")
        for city, refs in cities.items():
            if not set(refs) <= set(row["used_ids"]):
                raise SafetyError("OPPORTUNITY_SOURCE_REFERENCE_INVALID")
            stock[city] = sorted(set(stock.get(city, [])) | set(refs))
    return {"facebook_ads": ({"relevant_ads": len(meta_ids), "positive": meta["positive"] is True,
                              "advertisers": _count(depth.get("advertisers"))}, meta_ids),
            "new_advertisers": ({"recent_start_advertisers": _count(depth.get("recent_start_advertisers"))}, meta_ids),
            "google_searches": ({"usable": usable, "nonzero_points": _count(trends.get("nonzero_points")) if usable else None},
                                trend_ids),
            "rising_searches": ({"rising": usable and trends["positive"] is True,
                                 "previous_mean_upper": _mean(previous[1]) if previous else None,
                                 "recent_mean_lower": _mean(recent[0]) if recent else None,
                                 "breakout_queries": _count(trends.get("breakout_queries", 0)) if usable else 0}, trend_ids),
            "quick_commerce": ({"stock_cities": stock}, _union(stock))}


def presence_input(record, *, as_of):
    """A saved platform presence fact as score input; older than a week is not current."""
    if record is None:
        return None
    at = record["at"]
    if timestamp(at) > timestamp(as_of) or timestamp(as_of) - timestamp(at) > timedelta(days=PRESENCE_DAYS):
        return None
    result = {"at": at, "listed": {}, "on_list": {}, "new_arrival": {}}
    for route in record["platforms_carrying"]:
        items = [row for row in record["platforms"][route]["items"] if row["listed"]]
        for field, wanted in (("listed", items), ("on_list", [row for row in items if row["on_list"]]),
                              ("new_arrival", [row for row in items if row["new_arrival"]])):
            if wanted:
                result[field][route] = sorted({row["observation_id"] for row in wanted})
    return result


def crowding_input(fact):
    """The saved crowding fact as score input (``crowding.crowding_for``)."""
    return {"level": fact["level"], "rule": fact.get("rule"), "thresholds_version": fact.get("thresholds_version"),
            "counts": deepcopy(fact.get("counts")), "evidence_ids": sorted(fact.get("evidence_ids") or [])}


def score_inputs(state, concept_id, *, as_of):
    """(presence, crowding) for one type, from saved facts only."""
    from .crowding import crowding_for
    from .platform_roles import current_platform_presence
    return (presence_input(current_platform_presence(state, concept_id), as_of=as_of),
            crowding_input(crowding_for(state, concept_id)))


# Arithmetic -------------------------------------------------------------------

def _item_points(name, facts, settings):
    want, early = settings["want"], settings["early"]
    if name == "facebook_ads":
        rule = want["facebook_ads"]
        if facts["positive"] and (facts["advertisers"] or 0) >= rule["positive_with_advertisers"]["min_advertisers"]:
            return rule["positive_with_advertisers"]["points"]
        return rule["positive"] if facts["positive"] else rule["relevant_ads"] if facts["relevant_ads"] else 0
    if name == "google_searches":
        return (_points(want["google_searches"]["nonzero_points"], facts["nonzero_points"])
                if facts["usable"] and facts["nonzero_points"] is not None else 0)
    if name == "platform_lists":
        on_list = _points(want["platform_lists"]["on_list_platforms"], len(facts["on_list"]))
        return on_list or _points(want["platform_lists"]["listed_platforms"], len(facts["listed"]))
    if name == "quick_commerce":
        return _points(want["quick_commerce"]["stock_cities"], len(facts["stock_cities"]))
    if name == "rising_searches":
        rule = early["rising_searches"]
        if not facts["rising"]:
            return 0
        previous, recent = facts["previous_mean_upper"], facts["recent_mean_lower"]
        strong = facts["breakout_queries"] > 0 or (previous is not None and recent is not None and Decimal(previous) > 0
                 and Decimal(recent) * 100 >= Decimal(previous) * (100 + rule["strong_rise_percent"]))
        return rule["strong"] if strong else rule["rising"]
    if name == "new_advertisers":
        return _points(early["new_advertisers"]["recent_start_advertisers"], facts["recent_start_advertisers"] or 0)
    if name == "new_arrivals":
        return _points(early["new_arrivals"]["new_arrival_platforms"], len(facts["new_arrival"]))
    if name == "crowding":
        return 0 if facts["level"] == "not_checked" else settings["room"][facts["level"]]
    product = settings["product"]
    meets = facts["meets"]
    return product["base_points"] + product["per_extra_meet"] * (meets - product["base_meets"]) if meets >= product["base_meets"] else 0


def _evidence(name, facts, points, evidence):
    """The evidence ids behind a sub-point's points (crowding keeps its own)."""
    if name == "crowding":
        return evidence
    if not points:
        return []
    if name == "platform_lists":
        return _union(facts["on_list"]) if facts["on_list"] else _union(facts["listed"])
    if name == "new_arrivals":
        return _union(facts["new_arrival"])
    if name == "quick_commerce":
        return _union(facts["stock_cities"])
    return evidence


def _assemble(inputs, settings):
    """Score, label, eligibility and reason from each sub-point's (facts, evidence)."""
    parts = {}
    for part in PARTS:
        items = {}
        for name in ITEMS[part]:
            facts, evidence = inputs[name]
            points = _item_points(name, facts, settings)
            items[name] = {"points": points, "facts": deepcopy(facts),
                           "evidence_ids": list(_evidence(name, facts, points, evidence))}
        parts[part] = {"points": sum(row["points"] for row in items.values()), "max": settings["parts"][part],
                       "items": items}
    total = sum(parts[part]["points"] for part in PARTS)
    want, early = parts["want"]["points"], parts["early"]["points"]
    level, meets = inputs["crowding"][0]["level"], inputs["checklist"][0]["meets"]
    rules, labels = settings["eligibility"], settings["labels"]
    label = ("too_quiet" if want < rules["min_want"] else None if level == "not_checked"
             else "proven_but_crowded" if level == "high"
             else "early_bet" if early >= labels["early_bet_min_early"] and want < labels["early_bet_want_below"]
             else "sweet_spot")
    reason = ("fit_information_pending" if meets < rules["min_meets"] else "crowding_pending" if level == "not_checked"
              else "demand_too_quiet" if want < rules["min_want"] else "opportunity_scored")
    basis = {"settings_version": settings["version"], "parts": parts, "eligible": reason == "opportunity_scored",
             "reason": reason}
    return {"opportunity_policy_id": POLICY_ID, "opportunity_score": total, "opportunity_label": label,
            "opportunity_basis": basis, "eligible": basis["eligible"], "reason": reason}


def _fit_matches(fit):
    if (not isinstance(fit, dict) or fit.get("fit_policy_id") != FIT_POLICY_ID
            or type(fit.get("kpi_matches")) is not int or not 0 <= fit["kpi_matches"] <= 10
            or type(fit.get("kpi_total")) is not int or fit["kpi_total"] != 10):
        raise SafetyError("OPPORTUNITY_FIT_INVALID")
    return fit["kpi_matches"]


# A presence time is stored strictly in UTC, exactly as the portal checks it.
_UTC_AT = re.compile(r"[0-9]{4}-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9](?:\.[0-9]{1,6})?(?:Z|\+00:00)")


def _utc_at(value):
    """A real calendar instant written in UTC (portal ``utcAt`` accepts the same forms)."""
    if not isinstance(value, str) or not _UTC_AT.fullmatch(value):
        raise SafetyError("OPPORTUNITY_BASIS_INVALID")
    try:
        timestamp(value)
    except SafetyError:
        raise SafetyError("OPPORTUNITY_BASIS_INVALID") from None
    return value


def _presence_facts(presence):
    if presence is None:
        return {"presence_at": None, "on_list": {}, "listed": {}}, {"presence_at": None, "new_arrival": {}}
    _utc_at(presence["at"])
    listed = _routes(presence["listed"]); on_list = _routes(presence["on_list"]); arrivals = _routes(presence["new_arrival"])
    if any(route not in listed or not set(ids) <= set(listed[route])
           for value in (on_list, arrivals) for route, ids in value.items()):
        raise SafetyError("OPPORTUNITY_BASIS_INVALID")
    return ({"presence_at": presence["at"], "on_list": deepcopy(on_list), "listed": deepcopy(listed)},
            {"presence_at": presence["at"], "new_arrival": deepcopy(arrivals)})


def _crowding_facts(crowding):
    if not isinstance(crowding, dict) or crowding.get("level") not in CROWDING_LEVELS:
        raise SafetyError("OPPORTUNITY_CROWDING_INVALID")
    known = crowding["level"] != "not_checked"
    if (known != isinstance(crowding.get("rule"), str) or not isinstance(crowding.get("thresholds_version"), str)
            or crowding.get("counts") is not None and not isinstance(crowding["counts"], dict)):
        raise SafetyError("OPPORTUNITY_CROWDING_INVALID")
    _ids(crowding["evidence_ids"])
    return ({"level": crowding["level"], "rule": crowding["rule"], "thresholds_version": crowding["thresholds_version"],
             "counts": deepcopy(crowding["counts"])}, list(crowding["evidence_ids"]))


def evaluate_opportunity(fit, market, *, presence, crowding, settings=None):
    """The score record for one type from its fit, market signals, platform
    presence (``presence_input``) and crowding (``crowding_input``)."""
    settings = load_settings() if settings is None else settings
    meets = _fit_matches(fit)
    inputs = market_facts(market)
    lists, arrivals = _presence_facts(presence)
    inputs.update(platform_lists=(lists, []), new_arrivals=(arrivals, []), crowding=_crowding_facts(crowding),
                  checklist=({"meets": meets}, []))
    return _assemble(inputs, settings)


def score_type(state, concept_id, fit, market, *, as_of, settings=None):
    """``evaluate_opportunity`` with the type's saved presence and crowding."""
    presence, crowding = score_inputs(state, concept_id, as_of=as_of)
    return evaluate_opportunity(fit, market, presence=presence, crowding=crowding, settings=settings)


def basis_inputs(basis):
    """(presence, crowding) as recorded in a score basis, for an exact re-check."""
    try:
        lists = basis["parts"]["want"]["items"]["platform_lists"]["facts"]
        arrivals = basis["parts"]["early"]["items"]["new_arrivals"]["facts"]
        room = basis["parts"]["room"]["items"]["crowding"]
        presence = None if lists["presence_at"] is None else {
            "at": lists["presence_at"], "listed": deepcopy(lists["listed"]), "on_list": deepcopy(lists["on_list"]),
            "new_arrival": deepcopy(arrivals["new_arrival"])}
        return presence, dict(deepcopy(room["facts"]), evidence_ids=list(room["evidence_ids"]))
    except (KeyError, TypeError):
        raise SafetyError("OPPORTUNITY_BASIS_INVALID") from None


# Validation -------------------------------------------------------------------

_FACT_FIELDS = {"facebook_ads": {"relevant_ads", "positive", "advertisers"},
                "google_searches": {"usable", "nonzero_points"}, "platform_lists": {"presence_at", "on_list", "listed"},
                "quick_commerce": {"stock_cities"},
                "rising_searches": {"rising", "previous_mean_upper", "recent_mean_lower", "breakout_queries"},
                "new_advertisers": {"recent_start_advertisers"}, "new_arrivals": {"presence_at", "new_arrival"},
                "crowding": {"level", "rule", "thresholds_version", "counts"}, "checklist": {"meets"}}


def _checked_facts(name, facts):
    """Strict types for one sub-point's facts (the arithmetic is re-run after)."""
    if not isinstance(facts, dict) or set(facts) != _FACT_FIELDS[name]:
        raise SafetyError("OPPORTUNITY_BASIS_INVALID")
    flags = {"facebook_ads": ("positive",), "google_searches": ("usable",), "rising_searches": ("rising",)}.get(name, ())
    counts = {"facebook_ads": ("relevant_ads", "advertisers"), "google_searches": ("nonzero_points",),
              "rising_searches": ("breakout_queries",), "new_advertisers": ("recent_start_advertisers",),
              "checklist": ("meets",)}.get(name, ())
    if any(type(facts[field]) is not bool for field in flags):
        raise SafetyError("OPPORTUNITY_BASIS_INVALID")
    for field in counts:
        value = facts[field]
        if (value is None and field in ("relevant_ads", "breakout_queries", "meets")
                or value is not None and (type(value) is not int or value < 0 or field == "meets" and value > 10)):
            raise SafetyError("OPPORTUNITY_BASIS_INVALID")
    if name == "rising_searches":
        for field in ("previous_mean_upper", "recent_mean_lower"):
            if facts[field] is not None and (not isinstance(facts[field], str)
                                             or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.(?:25|5|75))?", facts[field])):
                raise SafetyError("OPPORTUNITY_BASIS_INVALID")
    if name in ("platform_lists", "new_arrivals") and facts["presence_at"] is not None:
        _utc_at(facts["presence_at"])
    if name == "quick_commerce":
        _routes(facts["stock_cities"])
    return facts


def _stored_inputs(basis):
    try:
        if set(basis) != {"settings_version", "parts", "eligible", "reason"} or set(basis["parts"]) != set(PARTS):
            raise SafetyError("OPPORTUNITY_BASIS_INVALID")
        inputs = {}
        for part in PARTS:
            if set(basis["parts"][part]) != {"points", "max", "items"} or set(basis["parts"][part]["items"]) != set(ITEMS[part]):
                raise SafetyError("OPPORTUNITY_BASIS_INVALID")
            for name in ITEMS[part]:
                row = basis["parts"][part]["items"][name]
                if not isinstance(row, dict) or set(row) != {"points", "facts", "evidence_ids"}:
                    raise SafetyError("OPPORTUNITY_BASIS_INVALID")
                inputs[name] = (_checked_facts(name, row["facts"]), _ids(row["evidence_ids"]))
        presence, crowding = basis_inputs(basis)
        lists, arrivals = _presence_facts(presence)
        if lists != inputs["platform_lists"][0] or arrivals != inputs["new_arrivals"][0]:
            raise SafetyError("OPPORTUNITY_BASIS_INVALID")
        inputs["crowding"] = _crowding_facts(crowding)
        meta = inputs["facebook_ads"][1]
        ads = inputs["facebook_ads"][0]
        if (inputs["facebook_ads"][1] and len(meta) != ads["relevant_ads"]
                or inputs["new_advertisers"][1] and inputs["new_advertisers"][1] != meta
                or inputs["rising_searches"][1] and inputs["google_searches"][1]
                and inputs["rising_searches"][1] != inputs["google_searches"][1]):
            raise SafetyError("OPPORTUNITY_SOURCE_REFERENCE_INVALID")
        return inputs
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SafetyError("OPPORTUNITY_BASIS_INVALID") from None


def validate_record(record, *, matches=None, settings=None):
    """Re-run the score from a stored record's own facts; refuse any difference."""
    settings = load_settings() if settings is None else settings
    if (not isinstance(record, dict) or not all(field in record for field in OPPORTUNITY_FIELDS)
            or record["opportunity_policy_id"] != POLICY_ID):
        raise SafetyError("OPPORTUNITY_POLICY_INVALID")
    basis = record["opportunity_basis"]
    if not isinstance(basis, dict) or basis.get("settings_version") != settings["version"]:
        raise SafetyError("OPPORTUNITY_SETTINGS_VERSION_MISMATCH")
    inputs = _stored_inputs(basis)
    if matches is not None and inputs["checklist"][0]["meets"] != matches:
        raise SafetyError("OPPORTUNITY_FIT_INVALID")
    expected = _assemble(inputs, settings)
    # Exact JSON comparison: a boolean or 12.0 is not the integer 12.
    if (digest(record["opportunity_basis"]) != digest(expected["opportunity_basis"])
            or type(record["opportunity_score"]) is not int
            or record["opportunity_score"] != expected["opportunity_score"]):
        raise SafetyError("OPPORTUNITY_SCORE_INVALID")
    if record["opportunity_label"] != expected["opportunity_label"]:
        raise SafetyError("OPPORTUNITY_LABEL_INVALID")
    return expected


def market_evidence(record):
    """Evidence ids of the market sub-points that earn points, by market site."""
    by_site = {}
    for part in ("want", "early"):
        for name, row in record["opportunity_basis"]["parts"][part]["items"].items():
            if name in MARKET_ITEMS and row["points"]:
                site = MARKET_ITEMS[name]
                if site is None:
                    for refs in row["facts"]["stock_cities"].values():
                        by_site.setdefault("quick-commerce", set()).update(refs)
                else:
                    by_site.setdefault(site, set()).update(row["evidence_ids"])
    return {site: sorted(refs) for site, refs in by_site.items()}


def validate_opportunity(product, *, matches=None, settings=None):
    """Check a marked selected record; unmarked older records return None."""
    if not isinstance(product, dict):
        raise SafetyError("OPPORTUNITY_RECORD_INVALID")
    if not any(field in product for field in OPPORTUNITY_FIELDS):
        return None
    if (not all(field in product for field in OPPORTUNITY_FIELDS) or product["opportunity_policy_id"] != POLICY_ID
            or product.get("fit_policy_id") != FIT_POLICY_ID):
        raise SafetyError("OPPORTUNITY_POLICY_INVALID")
    matches = product.get("kpi_matches") if matches is None else matches
    if type(matches) is not int or not 0 <= matches <= 10:
        raise SafetyError("OPPORTUNITY_FIT_INVALID")
    expected = validate_record(product, matches=matches, settings=settings)
    if not expected["eligible"]:
        raise SafetyError("OPPORTUNITY_NOT_ELIGIBLE")
    cited = {ref for refs in market_evidence(product).values() for ref in refs}
    if "source_refs" in product:
        refs = product["source_refs"]
        if (not isinstance(refs, list) or any(not isinstance(ref, dict) or not isinstance(ref.get("id"), str) for ref in refs)
                or not cited <= {ref["id"] for ref in refs}):
            raise SafetyError("OPPORTUNITY_SOURCE_REFERENCE_INVALID")
    if "evidence_refs" in product:
        refs = product["evidence_refs"]
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs) or not cited <= set(refs):
            raise SafetyError("OPPORTUNITY_SOURCE_REFERENCE_INVALID")
    return product["opportunity_score"]


def crowding_level(record):
    return record["opportunity_basis"]["parts"]["room"]["items"]["crowding"]["facts"]["level"]


def validate_issue_crowding(products, settings=None):
    """High crowding fills an issue only while it has fewer than ten Low/Medium
    types: in the issue's own order, no High type follows ten Low/Medium ones.
    (Low/Medium extras that the update cap let in only later may follow a High.)"""
    settings = load_settings() if settings is None else settings
    seen = 0
    for product in products:
        if product.get("opportunity_policy_id") != POLICY_ID:
            continue
        if crowding_level(product) == "high":
            if seen >= settings["selection"]["high_crowding_only_below"]:
                raise SafetyError("ISSUE_HIGH_CROWDING_RULE")
        else:
            seen += 1


# Order --------------------------------------------------------------------------

def opportunity_sort_key(product):
    """Low/Medium first, highest score, then early, then product (callers add the
    stable type id). Unmarked older records keep their fit-only order after."""
    score = validate_opportunity(product)
    if score is None:
        matches = product["kpi_matches"] if "kpi_matches" in product else product["kpi_states"].count("Meets")
        return (1, False, 0, 0, 0, product.get("fit_tier") == "decent", -matches,
                -product.get("positive_families", 0), -product.get("available_families", 0))
    return (0, *score_rank(product), False, 0, 0, 0)


def score_rank(record):
    """Low/Medium before High, then score, early and product points (highest first)."""
    parts = record["opportunity_basis"]["parts"]
    return (crowding_level(record) == "high", -record["opportunity_score"], -parts["early"]["points"],
            -parts["product"]["points"])


def work_rank(row):
    """Order of follow-up work rows (no eligibility needed): Low/Medium and higher score first."""
    if row.get("opportunity_policy_id") != POLICY_ID:
        return (1, False, 0)
    return (0, crowding_level(row) == "high", -row["opportunity_score"])


def opportunity_fields(result):
    """Copy the explicit decision fields into facts and frozen selections."""
    return {field: deepcopy(result[field]) for field in OPPORTUNITY_FIELDS}


# Report wording -----------------------------------------------------------------

_PART_TEXT = {"en": {"want": "Want", "early": "Early", "room": "Room", "product": "Product"},
              "hi-Latn": {"want": "Maang", "early": "Jaldi", "room": "Jagah", "product": "Product"}}
_LEVEL_TEXT = {"en": {"low": "Low", "medium": "Medium", "high": "High", "not_checked": "Not checked"},
               "hi-Latn": {"low": "Kam", "medium": "Madhyam", "high": "Zyada", "not_checked": "Abhi check nahi hui"}}


_SCORE_PREFIX = {"en": "Opportunity score", "hi-Latn": "Mauke ka score"}


def score_text(product, language="en"):
    """'Opportunity score 62/100 · Want 14/20 · ... · Sweet spot' (code-owned)."""
    parts = product["opportunity_basis"]["parts"]
    text = _SCORE_PREFIX[language] + " " + str(product["opportunity_score"]) + "/100"
    text += "".join(" · " + _PART_TEXT[language][part] + " " + str(parts[part]["points"]) + "/" + str(parts[part]["max"])
                    for part in PARTS)
    label = product["opportunity_label"]
    return text + (" · " + LABEL_TEXT[language][label] if label else "")


def crowding_text(product, language="en"):
    """'Amazon/Flipkart crowding: Medium (3 relevant listings: ...)' with the counts behind it."""
    facts = product["opportunity_basis"]["parts"]["room"]["items"]["crowding"]["facts"]
    level = _LEVEL_TEXT[language][facts["level"]]
    text = ("Amazon/Flipkart crowding: " if language == "en" else "Amazon/Flipkart par bheed: ") + level
    counts = facts["counts"]
    if facts["level"] == "not_checked" or not isinstance(counts, dict):
        return text
    listings = counts.get("relevant_listings") or {}
    amazon, flipkart = listings.get("amazon", 0), listings.get("flipkart", 0)
    ratings = counts.get("max_ratings")
    branded, hidden = counts.get("listings_with_brand") or 0, counts.get("brand_not_shown") or 0
    # Never "0 brands" when the pages simply do not show brands.
    if language == "en":
        brands = ("brands not shown" if not branded else f"{counts.get('brands_or_sellers', 0)} brands or sellers"
                  + (f" ({hidden} show no brand)" if hidden else ""))
        details = [f"{amazon + flipkart} relevant listings: Amazon {amazon}, Flipkart {flipkart}", brands,
                   "most ratings " + (str(ratings) if ratings is not None else "not shown"),
                   f"{counts.get('bestseller_hits', 0)} on a bestseller list"]
    else:
        brands = ("brand nahi dikhe" if not branded else f"{counts.get('brands_or_sellers', 0)} brand ya seller"
                  + (f" ({hidden} listing par brand nahi dikha)" if hidden else ""))
        details = [f"{amazon + flipkart} milti-julti listing: Amazon {amazon}, Flipkart {flipkart}", brands,
                   "sabse zyada ratings " + (str(ratings) if ratings is not None else "nahi dikhi"),
                   f"{counts.get('bestseller_hits', 0)} bestseller list par"]
    return text + " (" + "; ".join(details) + ")"


def workbook_headers(settings=None):
    settings = load_settings() if settings is None else settings
    maxima = settings["parts"]
    return ("Score", "Want /" + str(maxima["want"]), "Early /" + str(maxima["early"]), "Room /" + str(maxima["room"]),
            "Product /" + str(maxima["product"]), "Label", "Crowding")


def workbook_values(product):
    parts = product["opportunity_basis"]["parts"]
    label = product["opportunity_label"]
    return [product["opportunity_score"], *(parts[part]["points"] for part in PARTS),
            LABEL_TEXT["en"][label] if label else "Not scored", _LEVEL_TEXT["en"][crowding_level(product)]]


# Owner meter, desktop line and the one-time calibration ----------------------------

def score_tally(decisions):
    """Scored types per label (5+ Meets and a known crowding level), from a report pool."""
    labels = dict.fromkeys(LABELS, 0)
    waiting = 0
    for row in decisions:
        if row.get("opportunity_policy_id") != POLICY_ID:
            continue
        reason = row["opportunity_basis"]["reason"]
        if reason == "crowding_pending":
            waiting += 1
        elif reason in ("demand_too_quiet", "opportunity_scored"):
            labels[row["opportunity_label"]] += 1
    return {"labels": labels, "scored": sum(labels.values()), "waiting_for_crowding": waiting}


CALIBRATION_ID = "opportunity-score-calibration-once"  # one record in total, whatever the settings version


def calibration_record(state):
    saved = state.get("batch_runs", {}).get(CALIBRATION_ID)
    return deepcopy(saved) if isinstance(saved, dict) and saved.get("kind") == CALIBRATION_KIND else None


def _first_scored(decisions):
    """(concept id, label) of each scored type in a report pool, in id order
    (types first scored in the same pool share one scoring time)."""
    rows = {}
    for row in decisions:
        if (row.get("opportunity_policy_id") == POLICY_ID and isinstance(row.get("concept_id"), str)
                and row["opportunity_basis"]["reason"] in ("demand_too_quiet", "opportunity_scored")):
            rows.setdefault(row["concept_id"], row["opportunity_label"])
    return sorted(rows.items())


def _calibrate(memory, decisions, settings, at):
    """Exactly once in total: the first scored types, in the order they were
    first scored (each with the label it had then), are collected across report
    pools; when the sample is full, a skewed Too quiet share is flagged for the
    agreed one-time inner-threshold adjustment. A decided record never changes,
    and a later settings version does not start another sample."""
    from .research_memory import key
    saved = calibration_record(memory.snapshot())
    if saved is not None and saved["state"] != "collecting":
        return saved
    rule = settings["calibration"]
    sample = list(saved["sample"]) if saved else []
    seen = {row["concept_id"] for row in sample}
    added = [{"concept_id": cid, "label": label, "first_scored_at": at}
             for cid, label in _first_scored(decisions) if cid not in seen][:rule["after_scored_types"] - len(sample)]
    if not added:
        return saved
    sample += added
    record = {"kind": CALIBRATION_KIND, "policy_id": POLICY_ID, "sample_size": rule["after_scored_types"],
              "sample": sample, "state": "collecting", "at": at}
    if len(sample) >= rule["after_scored_types"]:
        labels = dict.fromkeys(LABELS, 0)
        for row in sample:
            labels[row["label"]] += 1
        quiet = labels["too_quiet"]
        share = Decimal(quiet) / Decimal(len(sample))
        needed = share > _share(rule["too_quiet_share_above"]) or share < _share(rule["too_quiet_share_below"])
        record.update(settings_version=settings["version"], scored_types=len(sample), too_quiet_types=quiet,
                      too_quiet_share=str(share.quantize(Decimal("0.0001"))), labels=labels,
                      state="calibration_needed" if needed else "in_range",
                      message=("Too quiet is outside the expected share of the first scored types; adjust only the inner "
                               "point thresholds once with a new settings version, update the portal mirror and redeploy "
                               "the portal (reserved and unreleased issues are then re-selected), and record the "
                               "before/after counts." if needed else
                               "Too quiet share of the first scored types is within the expected range."))
    memory.put("batch_runs", CALIBRATION_ID, record, previous=saved,
               event_id=key("opportunity-score-calibration-saved", digest(record)))
    if record["state"] != "collecting":
        from .operations import record_event
        record_event("opportunity_score_calibration", state=record["state"],
                     reason="SCORE_CALIBRATION_NEEDED" if record["state"] == "calibration_needed" else "SCORE_CALIBRATION_IN_RANGE")
    return record


def _summary(decisions, record, settings):
    tally = score_tally(decisions)
    labels, sample = tally["labels"], settings["calibration"]["after_scored_types"]
    collecting = record is None or record["state"] == "collecting"
    calibration = "waiting_for_sample" if collecting else record["state"]
    # While collecting: the saved sample plus this pool's newly scored types.
    sampled = min(sample, len({row["concept_id"] for row in (record or {}).get("sample", [])}
                              | {cid for cid, _ in _first_scored(decisions)}))
    alert = None
    if calibration == "calibration_needed":
        alert = ("SCORE CALIBRATION NEEDED: " + record["too_quiet_share"] + " of the first " + str(record["scored_types"])
                 + " scored types are Too quiet (" + record["settings_version"] + "). Adjust only the inner point thresholds "
                 "once in config/research/opportunity_score.json with a new version and record the before/after counts. "
                 "A new settings version makes every reserved or unreleased issue fail its score check "
                 "(OPPORTUNITY_SETTINGS_VERSION_MISMATCH) until it is selected again, and the portal mirror "
                 "(portal/opportunity-score.mjs SETTINGS) must be updated and the portal redeployed.")
    text = ("Opportunity score: " + str(tally["scored"]) + " types scored ("
            + ", ".join(str(labels[label]) + " " + LABEL_TEXT["en"][label].lower() for label in reversed(LABELS))
            + "), " + str(tally["waiting_for_crowding"]) + " waiting for the crowding check; calibration "
            + calibration.replace("_", " ") + ("" if not collecting else " (" + str(sampled) + " of " + str(sample) + ")") + ".")
    return {"settings_version": settings["version"], **deepcopy(tally), "calibration": calibration,
            "alert": alert, "text": text}


def score_summary(state, pool, settings=None):
    """Owner meter view from a report pool and the saved calibration record."""
    settings = load_settings() if settings is None else settings
    return _summary(pool.get("decisions", []), calibration_record(state), settings)


def _summary_path(root):
    from .storage import safe_path
    return safe_path(root, "opportunity_score") / "summary.json"


def record_score_summary(memory, pool, *, at, settings=None):
    """After a report pool: the one-time calibration, and the desktop line file
    (written only when its counts change)."""
    from .storage import atomic_json, load_json
    timestamp(at)
    settings = load_settings() if settings is None else settings
    decisions = pool.get("decisions", [])
    summary = _summary(decisions, _calibrate(memory, decisions, settings, at), settings)
    path = _summary_path(memory.root)
    value = {"schema_version": 1, "kind": SUMMARY_KIND, "at": at, **summary}
    try:
        old = load_json(path, 65536) if path.exists() else None
    except SafetyError:
        old = None
    if not isinstance(old, dict) or {k: v for k, v in old.items() if k != "at"} != {k: v for k, v in value.items() if k != "at"}:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(path, value)
    return summary


def score_line(root):
    """The desktop line (and the calibration alert) from the saved summary file."""
    from .storage import load_json
    path = _summary_path(root)
    if not path.exists() and not path.is_symlink():
        return "Opportunity score: no types scored yet."
    value = load_json(path, 65536)
    if (not isinstance(value, dict) or value.get("kind") != SUMMARY_KIND or value.get("schema_version") != 1
            or not isinstance(value.get("text"), str) or value.get("alert") is not None and not isinstance(value["alert"], str)):
        raise SafetyError("OPPORTUNITY_SUMMARY_INVALID")
    return value["text"] + ("\n" + value["alert"] if value["alert"] else "")
