"""A small synthetic research data folder for the research-trace tests.

Everything here is invented: made-up product types ("Folding test rack"),
made-up websites results and AI jobs. No real research data, supplier or
person names. Times span two owner days (India time) on purpose.
"""

import gzip
import json
from pathlib import Path

from product_radar.models import canonical, digest

T0 = "2026-10-16T06:00:00+00:00"   # 11:30 India time, 16 Oct (before the meter start)
START = "2026-10-16T06:30:00+00:00"  # the owner meter's start
T1 = "2026-10-16T07:00:00+00:00"   # 12:30 India time, 16 Oct
T2 = "2026-10-16T07:00:30+00:00"
T3 = "2026-10-16T09:00:00+00:00"
T4 = "2026-10-16T20:00:00+00:00"   # 01:30 India time, 17 Oct
AS_OF = "2026-10-16T21:00:00+00:00"
A, B, C, D = "concept-aaa", "concept-bbb", "concept-ccc", "concept-ddd"


def _json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def concept(label, category="Home and Kitchen"):
    return {"description": {"label": label, "category": category, "function": "Invented test function."},
            "status": "provisional"}


def task(route, source, status, reason, targets, results=None, prepared_at=T1):
    return {"kind": "all_source_task_v1", "route": route, "source": source, "status": status, "reason": reason,
            "targets": {target: "hash" for target in targets}, "target_results": results or {},
            "prepared_at": prepared_at, "dispatched_at": None, "tries": 0}


def owner(route, acquisition_id, state, created_at=T1):
    return {"kind": "source-acquisition-owner-v1", "state": state, "created_at": created_at,
            "binding": {"route": route, "acquisition_id": acquisition_id}}


def stage(acquisition_id, stage_type, recorded_at, **values):
    return {"kind": "source-acquisition-stage-v1", "acquisition_id": acquisition_id, "stage_type": stage_type,
            "recorded_at": recorded_at, **values}


def crowding_fact(concept_id, level):
    from product_radar.crowding import FACT_KIND, VERSION, load_thresholds
    return {"kind": FACT_KIND, "version": VERSION, "thresholds_version": load_thresholds()["version"],
            "concept_id": concept_id, "level": level, "rule": "test_rule", "assessed_at": T3}


def events():
    """(at, [(table, id, value), ...]) in saved order."""
    wave = {"kind": "discovery-wave-v2", "prepared_at": T1, "new_concept_ids": [C],
            "planned_actions": [
                {"family_id": "fam-a", "category": "Home and Kitchen",
                 "questions": [{"id": "q-1", "route": "meta-ads", "request": {"query": "folding rack"}}]},
                {"family_id": "fam-b", "category": "Home and Kitchen",
                 "questions": [{"id": "q-2", "route": "google-trends", "request": {"query": "test lamp"}}]},
                {"family_id": "fam-c", "category": "Home and Kitchen",
                 "questions": [{"id": "q-3", "route": "meta-ads", "request": {"query": "test mat"}}]}],
            "actions": {"q-1": {"state": "interpretation_pending", "observation_ids": ["obs-1"],
                                "acquisition_id": "discovery-acquisition-1"},
                        "q-2": {"state": "waiting", "reason": "OWNER_SIGN_IN_WINDOW_OPEN"}}}
    return [
        (T0, [("concepts", "concept-old", concept("Old test type"))]),
        (T1, [("batch_runs", "discovery-wave-1", wave),
              ("batch_runs", "wider-1", {"kind": "wider-discovery-read-v1", "route": "tradeindia",
                                         "lane": "supplier-listings", "state": "read", "candidates": 3}),
              ("observations", "obs-1", {"source": "meta", "semantic_id": "item-1", "capture_id": "cap-2",
                                         "semantic": {"body": "Invented folding rack advert"}, "facts": {}}),
              ("observations", "obs-2", {"source": "deodap", "semantic_id": "item-2", "capture_id": "cap-1",
                                         "semantic": {"title": "Invented folding rack"},
                                         "facts": {"list_type": "dropship_discovery_list"}}),
              ("observations", "obs-3", {"source": "amazon", "semantic_id": "item-3", "capture_id": "cap-1",
                                         "semantic": {"title": "Invented rack card"}, "facts": {"list_type": "product_search",
                                                                                                "query": "folding rack"}}),
              ("captures", "cap-1", {"source": "deodap", "reference": {"request": {"term": "rack", "lane": "lists"}}}),
              ("captures", "cap-2", {"source": "meta", "reference": "/somewhere/receipt.json"})]),
        (T1, [("descriptions", "item-1", {"job_id": "job-a", "result": {"kind": "product", "products": [{"label": "x"}]}}),
              ("descriptions", "item-2", {"job_id": "job-a", "result": {"kind": "service", "products": []}}),
              ("concepts", A, concept("Folding test rack")),
              ("concepts", B, concept("Quiet test lamp")),
              ("concepts", C, concept("Waiting test mat")),
              ("associations", "child-1", {"concept_id": A, "semantic_id": "item-2", "status": "resolved",
                                           "method": "no_known_match", "reason": "no_known_match"}),
              ("associations", "child-3", {"concept_id": None, "semantic_id": "item-9", "status": "excluded",
                                           "reason": "out_of_scope"}),
              ("associations", "child-4", {"concept_id": None, "semantic_id": "item-8", "status": "deferred",
                                           "reason": "Unclear invented text."}),
              ("jobs", "job-a", {"request": {"role": "A", "prompt_version": "research-abe-v1"},
                                 "binding_keys": {}, "turn_admissions": {}}),
              ("problems", "problem-1", {"stage": "description", "reason": "AI_RUNNER_NOT_ENABLED", "at": T1,
                                         "affected_id": "item-3"}),
              ("problems", "problem-4", {"stage": "description", "reason": "AI_PLAN_LIMIT_CREDITS_PROTECTED", "at": T1,
                                         "affected_id": "item-1"}),
              ("problems", "problem-2", {"stage": "fit_review", "reason": "WORK_TURN_MODEL_LIMIT", "at": T1,
                                         "affected_id": A}),
              ("problems", "problem-5", {"stage": "fit_review", "reason": "WORK_TURN_MODEL_LIMIT", "at": T1,
                                         "affected_id": B}),
              ("problems", "problem-3", {"stage": "identity", "reason": "AI_PLAN_LIMIT_CREDITS_PROTECTED", "at": T1,
                                         "affected_id": "child-3"}),
              ("problems", "problem-6", {"stage": "identity", "reason": "AI_RUNNER_NOT_ENABLED", "at": T1,
                                         "affected_id": "child-4"}),
              ("problems", "problem-8", {"stage": "identity", "reason": "AI_RUNNER_NOT_ENABLED", "at": T1,
                                         "affected_id": "item-2"}),
              ("problems", "problem-7", {"stage": "photos", "reason": "AI_PHOTO_CHECK_WAITING", "at": T1,
                                         "affected_id": C}),
              ("source_checks", "definition-1", {"kind": "functional-definition-v1", "accepted": True, "concept_id": A}),
              ("source_checks", "lead-1", {"kind": "dropship-platform-lead-v2", "route": "deodap", "concept_id": A}),
              ("assessments", A, {"job_id": "job-c", "result": {"criteria": []}})]),
        (T1, [("query_tasks", "task-1", task("amazon-search", "marketplace", "prepared",
                                             "Prepared; no source attempt yet.", [A])),
              ("query_tasks", "task-2", task("google-trends", "trends", "prepared", "OWNER_SIGN_IN_WINDOW_OPEN", [B])),
              ("query_tasks", "task-3", task("indiamart", "supplier", "failed", "SUPPLIER_NATIVE_READ_FAILED", [A])),
              ("query_tasks", "task-4", task("tradeindia", "supplier", "blocked", "Invented free-text reason.", [A])),
              ("query_tasks", "task-5", task("meta-ads", "meta", "completed_no_results", "No ads.", [B],
                                             {B: {"state": "completed_no_results"}})),
              ("query_tasks", "task-6", task("google-maps", "supplier", "prepared", "Prepared; no source attempt yet.", [C])),
              ("query_tasks", "question-1", {"kind": "research-question-v2", "route": "google-trends",
                                             "prepared_at": T1, "progress": {"acquisition": "prepared",
                                                                             "reason": "SOURCE_ACCESS_PROBE_PENDING"}}),
              ("batch_runs", "source-acquisition-1", owner("amazon-search", "source-acquisition-1", "response_saved")),
              ("batch_runs", "discovery-acquisition-1", owner("meta-ads", "discovery-acquisition-1", "completed_empty")),
              ("batch_runs", "source-acquisition-2", owner("google-trends", "source-acquisition-2", "reserved")),
              ("batch_runs", "source-acquisition-3", owner("google-trends", "source-acquisition-3", "failed_access")),
              ("batch_runs", "source-acquisition-old", owner("google-trends", "source-acquisition-old", "reserved", T0)),
              ("source_checks", "stage-1", stage("source-acquisition-1", "dispatch_intent", T1)),
              ("source_checks", "stage-2", stage("source-acquisition-1", "response", T2, source_state="usable")),
              ("source_checks", "stage-3", stage("discovery-acquisition-1", "dispatch_intent", T1)),
              ("source_checks", "stage-4", stage("discovery-acquisition-1", "response", T3, source_state="empty")),
              ("source_checks", "stage-5", stage("source-acquisition-3", "dispatch_intent", T1)),
              ("source_checks", "stage-6", stage("source-acquisition-3", "response", T2, source_state="blocked",
                                                 parse_reason="TRENDS_MINIMISED_NOT_RENDERED")),
              ("source_checks", "refusal-1", {"kind": "source-access-refusal-v1",
                                              "binding": {"acquisition_id": "source-acquisition-3", "challenge": True,
                                                          "observed_at": T2}}),
              ("source_checks", "refusal-2", {"kind": "source-access-refusal-v1",
                                              "binding": {"acquisition_id": "missing-acquisition", "observed_at": T2}})]),
        (T2, [("query_tasks", "task-1", {**task("amazon-search", "marketplace", "dispatching",
                                                "Prepared; no source attempt yet.", [A]), "dispatched_at": T2}),
              ("associations", "child-2", {"concept_id": B, "semantic_id": "item-1", "status": "resolved",
                                           "method": "llm_accepted", "reason": "Same use.", "job_id": "job-b"})]),
        (T2, [("concepts", D, concept("Invented hook")),
              ("associations", "child-5", {"concept_id": D, "semantic_id": "item-3", "status": "resolved",
                                           "method": "llm_accepted", "reason": "A different use, so a new type."})]),
        (T3, [("query_tasks", "task-1", {**task("amazon-search", "marketplace", "acquired", "MARKETPLACE_CARDS_OBSERVED", [A],
                                                {A: {"state": "interpretation_pending"}}), "dispatched_at": T2}),
              ("batch_runs", "crowding-1", crowding_fact(A, "high")),
              ("source_checks", "image-1", {"kind": "public_product_image", "state": "usable", "concept_id": A,
                                            "reason": "Saved."}),
              ("source_checks", "image-2", {"kind": "public_product_image", "state": "failed", "concept_id": B,
                                            "reason": "PRODUCT_IMAGE_TRANSPORT_FAILED"}),
              ("batch_runs", "programme-1", {"kind": "research-programme-v1", "status": "running", "concepts": [A, B, C]})]),
        (T4, [("issue_reservations", "reservation-1", {"status": "reserved", "concept_ids": [A]}),
              ("published_history", "published-1", {"status": "published", "concept_id": "concept-old"}),
              ("batch_runs", "programme-1", {"kind": "research-programme-v1", "status": "work_remaining",
                                             "concepts": [A, B, C]})]),
    ]


def write_events(folder, rows):
    folder.mkdir(parents=True, exist_ok=True)
    head, state = "0" * 64, {}
    for sequence, (at, changes) in enumerate(rows, 1):
        body = {"seq": sequence, "id": f"event-{sequence:04d}", "at": at, "previous": head, "evidence_type": "captured",
                "changes": [{"table": table, "id": record_id, "value": value,
                             "previous_hash": digest(state[(table, record_id)]) if (table, record_id) in state else None}
                            for table, record_id, value in changes]}
        for table, record_id, value in changes:
            state[(table, record_id)] = value
        head = digest(body)
        (folder / f"{sequence:08d}.json").write_bytes(canonical({**body, "hash": head}))


def run_rows():
    def row(at, event, **values):
        return {"schema_version": 1, "run_id": "run-test", "at": at, "event": event, **values}
    return [
        row(T0, "source_outcome", route="amazon-search", state="acquired", actual_attempt=True),  # before since
        row(T1, "source_outcome", route="amazon-search", state="acquired", actual_attempt=True,
            timings={"transport": 4.0, "handler": 9.0}),
        row(T1, "source_outcome", route="amazon-search", state="reused", actual_attempt=None, outcome_reused=False),
        row(T1, "source_outcome", route="amazon-search", state="not_attempted", actual_attempt=False,
            reason="WORK_TURN_SOURCE_LIMIT"),
        row(T2, "source_outcome", route="amazon-search", state="insufficient", actual_attempt=True,
            timings={"transport": 6.0, "handler": 11.0}),
        row(T2, "source_outcome", route="google-trends", state="blocked", actual_attempt=True),
        row(T2, "source_outcome", route="google-trends", state="blocked", actual_attempt=False),
        row(T2, "source_outcome", route="google-trends", state="waiting", actual_attempt=False,
            reason="SIGNED_IN_READ_SPACING"),
        row(T3, "source_outcome", route="company-pages", state="failed", actual_attempt=True,
            reason="SOURCE_ACTION_TIMEOUT", error_type="SourceActionDeadline"),
        row(T3, "source_outcome", route="company-pages", state="failed", actual_attempt=False, outcome_reused=True),
        row(T3, "preparation_deferred", reason="WORK_TURN_MODEL_LIMIT"),
        row(T3, "round_finished", state="finished"),
        row(T4, "round_finished", state="finished"),
    ]


def write_runs(folder):
    folder.mkdir(parents=True, exist_ok=True)
    rows = run_rows()
    (folder / "run-test.jsonl.2026-10-16_07").write_text("\n".join(json.dumps(r) for r in rows[:4]) + "\n")
    (folder / "run-test.jsonl").write_text("\n".join(json.dumps(r) for r in rows[4:]) + "\nnot json\n"
                                           + json.dumps({"event": "x", "at": "not a time"}) + "\n")
    (folder / "meter-summary-cache-v1.json").write_text("{}")


def _job(folder, name, **files):
    path = folder / name
    path.mkdir(parents=True)
    for file_name, value in files.items():
        target = path / (file_name + ".json") if not file_name.endswith(".json") else path / file_name
        if isinstance(value, str):
            target.write_text(value)
        else:
            target.write_text(json.dumps(value))


def write_ai_jobs(folder):
    from product_radar.spend_watch_ai import GUARD_CODES
    usage = {"input_tokens": 1000, "cached_input_tokens": 100, "output_tokens": 50}

    def intent(role, version, created_at=T1, payload=None):
        return {"created_at": created_at, "request": {"role": role, "prompt_version": version, "payload": payload or {}},
                "turn_admission": {"purpose": role, "item_count": 2}}
    _job(folder, "job-a", intent=intent("A", "research-abe-v1"), runner_execution={"started_at": T1},
         result={"completed_at": T2, "usage": usage}, completion={"exit_code": 0, "usage": usage})
    _job(folder, "job-b", intent=intent("B", "research-b-functional-v2"),
         failure={"code": GUARD_CODES[0], "at": T2, "usage": None})
    _job(folder, "job-c", intent=intent("E", "research-e-fit10-functional-v5", T4, {"items": [{"concept_id": A}]}),
         runner_execution={"started_at": T4}, result={"completed_at": "2026-10-16T20:00:20+00:00", "usage": usage},
         completion={"exit_code": 0, "usage": usage})
    _job(folder, "job-d", intent=intent("E", "research-e-source-relevance-v2"), runner_execution={"started_at": T1},
         failure={"code": "AI_INTERRUPTED_REVIEW_SAVED_JOB", "at": T3, "usage": None},
         retries=[{"reason": "x", "previous_failure": "AI_RATE_LIMITED", "at": T2}])
    _job(folder, "job-e", intent=intent("F", "research-f-v4"))
    _job(folder, "job-f", derived_result={"answer": "saved"})
    _job(folder, "job-g", **{"supplier_scoped_reference_map.json": {"map": {}}})
    _job(folder, "job-h", intent="{not json", result={"completed_at": T0})
    _job(folder, "job-old", intent=intent("A", "research-abe-v1", T0), result={"completed_at": T0, "usage": usage})
    _job(folder, "quote-repair-1", owner={"job_id": "job-a", "request": {"role": "F", "prompt_version": "research-f-v3"}})


def write_spend(folder):
    state = {"items": {"ai-week": {"label": "AI weekly allowance", "percent": 100, "used": "100", "allowance": "100",
                                   "unit": "percent", "updated_at": T3, "text": "AI weekly allowance: 100% used."}},
             "waits": {"ai": {"reason": "AI_PLAN_LIMIT_CREDITS_PROTECTED", "since": T3, "until": AS_OF}}}
    _json(folder / "state.json", {"state": state, "hash": digest(state)})
    (folder / "alerts").mkdir(parents=True)
    (folder / "alerts" / "2026-10-16.jsonl").write_text(
        json.dumps({"at": T3, "item": "ai-week", "message": "AI weekly allowance: 100% used.", "percent": 100}) + "\n"
        + "broken line\n" + json.dumps({"at": T2, "item": "ai-week", "message": "75% used.", "percent": 75}) + "\n")


def write_baseline(data_root):
    from product_radar.programme_summary import METER_COUNTS
    baseline = {"schema_version": 1, "kind": "owner-meter-baseline-v1", "started_at": START, "reason": "Test start.",
                "carried_attempt_ids": [], "carried_known_usd": "0", "carried_unsettled_hold_usd": "0",
                "counts": {name: 0 for name in METER_COUNTS}}
    _json(data_root / "data" / "operations" / "meter-baseline.json", {"baseline": baseline, "hash": digest(baseline)})


def _raw(folder, name, receipt):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(gzip.compress(canonical({"receipt": receipt, "body": "{}"})))


def write_platforms(memory):
    base = memory / "dropship_platforms"
    _raw(base / "deodap" / "raw" / "2026-10-16", "000001.json.gz",
         {"state": "read", "http_status": 200, "attempt_started_at": T1, "checked_at": T2, "read_at": T1})
    _raw(base / "deodap" / "raw" / "2026-10-16", "000002.json.gz",
         {"state": "failed", "reason": "DROPSHIP_PAGE_LAYOUT_UNRECOGNISED", "http_status": 500,
          "attempt_started_at": T3, "checked_at": T3, "read_at": T3})
    _json(base / "deodap" / "refresh.json", {"version": "dropship-refresh-v1", "route": "deodap", "cycle": None,
                                              "completed": {"full": {"items": 10}}, "failures": 0,
                                              "blocked_until": None, "wait": {}, "sequence": 2})
    (base / "dropdash" / "raw" / "2026-10-16").mkdir(parents=True)
    (base / "dropdash" / "raw" / "2026-10-16" / "000001.json.gz").write_bytes(b"not gzip")
    _json(base / "dropdash" / "refresh.json", {"version": "dropship-refresh-v1", "route": "dropdash", "cycle": None,
                                                "completed": {}, "failures": 2, "blocked_until": AS_OF,
                                                "wait": {"reason": "OWNER_SIGN_IN_REQUIRED", "at": T4}, "sequence": 1})
    (base / "baapstore").mkdir(parents=True)
    (base / "baapstore" / "refresh.json").write_text("{\"broken\": true}")


def build(data_root, *, with_baseline=True):
    """Write the whole synthetic folder; returns its ``data/research/captured`` path."""
    data_root = Path(data_root)
    memory = data_root / "data" / "research" / "captured"
    write_events(memory / "events", events())
    write_runs(data_root / "data" / "operations" / "research-runs")
    write_ai_jobs(memory / "ai_jobs")
    write_spend(memory / "spend_watch")
    write_platforms(memory)
    if with_baseline:
        write_baseline(data_root)
    return memory


def criteria():
    return [{"id": 1, "state": "Meets", "basis": "inferred", "reason": "Invented: small."},
            {"id": 2, "state": "Does not meet", "basis": "inferred", "reason": "Invented: bulky."}]


def basis(want, early, room, product, reason="opportunity_scored"):
    def part(points, maximum, name):
        return {"points": points, "max": maximum, "items": {name: {"points": points, "facts": {"n": points}}}}
    return {"reason": reason, "parts": {"want": part(want, 20, "facebook_ads"), "early": part(early, 30, "new_advertisers"),
                                        "room": part(room, 25, "crowding"), "product": part(product, 25, "checklist")}}


def judged():
    """A made-up report pool and meter, shaped like the programme's own."""
    decisions = [
        {"concept_id": A, "decision": "included", "codes": [], "reasons": [], "supplier_leads_checked": 2,
         "supplier_leads_found": 1, "opportunity_score": 52, "opportunity_label": "sweet_spot",
         "opportunity_basis": basis(8, 0, 25, 19)},
        {"concept_id": B, "decision": "deferred", "codes": ["demand_too_quiet"], "reasons": ["Too quiet so far."],
         "supplier_leads_checked": 0, "supplier_leads_found": 0, "opportunity_score": 16, "opportunity_label": "too_quiet",
         "opportunity_basis": basis(0, 0, 0, 16, "demand_too_quiet")},
        {"concept_id": C, "decision": "deferred", "codes": ["crowding_pending", "supplier_search_required"],
         "reasons": ["Crowding check not finished.", "No supplier search yet."]},
        {"concept_id": "concept-old", "decision": "excluded", "codes": ["out_of_scope"], "reasons": ["Outside."]},
    ]
    facts = {A: {"kpi_matches": 7, "kpi_total": 10, "criteria": criteria(), "supplier_search_status": "complete",
                 "supplier_leads_found": 1, "supplier_leads_checked": 2, "image_path": "photo.jpg", "image_candidates": ["x"]},
             B: {"kpi_matches": 5, "kpi_total": 10, "criteria": criteria(), "supplier_search_status": "attempted"},
             C: {"kpi_matches": 6, "kpi_total": 10, "criteria": [], "supplier_search_status": None}}
    meter = {"candidates": 4, "accepted_narrow_types": 3, "assessed_types": 1, "assessment_passing_types": 1,
             "report_eligible_types": 1, "report_sets_produced": 0, "ai_jobs": 1}
    return {"pool": {"decisions": decisions, "eligible": [{"concept_id": A}], "facts_by_concept": facts},
            "meter": meter, "errors": {}}


def catalogue():
    return {"families": [{"id": "fam-a", "disposition": "usable"}, {"id": "fam-b", "disposition": "usable"},
                         {"id": "fam-c", "disposition": "usable"}, {"id": "fam-d", "disposition": "usable"},
                         {"id": "fam-x", "disposition": "retired"}]}


MAP_CONFIG = {"topup": {"min_unsearched_headings": 2}}
