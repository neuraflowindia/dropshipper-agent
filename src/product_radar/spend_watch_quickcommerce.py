"""Quick-commerce credit allowance: balance checks, packs, expiry and the wait.

The balance check is the provider's free ``/v1/credits`` read. Used credits
are compared with credits held in the active packs. Purchases are read from
the provider's own pack list (active and inactive, trial packs excluded): each
new pack is known by its purchase time and size, priced from the published
pack table and given the provider's own expiry. A zero balance (or a provider
"credits exhausted" refusal) makes the paid route wait; only a positive
balance from a later free check clears it. After a provider refusal the
balance it reported is not trusted: the wait clears only once the balance
rises above the first reading after the refusal, or a new pack is detected.
Searches, purchases and retries never happen here.
"""

from copy import deepcopy
from datetime import timedelta

from .models import SafetyError, digest, timestamp, utc_now
from .spend_watch import (alert, clear_wait, load_settings, load_state, observe, observe_money, set_wait, writing)

LABEL = "Quick-commerce credits"
REASON = "QUICKCOMMERCE_CREDITS_EXHAUSTED"
PAUSED = "Quick-commerce credits are used up. Paid quick-commerce checks wait until credits are added; free research continues."
RESUMED = "Quick-commerce credits are available again; paid checks can continue."


def _detect_packs(state, alerts, reading, settings, at):
    """New paid packs from the provider's list; the price comes from the published table."""
    detected = state["quickcommerce"].setdefault("detected_packs", [])
    known = {pack["pack_id"] for pack in detected}
    prices = {pack["credits"]: pack["price_inr"] for pack in settings["quickcommerce"]["packs"]}
    for row in reading["packs"] + reading["inactive_packs"]:
        pack_id = digest([row["purchased_at"], row["credits_total"]])[:16]
        if row["is_trial"] or pack_id in known:
            continue
        known.add(pack_id)
        price = prices.get(row["credits_total"])
        detected.append({"pack_id": pack_id, "credits": row["credits_total"], "price_inr": price,
                         "purchased_at": row["purchased_at"], "detected_at": at, "expires_at": row["expires_at"]})
        expiry = f" It expires on {timestamp(row['expires_at']).date().isoformat()}." if row["expires_at"] else ""
        alert(alerts, item="quickcommerce", label=LABEL, kind="notice", period=pack_id, at=at,
              message=(f"A ₹{price} quick-commerce pack ({row['credits_total']:,} credits) was bought.{expiry}" if price else
                       f"A quick-commerce pack of {row['credits_total']:,} credits matches no published price, so its "
                       f"cost is not recorded; please confirm the purchase.{expiry}"))


def _expiry_flags(state, alerts, reading, settings, at):
    warn = timedelta(days=settings["quickcommerce"]["expiry_warning_days"])
    flagged = state["quickcommerce"].setdefault("expiry_flagged", [])
    for row in reading["packs"]:
        if not row["expires_at"] or not row["credits_remaining"]:
            continue
        pack_id = digest([row["purchased_at"], row["expires_at"], row["credits_total"]])[:16]
        if pack_id not in flagged and timestamp(row["expires_at"]) - timestamp(at) <= warn:
            flagged.append(pack_id)
            alert(alerts, item="quickcommerce", label=LABEL, kind="expiry", period=pack_id, at=at,
                  message=f"{row['credits_remaining']:,} quick-commerce credits expire on {timestamp(row['expires_at']).date().isoformat()}.")


def _observe_credits(state, alerts, *, available, used, packs, period, at, expiry):
    held = available + used
    if available == 0:
        used = held  # nothing left to spend is 100%, whatever the pack history says
    text = f"{LABEL}: {used:,} of {held:,} used, {available:,} left"
    text += f" in {packs} active pack(s)" if packs else ""
    text += f"; earliest expiry {timestamp(expiry).date().isoformat()}." if expiry else "."
    return observe(state, alerts, "quickcommerce", label=LABEL, period=period, used=used, allowance=held,
                   unit="credits", at=at, text=text, details={"available": available, "active_packs": packs,
                   "earliest_expiry": expiry})


def record_credit_reading(root, reading, *, as_of=None, settings=None):
    """Save one validated balance check; returns the remaining balance and wait."""
    at = as_of or utc_now(); timestamp(at)
    settings = settings or load_settings()
    if (not isinstance(reading, dict) or type(reading.get("total_available")) is not int
            or not isinstance(reading.get("packs"), list) or not isinstance(reading.get("inactive_packs"), list)):
        raise SafetyError("QUICKCOMMERCE_CREDITS_RESPONSE_INVALID")
    with writing(root, settings) as (state, alerts):
        commerce = state["quickcommerce"]
        _detect_packs(state, alerts, reading, settings, at)
        active = [row for row in reading["packs"] if row["expires_at"] is None or timestamp(row["expires_at"]) > timestamp(at)]
        expiries = sorted(row["expires_at"] for row in active if row["expires_at"])
        period = digest(sorted([row["purchased_at"] or "", row["credits_total"]] for row in active))[:16]
        _observe_credits(state, alerts, available=reading["total_available"],
                         used=sum(row["credits_used"] for row in active), packs=len(active), period=period, at=at,
                         expiry=expiries[0] if expiries else None)
        _expiry_flags(state, alerts, reading, settings, at)
        commerce["last_reading"] = {"at": at, "total_available": reading["total_available"],
                                    "total_used": reading["total_used"], "active_packs": reading["active_packs"],
                                    "period": period}
        commerce.pop("last_check_failure", None)
        refusal = commerce.get("refusal")
        if refusal is not None and refusal["baseline"] is None:
            refusal["baseline"] = reading["total_available"]  # the balance the refusal contradicted
        risen = refusal is None or (reading["total_available"] > refusal["baseline"]
                                    or len(commerce["detected_packs"]) > refusal["packs"])
        if reading["total_available"] == 0:
            set_wait(state, alerts, "quickcommerce", reason=REASON, at=at, label=LABEL, message=PAUSED, source="balance_check")
        elif risen:
            commerce.pop("refusal", None)
            clear_wait(state, alerts, "quickcommerce", at=at, label=LABEL, message=RESUMED)
        observe_money(state, alerts, settings, at)
        return {"available": reading["total_available"], "wait": deepcopy(state["waits"].get("quickcommerce"))}


def check_credits_before_batch(root, client, *, as_of=None, settings=None):
    """The free balance check before a paid batch; a failed check is recorded and raised."""
    try:
        reading = client.credits()
    except SafetyError as error:
        at = as_of or utc_now()
        settings = settings or load_settings()
        with writing(root, settings) as (state, alerts):
            state["quickcommerce"]["last_check_failure"] = {"at": at, "reason": str(error)}
        raise
    return record_credit_reading(root, reading, as_of=as_of, settings=settings)


def quickcommerce_gate(root, client, *, as_of=None, settings=None):
    """Check, then decide: the one call before every paid quick-commerce batch.

    The free balance check always runs, even while the route waits, so a pack
    the owner buys clears the wait at the next batch. A failed check fails
    closed: no paid call is allowed on an unknown balance.
    """
    try:
        result = check_credits_before_batch(root, client, as_of=as_of, settings=settings)
    except SafetyError as error:
        return {"allowed": False, "reason": str(error), "available": None}
    if result["wait"]:
        return {"allowed": False, "reason": result["wait"]["reason"], "available": result["available"]}
    return {"allowed": True, "reason": None, "available": result["available"]}


def record_response_remaining(root, credits_remaining, *, as_of=None, settings=None):
    """A search response's remaining count updates the allowance between checks."""
    at = as_of or utc_now(); timestamp(at)
    if type(credits_remaining) is not int or credits_remaining < 0:
        raise SafetyError("QUICKCOMMERCE_CREDITS_RESPONSE_INVALID")
    settings = settings or load_settings()
    with writing(root, settings) as (state, alerts):
        item = state["items"].get("quickcommerce")
        state["quickcommerce"]["last_remaining"] = {"at": at, "credits_remaining": credits_remaining}
        if item:
            held = int(item["allowance"])
            _observe_credits(state, alerts, available=credits_remaining, used=max(0, held - credits_remaining),
                             packs=item["details"]["active_packs"], period=item["period"], at=at,
                             expiry=item["details"]["earliest_expiry"])
        if credits_remaining == 0:
            set_wait(state, alerts, "quickcommerce", reason=REASON, at=at, label=LABEL, message=PAUSED, source="search_response")
        return deepcopy(state["waits"].get("quickcommerce"))


def record_credits_exhausted(root, *, as_of=None, settings=None):
    """The provider refused a search for lack of credits (HTTP 402)."""
    at = as_of or utc_now(); timestamp(at)
    settings = settings or load_settings()
    with writing(root, settings) as (state, alerts):
        commerce = state["quickcommerce"]
        commerce["refusal"] = {"at": at, "baseline": None, "packs": len(commerce.get("detected_packs", []))}
        return deepcopy(set_wait(state, alerts, "quickcommerce", reason=REASON, at=at, label=LABEL, message=PAUSED,
                                 source="provider_refusal"))


def credit_wait(root):
    """Read-only: the route-wide credits wait, or None.

    This is information, not the gate: while the wait exists the paid route
    must still run the free balance check (``quickcommerce_gate``) before each
    batch, because only a positive balance from that check clears the wait.
    """
    state = load_state(root)
    return deepcopy(state["waits"].get("quickcommerce")) if state else None
