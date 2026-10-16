"""One product type's whole journey, in time order, from saved records only.

Where it came from (website, search words, map heading, first seen), every AI
decision about it with its time, the checklist answers, every market and
supplier question with its outcome and time, the score parts, label,
report-ready reasons and any report allocation.
"""

import json

from . import trace_labels as words
from .trace_ai import classify

CHECKLIST_TEXT_LIMIT = 220
TIMELINE_LIMIT = 2000
TITLE_LIMIT = 90
# Programme-wide bookkeeping records list many types at once; they are not
# steps of one type's journey, so the time line leaves them out.
BOOKKEEPING_KINDS = frozenset(("research-programme-v1", "research-programme-clock-v1", "all_source_run_v1", "work-turn-v1",
                               "inventory-work-v1", "reporting-intake-v1", "reporting-intake-cursor-v1",
                               "discovery-wave-control-v2", "product-map-lane-v1", "source-access-scope-v1",
                               "research-programme-follow-on-v1", "opportunity-score-calibration-v2"))


def type_names(state, concept_id):
    """The type's names: its accepted report title (when defined) and its first label."""
    from .functional_types import effective_functional_definition
    from .models import SafetyError
    names = []
    try:
        title = effective_functional_definition(state, concept_id).get("report_title")
        if isinstance(title, str) and title:
            names.append(title)
    except (SafetyError, KeyError, TypeError, ValueError):
        pass
    label = (state["concepts"][concept_id].get("description") or {}).get("label")
    if isinstance(label, str) and label and label not in names:
        names.append(label)
    return names


def type_name(state, concept_id):
    names = type_names(state, concept_id)
    return " / ".join(names) if names else "unnamed"


def find_types(store, query):
    """Product types whose id equals ``query`` or whose id or name contains it."""
    concepts = store.state["concepts"]
    if query in concepts:
        return [query]
    needle = query.casefold()
    return sorted(cid for cid in concepts
                  if needle in cid.casefold() or any(needle in name.casefold() for name in type_names(store.state, cid)))


def _dict(value):
    return value if isinstance(value, dict) else {}


def _mentions(value, concept_id):
    return concept_id in json.dumps(value, ensure_ascii=False)


def _origin(store, concept_id):
    associations = [(record_id, row) for record_id, row in store.state["associations"].items()
                    if row.get("concept_id") == concept_id]
    semantic_ids = {row.get("semantic_id") for _, row in associations if row.get("status") == "resolved"}
    from .trace_funnel import type_creators, wave_creators
    created = type_creators(store).get(concept_id)
    creator = created[1].get("semantic_id") if created else None
    waves = {}
    for record_id, wave in store.state["batch_runs"].items():
        if wave.get("kind") != "discovery-wave-v2":
            continue
        planned = {question.get("id"): {**action, "query": _dict(question.get("request")).get("query")}
                   for action in wave.get("planned_actions") or [] for question in action.get("questions") or []}
        for question_id, action in (wave.get("actions") or {}).items():
            for observation_id in action.get("observation_ids") or []:
                waves[observation_id] = planned.get(question_id, {})
    listings = []
    for observation_id, row in store.state["observations"].items():
        if row.get("semantic_id") not in semantic_ids:
            continue
        facts = _dict(row.get("facts"))
        capture = _dict(store.state["captures"].get(row.get("capture_id")))
        request = _dict(_dict(capture.get("reference")).get("request"))
        heading = waves.get(observation_id, {})
        listings.append({"observation_id": observation_id, "semantic_id": row.get("semantic_id"),
                         "created_type": row.get("semantic_id") == creator, "source": row.get("source"),
                         "observed_at": row.get("observed_at"), "saved_at": store.first_at("observations", observation_id),
                         "title": str(_dict(row.get("semantic")).get("title") or _dict(row.get("semantic")).get("body")
                                      or "")[:TITLE_LIMIT], "list_type": facts.get("list_type"),
                         "search_words": facts.get("query") or request.get("term") or request.get("query") or heading.get("query"),
                         "lane": request.get("lane"), "heading": heading.get("family_id"),
                         "heading_category": heading.get("category")})
    listings.sort(key=lambda row: (not row["created_type"], row["saved_at"] or ""))
    wave = wave_creators(store).get(concept_id) if creator is None else None
    matches = [{"semantic_id": row.get("semantic_id"), "status": row.get("status"), "method": row.get("method"),
                "reason": row.get("reason"), "job_id": row.get("job_id"),
                "saved_at": store.first_at("associations", record_id)} for record_id, row in associations]
    return {"created_by_listing": creator, "created_by_heading_search": wave, "listings": listings, "matches": matches}


def _jobs(store, concept_id, related):
    """Every AI job whose answer or request is linked to this type."""
    job_ids = set()
    for _, _, row in related:
        if isinstance(row.get("job_id"), str):
            job_ids.add(row["job_id"])
    for job in store.ai_jobs:
        if job.get("intent") is not None and _mentions(job["intent"], concept_id):
            job_ids.add(job["folder"])
    semantic_ids = {row.get("semantic_id") for row in store.state["associations"].values()
                    if row.get("concept_id") == concept_id}
    for semantic_id in semantic_ids:
        job = (store.state["descriptions"].get(semantic_id) or {}).get("job_id")
        if job:
            job_ids.add(job)
    rows = []
    for job in store.ai_jobs:
        if job["folder"] in job_ids:
            row = classify(job)
            rows.append({**row, "label": words.ai_step(row["prompt_version"], row["role"])})
    return sorted(rows, key=lambda row: row["created_at"] or "")


def _checklist(judged, concept_id):
    from .research_ai import CRITERIA_BY_ID
    fact = ((judged["pool"] or {}).get("facts_by_concept") or {}).get(concept_id) or {}
    return [{"id": row.get("id"), "point": CRITERIA_BY_ID.get(row.get("id"), "Unknown point"), "answer": row.get("state"),
             "basis": row.get("basis"), "why": str(row.get("reason", ""))[:CHECKLIST_TEXT_LIMIT]}
            for row in fact.get("criteria") or []], fact


def _questions(store, concept_id):
    rows = []
    for record_id, row in store.state["query_tasks"].items():
        if row.get("kind") != "all_source_task_v1" or concept_id not in (row.get("targets") or {}):
            continue
        result = (row.get("target_results") or {}).get(concept_id) or {}
        changes = [{"at": at, "status": summary.get("status"), "reason": summary.get("reason")}
                   for at, _, summary in store.timeline.history.get(("query_tasks", record_id), [])]
        rows.append({"task_id": record_id, "route": row.get("route"), "source": row.get("source"),
                     "status": row.get("status"), "reason": row.get("reason"), "created_at": row.get("prepared_at"),
                     "asked_at": row.get("dispatched_at"), "tries": row.get("tries"),
                     "answer_for_this_type": result.get("state"), "changes": changes})
    return sorted(rows, key=lambda row: row["created_at"] or "")


def _timeline(store, related):
    lines = []
    for table_name, record_id, row in related:
        if row.get("kind") in BOOKKEEPING_KINDS:
            continue
        for at, sequence, summary in store.timeline.history.get((table_name, record_id), []):
            lines.append({"at": at, "event": sequence, "record": f"{table_name}/{record_id}",
                          "kind": summary.get("kind") or table_name,
                          "summary": {key: value for key, value in summary.items() if key != "kind"}})
    lines.sort(key=lambda row: (row["at"], row["event"]))
    return lines[:TIMELINE_LIMIT], len(lines)


def journey(store, judged, concept_id):
    concept = store.state["concepts"][concept_id]
    description = concept.get("description") or {}
    related = [(table_name, record_id, row) for table_name, records in store.state.items()
               if table_name not in ("observations", "captures", "descriptions")
               for record_id, row in records.items() if _mentions(row, concept_id)]
    checklist, fact = _checklist(judged, concept_id)
    decision = next((row for row in ((judged["pool"] or {}).get("decisions") or []) if row["concept_id"] == concept_id), {})
    basis = decision.get("opportunity_basis") or {}
    crowding = [row for _, _, row in related if row.get("kind") == "crowding-fact-v1"]
    timeline, total_lines = _timeline(store, related)
    return {"kind": "research-trace-product-v1", "concept_id": concept_id, "name": type_name(store.state, concept_id),
            "category": description.get("category"), "what_it_is": description.get("function"),
            "status": concept.get("status"), "first_seen": store.first_at("concepts", concept_id),
            "origin": _origin(store, concept_id), "ai_jobs": _jobs(store, concept_id, related),
            "checklist": checklist, "checklist_meets": fact.get("kpi_matches"), "checklist_total": fact.get("kpi_total"),
            "questions": _questions(store, concept_id),
            "crowding": [{"level": row.get("level"), "rule": row.get("rule"), "assessed_at": row.get("assessed_at")}
                         for row in crowding],
            "supplier": {name: fact.get(name) for name in ("supplier_search_status", "supplier_leads_checked",
                                                           "supplier_leads_found", "supplier_limitation")},
            "photo": fact.get("image_path"),
            "score": {"total": decision.get("opportunity_score"), "label": decision.get("opportunity_label"),
                      "reason": basis.get("reason"),
                      "parts": {part: {"points": value.get("points"), "max": value.get("max"),
                                       "items": {name: {"points": item.get("points"), "facts": item.get("facts")}
                                                 for name, item in value.get("items", {}).items()}}
                                for part, value in (basis.get("parts") or {}).items()}},
            "decision": decision.get("decision"), "codes": decision.get("codes", []), "reasons": decision.get("reasons", []),
            "report_ready": any(row["concept_id"] == concept_id for row in ((judged["pool"] or {}).get("eligible") or [])),
            "allocations": [record_id for table_name, record_id, _ in related if table_name == "issue_reservations"],
            "problems": [{"at": row.get("at"), "stage": row.get("stage"), "reason": row.get("reason")}
                         for table_name, _, row in related if table_name == "problems"],
            "timeline": timeline, "timeline_lines": total_lines}
