"""Relative-interest arithmetic for verified single-product chart captures."""

from datetime import date, timedelta

from .models import SafetyError, timestamp


def compute_trends_signal(observation, now=None):
    """Return a market family, preserving missing/bounded values and zero rules.

    Optional rising_related_queries must already have same-product relevance
    established by interpretation: query, rising=True, same_product=True and
    reference. The caller supplies related results from this capture (or a
    separately freshness-checked capture); this helper never decides semantic
    relatedness or verifies a supplied source quotation from keywords.
    """
    output = {"state": "Insufficient", "available": False, "positive": None, "used_ids": [],
              "reason": "No usable verified India twelve-month series.", "method": "last-four-versus-previous-four-v1"}
    if not isinstance(observation, dict) or observation.get("state") != "Usable":
        return {**output, "reason": observation.get("reason", output["reason"]) if isinstance(observation, dict) else output["reason"]}
    meta = observation.get("metadata", {})
    try:
        captured = timestamp(meta["collected_at"])
        instant = timestamp(now) if now is not None else captured
        if (meta.get("geo") != "IN" or meta.get("time_range") != "today 12-m" or meta.get("property") != "web"
                or meta.get("scope_verified") is not True):
            return output
        output["used_ids"] = [meta["source_reference"]]
        if instant < captured or instant - captured > timedelta(days=7):
            return {**output, "state": "Stale", "reason": "Capture is outside its seven-day evidence window."}
        series = observation["series"]
        if not isinstance(series, list) or len(series) > 1000:
            return {**output, "reason": "Series contract is invalid; no movement score calculated."}
        last_start = None
        for point in series:
            start, end = date.fromisoformat(point["start"]), date.fromisoformat(point["end"])
            if start > end or (last_start is not None and start <= last_start) or point.get("partial") not in (None, True, False):
                raise ValueError
            last_start = start
            if point["state"] == "observed":
                if type(point["value"]) is not int or not 0 <= point["value"] <= 100 or point["lower"] != point["value"] or point["upper"] != point["value"]:
                    raise ValueError
            elif point["state"] == "bounded_nonzero":
                if point["value"] is not None or point["lower"] != 0 or point["upper"] != 1 or point.get("upper_exclusive") is not True:
                    raise ValueError
            elif point["state"] != "missing" or any(point[k] is not None for k in ("value", "lower", "upper")):
                raise ValueError
        completed = [p for p in series if p.get("partial") is not True and date.fromisoformat(p["end"]) < captured.date()]
        output["excluded_ongoing_intervals"] = len(series) - len(completed)
        if len(completed) < 8:
            return {**output, "reason": "Fewer than eight completed comparable intervals."}
        coverage_days = (date.fromisoformat(completed[-1]["end"]) - date.fromisoformat(completed[0]["start"])).days
        if not 300 <= coverage_days <= 400:
            return {**output, "reason": "Available series does not cover the requested twelve-month period."}
        interval = completed[-1]["interval"]
        if interval not in ("weekly", "monthly") or any(p["interval"] != interval for p in completed):
            return {**output, "reason": "Intervals are not comparable."}
        if (captured.date() - date.fromisoformat(completed[-1]["end"])).days > (7 if interval == "weekly" else 32):
            return {**output, "state": "Stale", "reason": "Latest completed chart interval is not current at capture."}
        windows = completed[-8:]
        for previous, current in zip(windows, windows[1:]):
            if date.fromisoformat(previous["end"]) + timedelta(days=1) != date.fromisoformat(current["start"]):
                return {**output, "reason": "Comparison windows contain an interval gap."}
        if any(p["lower"] is None or p["upper"] is None for p in windows):
            return {**output, "reason": "Comparison windows contain missing interest values."}
        nonzero = sum(p["state"] == "bounded_nonzero" or (p["value"] is not None and p["value"] > 0) for p in completed)
        output["nonzero_points"] = nonzero
        if nonzero < 4:
            return {**output, "reason": "Series has fewer than four nonzero points; zero is not proof of absent demand."}
        previous, recent = windows[:4], windows[4:]
        low_previous, high_previous = (sum(p[field] for p in previous) / 4 for field in ("lower", "upper"))
        low_recent, high_recent = (sum(p[field] for p in recent) / 4 for field in ("lower", "upper"))
        output.update(previous_mean_bounds=[low_previous, high_previous], recent_mean_bounds=[low_recent, high_recent],
                      comparison_intervals=[p["start"] for p in windows])
        related = observation.get("rising_related_queries", [])
        supporting = [q for q in related if isinstance(q, dict) and q.get("rising") is True and q.get("same_product") is True
                      and isinstance(q.get("query"), str) and q["query"] and isinstance(q.get("reference"), str) and q["reference"]]
        if supporting:
            output["related_query_references"] = sorted({q["reference"] for q in supporting})
            output["used_ids"] = sorted(set(output["used_ids"] + output["related_query_references"]))
        if high_previous == 0:
            recent_nonzero = sum(p["state"] == "bounded_nonzero" or p["value"] > 0 for p in recent)
            if recent_nonzero >= 2 and supporting:
                return {**output, "state": "Positive", "available": True, "positive": True,
                        "reason": "Zero previous mean; at least two recent nonzero periods and a same-product rising query."}
            return {**output, "reason": "Zero previous mean needs two recent nonzero periods and supporting rising-query evidence."}
        if supporting:
            return {**output, "state": "Positive", "available": True, "positive": True,
                    "reason": "Supported same-product rising related query satisfies the alternative rule."}
        if low_previous == 0:
            return {**output, "reason": "Bounded values leave the previous mean potentially zero; exact movement cannot be established."}
        ratio_low, ratio_high = low_recent / high_previous, high_recent / low_previous
        increase_low, increase_high = low_recent - high_previous, high_recent - low_previous
        output.update(ratio_bounds=[ratio_low, ratio_high], increase_bounds=[increase_low, increase_high])
        if ratio_low >= 1.15 and increase_low >= 5:
            return {**output, "state": "Positive", "available": True, "positive": True,
                    "reason": "Recent mean is at least 15 percent and five points above the preceding mean."}
        if ratio_high < 1.15 or increase_high < 5:
            return {**output, "state": "Not-positive", "available": True, "positive": False,
                    "reason": "Comparable relative interest does not meet the rising rule; this is not a sales or demand estimate."}
        return {**output, "reason": "The rising decision depends on the unknown exact value of a bounded '<1' point."}
    except (KeyError, TypeError, ValueError, SafetyError, ZeroDivisionError):
        return {**output, "reason": "Series contract is invalid; no movement score calculated."}
