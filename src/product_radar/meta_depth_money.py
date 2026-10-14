"""Money for the deeper Meta read (Plan R10 W11): allowance checks, waits,
the live Apify usage figure and the automatic monthly renewal.

The ledger (``storage.Journal``) is the only gate: nothing here grants money.
``money_check`` applies the ledger's own reservation rule offline. The free
``GET /v2/users/me/limits`` read gives Apify's own monthly cycle, usage limit
and usage so far; it catches use outside this project's ledger and, when an
account month ends, records the new month automatically (same account, same
key, no paid plan) and earmarks it from ``config/research/apify_allocation.json``.
Every wait is calm and named; waits the owner may need to act on become a
plain-language notice in the spend watch after six hours.
"""

from datetime import timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path

from .models import SafetyError, identifier, timestamp, usd

ALLOCATION = Path(__file__).resolve().parents[2] / "config/research/apify_allocation.json"
READ_HOLD_USD = "0.02"
FREE_PLAN_MAX_USD = Decimal("5")
LABEL = "Apify free allowance"
ACCOUNT_RECHECK = timedelta(hours=1)
NOTICE_AFTER = timedelta(hours=6)
WAITS = {"META_DEPTH_ALLOWANCE_USED": None,  # until the account month ends
         "META_DEPTH_PERIOD_RENEWAL_PENDING": timedelta(hours=6),
         "META_DEPTH_EARLIER_RUN_UNSETTLED": timedelta(minutes=10),
         "APIFY_LIMITS_READ_DENIED": timedelta(hours=6),
         "META_DEPTH_ACCOUNT_POOL_REQUIRED": timedelta(hours=6)}
_ALLOWANCE_CODES = {"BUDGET_EXHAUSTED", "CURRENT_BALANCE_INSUFFICIENT", "PRODUCTION_ALLOCATION_EXHAUSTED"}
_UNSETTLED_CODES = {"UNRESOLVED_PROVIDER_ATTEMPT", "PRIOR_PRODUCTION_CONTROLS_UNRESOLVED"}
# Money waits pause the route at once, beside the allowance line.
PAUSES = {
    "META_DEPTH_ALLOWANCE_USED": "The free Apify allowance is used down to its protected US$0.50. Deeper Meta reads "
                                 "wait for the next monthly allowance; free research continues.",
    "META_DEPTH_PERIOD_RENEWAL_PENDING": "The Apify allowance month has ended. Deeper Meta reads wait until the new "
                                         "month's allowance is recorded; free research continues."}
# Other waits become a notice only when they last six hours.
NOTICES = {
    "UNREVIEWED_INPUT": "An earlier Apify job in the spending record no longer matches a reviewed request, so deeper Meta reads are paused. The agent needs to look at it.",
    "RECOVERY_ACCOUNT_MISMATCH": "The saved Apify account no longer matches an earlier job's account, so deeper Meta reads are paused. The agent needs to set up the account list (see the cutover steps).",
    "PRODUCTION_SCOPE_NOT_ADOPTED": "This month's Apify allowance has not been set aside for research yet, so deeper Meta reads are paused.",
    "META_DEPTH_EARLIER_RUN_UNSETTLED": "An earlier Apify job's bill is still unknown, so deeper Meta reads are paused until Apify reports it.",
    "BILLING_REFRESH_DEADLINE_EXCEEDED": "Re-checking earlier Apify bills took too long, so deeper Meta reads are paused. The agent needs to look at it.",
    "APIFY_LIMITS_READ_DENIED": "The saved Apify key may not read the account's monthly usage. Please create a key that can, or tell the agent; until then the new month is not recorded automatically.",
    "META_DEPTH_ACCOUNT_POOL_REQUIRED": "The Apify month has ended and the account list needed to start the new month automatically is not set up. The agent needs to run the cutover step.",
    "APIFY_PAID_PLAN_REFUSED": "The Apify account shows a paid plan's usage limit. Deeper Meta reads are paused: the research only uses the free monthly allowance.",
}
GENERIC_NOTICE = "Deeper Meta reads have been waiting for six hours ({code}). The agent needs to look at it."


class MetaDepthWait(SafetyError):
    """A calm wait: nothing was paid or requested. The source controller keeps
    the question waiting (not failed) until ``next_retry_at``."""

    calm_wait = True

    def __init__(self, reason, *, next_retry_at=None):
        super().__init__(reason)
        self.next_retry_at = next_retry_at


def wait_for(code, *, now, period_ends_at=None):
    reason = ("META_DEPTH_ALLOWANCE_USED" if code in _ALLOWANCE_CODES else
              "META_DEPTH_PERIOD_RENEWAL_PENDING" if code == "PROVIDER_PERIOD_EXPIRED" else
              "META_DEPTH_EARLIER_RUN_UNSETTLED" if code in _UNSETTLED_CODES else code)
    step = WAITS.get(reason, ACCOUNT_RECHECK)
    at = period_ends_at if step is None and period_ends_at else (now + (step or ACCOUNT_RECHECK)).isoformat()
    return MetaDepthWait(reason, next_retry_at=at)


def funded_entries(registry):
    """Enabled accounts in priority order; only a funded one can ever reserve."""
    return sorted((row for row in registry["accounts"] if row["enabled"]), key=lambda row: (row["priority"], row["id"]))


def _bills_only_unknown(journal, namespace, account_scope):
    """True when every unresolved earlier job has finished and lacks only its bill."""
    pending = [a for a in journal.state()[1].values() if (not account_scope or a["namespace"] == namespace)
               and a["submission"] in ("uncertain", "submitted") and (not a["remote_status"] or a["charge"] is None)]
    return bool(pending) and all(a["submission"] == "submitted" and a["remote_status"] for a in pending)


def money_check(root, journal, registry, prepared, *, now, available=None):
    """Offline (no key, no provider): may the funded account reserve this read?

    The ledger's own reservation rule against each enabled account's saved
    binding and dated handover facts (or ``available``, Apify's live figure).
    Returns the account's period facts; ``refresh`` is true when the only
    blockers are finished earlier jobs whose bills are not yet known (the
    controller's bounded bill refresh may clear them). Otherwise raises a
    calm ``MetaDepthWait``.
    """
    from .accounts import _private_reference, load_binding, load_handover
    first = None
    for entry in funded_entries(registry):
        period_ends_at = None
        try:
            binding = load_binding(_private_reference(root, entry["binding"]))
            facts = load_handover(_private_reference(root, entry["handover"]), now)
            period_ends_at = facts["period_ends_at"]
            if facts["period"] != binding.period:
                raise SafetyError("HANDOVER_ACCOUNT_MISMATCH")
            result = {"account_id": entry["id"], "namespace": binding.namespace, "period": binding.period,
                      "period_ends_at": period_ends_at, "refresh": False}
            # Apify's live figure is a current balance; the handover's is the dated opening one.
            current = facts["available_usd"] if available is None else available
            with journal.locked(timeout=5):
                try:
                    journal.check_reservation(prepared, binding, stage="meta", current_available=current,
                                              read_reserve=READ_HOLD_USD, account_scope=registry["pool_enabled"])
                except SafetyError as error:
                    if str(error) != "UNRESOLVED_PROVIDER_ATTEMPT" or not _bills_only_unknown(
                            journal, binding.namespace, registry["pool_enabled"]):
                        raise
                    result["refresh"] = True
            return result
        except SafetyError as error:
            if str(error) == "RUNNER_ALREADY_ACTIVE":
                raise  # a busy ledger is checked again on the next pass, not a long wait
            # Only the funded account is ever used; an unfunded one adds nothing.
            first = first or wait_for(str(error), now=now, period_ends_at=period_ends_at)
    raise first or MetaDepthWait("ACCOUNT_PROFILE_NOT_CONFIGURED", next_retry_at=(now + ACCOUNT_RECHECK).isoformat())


# ----- Apify's own monthly figures ------------------------------------------

def _number(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise SafetyError("APIFY_LIMITS_RESPONSE_INVALID") from None
    if isinstance(value, bool) or not number.is_finite() or number < 0 or number > 100000:
        raise SafetyError("APIFY_LIMITS_RESPONSE_INVALID")
    return number


def read_limits(transport, *, now):
    """Apify's current monthly cycle and usage (official ``GET /v2/users/me/limits``).

    Refuses a paid plan's usage limit. The cycle's last instant (for example
    ``23:59:59.999Z``) closes the period at the next whole second.
    """
    from .http_io import HttpFailure
    try:
        payload = transport.limits()
    except HttpFailure as error:
        if error.status in (401, 403):
            raise SafetyError("APIFY_LIMITS_READ_DENIED") from None
        raise
    try:
        data = payload["data"]
        start, end = timestamp(data["monthlyUsageCycle"]["startAt"]), timestamp(data["monthlyUsageCycle"]["endAt"])
        maximum, used = _number(data["limits"]["maxMonthlyUsageUsd"]), _number(data["current"]["monthlyUsageUsd"])
    except (KeyError, TypeError, SafetyError):
        raise SafetyError("APIFY_LIMITS_RESPONSE_INVALID") from None
    if maximum > FREE_PLAN_MAX_USD:
        raise SafetyError("APIFY_PAID_PLAN_REFUSED")
    ends = (end + timedelta(milliseconds=1)).replace(microsecond=0) if end.microsecond else end
    if not start <= now < ends:
        raise SafetyError("APIFY_LIMITS_CYCLE_INVALID")
    available = max(Decimal(0), maximum - used).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    return {"period": start.date().isoformat() + "_" + ends.date().isoformat(), "period_ends_at": ends.isoformat(),
            "max_usd": str(maximum), "used_usd": str(used), "available_usd": str(available)}


def account_transport(root, registry, transport_factory):
    """The funded account's own transport (its saved key), for free account reads."""
    from .accounts import _private_reference, load_apify_token
    entry = funded_entries(registry)[0] if funded_entries(registry) else None
    if entry is None:
        raise SafetyError("ACCOUNT_PROFILE_NOT_CONFIGURED")
    transport = transport_factory(load_apify_token(_private_reference(root, entry["credential"])))
    transport.deadline = transport.clock() + 60
    return transport


def load_allocation(path=ALLOCATION):
    from .storage import load_json
    return validate_allocation(load_json(path, 8192))


def validate_allocation(value):
    """The monthly earmark settings (also checked when they are backed up)."""
    from .storage import STAGES
    try:
        if (set(value) != {"schema_version", "kind", "reserve_usd", "sources", "note"} or value["schema_version"] != 1
                or value["kind"] != "apify-period-allocation-v1" or usd(value["reserve_usd"]) < Decimal("0.55")
                or not isinstance(value["sources"], dict) or not value["sources"] or not set(value["sources"]) <= set(STAGES)
                or any(usd(cap) <= 0 for cap in value["sources"].values())):
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise SafetyError("APIFY_ALLOCATION_SETTINGS_INVALID") from None
    return value


def renew_period(root, journal, registry, transport_factory, *, now, allocation=None):
    """Record a new Apify month for the same account and key, then earmark it.

    Free reads only. Needs the account list (``accounts.load_account_registry``
    with the pool switched on). The new handover keeps the same-key owner
    confirmations; Apify's own figures give the period, its end and what is
    left. If the binding step refuses, the previous handover is put back.
    """
    from .accounts import _private_reference, bind_registered_account
    from .storage import atomic_json, load_json
    if not registry["pool_enabled"]:
        raise SafetyError("META_DEPTH_ACCOUNT_POOL_REQUIRED")
    entry = funded_entries(registry)[0]
    path = _private_reference(root, entry["handover"])
    old = load_json(path, 8192)
    if not isinstance(old, dict) or old.get("schema_version") != 2 or old.get("plan") != "Free":
        raise SafetyError("HANDOVER_INVALID")
    try:
        limits = read_limits(account_transport(root, registry, transport_factory), now=now)
    except SafetyError as error:
        if str(error) != "APIFY_LIMITS_CYCLE_INVALID":
            raise
        raise SafetyError("PROVIDER_PERIOD_EXPIRED") from None  # Apify has not started the new month yet
    if limits["period"] == old["period"]:
        raise SafetyError("PROVIDER_PERIOD_EXPIRED")
    settings = allocation or load_allocation()
    room = max(Decimal(0), usd(limits["available_usd"]) - usd(settings["reserve_usd"]))
    shares, total = {}, Decimal(0)
    for source, cap in sorted(settings["sources"].items()):
        shares[source] = min(usd(cap), room - total).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        total += shares[source]
    handover = dict(old, period=identifier(limits["period"]), period_ends_at=limits["period_ends_at"],
                    available_usd=limits["available_usd"], balance_checked_at=now.isoformat(),
                    production_provision_usd=str(total))
    atomic_json(path, handover)
    try:
        binding = bind_registered_account(root, journal, entry["id"], transport_factory, now=now, registry=registry)
    except SafetyError:
        atomic_json(path, old)
        raise
    decision = "auto-" + binding.period
    with journal.locked(timeout=5):
        journal.apply_policy(binding.namespace, str(total), decision)
        journal.adopt_production_scope(binding.namespace, {k: str(v) for k, v in shares.items()},
            "Automatic earmark of this month's free Apify allowance (Plan R10 W11).", decision)
    return {"namespace": binding.namespace, "period": binding.period, "allocations": {k: str(v) for k, v in shares.items()},
            "available_usd": limits["available_usd"]}


# ----- the spend watch -------------------------------------------------------

def record_pause(spend_root, wait, *, at):
    """A money wait beside the Apify allowance line (one alert per new wait)."""
    from .spend_watch import load_settings, set_wait, writing
    with writing(spend_root, load_settings()) as (state, alerts):
        set_wait(state, alerts, "apify", reason=str(wait), at=at, label=LABEL, message=PAUSES[str(wait)],
                 next_retry_at=wait.next_retry_at)


def note_hold(spend_root, reason, *, at):
    """Remember a non-money wait; once it has lasted six hours, notify once."""
    from .spend_watch import alert, load_settings, writing
    with writing(spend_root, load_settings()) as (state, alerts):
        hold = state["money"].get("apify_hold")
        if not hold or hold["reason"] != reason:
            hold = state["money"]["apify_hold"] = {"reason": reason, "since": at, "noticed": False}
        if not hold["noticed"] and timestamp(at) - timestamp(hold["since"]) >= NOTICE_AFTER:
            hold["noticed"] = True
            alert(alerts, item="apify", label=LABEL, kind="notice", period=reason + "@" + hold["since"], at=at,
                  message=NOTICES.get(reason, GENERIC_NOTICE.format(code=reason)))


def clear(spend_root, *, at, settled=False):
    """The allowance has room: end the money wait; once a read is admitted
    (``settled``) also forget a held reason, so its six-hour clock restarts."""
    from .spend_watch import clear_wait, load_settings, load_state, writing
    state = load_state(spend_root)
    if state and ("apify" in state["waits"] or settled and "apify_hold" in state["money"]):
        with writing(spend_root, load_settings()) as (state, alerts):
            if settled:
                state["money"].pop("apify_hold", None)
            clear_wait(state, alerts, "apify", at=at, label=LABEL,
                       message="The Apify allowance has room again; deeper Meta reads continue.")


def notice_once(spend_root, reason, *, at, period):
    """A one-time owner notice (for example a key that cannot read usage)."""
    from .spend_watch import alert, load_settings, writing
    with writing(spend_root, load_settings()) as (state, alerts):
        sent = state["money"].setdefault("apify_notices", [])
        key = reason + "@" + period
        if key not in sent:
            sent.append(key)
            alert(alerts, item="apify", label=LABEL, kind="notice", period=key, at=at,
                  message=NOTICES.get(reason, GENERIC_NOTICE.format(code=reason)))


def show(spend_root, *, at, journal=None, wait=None, proceed=False, settled=False):
    """Keep the allowance lines, waits and notices current; a display problem never moves money."""
    from .operations import record_error
    from .spend_watch import refresh_ledger_allowances
    try:
        if wait is not None:
            if str(wait) in PAUSES:
                record_pause(spend_root, wait, at=at)
            else:
                note_hold(spend_root, str(wait), at=at)
        if proceed or settled:
            clear(spend_root, at=at, settled=settled)
        if journal is not None:
            refresh_ledger_allowances(spend_root, journal, as_of=at)
    except (SafetyError, OSError) as error:
        record_error("spend_watch_refresh_failed", error, stage="acquire", route="meta-ads-depth")
