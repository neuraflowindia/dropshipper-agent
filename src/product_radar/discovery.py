"""Pure local retrieval and calculations. Retrieval scores never merge products."""

from datetime import date, timedelta
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from .models import SafetyError, timestamp, digest


def normalise(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def tokens(value):
    return set(re.findall(r"\w+", normalise(value))) - {"a", "an", "the", "for", "with", "and", "of", "in", "to", "product", "unknown"}


# These words describe presentation, quantity or broad context, not a useful
# product identity. This is local candidate retrieval, not source rewriting or
# a registry of product nouns. Materials may still break already-admitted ties.
_RETRIEVAL_ATTRIBUTES = set("""
pack packs piece pieces pcs set sets size sizes inch inches cm mm metre meter
meters metres litre liter litres liters ml kg gm gram grams g oz lb lbs
black white blue pink red green grey gray yellow orange purple brown silver
gold golden beige cream navy multicolour multicolor assorted printed coated
pvd stainless steel aluminium aluminum metal metallic plastic pvc fabric
wood wooden glass silicone rubber cotton polyester colour color colours colors
""".split())

_RETRIEVAL_WEAK = _RETRIEVAL_ATTRIBUTES | set("""
or not no without by from on at into onto as is are be being been has have had
its their this that these those it one two three four five six seven eight nine
ten twelve people person use using used provide provides providing suitable
designed intended can may will exact unspecified specified unclear stated unknown
fit fits fitted fitting part
home kitchen decor baby travel outdoor adult adults household personal
""".split())


def meaningful_tokens(value):
    """Return lexical retrieval anchors; never infer aliases or an identity."""
    return {word for word in tokens(value)
            if word not in _RETRIEVAL_WEAK and len(word) > 1
            and not any(character.isdigit() for character in word)}


def find_possible_matches(product, concepts, limit=10):
    ranked = []
    label = meaningful_tokens(product["label"])
    functional = meaningful_tokens(product["function"] + " " + product["mechanism"])
    for concept_id, concept in concepts.items():
        if concept.get("status") in ("merged", "retired"):
            continue
        other = concept["description"]
        score, reasons, exact_anchor = 0, [], False
        for field, weight in (("label", 6), ("function", 4), ("mechanism", 3), ("audience", 1)):
            if (normalise(product[field]) not in ("unknown", "not stated", "unclear")
                    and normalise(product[field]) == normalise(other[field])
                    and (field == "audience" or meaningful_tokens(product[field]))):
                score += weight
                reasons.append(field)
                exact_anchor = exact_anchor or field != "audience"
        shared_label = label & meaningful_tokens(other["label"])
        shared_functional = functional & meaningful_tokens(other["function"] + " " + other["mechanism"])
        if not (exact_anchor or shared_label or len(shared_functional) >= 2):
            continue
        if shared_label:
            score += min(len(shared_label), 3)
            reasons.append("label_overlap")
        # Count a repeated word once rather than inflate two field scores.
        remaining = shared_functional - shared_label
        if remaining:
            score += min(len(remaining), 2)
            reasons.append("functional_overlap")
        weak = (tokens(product["label"] + " " + product["function"])
                & tokens(other["label"] + " " + other["function"])) & _RETRIEVAL_ATTRIBUTES
        ranked.append({"id": concept_id, "description": other, "retrieval_score": score,
                       "retrieval_reasons": reasons, "_weak_tie": min(len(weak), 3)})
    ranked.sort(key=lambda item: (-item["retrieval_score"], -item["_weak_tie"], item["id"]))
    for item in ranked:
        item.pop("_weak_tie")
    return {"candidates": ranked[:limit], "omitted": max(0, len(ranked) - limit)}


def explicit_conflict(product, other):
    # Only concrete incompatible known values; absent facts are neutral.
    if product["basis"] != "observed" or other["basis"] != "observed":
        return False
    for field, pair in (("mechanism", {"manual", "powered"}), ("audience", {"adult", "baby"})):
        if {normalise(product[field]), normalise(other[field])} == pair:
            return True
    return False


def latest_observations(observations, as_of, *, max_age_days=7):
    """Resolve replacements, keeping location-specific catalogue observations.

    Demand consumers retain the seven-day default. A work-history view may
    request no age cutoff; this proves an earlier check, not a fresh demand
    result. Dates after as_of are always excluded.
    """
    if max_age_days is not None and (type(max_age_days) is not int or max_age_days < 1):
        raise SafetyError("OBSERVATION_AGE_OPTION_INVALID")
    now = timestamp(as_of)
    latest = {}
    for obs_id, observation in observations.items():
        at = timestamp(observation["observed_at"])
        if at > now or max_age_days is not None and now - at > timedelta(days=max_age_days):
            continue
        identity = (observation["source"], observation["source_item"])
        if observation["source"] in ("blinkit", "zepto", "instamart"):
            identity += (digest(observation.get("facts", {}).get("location")),)
        if identity not in latest or at > timestamp(latest[identity][1]["observed_at"]):
            latest[identity] = (obs_id, observation)
    return dict(latest.values())


def compute_market_signals(observations, relevance, as_of, trends_evidence=(), *, source_support=()):
    """Count relevant source facts; optionally use controlled Trends exports."""
    relevant = [(i, o) for i, o in latest_observations(observations, as_of).items()
                if relevance.get(o["semantic_id"]) in ("exact_product", "relevant_family")]
    meta = [(i, o) for i, o in relevant if o["source"] == "meta"]
    ads = {}
    conflicts = set()
    for obs_id, observation in meta:
        facts = observation["facts"]
        ad_id = facts["ad_id"]
        advertiser = facts["advertiser_id"]
        if advertiser is None and isinstance(facts.get("advertiser_url"), str):
            # A visible official advertiser-page address is a usable identity
            # even when its private numeric page ID is absent. No name guess,
            # invented numeric ID or search-filter inference is involved.
            try:
                url = urlsplit(facts["advertiser_url"])
                if (url.scheme == "https" and url.hostname in ("facebook.com", "www.facebook.com")
                        and not url.username and not url.password and url.path.strip("/")
                        and not url.path.startswith(("/ads/", "/login", "/help", "/privacy", "/policies"))):
                    advertiser = "page-url:" + urlunsplit(("https", "www.facebook.com", url.path.rstrip("/").casefold(), url.query, ""))
            except ValueError:
                pass
        value = (advertiser, facts["active"], facts["started_at"])
        if ad_id in ads and ads[ad_id][0] != value:
            conflicts.add(ad_id)
        ads[ad_id] = (value, obs_id, observation["observed_at"])
    valid_ads = [v for k, v in ads.items() if k not in conflicts]
    advertisers = {v[0][0] for v in valid_ads if v[0][0] is not None}
    active_ads = [v for v in valid_ads if v[0][1] is True]
    # Age is established at collection, not by ageing a cached ad on our disk.
    aged = any(v[0][2] is not None and timestamp(v[2]) - timestamp(v[0][2]) >= timedelta(days=14) for v in active_ads)
    positive = len(advertisers) >= 3 or (len(active_ads) >= 5 and aged)
    complete = bool(valid_ads) and not conflicts and all(all(x is not None for x in v[0]) for v in valid_ads)
    meta_state = "Positive" if positive else "Not-positive" if complete else "Insufficient"
    from .marketplace_browser import verified_list_observation
    marketplace = [(i, o) for i, o in relevant if verified_list_observation(o)]
    families = {
        "meta": {"state": meta_state, "available": positive or complete, "positive": True if positive else False if complete else None,
                 "advertisers": len(advertisers) if advertisers else None,
                 "active_ads": len(active_ads) if any(v[0][1] is not None for v in valid_ads) else None,
                 "missing_advertiser_ids": sum(v[0][0] is None for v in valid_ads),
                 "unknown_active_status": sum(v[0][1] is None for v in valid_ads),
                 "used_ids": sorted({v[1] for v in valid_ads}), "reason": "Relevant retained Meta sample; unique parent ads, seven-day freshness."},
        "marketplace": {"state": "Positive" if marketplace else "Insufficient", "available": bool(marketplace),
                        "positive": True if marketplace else None, "used_ids": sorted(i for i, _ in marketplace),
                        "reason": "Relevant reviewed India list presence." if marketplace else "No fresh verified relevant list evidence in this slice."},
        "trends": {"state": "Insufficient", "available": False, "positive": None, "used_ids": [],
                   "reason": "Comparable Trends series is not connected in this slice."},
    }
    # Keep those original three-group figures unchanged for historical report
    # contracts. Current opportunity ranking separately counts distinct sites.
    from .opportunity_policy import CURRENT_OPPORTUNITY_POLICY_ID, SOURCE_PLATFORMS
    sources = {source: {"available": False, "positive": None, "used_ids": [],
                        "reason": "No fresh reviewed relevant source information."}
               for source in SOURCE_PLATFORMS}
    sources["meta"] = {"available": bool(valid_ads), "positive": True if positive else False if valid_ads else None,
                       "used_ids": families["meta"]["used_ids"],
                       "reason": "Relevant retained ads; missing ad metadata is not negative demand proof.",
                       "context_only": not positive}
    for source in ("amazon", "flipkart", "blinkit", "zepto", "instamart"):
        rows = [(i, o) for i, o in relevant if o["source"] == source and
                (verified_list_observation(o)
                 or source in ("amazon", "flipkart") and o["facts"].get("list_type") == "product_search"
                 or source in ("blinkit", "zepto", "instamart") and o["facts"].get("list_type") == "quickcommerce_context")]
        list_proof = any(verified_list_observation(o) for _, o in rows)
        sources[source] = {"available": bool(rows), "positive": True if list_proof else False if rows else None,
                           "used_ids": sorted(i for i, _ in rows), "context_only": not list_proof,
                           "reason": ("Relevant reviewed India list presence; all Amazon routes count as one source." if list_proof
                                      else "Relevant current listings supply product, price or availability context; not sales proof." if rows
                                      else "No fresh reviewed relevant source information.")}
    if trends_evidence:
        from .trends import compute_trends_signal
        from .trends_related import chart_with_related_support
        trends_evidence = list(trends_evidence)
        signals, source_signals = [], []
        for evidence in trends_evidence:
            if evidence.get("evidence_kind") == "related_queries":
                continue
            parsed = chart_with_related_support(evidence, trends_evidence, as_of)
            captured_at = parsed.get("metadata", {}).get("collected_at")
            if captured_at is not None and timestamp(captured_at) <= timestamp(as_of):
                signal = compute_trends_signal(parsed, now=as_of)
                signals.append((timestamp(captured_at), evidence["id"], signal, parsed.get("state") == "Usable"))
                source_signals.append((timestamp(captured_at), evidence["id"], signal, parsed,
                                       _usable_trends_context(parsed, as_of)))
        if signals:
            # Never cherry-pick an older positive after a newer contradictory
            # capture; the newest attached controlled evidence governs.
            # A failed export does not erase a still-fresh usable earlier one.
            # A newer successfully parsed negative series does replace it.
            usable = [s for s in signals if s[3]]
            signals = usable or signals
            signals.sort(key=lambda item: (item[0], item[1]))
            families["trends"] = signals[-1][2]
        if source_signals:
            parsed_signals = [row for row in source_signals if row[3].get("state") == "Usable"]
            selected = max(parsed_signals or source_signals, key=lambda row: (row[0], row[1]))
            _, evidence_id, signal, parsed, usable_context = selected
            sources["google-trends"] = {"available": usable_context, "positive": signal.get("positive") if usable_context else None,
                                         "used_ids": [evidence_id] if usable_context else [],
                                         "reason": signal["reason"], "context_only": signal.get("positive") is not True}
            if usable_context and signal.get("evidence_support") == "reasonably_positive":
                sources["google-trends"]["evidence_support"] = "reasonably_positive"
    applied_support = []
    for support in source_support:
        if not isinstance(support, dict) or support.get("source") not in sources:
            continue
        source, refs = support["source"], support.get("references")
        signal = sources[source]
        if (signal["available"] is not True or not isinstance(refs, list)
                or any(not isinstance(ref, str) for ref in refs)
                or not set(refs) <= set(signal["used_ids"])
                or not isinstance(support.get("review_id"), str)
                or support.get("judgment") not in ("reasonably_positive", "neutral", "weak", "unclear")):
            continue
        positive_support = support["judgment"] == "reasonably_positive" and bool(refs)
        if positive_support and source == "google-trends":
            # An all-zero or missing chart cannot become observed demand by
            # interpretation. Stable actual nonzero interest need not grow.
            charts = [row for row in trends_evidence if row["id"] in refs]
            positive_support = any(any(point.get("state") == "bounded_nonzero"
                or point.get("state") == "observed" and type(point.get("value")) is int and point["value"] > 0
                for point in chart.get("parsed", {}).get("series", [])) for chart in charts)
        if positive_support:
            signal["evidence_support"] = "reasonably_positive"
        # Keep measured source arithmetic and historical three-group rules
        # untouched. The current recommendation additionally uses this opinion.
        zero_chart_veto = support["judgment"] == "reasonably_positive" and source == "google-trends" and not positive_support
        signal["support_judgment"] = "unclear" if zero_chart_veto else support["judgment"]
        signal["support_reason"] = ("No observed nonzero chart interest is available; this is not proof of absent demand."
                                    if zero_chart_veto else support.get("reason", ""))
        signal["support_basis"] = "not_stated" if zero_chart_veto else support.get("basis")
        signal["support_accepted"] = not zero_chart_veto
        signal["support_review_id"] = support["review_id"]
        applied_support.append(support)
    positive_count = sum(f["positive"] is True for f in families.values())
    available_count = sum(f["available"] for f in families.values())
    confirmed_count = sum(source["available"] is True for source in sources.values())
    supported_count = sum(source["available"] is True and
                          (source["positive"] is True or source.get("evidence_support") == "reasonably_positive")
                          for source in sources.values())
    return {"families": families, "positive_families": positive_count, "available_families": available_count,
            "source_policy_id": CURRENT_OPPORTUNITY_POLICY_ID, "sources": sources,
            **({"source_support": applied_support} if applied_support else {}),
            "available_sources": confirmed_count, "positive_sources": supported_count,
            "source_label": (str(confirmed_count) + " sources with relevant information; no sales guarantee."
                             if confirmed_count else "No current usable relevant source information."),
            "label": ("Weak", "Worth Watching", "Promising Market Signal", "Strong Market Signal")[positive_count]
                     if available_count else "Insufficient evidence"}


def _usable_trends_context(parsed, as_of):
    """Usable observed interest is separate from a rising-demand decision.

    A verified zero or flat chart is still information. Missing intervals,
    unknown scope, malformed points and stale captures are not confirmations.
    No zero chart is turned into either absent demand or positive support.
    """
    if not isinstance(parsed, dict) or parsed.get("state") != "Usable":
        return False
    meta = parsed.get("metadata", {})
    try:
        captured, now = timestamp(meta["collected_at"]), timestamp(as_of)
        if (meta.get("geo") != "IN" or meta.get("time_range") != "today 12-m" or meta.get("property") != "web"
                or meta.get("scope_verified") is not True or not 0 <= (now - captured).total_seconds() <= 7 * 86400):
            return False
        series = parsed["series"]
        if not isinstance(series, list) or not 8 <= len(series) <= 1000:
            return False
        previous = None; completed = []
        for point in series:
            start, end = date.fromisoformat(point["start"]), date.fromisoformat(point["end"])
            interval = point["interval"]
            if (start > end or previous is not None and start != previous + timedelta(days=1)
                    or interval not in ("weekly", "monthly") or type(point.get("partial")) not in (bool, type(None))):
                return False
            if point["state"] == "observed":
                if (type(point["value"]) is not int or not 0 <= point["value"] <= 100
                        or type(point["lower"]) is not int or type(point["upper"]) is not int
                        or point["lower"] != point["value"] or point["upper"] != point["value"]):
                    return False
            elif point["state"] == "bounded_nonzero":
                if (point["value"] is not None or type(point["lower"]) is not int or type(point["upper"]) is not int
                        or point["lower"] != 0 or point["upper"] != 1 or point.get("upper_exclusive") is not True):
                    return False
            else:
                return False
            previous = end
            if point.get("partial") is not True and end < captured.date():
                completed.append(point)
        if len(completed) < 8 or len({point["interval"] for point in completed}) != 1:
            return False
        start, end = date.fromisoformat(completed[0]["start"]), date.fromisoformat(completed[-1]["end"])
        return 300 <= (end - start).days <= 400 and (captured.date() - end).days <= (7 if completed[-1]["interval"] == "weekly" else 32)
    except (KeyError, TypeError, ValueError, SafetyError):
        return False
