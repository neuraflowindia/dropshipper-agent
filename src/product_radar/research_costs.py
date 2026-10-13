"""Exact saved costs and conditional yield forecasts, never invented bills."""

from collections import Counter
from decimal import Decimal, ROUND_CEILING

from .models import SafetyError, identifier, usd


def _attempt_totals(attempt):
    """Exact money for one canonical attempt without copying renewal rows."""
    if not isinstance(attempt, dict) or not isinstance(attempt.get("renewed_reads", {}), dict):
        raise SafetyError("COST_ATTEMPT_INVALID")
    actor, reads = attempt["charge"], attempt["read_charge"]
    known = usd(actor if actor is not None else "0") + usd(reads if reads is not None else "0")
    held = (usd(attempt["actor_cap"]) if actor is None else Decimal(0)) + (
        usd(attempt["read_hold"]) if reads is None else Decimal(0))
    complete, count = actor is not None and reads is not None, 0
    for renewal in attempt.get("renewed_reads", {}).values():
        if not isinstance(renewal, dict): raise SafetyError("COST_RENEWED_READ_INVALID")
        charge = renewal["charge"]; count += 1
        if charge is None:
            held += usd(renewal["cap"]); complete = False
        else:
            known += usd(charge)
    return known, held, complete, count


def collection_cost_totals(financial_state, attempt_ids=None):
    """Aggregate a verified saved pair without a lifetime-size reporting cap.

    This does not read a journal or allocate a second all-history row list.
    The existing Journal caller has already verified canonical history and
    supplies its full (budgets, attempts) pair. Each individual monetary value
    remains validated; old and failed attempts and unknown renewals stay in
    the totals. This is a memory-light reduction, not a new storage engine:
    Journal.state() still supplies the current full pair.
    """
    if (not isinstance(financial_state, (list, tuple)) or len(financial_state) != 2
            or any(not isinstance(value, dict) for value in financial_state)):
        raise SafetyError("COST_LEDGER_INVALID")
    attempts = financial_state[1]
    ids = attempts if attempt_ids is None else dict.fromkeys(attempt_ids)
    known, held, complete, count, renewals = Decimal(0), Decimal(0), True, 0, 0
    sources = Counter()
    for attempt_id in ids:
        identifier(attempt_id)
        if attempt_id not in attempts: raise SafetyError("COST_ATTEMPT_NOT_SAVED")
        attempt = attempts[attempt_id]
        actual, pending, settled, renewed_count = _attempt_totals(attempt)
        source = attempt["stage"]
        if not isinstance(source, str): raise SafetyError("COST_ATTEMPT_SOURCE_INVALID")
        known += actual; held += pending; complete = complete and settled
        count += 1; renewals += renewed_count; sources[source] += 1
    return {"physical_acquisitions": count, "renewed_read_operations_count": renewals,
        "acquisitions_by_source": dict(sorted(sources.items())), "known_actual_usd": str(known),
        "unsettled_hold_usd": str(held), "accounting_complete": complete,
        "note": "All verified saved original attempts count once, including failed collections and later dataset reads. Unknown charges remain holds, not zero bills. No lifetime record-count limit is applied; AI and hosting are separate."}


def collection_costs(journal, attempt_ids=None):
    """Charge each physical acquisition once, including failed collections."""
    _, attempts = journal.state()
    ids = list(attempts) if attempt_ids is None else list(dict.fromkeys(attempt_ids))
    for attempt_id in ids:
        identifier(attempt_id)
        if attempt_id not in attempts:
            raise SafetyError("COST_ATTEMPT_NOT_SAVED")
    rows = []
    for attempt_id in ids:
        attempt = attempts[attempt_id]
        actor, reads = attempt["charge"], attempt["read_charge"]
        actual, hold, _, _ = _attempt_totals(attempt)
        renewed = list(attempt.get("renewed_reads", {}).values())
        rows.append({"attempt_id": attempt_id, "namespace": attempt["namespace"], "generation": attempt["generation"],
                     "source": attempt["stage"], "actor_actual_usd": actor, "read_actual_usd": reads,
                     "renewed_read_operations": [{"read_id": row["read_id"], "namespace": row["namespace"],
                         "generation": row["generation"], "actual_usd": row["charge"],
                         "unsettled_hold_usd": row["cap"] if row["charge"] is None else "0"} for row in renewed],
                     "known_actual_usd": str(actual),
                     "unsettled_hold_usd": str(hold), "submission": attempt["submission"],
                     "remote_status": attempt["remote_status"], "capture_status": attempt["capture_status"]})
    return {"physical_acquisitions": len(ids), "rows": rows,
            "known_actual_usd": str(sum((Decimal(row["known_actual_usd"]) for row in rows), Decimal(0))),
            "unsettled_hold_usd": str(sum((Decimal(row["unsettled_hold_usd"]) for row in rows), Decimal(0))),
            "accounting_complete": all(row["actor_actual_usd"] is not None and row["read_actual_usd"] is not None
                and all(read["actual_usd"] is not None for read in row["renewed_read_operations"]) for row in rows),
            "note": "Saved actual collection costs include failed attempts. Holds are not bills; AI, direct-source computer effort and hosting are separate."}


def yield_forecast(*, baseline_collection_usd, baseline_candidates, new_products_per_report, report_count,
                   yields=("0.07051282", "0.05", "0.03"), separately_estimated_usd=None):
    """Explicit sensitivity math; the historical sample is not a guarantee.

    Per-candidate collection effort is held constant as an assumption; lower
    useful yield increases required candidates and cost. Shared/replayed
    captures are not free new inventory. No available-credit quota is treated
    as evidence that this workload will fit.
    """
    counts = (baseline_candidates, new_products_per_report, report_count)
    if any(type(number) is not int or number < 1 or number > 100000 for number in counts):
        raise SafetyError("YIELD_FORECAST_COUNTS_INVALID")
    if not isinstance(yields, (list, tuple)) or not 1 <= len(yields) <= 10:
        raise SafetyError("YIELD_FORECAST_RATES_INVALID")
    baseline = usd(baseline_collection_usd)
    cost_per_candidate = baseline / baseline_candidates
    rows = []
    for raw in yields:
        rate = usd(raw)
        if rate <= 0 or rate > 1:
            raise SafetyError("YIELD_FORECAST_RATES_INVALID")
        required = int((Decimal(new_products_per_report) / rate).to_integral_value(rounding=ROUND_CEILING))
        per_report = cost_per_candidate * required
        monthly = per_report * report_count
        extra = None if separately_estimated_usd is None else usd(separately_estimated_usd)
        rows.append({"yield_fraction": str(rate), "candidates_per_report": required,
                     "candidates_total": required * report_count,
                     "collection_per_report_usd": format(per_report.quantize(Decimal("0.00000001"), rounding=ROUND_CEILING), "f"),
                     "collection_total_usd": format(monthly.quantize(Decimal("0.00000001"), rounding=ROUND_CEILING), "f"),
                     "separate_cost_estimate_usd": None if extra is None else str(extra),
                     "all_in_estimate_usd": None if extra is None else format((monthly + extra).quantize(Decimal("0.00000001"), rounding=ROUND_CEILING), "f")})
    return {"baseline_collection_usd": str(baseline), "baseline_candidates": baseline_candidates,
            "new_products_per_report": new_products_per_report, "report_count": report_count,
            "scenarios": rows,
            "note": "These are collection-only conditional estimates, not bills or promised yield. Failed/duplicate work must remain in the measured baseline. New source mix, direct-source work, AI and hosting can change the result."}
