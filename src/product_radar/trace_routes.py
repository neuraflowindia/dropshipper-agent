"""Every website the research reads ("route"): reads, answers, reuse, failures,
waits, refusals and time per read, from saved records only.

Three saved sources are kept apart so nothing is counted twice:

* research records: one "acquisition" per planned website read, with its
  "about to send" and "answer saved" stages (all steps, discovery included);
* run logs: the product-check step's outcome lines (reused answers, waits and
  their reasons, refusals, failures, and the website time of each read);
* the dropshipping platforms' own saved catalogue reads.

The run-log wait/refusal rules are the owner meter's own (``operations``).
"""

from collections import Counter, defaultdict
from statistics import mean, median

from . import trace_labels as words
from .operations import _WAIT_STATES, _access_outcome
from .trace_records import at_or_after, seconds_between

OWNER_KIND = "source-acquisition-owner-v1"
STAGE_KIND = "source-acquisition-stage-v1"
REFUSAL_KIND = "source-access-refusal-v1"
FRESH_STATES = ("acquired", "completed_no_results", "insufficient")
GOOD_ANSWERS = ("usable", "partial", "captured", "empty", "completed_no_results")
CATALOGUE_ROUTES = ("deodap", "baapstore")


def _timing(values):
    values = [value for value in values if value is not None]
    if not values:
        return {"reads_timed": 0, "average_seconds": None, "median_seconds": None}
    return {"reads_timed": len(values), "average_seconds": round(mean(values), 1),
            "median_seconds": round(median(values), 1)}


def _latest(current, candidate):
    return candidate if candidate and (current is None or candidate > current) else current


def record_reads(store, since):
    """Per route, from the saved research records (every step)."""
    stages = defaultdict(dict)
    for row in store.state["source_checks"].values():
        if row.get("kind") == STAGE_KIND and isinstance(row.get("acquisition_id"), str):
            stages[row["acquisition_id"]][row.get("stage_type")] = row
    routes = defaultdict(lambda: {"planned": 0, "sent": 0, "states": Counter(), "answers": Counter(), "problems": Counter(),
                                  "lanes": Counter(), "seconds": [], "last_answer_at": None, "refusals": 0,
                                  "refusal_challenges": 0})
    owner_route = {}
    for record_id, row in store.state["batch_runs"].items():
        if row.get("kind") != OWNER_KIND:
            continue
        route = (row.get("binding") or {}).get("route") or "unknown"
        owner_route[(row.get("binding") or {}).get("acquisition_id") or record_id] = route
        if not at_or_after(row.get("created_at") or store.first_at("batch_runs", record_id) or "", since):
            continue
        item = routes[route]
        item["planned"] += 1
        item["lanes"]["discovery" if record_id.startswith("discovery-") else "product checks"] += 1
        item["states"][row.get("state")] += 1
        found = stages.get((row.get("binding") or {}).get("acquisition_id") or record_id, {})
        if "dispatch_intent" in found:
            item["sent"] += 1
        response = found.get("response")
        if response:
            answer = response.get("source_state") or "saved"
            item["answers"][answer] += 1
            item["seconds"].append(seconds_between(found.get("dispatch_intent", {}).get("recorded_at"),
                                                   response.get("recorded_at")))
            if answer in GOOD_ANSWERS:
                item["last_answer_at"] = _latest(item["last_answer_at"], response.get("recorded_at"))
            else:
                item["problems"][f"{answer}: {response.get('parse_reason') or 'no reason saved'}"] += 1
    for record_id, row in store.state["source_checks"].items():
        if row.get("kind") != REFUSAL_KIND:
            continue
        binding = row.get("binding") or {}
        if not at_or_after(binding.get("observed_at") or store.first_at("source_checks", record_id) or "", since):
            continue
        route = owner_route.get(binding.get("acquisition_id"), "unknown")
        routes[route]["refusals"] += 1
        routes[route]["refusal_challenges"] += bool(binding.get("challenge"))
    return routes


def log_reads(store, since):
    """Per route, from the run logs' product-check outcome lines."""
    routes = defaultdict(lambda: {"attempted": 0, "fresh": Counter(), "reused": 0, "reused_failures": 0, "failures": Counter(),
                                  "waits": Counter(), "blocks": Counter(), "website_seconds": [],
                                  "step_seconds": [], "last_success_at": None})
    for row in store.run_rows:
        if row["event"] != "source_outcome" or not at_or_after(row["at"], since):
            continue
        item = routes[row.get("route") or "unknown"]
        state, reason = row.get("state"), row.get("reason") or row.get("error_type") or row.get("state")
        timings = row.get("timings") if isinstance(row.get("timings"), dict) else {}
        if row.get("actual_attempt") is True:
            item["attempted"] += 1
            item["website_seconds"].append(timings.get("transport"))
            item["step_seconds"].append(timings.get("handler"))
        if state == "reused":
            item["reused"] += 1
        elif state in FRESH_STATES and row.get("actual_attempt") is True:
            item["fresh"][state] += 1
            if state != "insufficient":
                item["last_success_at"] = _latest(item["last_success_at"], row["at"])
        access = _access_outcome("source_outcome", row)
        if access == "block":
            item["blocks"][reason] += 1
        elif state in _WAIT_STATES or access == "held":
            item["waits"][reason] += 1
        elif row.get("error_type") or state == "failed":
            # The meter's rule: an earlier saved failure shown again (no new
            # visit, nothing newly reported) is not a new failure.
            if (row.get("outcome_reused") is True and row.get("actual_attempt") is False
                    and not row.get("error_reported")):
                item["reused_failures"] += 1
            else:
                item["failures"][reason] += 1
    return routes


def platform_reads(store, since):
    """Per dropshipping platform, from its own saved catalogue reads."""
    result = {}
    for route, value in sorted(store.platforms.items()):
        reads = [row for row in value["reads"] if at_or_after(row.get("read_at") or row.get("checked_at") or "", since)]
        refresh = value["refresh"]
        last = None
        for row in reads:
            if row.get("state") == "read":
                last = _latest(last, row.get("read_at"))
        result[route] = {"reads": len(reads), "states": Counter(row.get("state") for row in reads),
                         "failures": Counter(row.get("reason") for row in reads if row.get("state") != "read"),
                         "http_status": Counter(str(row.get("http_status")) for row in reads),
                         **_timing([seconds_between(row.get("attempt_started_at"), row.get("checked_at")) for row in reads]),
                         "last_success_at": last, "wait_now": (refresh.get("wait") or {}).get("reason"),
                         "blocked_until": refresh.get("blocked_until"), "failures_in_a_row": refresh.get("failures")}
    return result


def routes_report(store, since):
    records, logs = record_reads(store, since), log_reads(store, since)
    rows = []
    for route in sorted(set(records) | set(logs)):
        rec, log = records.get(route), logs.get(route)
        row = {"route": route, "label": words.route(route)}
        if rec:
            row["records"] = {"planned": rec["planned"], "sent": rec["sent"],
                              "not_sent_yet": rec["states"].get("reserved", 0),
                              "answers_saved": sum(rec["answers"].values()), "answers_by_result": dict(rec["answers"]),
                              "unusable_answers_by_reason": dict(rec["problems"]),
                              "read_states": dict(rec["states"]), "lanes": dict(rec["lanes"]),
                              "refusals": rec["refusals"], "refusals_with_challenge_page": rec["refusal_challenges"],
                              "last_answer_at": rec["last_answer_at"], **_timing(rec["seconds"])}
        if log:
            row["run_logs"] = {"attempted": log["attempted"], "fresh_answers": sum(log["fresh"].values()),
                               "fresh_by_result": dict(log["fresh"]), "reused_saved_answers": log["reused"],
                               "reused_saved_failures": log["reused_failures"],
                               "failures": dict(log["failures"]), "waits": dict(log["waits"]),
                               "access_blocks": dict(log["blocks"]), "last_success_at": log["last_success_at"],
                               "website": _timing(log["website_seconds"]), "whole_step": _timing(log["step_seconds"])}
        if route in CATALOGUE_ROUTES:
            row["note"] = ("Product checks here search the saved copy of the platform's catalogue (no website visit); "
                           "the website itself is read in the catalogue reads listed under the platforms.")
        rows.append(row)
    return {"kind": "research-trace-routes-v1", "since": since, "routes": rows,
            "platforms": platform_reads(store, since),
            "notes": ["'Research records' cover every step, discovery included; 'run logs' cover only the product-check step, "
                      "where reused answers and waits are written down. Seconds from 'about to send' to 'answer saved' include "
                      "time a read waited inside the programme; 'website seconds' are the website's own answer time."]}
