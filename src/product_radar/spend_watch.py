"""Spend and allowance watch: one saved record per allowance item (Plan R10 W12).

Readers use saved records only and never call a provider or write. Writers
flag 50, 75, 90 and 100 percent of an allowance once per threshold per
allowance period, appending each flag to a dated alert log. At 100 percent a
money route (Apify, quick-commerce credits) waits while free work continues.
AI usage windows only ever flag (owner decision A4); the separate paid-credits
guard is in ``spend_watch_ai``. Account identifiers and keys are never kept.
"""

from contextlib import contextmanager
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import SafetyError, canonical, decode_json, digest, timestamp, usd, utc_now
from .runtime_storage import runtime_path
from .storage import (PERIOD_CAP, PERIOD_GRANT, PROTECTED_BUFFER, atomic_json, file_lock, load_json,
                      renewed_read_commitment, safe_path)

VERSION = "spend-watch-v1"
SETTINGS = Path(__file__).resolve().parents[2] / "config/research/spend_watch.json"
AREA = "data/research/captured/spend_watch"
THRESHOLDS = (50, 75, 90, 100)
# Saved waits. The Apify ledger itself refuses a reservation past the protected
# buffer; the deeper Meta read (meta_depth) records its resulting wait here.
WAIT_ROUTES = ("quickcommerce", "ai", "apify")
MAX_STATE_BYTES = 1024 * 1024
MAX_ALERT_FILE_BYTES = 2 * 1024 * 1024
ALERT_DAYS_READ = 14
_STATE_FIELDS = {"schema_version", "kind", "items", "waits", "money", "quickcommerce", "ai"}
_ALERT_KINDS = {"threshold", "paused", "resumed", "review", "expiry", "notice"}


def _inr(value, *, positive=False):
    if not isinstance(value, str):
        raise SafetyError("SPEND_WATCH_SETTINGS_INVALID")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise SafetyError("SPEND_WATCH_SETTINGS_INVALID") from None
    if (not number.is_finite() or number < 0 or positive and number == 0 or number > 10000000
            or number.as_tuple().exponent < -2):
        raise SafetyError("SPEND_WATCH_SETTINGS_INVALID")
    return number


def validate_settings(value):
    """Recorded plan facts and published prices; no secret or account field."""
    try:
        money, commerce, hosting = value["money"], value["quickcommerce"], value["hosting"]
        packs = commerce["packs"]
        if (set(value) != {"schema_version", "kind", "money", "quickcommerce", "hosting"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["kind"] != "spend-watch-settings-v1"
                or set(money) != {"monthly_target_inr", "review_above_inr", "usd_inr_rate", "rate_note", "timezone"}
                or money["timezone"] != "Asia/Kolkata" or not isinstance(money["rate_note"], str)
                or not 1 <= len(money["rate_note"]) <= 300
                or set(commerce) != {"packs", "expiry_warning_days", "prices_checked_on"}
                or type(commerce["expiry_warning_days"]) is not int or not 0 <= commerce["expiry_warning_days"] <= 30
                or not isinstance(packs, list) or not 1 <= len(packs) <= 10
                or any(not isinstance(pack, dict) or set(pack) != {"price_inr", "credits"}
                       or type(pack["credits"]) is not int or not 1 <= pack["credits"] <= 10000000 for pack in packs)
                or [pack["credits"] for pack in packs] != sorted({pack["credits"] for pack in packs})
                or set(hosting) != {"provider", "plan", "budget_alert", "provider_polling"}
                or hosting["provider_polling"] is not False
                or any(not isinstance(hosting[field], str) or not 1 <= len(hosting[field]) <= 200
                       for field in ("provider", "plan", "budget_alert"))):
            raise SafetyError("SPEND_WATCH_SETTINGS_INVALID")
        target, review = _inr(money["monthly_target_inr"], positive=True), _inr(money["review_above_inr"], positive=True)
        _inr(money["usd_inr_rate"], positive=True)
        if review < target:
            raise SafetyError("SPEND_WATCH_SETTINGS_INVALID")
        for pack in packs:
            _inr(pack["price_inr"], positive=True)
        date.fromisoformat(commerce["prices_checked_on"])
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SafetyError("SPEND_WATCH_SETTINGS_INVALID") from None
    return deepcopy(value)


def load_settings(path=SETTINGS):
    return validate_settings(load_json(path, 16384))


def _folder(root):
    return safe_path(runtime_path(root, AREA))


def _empty():
    return {"schema_version": 1, "kind": VERSION, "items": {}, "waits": {}, "money": {},
            "quickcommerce": {}, "ai": {}}


def _validate_state(state):
    if (not isinstance(state, dict) or set(state) != _STATE_FIELDS or state["schema_version"] != 1
            or state["kind"] != VERSION or any(not isinstance(state[field], dict) for field in _STATE_FIELDS - {"schema_version", "kind"})
            or not set(state["waits"]) <= set(WAIT_ROUTES)):
        raise SafetyError("SPEND_WATCH_STATE_INVALID")
    for item in state["items"].values():
        if (not isinstance(item, dict) or not isinstance(item.get("flags"), list)
                or not set(item["flags"]) <= set(THRESHOLDS) or not isinstance(item.get("text"), str)):
            raise SafetyError("SPEND_WATCH_STATE_INVALID")
    for wait in state["waits"].values():
        if not isinstance(wait, dict) or not isinstance(wait.get("reason"), str):
            raise SafetyError("SPEND_WATCH_STATE_INVALID")
        timestamp(wait.get("since"))
    return state


def load_state(root):
    """Saved state, or None before the first record; never creates a folder."""
    path = runtime_path(root, AREA, "state.json")
    if not path.exists() and not path.is_symlink():
        return None
    wrapper = load_json(path, MAX_STATE_BYTES)
    if (not isinstance(wrapper, dict) or set(wrapper) != {"state", "hash"}
            or not isinstance(wrapper["state"], dict) or digest(wrapper["state"]) != wrapper["hash"]):
        raise SafetyError("SPEND_WATCH_STATE_INVALID")
    return _validate_state(wrapper["state"])


def _append_alert(folder, alert, zone):
    day = timestamp(alert["at"]).astimezone(ZoneInfo(zone)).date().isoformat()
    directory = safe_path(folder, "alerts")
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(directory / (day + ".jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o644)
    try:
        encoded = canonical(alert) + b"\n"
        if os.write(descriptor, encoded) != len(encoded):
            raise SafetyError("SPEND_WATCH_ALERT_WRITE_FAILED")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def writing(root, settings):
    """One locked read-change-write; alerts are logged before the state saves.

    Alert ids are deterministic, so the rare repeat after a crash between the
    two writes is recognisable, while a flag can never be saved unlogged.
    """
    folder = _folder(root)
    # The shared local busy code: callers already treat it as "try again".
    with file_lock(folder / "runner.lock", 5, "RUNNER_ALREADY_ACTIVE"):
        state = load_state(root) or _empty()
        alerts = []
        yield state, alerts
        _validate_state(state)
        for alert in alerts:
            _append_alert(folder, alert, settings["money"]["timezone"])
        atomic_json(folder / "state.json", {"state": state, "hash": digest(state)})


def alert(alerts, *, item, label, kind, at, message, period=None, threshold=None, percent=None):
    if kind not in _ALERT_KINDS:
        raise SafetyError("SPEND_WATCH_ALERT_INVALID")
    row = {"item": item, "label": label, "kind": kind, "period": period, "threshold": threshold,
           "percent": percent, "at": at, "message": message}
    row["alert_id"] = digest([item, kind, period, threshold, None if kind in {"threshold", "expiry", "notice"} else at])[:24]
    alerts.append(row)
    return row


def percent_used(used, allowance):
    used, allowance = Decimal(used), Decimal(allowance)
    if allowance <= 0:
        return 100
    return int((used * 100 / allowance).to_integral_value(rounding=ROUND_FLOOR))


def observe(state, alerts, key, *, label, period, used, allowance, unit, at, text, details=None,
            flag_message=None):
    """Save one reading and flag each newly crossed threshold once per period."""
    percent = percent_used(used, allowance)
    old = state["items"].get(key)
    flags = list(old["flags"]) if old and old.get("period") == period else []
    for threshold in THRESHOLDS:
        if percent >= threshold and threshold not in flags:
            flags.append(threshold)
            alert(alerts, item=key, label=label, kind="threshold", period=period, threshold=threshold, percent=percent,
                  at=at, message=(flag_message or "{label}: {percent}% used (passed {threshold}%).").format(
                      label=label, percent=percent, threshold=threshold))
    state["items"][key] = {"label": label, "period": period, "used": str(used), "allowance": str(allowance),
                           "unit": unit, "percent": percent, "flags": sorted(flags), "updated_at": at,
                           "text": text, "details": details or {}}
    return percent


def set_wait(state, alerts, route, *, reason, at, label, message, **fields):
    if route not in WAIT_ROUTES:
        raise SafetyError("SPEND_WATCH_ROUTE_INVALID")
    old = state["waits"].get(route)
    if old and old["reason"] == reason and all(old.get(name) == value for name, value in fields.items()):
        return old
    state["waits"][route] = {"reason": reason, "since": old["since"] if old and old["reason"] == reason else at,
                             **fields}
    if not old or old["reason"] != reason:
        alert(alerts, item=route, label=label, kind="paused", at=at, message=message)
    return state["waits"][route]


def clear_wait(state, alerts, route, *, at, label, message):
    if state["waits"].pop(route, None) is not None:
        alert(alerts, item=route, label=label, kind="resumed", at=at, message=message)


def money_route_wait(root, route):
    """Read-only: the saved wait of a money route ("quickcommerce", "ai" or "apify"), or None."""
    if route not in WAIT_ROUTES:
        raise SafetyError("SPEND_WATCH_ROUTE_INVALID")
    state = load_state(root)
    return deepcopy(state["waits"].get(route)) if state else None


def _month(at, settings):
    return timestamp(at).astimezone(ZoneInfo(settings["money"]["timezone"])).strftime("%Y-%m")


def _attempt_commitment(attempt):
    actor = usd(attempt["cap"]) if attempt["charge"] is None else usd(attempt["charge"])
    reads = Decimal(0) if attempt["charge"] is None else usd(
        attempt["read_charge"] if attempt["read_charge"] is not None else attempt["read_hold"])
    return actor + reads


def apify_usage(events, financial_state, settings):
    """Per current account period: charges plus unsettled holds against 4.50.

    The current period of an account is its latest saved budget. Holds count
    in full (an unknown charge is not a zero bill). Money is assigned to the
    month its reservation was saved, in the owner's time zone.
    """
    budgets, attempts = financial_state
    latest, months = {}, {}
    for event in events:
        data = event["data"]
        if event["kind"] == "budget":
            latest[data["namespace"].split(":")[0]] = data["namespace"]
        elif event["kind"] in {"reserved", "reserved_production"}:
            months[data["attempt"]] = _month(event["at"], settings)
        elif event["kind"] == "renewed_read_reserved":
            months[(data["attempt"], data["read_id"])] = _month(event["at"], settings)
    by_month = {}
    for attempt_id, attempt in attempts.items():
        month = months.get(attempt_id)
        if month is None:
            raise SafetyError("SPEND_WATCH_LEDGER_INVALID")
        by_month[month] = by_month.get(month, Decimal(0)) + _attempt_commitment(attempt)
        for read in attempt.get("renewed_reads", {}).values():
            read_month = months.get((attempt_id, read["read_id"]))
            if read_month is None:
                raise SafetyError("SPEND_WATCH_LEDGER_INVALID")
            by_month[read_month] = by_month.get(read_month, Decimal(0)) + usd(read["charge"] if read["charge"] is not None else read["cap"])
    current = []
    for namespace in sorted(latest.values()):
        budget = budgets[namespace]
        usable = max(Decimal(0), min(PERIOD_CAP, min(usd(budget["allowance"]), PERIOD_GRANT) - PROTECTED_BUFFER))
        used = sum((_attempt_commitment(a) for a in attempts.values() if a["namespace"] == namespace), Decimal(0))
        used += renewed_read_commitment(attempts, namespace)
        current.append({"namespace": namespace, "period": namespace.split(":")[1], "used_usd": used, "usable_usd": usable})
    return {"current": current, "by_month": {month: str(value) for month, value in sorted(by_month.items())}}


def _ledger(journal):
    if journal._locked:
        return journal.events(), journal.state()
    with journal.locked(timeout=5):
        return journal.events(), journal.state()


def _rupees(value):
    return format(Decimal(value).quantize(Decimal("1")), ",f")


def observe_money(state, alerts, settings, at):
    """Monthly rupees: Apify use at the recorded rate plus detected packs."""
    money, month = settings["money"], _month(at, settings)
    apify_usd = Decimal(state["money"].get("apify_usd_by_month", {}).get(month, "0"))
    packs = [pack for pack in state["quickcommerce"].get("detected_packs", [])
             if _month(pack.get("purchased_at") or pack["detected_at"], settings) == month]
    unknown = sum(1 for pack in packs if pack["price_inr"] is None)
    total = apify_usd * Decimal(money["usd_inr_rate"]) + sum((Decimal(p["price_inr"]) for p in packs if p["price_inr"] is not None), Decimal(0))
    target, review = Decimal(money["monthly_target_inr"]), Decimal(money["review_above_inr"])
    text = f"Money this month: ₹{_rupees(total)} of the ₹{_rupees(target)} target."
    if unknown:
        text += f" {unknown} credit purchase(s) have no recorded price."
    observe(state, alerts, "money-inr", label="Money this month", period=month, used=total, allowance=target,
            unit="INR", at=at, text=text, details={"apify_usd": str(apify_usd), "usd_inr_rate": money["usd_inr_rate"],
            "rate_note": money["rate_note"], "packs": len(packs), "packs_without_price": unknown})
    reviewed = state["money"].setdefault("review_flagged", [])
    if total > review and month not in reviewed:
        reviewed.append(month)
        alert(alerts, item="money-inr", label="Money this month", kind="review", period=month, at=at,
              message=f"Money this month is ₹{_rupees(total)}, above ₹{_rupees(review)}: please review spending.")


def refresh_ledger_allowances(root, journal, *, as_of=None, settings=None):
    """Apify allowance and monthly money from the local ledger; no provider call."""
    at = as_of or utc_now(); timestamp(at)
    settings = settings or load_settings()
    events, financial = _ledger(journal)
    usage = apify_usage(events, financial, settings)
    with writing(root, settings) as (state, alerts):
        keys = set()
        for index, row in enumerate(usage["current"], 1):
            key = "apify-" + digest(row["namespace"])[:16]
            label = "Apify free allowance" + (f" (account {index})" if len(usage["current"]) > 1 else "")
            used, usable = row["used_usd"], row["usable_usd"]
            percent = percent_used(used, usable)
            text = f"{label}: US${used:.2f} of US${usable:.2f} used ({percent}%)."
            if percent >= 100:
                text += " Apify reads wait for the next monthly allowance; free research continues."
            observe(state, alerts, key, label=label, period=digest(row["namespace"])[:16], used=used, allowance=usable,
                    unit="USD", at=at, text=text, details={"period": row["period"]})
            keys.add(key)
        for key in [key for key in state["items"] if key.startswith("apify-") and key not in keys]:
            del state["items"][key]  # an ended account period is no longer an allowance
        state["money"]["apify_usd_by_month"] = usage["by_month"]
        observe_money(state, alerts, settings, at)
        return {"apify_periods": len(keys), "alerts": len(alerts)}


def _read_alerts(root, *, after=None, limit=20):
    folder = runtime_path(root, AREA, "alerts")
    if not folder.is_dir() or folder.is_symlink():
        return []
    safe_path(folder)
    rows = []
    for path in sorted(folder.glob("*.jsonl"))[-ALERT_DAYS_READ:]:
        if path.is_symlink():
            raise SafetyError("SYMLINK_PATH_REFUSED")
        with path.open("rb") as handle:
            raw = handle.read(MAX_ALERT_FILE_BYTES + 1)
        if len(raw) > MAX_ALERT_FILE_BYTES:
            raise SafetyError("SPEND_WATCH_ALERT_LOG_TOO_LARGE")
        for line in raw.splitlines():
            row = decode_json(line)
            if not isinstance(row, dict) or not isinstance(row.get("alert_id"), str):
                raise SafetyError("SPEND_WATCH_ALERT_LOG_INVALID")
            if after is None or timestamp(row["at"]) >= timestamp(after):
                rows.append(row)
    return rows[-limit:]


def allowance_summary(root, *, as_of=None, alerts_after=None, alert_limit=20, settings=None):
    """Owner lines from saved records only: no provider call, model call or write.

    ``alerts_after`` returns alerts at or after that time; a reader that shows
    notifications de-duplicates them by ``alert_id``.
    """
    at = as_of or utc_now(); timestamp(at)
    if alerts_after is not None:
        timestamp(alerts_after)
    if type(alert_limit) is not int or not 1 <= alert_limit <= 200:
        raise SafetyError("SPEND_WATCH_SUMMARY_INVALID")
    settings = settings or load_settings()
    state = load_state(root)
    hosting = settings["hosting"]
    lines = [{"item": key, "label": item["label"], "percent": item["percent"], "flags": item["flags"],
              "unit": item["unit"], "text": item["text"], "updated_at": item["updated_at"]}
             for key, item in sorted((state or _empty())["items"].items())]
    lines.append({"item": "hosting", "label": f"{hosting['provider']} hosting", "percent": None, "flags": [],
                  "unit": None, "updated_at": None,
                  "text": f"{hosting['provider']} hosting: {hosting['plan']}; {hosting['budget_alert']} (not polled)."})
    waits = [{"route": route, **deepcopy(wait)} for route, wait in sorted((state or _empty())["waits"].items())]
    return {"schema_version": 1, "kind": "spend-watch-summary-v1",
            "state": "recorded" if state else "not_recorded", "checked_at": at, "lines": lines, "waits": waits,
            "ai_paused": any(row["route"] == "ai" for row in waits),
            "alerts": _read_alerts(root, after=alerts_after, limit=alert_limit)}
