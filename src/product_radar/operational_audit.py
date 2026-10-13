"""Small saved-record cost/time audit; no billing polls or external calls.

Collection charges are taken from the original ledger and keep unsettled
allowances separate. AI token receipts and durations describe recorded effort,
not a new invoice. A duration sum is not elapsed programme time where actions
overlap, and an internal report's readiness is not measured product yield.
"""

from collections import Counter
import math
from pathlib import Path

from .models import SafetyError, digest, timestamp, utc_now
from .research_costs import collection_costs
from .storage import load_json, safe_path


_TOKENS = ("input_tokens", "cached_input_tokens", "output_tokens")
MAX_RECEIPT_BYTES = 16 * 1024 * 1024


def _optional(path):
    if not path.exists():
        return None
    try:
        value = load_json(path, MAX_RECEIPT_BYTES)
    except SafetyError:
        return {"audit_read_failed": True}
    return value if isinstance(value, dict) else {"audit_read_failed": True}


def _usage(value):
    if (not isinstance(value, dict) or any(type(value.get(field)) is not int or value[field] < 0 for field in _TOKENS)
            or value["cached_input_tokens"] > value["input_tokens"]):
        return None
    return {field: value[field] for field in _TOKENS}


def _duration(started, ended):
    if started is None or ended is None:
        return None
    try:
        seconds = (timestamp(ended) - timestamp(started)).total_seconds()
        return seconds if seconds >= 0 else None
    except (SafetyError, TypeError, ValueError):
        return None


def model_effort(memory, state):
    """Each saved job/attempt is counted once, including failed responses."""
    rows = []
    for job_id, job in sorted(state["jobs"].items()):
        request = job.get("request", {})
        base = safe_path(memory.root, "ai_jobs", job_id)
        folders = [("initial", base)]
        if base.exists():
            folders.extend((path.name, path) for path in sorted(base.glob("retry-*"))
                           if path.is_dir() and path.name[6:].isdigit())
        for attempt, folder in folders:
            intent = _optional(folder / "intent.json")
            execution = _optional(folder / "runner_execution.json")
            result = _optional(folder / "result.json")
            completion = _optional(folder / "completion.json")
            failure = _optional(folder / "failure.json")
            if not any(value is not None for value in (intent, execution, result, completion, failure)):
                continue  # A prepared job is not proof of an execution.
            receipt = result if result is not None else completion
            bound = (result is None or result.get("request_hash") == digest(request))
            if intent is not None and intent.get("request") != request:
                bound = False
            if execution is not None and (execution.get("request_hash") != digest(request)
                                           or execution.get("job_id") != job_id or execution.get("attempt") != attempt):
                bound = False
            if any(value is not None and value.get("audit_read_failed") for value in (intent, execution, result, completion, failure)):
                bound = False
            usage = _usage(receipt.get("usage")) if receipt is not None and bound else None
            started = execution.get("started_at") if execution is not None else None
            ended = receipt.get("completed_at") if receipt is not None else None
            if ended is None and failure is not None:
                ended = failure.get("at")
            # A foreign/unreadable receipt cannot supply either tokens or the
            # end of this attempt's elapsed time. Preserve valid legacy times.
            duration = _duration(started, ended) if bound else None
            state_name = ("receipt_needs_review" if not bound else "response_saved" if receipt is not None
                          else "failed" if failure is not None else "execution_or_reconciliation_pending")
            rows.append({"job_id": job_id, "attempt": attempt, "role": request.get("role"), "state": state_name,
                         "execution_receipt_saved": execution is not None and bound,
                         "response_saved": receipt is not None and bound,
                         "usage": usage, "duration_seconds": duration,
                         "failure_reason": failure.get("code") if failure is not None else None})
    measured = [row["usage"] for row in rows if row["usage"] is not None]
    durations = [row["duration_seconds"] for row in rows if row["duration_seconds"] is not None]
    return {"saved_job_attempts": len(rows), "rows": rows, "states": dict(Counter(row["state"] for row in rows)),
            "usage_receipts": len(measured), "attempts_without_usable_token_counts": len(rows) - len(measured),
            "recorded_tokens": {field: sum(value[field] for value in measured) for field in _TOKENS},
            "duration_receipts": len(durations), "recorded_duration_sum_seconds": round(sum(durations), 3),
            "incremental_model_cost_usd": None,
            "note": "Saved tokens and response times are measured effort, not an inferred bill. Missing token/time records remain unknown. Reusing a saved response does not create another attempt."}


def direct_capture_effort(receipts):
    """Audit explicit canonical direct receipts, deduplicating their IDs.

    This does not infer all historical direct-source work from folder names.
    The caller supplies actual receipt dictionaries or exact receipt paths.
    """
    rows, originals, unrecognised, invalid = [], {}, 0, []
    for supplied in receipts:
        try:
            receipt = supplied if isinstance(supplied, dict) else load_json(Path(supplied), MAX_RECEIPT_BYTES)
        except (SafetyError, TypeError, ValueError):
            invalid.append({"reason": "DIRECT_AUDIT_RECEIPT_UNREADABLE"}); continue
        if (not isinstance(receipt, dict) or receipt.get("evidence_type") != "captured"
                or not isinstance(receipt.get("acquisition_id"), str) or "dispatch_started" not in receipt):
            unrecognised += 1; continue
        acquisition_id = receipt["acquisition_id"]
        try:
            receipt_hash = digest(receipt)
        except (TypeError, ValueError, RecursionError):
            invalid.append({"acquisition_id": acquisition_id, "reason": "DIRECT_AUDIT_RECEIPT_INVALID"}); continue
        if acquisition_id in originals:
            if originals[acquisition_id] != receipt_hash:
                raise SafetyError("DIRECT_AUDIT_RECEIPT_CONFLICT")
            continue
        originals[acquisition_id] = receipt_hash
        actual = receipt["dispatch_started"]
        if actual is not None and type(actual) is not bool:
            raise SafetyError("DIRECT_AUDIT_DISPATCH_INVALID")
        elapsed = receipt.get("elapsed_seconds")
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0:
            elapsed = _duration(receipt.get("started_at"), receipt.get("completed_at"))
        rows.append({"acquisition_id": acquisition_id, "route": receipt.get("route"), "state": receipt.get("state"),
                     "actual_dispatch": actual, "duration_seconds": elapsed, "reason": receipt.get("reason"),
                     "provider_cost_usd": receipt.get("provider_cost_usd", receipt.get("apify_charge_usd")),
                     "apify_requests": receipt.get("apify_requests")})
    durations = [row["duration_seconds"] for row in rows if row["duration_seconds"] is not None]
    return {"recognised_acquisitions": len(rows), "actual_dispatches": sum(row["actual_dispatch"] is True for row in rows),
            "proved_pre_dispatch_failures": sum(row["actual_dispatch"] is False for row in rows),
            "uncertain_dispatches": sum(row["actual_dispatch"] is None for row in rows),
            "states": dict(Counter(str(row["state"]) for row in rows)), "rows": rows,
            "duration_receipts": len(durations), "recorded_duration_sum_seconds": round(sum(durations), 3),
            "unrecognised_receipts": unrecognised,
            "invalid_receipts": invalid,
            "note": "This covers only the supplied direct receipts. Actual empty, partial, blocked and local failures remain distinct. Duration sums are computer effort, not total programme wall-clock time."}


def audit_operational_checkpoint(memory, *, direct_receipts=(), as_of=None):
    if as_of is not None:
        timestamp(as_of)
    state, head, sequence, _ = memory._state(readonly=True)
    with memory.journal.locked(timeout=5):
        collection = collection_costs(memory.journal)
    return {"schema_version": 1, "kind": "saved_operational_audit", "checked_at": as_of or utc_now(),
            "evidence_type": memory.evidence_type, "research_events": sequence, "research_head": head,
            "research_state_hash": digest(state), "saved_product_ideas": len(state["concepts"]),
            "published_logical_issues": len({row["issue_id"] for row in state["published_history"].values()
                                             if row.get("status") == "published" and isinstance(row.get("issue_id"), str)}),
            "active_reserved_issues": sum(row.get("state") in ("reserved", "uploaded", "published")
                                          for row in state["issue_reservations"].values()),
            "collection": collection, "model_effort": model_effort(memory, state),
            "direct_effort": direct_capture_effort(direct_receipts),
            "limitations": ["Collection totals include the selected project's historical ledger, not the exclusive cost of one report.",
                            "AI subscriptions, computer/browser effort and hosting are not invented as zero or added to an unknown invoice.",
                            "Report readiness and current inventory are not measured recurring discovery yield."]}
