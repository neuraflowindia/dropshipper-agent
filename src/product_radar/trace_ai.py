"""AI use per research step: jobs, outcomes, tokens and time, from saved job folders.

Each AI job folder keeps the request (role and prompt version = the step),
when the runner started, when the answer was saved, the token use reported by
the AI runner, and any failure record. Spend-watch refusals (the allowance
guard stopping a job before it starts) are counted as "refused", not failed.
Allowance readings come from the spend watch, which keeps only the latest
reading per allowance plus threshold alerts; it never records a per-job share.
"""

from collections import Counter, defaultdict
from statistics import median

from . import trace_labels as words
from .spend_watch_ai import GUARD_CODES
from .trace_records import at_or_after, day_of, problem_outcome, seconds_between

TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
# A job's outcome is its current attempt; tokens and runner time add every
# attempt (an automatic retry after a refusal or a short outage).
OUTCOMES = ("finished", "failed", "refused", "no_answer_saved", "derived_no_ai_call", "not_started", "repair_claim")
JOB_OUTCOMES = ("finished", "failed", "refused", "no_answer_saved")
OUTCOME_WORDS = {
    "finished": "Finished (answer saved)",
    "failed": "Failed",
    "refused": "Refused before starting (allowance watch)",
    "no_answer_saved": "Started, no answer saved (stopped or still running)",
    "derived_no_ai_call": "Answered from saved records (no AI call)",
    "not_started": "Prepared, AI not started (no request saved)",
    "repair_claim": "Price-quote repair claim (points to another job)",
}
NOT_RECORDED = ("Per-job allowance percentages are not recorded: the spend watch keeps only the latest reading of "
                "each allowance (and an alert when it passes 50/75/90/100%), so a job's own share of the allowance "
                "cannot be traced.")


def _outcome(attempt, job):
    failure = attempt.get("failure") or {}
    if job.get("owner") is not None and job.get("intent") is None:
        return "repair_claim"
    if failure:
        return "refused" if failure.get("code") in GUARD_CODES else "failed"
    if attempt.get("result") is not None:
        return "finished"
    if attempt.get("intent") is not None:
        return "no_answer_saved"
    if job.get("derived_result") is not None:
        return "derived_no_ai_call"
    return "not_started"


def _attempt_use(attempt):
    """One attempt's token use and runner time (None when it never ran)."""
    intent, result = attempt.get("intent") or {}, attempt.get("result") or {}
    failure, completion = attempt.get("failure") or {}, attempt.get("completion") or {}
    usage = result.get("usage") or completion.get("usage") or failure.get("usage") or {}
    started = (attempt.get("runner_execution") or {}).get("started_at")
    finished = result.get("completed_at") or (failure.get("at") if started else None)
    return ({name: usage.get(name) if isinstance(usage.get(name), int) else 0 for name in TOKEN_FIELDS},
            seconds_between(started, finished) if started else None, started, finished)


def classify(job):
    """One job's current outcome, step, times and token use (all attempts added up).

    The outcome is the current (last saved) attempt's; tokens and runner time
    add every attempt that reached the AI, since each one used the allowance.
    """
    attempts = job.get("attempts") or [job]
    current = attempts[-1]
    intent = current.get("intent") or job.get("intent") or {}
    request = intent.get("request") or (job.get("owner") or {}).get("request") or {}
    outcome = _outcome(current, job)
    tokens = dict.fromkeys(TOKEN_FIELDS, 0)
    seconds, timed = 0.0, False
    for attempt in attempts:
        used, wall, _, _ = _attempt_use(attempt)
        for name in TOKEN_FIELDS:
            tokens[name] += used[name]
        if wall is not None:
            seconds, timed = seconds + wall, True
    _, _, started, finished = _attempt_use(current)
    first_intent = (attempts[0].get("intent") or {}) if attempts else {}
    failure = current.get("failure") or {}
    return {"job": job["folder"], "outcome": outcome, "role": request.get("role"),
            "prompt_version": request.get("prompt_version"),
            "purpose": (intent.get("turn_admission") or {}).get("purpose"),
            "items": (intent.get("turn_admission") or {}).get("item_count"),
            "created_at": first_intent.get("created_at") or intent.get("created_at"), "started_at": started,
            "finished_at": finished or (failure.get("at") if failure else None),
            "wall_seconds": round(seconds, 3) if timed else None,
            "failure_code": failure.get("code"), "exit_code": (current.get("completion") or {}).get("exit_code"),
            "attempts": len(attempts), "earlier_outcomes": [_outcome(attempt, job) for attempt in attempts[:-1]],
            **tokens}


def _bucket():
    return {"jobs": 0, "outcomes": Counter(), "failures": Counter(), "seconds": [], "items": 0,
            "retried_jobs": 0, "earlier_attempts": Counter(), **{name: 0 for name in TOKEN_FIELDS}}


def _add(bucket, row):
    bucket["jobs"] += 1
    bucket["outcomes"][row["outcome"]] += 1
    if row["failure_code"]:
        bucket["failures"][row["failure_code"]] += 1
    if row["wall_seconds"] is not None:
        bucket["seconds"].append(row["wall_seconds"])
    bucket["items"] += row["items"] or 0
    bucket["retried_jobs"] += row["attempts"] > 1
    bucket["earlier_attempts"].update(row["earlier_outcomes"])
    for name in TOKEN_FIELDS:
        bucket[name] += row[name]


def _finish(bucket):
    seconds = bucket.pop("seconds")
    return {**bucket, "outcomes": {name: bucket["outcomes"].get(name, 0) for name in OUTCOMES if bucket["outcomes"].get(name)},
            "failures": dict(bucket["failures"]), "earlier_attempts": dict(bucket["earlier_attempts"]),
            "total_wall_seconds": round(sum(seconds), 1),
            "median_wall_seconds": round(median(seconds), 1) if seconds else None, "timed_jobs": len(seconds)}


def ai_report(store, since):
    rows = [classify(job) for job in store.ai_jobs]
    rows = [row for row in rows if at_or_after(row["created_at"] or row["finished_at"] or "", since)
            or row["created_at"] is None and row["finished_at"] is None]
    steps, days, day_steps = defaultdict(_bucket), defaultdict(_bucket), defaultdict(_bucket)
    total, other = _bucket(), Counter(row["outcome"] for row in rows if row["outcome"] not in JOB_OUTCOMES)
    rows = [row for row in rows if row["outcome"] in JOB_OUTCOMES]
    for row in rows:
        key = row["prompt_version"] or row["role"] or "unknown"
        _add(steps[key], row)
        _add(total, row)
        when = row["finished_at"] or row["created_at"]
        if when:
            _add(days[day_of(when)], row)
            _add(day_steps[(day_of(when), key)], row)
    step_rows = []
    for key, bucket in sorted(steps.items(), key=lambda item: -item[1]["jobs"]):
        sample = next(row for row in rows if (row["prompt_version"] or row["role"] or "unknown") == key)
        step_rows.append({"step": key, "label": words.ai_step(sample["prompt_version"], sample["role"]),
                          "role": sample["role"], **_finish(bucket)})
    waits = Counter(row.get("reason") for row in store.run_rows
                    if row["event"] == "preparation_deferred" and at_or_after(row["at"], since))
    blocked = Counter((row.get("stage"), row.get("reason"), problem_outcome(store.state, row))
                      for record_id, row in store.state["problems"].items()
                      if str(row.get("reason", "")).startswith(("AI_", "WORK_TURN_"))
                      and at_or_after(store.first_at("problems", record_id) or "", since))
    spend = store.spend
    readings = []
    for item_id, item in sorted(((spend.get("state") or {}).get("items") or {}).items()):
        readings.append({"item": item_id, "label": item.get("label"), "percent": item.get("percent"),
                         "used": item.get("used"), "allowance": item.get("allowance"), "unit": item.get("unit"),
                         "read_at": item.get("updated_at"), "text": item.get("text")})
    return {"kind": "research-trace-ai-v1", "since": since, "steps": step_rows, "total": _finish(total),
            "other_ai_folders": {name: other[name] for name in OUTCOMES if other.get(name)},
            "by_day": [{"day": day, **_finish(bucket)} for day, bucket in sorted(days.items())],
            "by_day_and_step": [{"day": day, "step": key, **_finish(bucket)} for (day, key), bucket in sorted(day_steps.items())],
            "round_waits_for_ai": dict(waits),
            "ai_steps_that_could_not_start": [{"stage": stage, "reason": reason, "later": later, "count": count}
                                              for (stage, reason, later), count in blocked.most_common()],
            "allowance_readings_latest": readings,
            "allowance_alerts": [{"at": row["at"], "message": row.get("message"), "percent": row.get("percent"),
                                  "item": row.get("item")} for row in spend.get("alerts", [])],
            "allowance_waits_now": [{"route": route, **wait} for route, wait in
                                    sorted(((spend.get("state") or {}).get("waits") or {}).items())],
            "per_job_allowance": "not_recorded", "notes": [NOT_RECORDED]}
