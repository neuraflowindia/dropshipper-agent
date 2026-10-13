"""Continue relevance checking after real saved source acquisition.

Collection and interpretation are separate durable steps. A usable capture is
not promoted to a product confirmation until its content is matched. Matching
never changes the practical checklist or recollects a source. Exact saved
meanings are reused before the bounded model step.
"""

from copy import deepcopy

from .market_validation import REVIEW_VERSION, SOURCE_SUPPORT_VERSION, current_source_support, source_evidence
from .models import SafetyError, digest, timestamp, utc_now
from .research_ai import Jobs, MODEL, SOURCE_RELEVANCE_PROMPT_VERSION, SOURCE_SUPPORT_PROMPT_VERSION
from .model_recovery import run_current_model_job
from .research_flow import QUALITY_FAILURES
from .research_memory import change, key
from .source_coverage import TASK_KIND, bind_source_relevance
from .suppliers import _interpretation_failure


FUNCTIONAL_MATCH_METHOD = "functional_source_match_v4"
FUNCTIONAL_SUPPORT_METHOD = "functional_source_support_v4"


def _functional_context(state, concept_id):
    from .functional_types import functional_role_context
    return functional_role_context(state, concept_id, role="C")


def _source_identity(row):
    identity = (row["source"], row["source_item"])
    if row["source"] in ("blinkit", "zepto", "instamart"):
        identity += (digest(row.get("facts", {}).get("location")),)
    return identity


def _review_task_scope(state, task_ids, run_id):
    if task_ids is None:
        return None
    if (not isinstance(task_ids, (list, tuple)) or not task_ids
            or any(not isinstance(ref, str) for ref in task_ids) or len(set(task_ids)) != len(task_ids)
            or any(state["query_tasks"].get(ref, {}).get("kind") != TASK_KIND
                   or run_id is not None and state["query_tasks"][ref].get("run_id") != run_id for ref in task_ids)):
        raise SafetyError("SOURCE_REVIEW_TASK_SCOPE_INVALID")
    return set(task_ids)


def functional_source_questions(state, *, run_id=None, as_of, concept_ids=None, task_ids=None):
    """Pure finite saved-capture cohorts; accepted row bindings are the cursor.

    Discovery/task membership nominates actual bytes for review, never a match
    or demand vote. Global source-item/location replacements are selected before
    narrowing to these candidates. A positive first row does not hide the rest.
    """
    from .discovery import latest_observations
    from .research_scope import _source_context, validated_scoped_evidence
    from .market_validation import _functional_match
    timestamp(as_of)
    selected_tasks = _review_task_scope(state, task_ids, run_id)
    latest = latest_observations(state["observations"], as_of, max_age_days=None)
    latest_charts = {}
    def chart_identity(row):
        metadata = row.get("parsed", {}).get("metadata", {})
        return digest({field: metadata.get(field) for field in ("term", "geo", "time_range", "property", "category")})
    for eid, row in state["trends_evidence"].items():
        date = source_evidence(state, eid)[1]
        if row.get("evidence_kind") == "related_queries" or date is None or timestamp(date) > timestamp(as_of):
            continue
        identity, order = chart_identity(row), (timestamp(date), eid)
        if identity not in latest_charts or order > latest_charts[identity][0]:
            latest_charts[identity] = order, eid
    chosen = sorted(state["concepts"] if concept_ids is None else concept_ids)
    tasks = [(tid, row) for tid, row in sorted(state["query_tasks"].items())
             if row.get("kind") == TASK_KIND and row.get("source") != "supplier"
             and (selected_tasks is None or tid in selected_tasks)
             and (run_id is None or row.get("run_id") == run_id)]
    result = []
    for cid in chosen:
        if state["concepts"][cid].get("status") in {"excluded", "merged", "retired"}:
            continue
        context = _functional_context(state, cid)
        if context is None:
            continue
        candidate_ids, owners = set(), {}
        for tid, task in tasks:
            if cid not in task.get("targets", {}):
                continue
            for receipt_id in task.get("receipt_refs", []):
                check = state["source_checks"].get(receipt_id, {})
                envelope = check.get("envelope", {})
                if (check.get("kind") != "all_source_receipt_v1"
                        or check.get("envelope_hash") != digest(envelope) or check.get("task_id") != tid
                        or check.get("route") != task["route"] or envelope.get("route") != task["route"]
                        or check.get("request_hash") != task["request_hash"]
                        or envelope.get("acquisition_id") not in set(task.get("attempt_refs", [])) | {task.get("acquisition_id")}):
                    raise SafetyError("SOURCE_REVIEW_TASK_BINDING_INVALID")
                if timestamp(envelope["observed_at"]) > timestamp(as_of):
                    continue
                for ref in envelope.get("observation_ids", []) + envelope.get("evidence_refs", []):
                    if ref in state["observations"] or ref in state["trends_evidence"]:
                        candidate_ids.add(ref); owners.setdefault(ref, set()).add(tid)
        if run_id is None and selected_tasks is None:
            semantics = {row.get("semantic_id") for row in state["associations"].values()
                         if row.get("concept_id") == cid and row.get("status") == "resolved"}
            candidate_ids.update(oid for oid, row in state["observations"].items()
                                 if row.get("semantic_id") in semantics)
            candidate_ids.update(eid for eid, row in state["trends_evidence"].items()
                                 if row.get("concept_id") == cid)
        identities = {_source_identity(state["observations"][oid]) for oid in candidate_ids
                      if oid in state["observations"]}
        actual_ids = {oid for oid, row in latest.items() if _source_identity(row) in identities}
        chart_identities = {chart_identity(state["trends_evidence"][eid]) for eid in candidate_ids
                            if eid in state["trends_evidence"]}
        actual_ids.update(row[1] for identity, row in latest_charts.items() if identity in chart_identities)
        completed = {}
        scoped = validated_scoped_evidence(state, cid, as_of=as_of)
        applied_jobs = {(row["job_proof"]["job_id"], row["job_proof"]["accepted_item_hash"])
                        for row in scoped if row["method"] == FUNCTIONAL_SUPPORT_METHOD and row.get("job_proof")}
        for row in scoped:
            if row["method"] != FUNCTIONAL_MATCH_METHOD:
                continue
            proof = row.get("job_proof") or {}
            if (proof.get("job_id"), proof.get("accepted_item_hash")) not in applied_jobs:
                continue  # Partial application resumes the same saved job first.
            for ref in row["references"]:
                if ref["table"] in ("observations", "trends_evidence") and _functional_match(row, ref["id"]):
                    completed[ref["id"]] = row
        groups = {}
        for ref in sorted(actual_ids):
            table = "observations" if ref in state["observations"] else "trends_evidence"
            row = state[table][ref]
            source_context = _source_context(table, ref, row)
            route = source_context.get("route") or ("google-trends" if table == "trends_evidence" else None)
            if route is None and table == "observations":
                capture = state["captures"].get(row.get("capture_id"))
                if capture is not None:
                    route = _source_context("captures", row["capture_id"], capture).get("route")
                if route is None:
                    actual_routes = {state["query_tasks"][tid]["route"] for tid in owners.get(ref, ())}
                    if len(actual_routes) == 1:
                        route = next(iter(actual_routes))
            if route is None:
                continue  # Missing real route is a provenance gap, not a guessed request.
            capture = row.get("capture_id") if table == "observations" else ref
            groups.setdefault((route, capture), []).append(ref)
        for (route, capture), refs in sorted(groups.items()):
            # A bounded immutable job freezes each cohort and its supplied
            # slice. More retained rows form another finite cohort, not a
            # lifetime cap or a positive-seeking repeat.
            for start in range(0, len(refs), 100):
                cohort = refs[start:start + 100]
                hashes = {ref: digest(source_evidence(state, ref)[2]) for ref in cohort}
                cohort_hash = digest([cid, context["meaning_hash"], route, hashes])
                unknown = [ref for ref in cohort if ref not in completed]
                result.append({"kind": "functional_source_scope", "task_id": key("functional-source-cohort", cohort_hash),
                    "concept_id": cid, "meaning_hash": context["meaning_hash"], "description": context["definition"],
                    "route": route, "capture_id": capture, "cohort_hash": cohort_hash, "evidence_hashes": hashes,
                    "observation_ids": cohort, "unknown_ids": unknown,
                    "reviewed_ids": [ref for ref in cohort if ref in completed],
                    "original_task_ids": sorted({tid for ref in cohort for tid in owners.get(ref, ())})})
    return result


def _known_relations(state, concept_id, *, as_of=None):
    description_hash = digest(state["concepts"][concept_id]["description"])
    known = {}
    from .research_flow import current_assessment_fit
    if current_assessment_fit(state, concept_id):
        for relation in state["assessments"][concept_id]["result"]["relevance"]:
            observation = state["observations"].get(relation["id"])
            if observation:
                known[observation["semantic_id"]] = deepcopy(relation)
    reviews = sorted((row for row in state["market_reviews"].values()
                      if row.get("review_version") == REVIEW_VERSION and row.get("concept_id") == concept_id
                      and row.get("description_hash") == description_hash
                      and (as_of is None or timestamp(row["reviewed_at"]) <= timestamp(as_of))),
                     key=lambda row: (timestamp(row["reviewed_at"]), row["input_hash"]))
    for review in reviews:
        for relation in review["relevance"]:
            observation = state["observations"].get(relation["id"])
            if observation:
                known[observation["semantic_id"]] = deepcopy(relation)
    return known


def _observations(state, task):
    ids = []
    for receipt_id in task["receipt_refs"]:
        receipt = state["source_checks"][receipt_id]["envelope"]
        ids.extend(receipt["observation_ids"])
    return sorted(set(ids))


def _questions(state, *, run_id=None, as_of=None, task_ids=None, concept_ids=None):
    selected_tasks = _review_task_scope(state, task_ids, run_id)
    questions = []
    for task_id, task in sorted(state["query_tasks"].items()):
        if (task.get("kind") != TASK_KIND or task["status"] != "acquired"
                or selected_tasks is not None and task_id not in selected_tasks
                or run_id is not None and task["run_id"] != run_id or task["source"] == "supplier"):
            continue
        observations = _observations(state, task)
        if not observations:
            continue  # Timeline readers own their chart binding, not E listing matching.
        for concept_id, target in sorted(task["target_results"].items()):
            if concept_ids is not None and concept_id not in concept_ids:
                continue
            if _functional_context(state, concept_id) is not None:
                continue
            if (target["state"] != "interpretation_pending"
                    or target["description_hash"] != digest(state["concepts"][concept_id]["description"])):
                continue
            known = _known_relations(state, concept_id, as_of=as_of)
            relevant = [obs_id for obs_id in observations
                        if known.get(state["observations"][obs_id]["semantic_id"], {}).get("relationship")
                        in {"exact_product", "relevant_family"}]
            uncertain = [obs_id for obs_id in observations
                         if known.get(state["observations"][obs_id]["semantic_id"], {}).get("relationship") == "unclear"]
            unknown = [obs_id for obs_id in observations if state["observations"][obs_id]["semantic_id"] not in known]
            questions.append({"task_id": task_id, "concept_id": concept_id, "description_hash": target["description_hash"],
                              "observation_ids": observations, "relevant_ids": relevant,
                              "uncertain_ids": uncertain, "unknown_ids": unknown})
    at = as_of or utc_now()
    return questions + _support_questions(state, run_id=run_id, as_of=at, task_ids=task_ids, concept_ids=concept_ids) + [
        row for row in functional_source_questions(state, run_id=run_id, as_of=at, task_ids=task_ids,
                                                  concept_ids=concept_ids) if row["unknown_ids"]]


def _support_questions(state, *, run_id, as_of, task_ids=None, concept_ids=None):
    """Interpret one usable saved site once; do not fabricate acquisition tasks."""
    from types import SimpleNamespace
    from .fit_policy import FIT_POLICY_ID
    from .research_flow import candidate_summary
    if not state["assessments"]:
        return []
    selected_tasks = _review_task_scope(state, task_ids, run_id)
    selected_originals = None
    if selected_tasks is not None:
        selected_originals = {ref for tid in selected_tasks for receipt_id in state["query_tasks"][tid]["receipt_refs"]
            for ref in state["source_checks"][receipt_id]["envelope"]["observation_ids"]}
    scoped_ids = None
    if run_id is not None or selected_tasks is not None:
        scoped_ids = set()
        for tid, task in state["query_tasks"].items():
            if (task.get("kind") != TASK_KIND or run_id is not None and task.get("run_id") != run_id
                    or selected_tasks is not None and tid not in selected_tasks):
                continue
            # concept_ids belongs to the collector invocation. The durable
            # task owns targets, each bound to the description it was asked
            # to check. Old work must not nominate a changed product.
            targets = task.get("targets")
            if (not isinstance(targets, dict) or any(not isinstance(cid, str)
                    or not isinstance(expected, str) for cid, expected in targets.items())):
                raise SafetyError("SOURCE_REVIEW_TASK_BINDING_INVALID")
            scoped_ids.update(cid for cid, expected in targets.items()
                              if cid in state["concepts"]
                              and digest(state["concepts"][cid]["description"]) == expected)
    concepts = {cid: row for cid, row in state["concepts"].items()
                if cid in state["assessments"] and (scoped_ids is None or cid in scoped_ids)
                and (concept_ids is None or cid in concept_ids)}
    if not concepts:
        return []
    view = dict(state, concepts=concepts)
    summary = candidate_summary(SimpleNamespace(snapshot=lambda: view, evidence_type="captured"), as_of=as_of)
    questions = []
    for product in summary["products"]:
        if _functional_context(state, product["id"]) is not None:
            continue
        if (product.get("fit_policy_id") != FIT_POLICY_ID or not (product.get("fit") or {}).get("eligible") or product["status"] in
                ("excluded_established_brand", "decision_binding_pending")):
            continue
        usable = [(source, signal) for source, signal in product["market"]["sources"].items()
                  if signal["available"] is True]
        if len(usable) != 1:
            continue
        source, signal = usable[0]
        if signal["positive"] is True or signal.get("evidence_support") == "reasonably_positive":
            continue
        ids = sorted(signal["used_ids"])
        if selected_originals is not None and not set(ids) <= selected_originals:
            continue  # A narrow repair cannot interpret another original's rows.
        support = current_source_support(state, product["id"], as_of=as_of)
        # A saved neutral/weak/unclear judgment is completed work, not an
        # invitation to ask again until a desired answer appears.
        if any(row["source"] == source and set(ids) <= set(row["scope_ids"]) for row in support):
            continue
        description_hash = digest(state["concepts"][product["id"]]["description"])
        task_id = key("source-support", [product["id"], description_hash, source,
            [(ref, digest(source_evidence(state, ref)[2])) for ref in ids]])
        questions.append({"kind": "saved_source_support", "task_id": task_id,
            "source": source, "concept_id": product["id"], "description_hash": description_hash,
            "observation_ids": ids, "relevant_ids": ids, "uncertain_ids": [], "unknown_ids": ids})
    return questions


def _bind(memory, question, *, as_of):
    if question.get("kind") in ("saved_source_support", "functional_source_scope"):
        return None  # This is interpretation of existing proof, not collection.
    if question["relevant_ids"]:
        state, refs, reason = "completed_relevant", question["relevant_ids"], "The captured source contains a reviewed matching product type. This is not a sales guarantee."
    elif question["unknown_ids"]:
        return None
    elif question["uncertain_ids"]:
        state, refs, reason = "insufficient", question["observation_ids"], "All returned entries were reviewed, but their descriptions did not establish a matching product type."
    else:
        state, refs, reason = "completed_no_results", question["observation_ids"], "All entries in this captured search were reviewed; none matched this product type. This is not proof of absence across the platform."
    return bind_source_relevance(memory, question["task_id"], [{"concept_id": question["concept_id"],
        "description_hash": question["description_hash"], "state": state, "evidence_refs": refs, "reason": reason}], as_of=as_of)


def _item(state, question, per_product):
    functional = question.get("kind") == "functional_source_scope"
    ids = question["unknown_ids"][:min(per_product, 20) if functional else per_product]
    observations = []
    for evidence_id in ids:
        source, observed_at, row = source_evidence(state, evidence_id)
        if source == "google-trends":
            parsed = row["parsed"]
            facts = {"metadata": deepcopy(parsed.get("metadata", {})), "state": parsed.get("state"),
                     "series": deepcopy(parsed.get("series", [])),
                     "limitations": deepcopy(parsed.get("limitations", []))}
            source_text = {"term": row.get("term", parsed.get("metadata", {}).get("term")),
                           "scope": row.get("scope", "Accepted captured India product-type chart.")}
            semantic_id = evidence_id
        else:
            facts, source_text, semantic_id = deepcopy(row["facts"]), deepcopy(row["semantic"]), row["semantic_id"]
        observations.append({"id": evidence_id, "semantic_id": semantic_id, "source": source,
                             "observed_at": observed_at, "source_text": source_text, "captured_facts": facts})
        if functional:
            from .research_scope import _source_context, meta_capture_context
            table = "observations" if evidence_id in state["observations"] else "trends_evidence"
            observations[-1]["source_context"] = _source_context(table, evidence_id, row)
            observations[-1]["reference"] = {"table": table, "id": evidence_id, "hash": digest(row)}
            if source == "meta":
                observations[-1]["capture_context"] = meta_capture_context(state, evidence_id)
    item = {"id": question["concept_id"], "description": deepcopy(question["description"] if functional else state["concepts"][question["concept_id"]]["description"]),
            "observations": observations, "reference_aliases": {"R" + str(i + 1): value["id"] for i, value in enumerate(observations)}}
    if functional:
        item.update(functional_contract="functional-source-scope-v1", meaning_hash=question["meaning_hash"],
                    source_scope={"route": question["route"], "cohort_hash": question["cohort_hash"],
                        "cohort_refs": [{"table": "observations" if ref in state["observations"] else "trends_evidence",
                                         "id": ref, "hash": expected} for ref, expected in question["evidence_hashes"].items()],
                        "captured_context_count": len(question["observation_ids"]),
                        "omitted_context_count": len(question["unknown_ids"]) - len(observations)})
    if question.get("kind") == "saved_source_support":
        item["source_scope"] = {"source": question["source"], "captured_context_count": len(question["observation_ids"]),
            "omitted_context_count": max(0, len(question["observation_ids"]) - len(observations)),
            "evidence_scope_hash": digest([(ref, digest(source_evidence(state, ref)[2])) for ref in question["observation_ids"]])}
    return item


def _bound_job(state, payload, item, runner_identity):
    """Bind application to the actual model request, not a reshaped batch ID."""
    context = {field: value for field, value in payload.items() if field != "items"}
    from .research_ai import FUNCTIONAL_SOURCE_PROMPT_VERSION, FUNCTIONAL_SOURCE_STYLE
    version = (FUNCTIONAL_SOURCE_PROMPT_VERSION if payload.get("relevance_style") == FUNCTIONAL_SOURCE_STYLE else
               SOURCE_SUPPORT_PROMPT_VERSION if payload.get("relevance_style") == "short-references-support-v3" else SOURCE_RELEVANCE_PROMPT_VERSION)
    binding = digest(["E", item, context, MODEL, version, runner_identity])
    owners = []
    for job_id, job in state["jobs"].items():
        if job.get("binding_keys", {}).get(item["id"]) != binding:
            continue
        request = job.get("request", {})
        body = request.get("payload", {})
        if (key("job", request) != job_id or request.get("role") != "E"
                or request.get("model") != MODEL or request.get("prompt_version") != version
                or request.get("runner") != runner_identity
                or {field: value for field, value in body.items() if field != "items"} != context
                or [row for row in body.get("items", []) if row.get("id") == item["id"]] != [item]):
            raise SafetyError("SOURCE_REVIEW_JOB_BINDING_INVALID")
        owners.append(job_id)
    if len(owners) != 1:
        raise SafetyError("SOURCE_REVIEW_JOB_BINDING_INVALID")
    return owners[0]


def _market_fields(state, refs):
    """Select actual demand/context fields, not physical specs or price alone."""
    fields = []
    allowed = {"meta": ("ad_id", "advertiser_id", "advertiser_url", "active", "start_date", "end_date"),
        "amazon": ("list_type", "list_url", "rank", "position", "availability", "rating", "reviews", "query", "country"),
        "flipkart": ("list_type", "availability", "rating", "reviews", "query", "country"),
        "blinkit": ("availability", "query", "location", "list_type"),
        "zepto": ("availability", "query", "location", "list_type"),
        "instamart": ("availability", "query", "location", "list_type")}
    for ref in refs:
        if ref["table"] == "trends_evidence":
            fields.append({"reference": {"table": ref["table"], "id": ref["id"]}, "path": ["parsed", "series"]})
        else:
            row = state["observations"][ref["id"]]
            for name in allowed.get(row["source"], ()):
                if name in row["facts"] and row["facts"][name] not in (None, "", [], {}):
                    fields.append({"reference": {"table": "observations", "id": ref["id"]}, "path": ["facts", name]})
    # Bound the selected market projection without replacing it with unrelated
    # full physical specs/prices. An actual context path still exists per row.
    if len(fields) > 80:
        fields = fields[:80]
    return fields


def _accept_functional_answer(memory, question, item, answer, *, job_id, as_of):
    from .research_scope import _field_values, _references, _support_fields, accept_scoped_evidence
    state = memory.snapshot()
    refs = {row["id"]: row["reference"] for row in item["observations"]}
    rows = {row["id"]: row for row in answer["relevance"]}
    saved = []
    for ref, relation in rows.items():
        original = refs[ref]
        fields = ([{"reference": {"table": original["table"], "id": ref}, "path": ["facts"]},
                   {"reference": {"table": original["table"], "id": ref}, "path": ["semantic"]}]
                  if original["table"] == "observations" else
                  [{"reference": {"table": "trends_evidence", "id": ref}, "path": ["parsed", "series"]}])
        saved.append(accept_scoped_evidence(memory, item["id"], route=question["route"],
            scope=relation["scope"], relationship=relation["relationship"], support="insufficient",
            references=[original], usable_fields=fields, limitations=relation["limitations"],
            reason=relation["reason"], reviewer="source-e-v4", as_of=as_of, expected_meaning_hash=item["meaning_hash"],
            method=FUNCTIONAL_MATCH_METHOD, job_id=job_id, job_item_id=item["id"])["id"])
    states = {"reasonably_positive": "positive", "neutral": "neutral", "weak": "not_positive", "unclear": "insufficient"}
    for opinion in answer["source_support"]:
        cited = opinion["references"]
        selected = [refs[ref] for ref in cited] if cited else list(refs.values())
        narrow = bool(cited) and all(rows[ref]["scope"] == "narrow_type" and rows[ref]["relationship"] in
                                      ("exact_product", "relevant_family") for ref in cited)
        fields = _market_fields(state, selected)
        support = states[opinion["judgment"]] if fields else "insufficient"
        limitations = sorted({limitation for row in rows.values() for limitation in row["limitations"]})
        # Retained captures sometimes lack a complete original request. Their
        # rows can still be reviewed without inventing that missing provenance
        # or retrying toward a desired opinion. Preserve the completed opinion
        # but do not adopt it as supported market evidence.
        bound, contexts, _, requests = _references(state, selected, as_of=as_of, route=question["route"])
        try:
            _support_fields(support, _field_values(state, fields, bound), contexts, requests,
                source_requests=[row["capture_context"]["request"] for row in item["observations"]
                    if row["id"] in {ref["id"] for ref in selected}
                    and row.get("capture_context", {}).get("state") == "verified"])
        except SafetyError as error:
            if str(error) not in {"RESEARCH_SCOPE_REQUEST_CONTEXT_REQUIRED", "RESEARCH_SCOPE_SUBSTANTIVE_FIELDS_REQUIRED"}:
                raise
            support = "insufficient"
            limitations.append("The original capture lacks usable market/request context; its interpretation is retained, not confirmed demand.")
        saved.append(accept_scoped_evidence(memory, item["id"], route=question["route"], scope="narrow_type",
            relationship="relevant_family" if narrow else "unclear", support=support,
            references=selected, usable_fields=fields, limitations=limitations,
            reason=opinion["reason"], reviewer="source-e-v4", as_of=as_of,
            expected_meaning_hash=item["meaning_hash"], method=FUNCTIONAL_SUPPORT_METHOD,
            job_id=job_id, job_item_id=item["id"])["id"])
    return saved


def _review_functional_source_results(memory, runner, *, run_id, limit, per_product, as_of, skipped,
                                      task_ids=None, concept_ids=None):
    from .research_ai import FUNCTIONAL_SOURCE_PROMPT_VERSION, FUNCTIONAL_SOURCE_STYLE
    state = memory.snapshot()
    questions = [row for row in functional_source_questions(state, run_id=run_id, as_of=as_of,
        task_ids=task_ids, concept_ids=concept_ids) if row["unknown_ids"]]
    selected, chosen, outcomes = [], set(), []
    for question in questions:
        if (len(selected) >= limit or question["concept_id"] in chosen
                or (question["task_id"], question["concept_id"]) in skipped):
            continue
        selected.append((question, _item(state, question, per_product))); chosen.add(question["concept_id"])
    for start in range(0, len(selected), 5):
        chunk = selected[start:start + 5]
        payload = {"mode": "marketplace_relevance_only", "relevance_style": FUNCTIONAL_SOURCE_STYLE,
                   "criteria": [], "items": [item for _, item in chunk]}
        try:
            job_id, result = run_current_model_job(memory, runner, "E", payload,
                declared_version=FUNCTIONAL_SOURCE_PROMPT_VERSION, current_items=[item for _, item in selected])
        except (SafetyError, OSError) as error:
            outcomes.extend({"task_id": question["task_id"], "concept_id": item["id"], "state": "waiting",
                "reason": _interpretation_failure(error, local_code="SOURCE_REVIEW_LOCAL_FILE_FAILED")}
                for question, item in chunk)
            continue
        for question, item in chunk:
            try:
                original = _bound_job(memory.snapshot(), payload, item, getattr(runner, "identity", "codex-chatgpt-high-v1"))
                answer, answer_job = result["accepted"].get(item["id"]), original
                if answer is None:
                    failure = result["failures"][item["id"]]
                    if runner is not None and failure in QUALITY_FAILURES | {
                        "AI_RELEVANCE_RESULT_MISSING", "AI_FIT_REFERENCE_ALIASES_INVALID",
                        "AI_SOURCE_SUPPORT_SOURCES_INVALID", "AI_SOURCE_SUPPORT_REFERENCE_INVALID",
                        "AI_SOURCE_SUPPORT_UNSUPPORTED", "AI_SOURCE_SUPPORT_RESULT_MISSING",
                        "AI_FUNCTIONAL_SOURCE_SCOPE_INVALID", "AI_FUNCTIONAL_SOURCE_LIMITATIONS_INVALID",
                        "AI_FUNCTIONAL_SOURCE_SUPPORT_SCOPE_INVALID"}:
                        from .checklist_repair import _run_repair
                        repaired = dict(payload, items=[item], checklist_repair={
                            "scope": "one-source-relevance-contract-repair-v1", "original_job_id": original,
                            "concept_id": item["id"], "input_hash": digest(item), "failure_code": failure,
                            "rules": "Correct only the supplied scoped references and output shape. Retain these facts and meaning; do not target a positive answer."})
                        answer_job, correction = _run_repair(memory, runner, repaired, memory.snapshot(), FUNCTIONAL_SOURCE_PROMPT_VERSION)
                        answer = correction["accepted"].get(item["id"])
                        if answer is None:
                            failure = correction["failures"][item["id"]]
                    if answer is None:
                        outcomes.append({"task_id": question["task_id"], "concept_id": item["id"], "state": "waiting",
                                         "reason": failure, "job_id": answer_job})
                        continue
                refs = _accept_functional_answer(memory, question, item, answer, job_id=answer_job, as_of=as_of)
                outcomes.append({"task_id": question["task_id"], "concept_id": item["id"], "state": "reviewed",
                                 "job_id": answer_job, "scoped_evidence_refs": refs})
            except (SafetyError, OSError) as error:
                outcomes.append({"task_id": question["task_id"], "concept_id": item["id"], "state": "waiting", "job_id": job_id,
                    "reason": _interpretation_failure(error, local_code="SOURCE_REVIEW_LOCAL_FILE_FAILED")})
    return outcomes, len(selected)


def review_source_results(memory, runner=None, *, run_id=None, limit=5, per_product=30, as_of=None,
                          skip_targets=(), task_ids=None, concept_ids=None):
    """Advance a bounded slice and report remaining work; no source/model retry loop.

    The next programme pass continues returned remaining questions. A confirmed
    relevant result completes that source probe; no matching result is claimed
    until every returned observation has an accepted judgment. No fuzzy lexical
    absence or checklist score becomes a source confirmation.
    """
    if type(limit) is not int or not 1 <= limit <= 20 or type(per_product) is not int or not 1 <= per_product <= 50:
        raise SafetyError("SOURCE_REVIEW_LIMIT_INVALID")
    if (not isinstance(skip_targets, (list, tuple, set))
            or any(not isinstance(row, (tuple, list)) or len(row) != 2
                   or any(not isinstance(value, str) for value in row) for row in skip_targets)):
        raise SafetyError("SOURCE_REVIEW_TARGETS_INVALID")
    skipped = {tuple(row) for row in skip_targets}
    at = as_of or utc_now(); timestamp(at)
    if (concept_ids is not None and (not isinstance(concept_ids, (list, tuple)) or not concept_ids
            or any(not isinstance(cid, str) or cid not in memory.snapshot()["concepts"] for cid in concept_ids)
            or len(set(concept_ids)) != len(concept_ids))):
        raise SafetyError("SOURCE_REVIEW_CONCEPT_SCOPE_INVALID")
    functional_outcomes, functional_selected = _review_functional_source_results(memory, runner, run_id=run_id,
        limit=limit, per_product=per_product, as_of=at, skipped=skipped, task_ids=task_ids, concept_ids=concept_ids)
    limit -= functional_selected
    state = memory.snapshot(); questions = [row for row in _questions(state, run_id=run_id, as_of=at,
        task_ids=task_ids, concept_ids=concept_ids)
                                          if row.get("kind") != "functional_source_scope"]
    reused, outcomes, selected, chosen = [], functional_outcomes, [], set()
    for question in questions:
        if question.get("kind") != "saved_source_support" and (question["relevant_ids"] or not question["unknown_ids"]):
            try:
                receipt_id = _bind(memory, question, as_of=at)
                reused.append({"task_id": question["task_id"], "concept_id": question["concept_id"], "receipt_id": receipt_id})
            except (SafetyError, OSError) as error:
                outcomes.append({"task_id": question["task_id"], "concept_id": question["concept_id"], "state": "waiting",
                                 "reason": _interpretation_failure(error, local_code="SOURCE_REVIEW_LOCAL_FILE_FAILED")})
        elif (len(selected) < limit and question["concept_id"] not in chosen
              and (question["task_id"], question["concept_id"]) not in skipped):
            selected.append((question, _item(state, question, per_product))); chosen.add(question["concept_id"])
    for offset in range(0, len(selected), 5):
        chunk = selected[offset:offset + 5]
        payload = {"mode": "marketplace_relevance_only", "relevance_style": "short-references-support-v3", "criteria": [],
                   "items": [item for _, item in chunk]}
        try:
            job_id, result = run_current_model_job(memory, runner, "E", payload,
                declared_version=SOURCE_SUPPORT_PROMPT_VERSION, current_items=[item for _, item in selected])
        except (SafetyError, OSError) as error:
            failure = _interpretation_failure(error, local_code="SOURCE_REVIEW_LOCAL_FILE_FAILED")
            outcomes.extend({"task_id": question["task_id"], "concept_id": item["id"], "state": "waiting", "reason": failure} for question, item in chunk)
            continue
        for question, item in chunk:
            concept_id = item["id"]; answer = result["accepted"].get(concept_id)
            try:
                original_job = _bound_job(memory.snapshot(), payload, item,
                                         getattr(runner, "identity", "codex-chatgpt-high-v1"))
            except (SafetyError, OSError) as error:
                outcomes.append({"task_id": question["task_id"], "concept_id": concept_id,
                                 "state": "waiting", "reason": _interpretation_failure(error, local_code="SOURCE_REVIEW_LOCAL_FILE_FAILED"), "job_id": job_id})
                continue
            answer_job = original_job
            if answer is None:
                failure = result["failures"][concept_id]
                if runner is not None and failure in QUALITY_FAILURES | {"AI_RELEVANCE_RESULT_MISSING", "AI_FIT_REFERENCE_ALIASES_INVALID",
                    "AI_SOURCE_SUPPORT_SOURCES_INVALID", "AI_SOURCE_SUPPORT_REFERENCE_INVALID",
                    "AI_SOURCE_SUPPORT_UNSUPPORTED", "AI_SOURCE_SUPPORT_RESULT_MISSING"}:
                    from .checklist_repair import _run_repair
                    repaired = dict(payload, items=[item], checklist_repair={
                        "scope": "one-source-relevance-contract-repair-v1", "original_job_id": original_job,
                        "concept_id": concept_id, "input_hash": digest(item), "failure_code": failure,
                        "rules": "Correct only missing or invalid supplied reference aliases and output shape. Use these same facts; do not target positive relevance."})
                    try:
                        answer_job, correction = _run_repair(memory, runner, repaired, memory.snapshot(), SOURCE_SUPPORT_PROMPT_VERSION)
                        answer = correction["accepted"].get(concept_id)
                        if answer is None: failure = correction["failures"][concept_id]
                    except (SafetyError, OSError) as error:
                        failure = _interpretation_failure(error, local_code="SOURCE_REVIEW_LOCAL_FILE_FAILED")
                if answer is None:
                    outcomes.append({"task_id": question["task_id"], "concept_id": concept_id, "state": "waiting", "reason": failure, "job_id": answer_job})
                    continue
            record_id = key("market-review", [concept_id, digest(item), REVIEW_VERSION, MODEL, SOURCE_SUPPORT_PROMPT_VERSION])
            evidence_hashes = {row["id"]: digest(source_evidence(state, row["id"])[2]) for row in item["observations"]}
            scope_hashes = {ref: digest(source_evidence(state, ref)[2]) for ref in question["observation_ids"]} if question.get("kind") == "saved_source_support" else evidence_hashes
            record = {"concept_id": concept_id, "description_hash": question["description_hash"], "input_hash": digest(item),
                      "job_id": answer_job, "reviewed_at": at,
                      "observation_ids": [row["id"] for row in item["observations"] if row["id"] in state["observations"]],
                      "semantic_ids": [row["semantic_id"] for row in item["observations"] if row["id"] in state["observations"]],
                      "observed_at": {row["id"]: row["observed_at"] for row in item["observations"]},
                      "input": deepcopy(item), "evidence_hashes": evidence_hashes,
                      "scope_evidence_hashes": scope_hashes,
                      "support_version": SOURCE_SUPPORT_VERSION, "source_support": answer["source_support"],
                      "relevance": answer["relevance"], "assessment_unchanged": True, "review_version": REVIEW_VERSION,
                      "model": MODEL, "prompt_version": SOURCE_SUPPORT_PROMPT_VERSION,
                      "retrieval": {"reviewed": len(item["observations"]), "omitted": max(0, len(question["unknown_ids"]) - len(item["observations"])),
                                    "method": "captured-source-rows-no-title-equality-gate"}}
            def unchanged(current, cid=concept_id, expected=question["description_hash"], hashes=scope_hashes):
                if digest(current["concepts"][cid]["description"]) != expected:
                    raise SafetyError("SOURCE_TARGET_DESCRIPTION_CHANGED")
                if any(digest(source_evidence(current, ref)[2]) != value for ref, value in hashes.items()):
                    raise SafetyError("SOURCE_SUPPORT_EVIDENCE_CHANGED")
            try:
                previous = memory.snapshot()["market_reviews"].get(record_id)
                if previous is None:
                    memory.commit(record_id, [change("market_reviews", record_id, record)], validate=unchanged)
                else:
                    original = dict(record, reviewed_at=previous["reviewed_at"])
                    if original != previous: raise SafetyError("SOURCE_REVIEW_RESULT_CONFLICT")
                outcomes.append({"task_id": question["task_id"], "concept_id": concept_id, "state": "reviewed", "job_id": answer_job})
            except (SafetyError, OSError) as error:
                outcomes.append({"task_id": question["task_id"], "concept_id": concept_id, "state": "waiting",
                                 "reason": _interpretation_failure(error, local_code="SOURCE_REVIEW_LOCAL_FILE_FAILED"), "job_id": answer_job})
    final = [row for row in _questions(memory.snapshot(), run_id=run_id, as_of=at, task_ids=task_ids, concept_ids=concept_ids)
             if row.get("kind") != "functional_source_scope"]
    for question in final:
        if question.get("kind") != "saved_source_support" and (question["relevant_ids"] or not question["unknown_ids"]):
            try: _bind(memory, question, as_of=at)
            except (SafetyError, OSError) as error:
                outcomes.append({"task_id": question["task_id"], "concept_id": question["concept_id"], "state": "waiting",
                                 "reason": _interpretation_failure(error, local_code="SOURCE_REVIEW_LOCAL_FILE_FAILED")})
    remaining = _questions(memory.snapshot(), run_id=run_id, as_of=at, task_ids=task_ids, concept_ids=concept_ids)
    return {"outcomes": outcomes, "reused": reused, "remaining_questions": len(remaining),
            "source_calls": 0, "checklist_changed": False, "client_publication_attempted": False}


def _current_supplier_record(state, row, saved, *, as_of):
    """Current saved meaning is bound to its original versioned F request."""
    from .research_ai import SUPPLIER_PROMPT_VERSION
    from .suppliers import VERSION
    try:
        request = state["jobs"].get(row.get("job_id"), {}).get("request", {})
        return (row.get("version") == VERSION and row.get("input") == saved["item"]
                and row.get("input_hash") == digest(saved["item"])
                and row.get("source_dates") == saved["page_dates"]
                and timestamp(row["checked_at"]) <= timestamp(as_of)
                and request.get("role") == "F" and request.get("model") == MODEL
                and request.get("prompt_version") == SUPPLIER_PROMPT_VERSION)
    except (KeyError, TypeError, SafetyError):
        return False


def _validated_supplier_captures(memory, captured, state):
    """Recheck exact originals against a fresh use-boundary view, no fetching."""
    from .supplier_capture_store import load_saved_supplier_capture
    valid, failures = {}, {}
    for path, saved in captured.items():
        try:
            capture = load_saved_supplier_capture(memory, path, state=state)
            if digest(capture) != saved["capture_hash"]:
                raise SafetyError("SUPPLIER_CAPTURE_ORIGINAL_CHANGED")
            valid[path] = saved
        except (SafetyError, OSError) as error:
            failures[path] = _interpretation_failure(error, local_code="SUPPLIER_REVIEW_LOCAL_FILE_FAILED")
    return valid, failures


def _supplier_capture_failures(failures):
    from pathlib import Path
    return [{"capture_id": Path(path).parent.name, "state": "not_interpreted", "reason": reason}
            for path, reason in failures.items()]


def _completed_supplier_quote_failure(memory, saved, state, *, as_of):
    """An exact ended correction is a limitation, not an unchecked page.

    This is a saved-only proof lookup. An uncertain execution, missing original
    or result completed after a historical cutoff cannot settle the search.
    """
    from .supplier_quote_repair import supplier_model_question
    question = supplier_model_question(memory, saved["item"], state=state)
    if (question["state"] != "terminal" or question["completed_at"] is None
            or timestamp(question["completed_at"]) > timestamp(as_of)):
        return None
    return {field: question[field] for field in
            ("original_job_id", "job_id", "scope_id", "failure", "completed_at")}


def _supplier_capture_scan_progress(scanned, remaining):
    """Bounded diagnostics only; no result credit or activity-clock change."""
    from .operations import record_event
    try:
        record_event("supplier_capture_scan", scanned=scanned, remaining=remaining, useful_result=False)
    except Exception:
        pass  # Optional recorder failures cannot stop original saved recovery.


def _bind_supplier_tasks(memory, captured, *, as_of, failed_captures=None, skipped_captures=None):
    """Settle per-target source results from the exact captured-page F records."""
    state = memory.snapshot(); results = []
    # F may have finished after selection. Re-read original capture hashes and
    # current product/brand/status once against this fresh binding view; the
    # initial scan view cannot grant current-source settlement. The ordinary
    # bind commit below still checks the exact current description itself.
    captured, capture_failures = _validated_supplier_captures(memory, captured, state)
    failed_captures, page_failures = _validated_supplier_captures(memory, failed_captures or {}, state)
    if skipped_captures is not None:
        skipped_captures.extend(_supplier_capture_failures({**capture_failures, **page_failures}))
    reviewed = {}
    for path, saved in captured.items():
        if "scoped_prepared" in saved:
            from .supplier_checks import current_supplier_capture_review
            try:
                current = current_supplier_capture_review(state, saved["scoped_prepared"], as_of=as_of)
            except (SafetyError, OSError) as error:
                failure = _interpretation_failure(error, local_code="SUPPLIER_REVIEW_LOCAL_FILE_FAILED")
                if skipped_captures is not None:
                    skipped_captures.extend(_supplier_capture_failures({path: failure}))
                continue
            if current["complete"]:
                relations = current["relationships"]
                relationship = next((kind for kind in ("exact_product", "relevant_company", "unclear") if kind in relations), "unrelated")
                reviewed[path] = {"concept_id": saved["item"]["product"]["concept_id"],
                    "urls": {page["url"] for page in saved["item"]["pages"]} | set(saved.get("requested_urls", [])),
                    "relationship": relationship}
            continue
        records = [row for row in state["supplier_assessments"].values()
                   if _current_supplier_record(state, row, saved, as_of=as_of)]
        if records:
            row = max(records, key=lambda value: timestamp(value["checked_at"]))
            reviewed[path] = {"concept_id": saved["item"]["product"]["concept_id"],
                              "urls": {page["url"] for page in saved["item"]["pages"]} | set(saved.get("requested_urls", [])),
                              "relationship": row["result"]["relevance"]["relationship"]}
        else:
            try:
                failure = _completed_supplier_quote_failure(memory, saved, state, as_of=as_of)
            except (SafetyError, OSError) as error:
                failure_reason = _interpretation_failure(error, local_code="SUPPLIER_REVIEW_LOCAL_FILE_FAILED")
                if skipped_captures is not None:
                    skipped_captures.extend(_supplier_capture_failures({path: failure_reason}))
                continue
            if failure is not None:
                reviewed[path] = {"concept_id": saved["item"]["product"]["concept_id"],
                    "urls": {page["url"] for page in saved["item"]["pages"]} | set(saved.get("requested_urls", [])),
                    "relationship": "unclear", "interpretation_failure": failure}
    # Settle company pages first so the same pass can close their original
    # search questions. A failed readable-page attempt is not an F result.
    resolved = {}
    tasks = sorted(state["query_tasks"].items(), key=lambda pair: (pair[1].get("route") != "company-pages", pair[0]))
    for task_id, task in tasks:
        if task.get("kind") != TASK_KIND or task.get("source") != "supplier" or task["status"] != "acquired":
            continue
        if any(timestamp(state["source_checks"][ref]["envelope"]["observed_at"]) > timestamp(as_of)
               for ref in task["receipt_refs"]):
            continue
        for cid, target in task["target_results"].items():
            if (target["state"] != "interpretation_pending"
                    or target["description_hash"] != digest(state["concepts"][cid]["description"])):
                continue
            refs = target["evidence_refs"]
            if not refs:
                continue  # No captured page is not an evaluated empty search.
            if task["route"] == "company-pages":
                answers = [reviewed[ref] for ref in refs if ref in reviewed and reviewed[ref]["concept_id"] == cid]
                unreadable = [failed_captures[ref] for ref in refs if ref in failed_captures
                              and failed_captures[ref]["concept_id"] == cid]
                if len(answers) + len(unreadable) != len(refs):
                    continue
                positive = any(row["relationship"] in {"exact_product", "relevant_company"} for row in answers)
                uncertain = bool(unreadable) or any(row["relationship"] == "unclear" for row in answers)
                outcome = "completed_relevant" if positive else "insufficient" if uncertain else "completed_no_results"
                reason = ("A reviewed captured page contains a related supplier. Unstated ordering or dispatch terms still need confirmation." if positive else
                          "The bounded supplier-page checks did not produce a usable matching lead; some pages could not be read. This is not proof that supply is absent." if unreadable else
                          "The supplier pages were read, but the saved answer and its one correction did not produce a usable assessment. This is not proof that supply is absent." if any(row.get("interpretation_failure") for row in answers) else
                          "The captured supplier pages were reviewed, but their product match remains unclear." if uncertain else
                          "The captured supplier pages were reviewed; none supplied a usable matching lead in this bounded search.")
            else:
                checks = {ref: state["source_checks"][ref] for ref in refs if ref in state["source_checks"]
                          and state["source_checks"][ref].get("kind") == "supplier_route_search"
                          and state["source_checks"][ref].get("concept_id") == cid
                          and state["source_checks"][ref].get("description_hash") == target["description_hash"]
                          and state["source_checks"][ref].get("route") == task["route"]}
                urls = {row["url"] for check in checks.values() for row in check["candidates"]}
                answers = [row for row in reviewed.values() if row["concept_id"] == cid and row["urls"] & urls]
                positive = any(row["relationship"] in {"exact_product", "relevant_company"} for row in answers)
                answered_urls = {url for row in answers for url in row["urls"]}
                if positive:
                    outcome = "completed_relevant"
                    reason = "This search produced a supplier destination with a reviewed matching product or company page. Fulfilment terms remain separate."
                elif urls and urls <= answered_urls:
                    uncertain = any(row["relationship"] == "unclear" for row in answers)
                    outcome = "insufficient" if uncertain else "completed_no_results"
                    reason = ("The captured supplier destinations were reviewed, but none established a usable matching lead." if uncertain else
                              "All returned destinations in this bounded search were reviewed; none supplied a matching lead. This is not proof that supply is absent.")
                else:
                    # Only the exact frozen downstream pass may close an
                    # original search whose candidates were bounded/failed.
                    # Unattempted or unrelated company work cannot do so.
                    children = [(child_id, child) for child_id, child in tasks
                                if child.get("kind") == TASK_KIND and child.get("route") == "company-pages"
                                and child.get("run_id") == task.get("run_id")
                                and child.get("targets", {}).get(cid) == target["description_hash"]
                                and checks and set(checks) <= set(child.get("request", {}).get("candidate_check_ids", {}).get(cid, []))
                                and urls <= set(child.get("request", {}).get("candidate_urls", {}).get(cid, []))
                                and timestamp(child["prepared_at"]) <= timestamp(as_of)]
                    if not children:
                        continue
                    child_id, child = max(children, key=lambda pair: (timestamp(pair[1]["prepared_at"]), pair[0]))
                    child_target = child.get("target_results", {}).get(cid, {})
                    child_state = resolved.get((child_id, cid), child_target.get("state"))
                    if (child_state not in {"completed_relevant", "completed_no_results", "insufficient", "failed", "blocked"}
                            or child_target.get("description_hash") != target["description_hash"] or not child["receipt_refs"]
                            or any(timestamp(state["source_checks"][ref]["envelope"]["observed_at"]) > timestamp(as_of)
                                   for ref in child["receipt_refs"])):
                        continue
                    outcome = "insufficient"
                    reason = ("This search produced destinations, but the bounded supplier-page checks did not produce a usable matching lead for them. "
                              "This is not proof that supply is absent. Company task: " + child_id + ".")
                if not checks:
                    continue
            try:
                check = bind_source_relevance(memory, task_id, [{"concept_id": cid,
                    "description_hash": target["description_hash"], "state": outcome,
                    "evidence_refs": refs, "reason": reason}], as_of=as_of)
                results.append({"task_id": task_id, "concept_id": cid, "state": outcome, "receipt_id": check})
                resolved[(task_id, cid)] = outcome
            except (SafetyError, OSError) as error:
                results.append({"task_id": task_id, "concept_id": cid, "state": "waiting",
                                "reason": _interpretation_failure(error, local_code="SUPPLIER_REVIEW_LOCAL_FILE_FAILED")})
    return results


def review_supplier_results(memory, runner=None, *, limit=5, as_of=None, skip_capture_ids=(), concept_ids=None,
                            scoped_concept_ids=()):
    """Interpret current captured supplier pages without another page request.

    The trusted supplier loader validates each original receipt and current
    product scope. Commercial terms use existing exact quotes; missing terms
    are not invented. Failed/corrupt/stale bundles are reported individually.
    Live calls use the completion clock for final saved-only binding; an
    explicit historical cutoff stays fixed throughout the call.
    """
    from .supplier_capture_store import load_saved_supplier_capture
    from .suppliers import extract_supplier_terms
    from .storage import safe_path
    if type(limit) is not int or not 1 <= limit <= 50:
        raise SafetyError("SOURCE_REVIEW_LIMIT_INVALID")
    if not isinstance(skip_capture_ids, (list, tuple, set)) or any(not isinstance(row, str) for row in skip_capture_ids):
        raise SafetyError("SOURCE_REVIEW_TARGETS_INVALID")
    state = memory.snapshot()
    if concept_ids is not None and (not isinstance(concept_ids, (list, tuple, set))
            or any(not isinstance(row, str) or row not in state["concepts"] for row in concept_ids)):
        raise SafetyError("SOURCE_REVIEW_TARGETS_INVALID")
    scope_ids = None if concept_ids is None else set(concept_ids)
    if (not isinstance(scoped_concept_ids, (list, tuple, set))
            or any(not isinstance(cid, str) or cid not in state["concepts"] for cid in scoped_concept_ids)
            or scope_ids is not None and not set(scoped_concept_ids) <= scope_ids):
        raise SafetyError("SOURCE_REVIEW_TARGETS_INVALID")
    if scoped_concept_ids:
        from .functional_types import effective_functional_definition
        if any(not effective_functional_definition(state, cid)["accepted"] for cid in scoped_concept_ids):
            raise SafetyError("SUPPLIER_FUNCTIONAL_DEFINITION_REQUIRED")
    at = utc_now() if as_of is None else as_of; timestamp(at)
    pending, scoped_pending, skipped, reused, captured, failed_captures = [], [], [], [], {}, {}
    terminal_inputs = {}
    # Each named directory is a previously captured supplier, not a nominated
    # result heading. Loading it is read-only and cannot fetch a missing page.
    root = safe_path(memory.root, "supplier_pages")
    paths = sorted(root.glob("supplier-capture-*/capture.json"))
    _supplier_capture_scan_progress(0, len(paths))
    for scanned, path in enumerate(paths, 1):
        try:
            capture = load_saved_supplier_capture(memory, path, state=state)
        except (SafetyError, OSError) as error:
            skipped.append({"capture_id": path.parent.name, "state": "not_interpreted",
                            "reason": _interpretation_failure(error, local_code="SUPPLIER_REVIEW_LOCAL_FILE_FAILED")})
            continue
        finally:
            if scanned % 8 == 0 or scanned == len(paths):
                _supplier_capture_scan_progress(scanned, len(paths) - scanned)
        if scope_ids is not None and capture["product"]["concept_id"] not in scope_ids:
            continue
        if capture["bundle"]["item"] is None:
            dates = [row.get("checked_at") for row in capture["bundle"]["receipts"] if row.get("checked_at")]
            try:
                if all(timestamp(value) <= timestamp(at) for value in dates):
                    failed_captures[str(path)] = {"concept_id": capture["product"]["concept_id"],
                        "capture_hash": digest(capture)}
            except SafetyError:
                pass
            skipped.append({"capture_id": capture["capture_id"], "state": "not_interpreted", "reason": "SUPPLIER_CAPTURE_NO_USABLE_PAGES"})
            continue
        item = capture["bundle"]["item"]
        page_dates = {page["id"]: next(row["checked_at"] for row in capture["bundle"]["receipts"] if row.get("id") == page["id"])
                      for page in item["pages"]}
        saved = {"item": item, "page_dates": page_dates, "capture_id": capture["capture_id"],
                 "requested_urls": capture["request"]["lead"]["urls"],
                 "capture_path": str(path), "capture_hash": digest(capture)}
        if scope_ids is not None and item["product"]["concept_id"] not in scope_ids:
            continue
        if capture["product"]["concept_id"] in scoped_concept_ids:
            from .supplier_checks import prepare_supplier_candidates, current_supplier_capture_review
            try:
                prepared = prepare_supplier_candidates(memory, path, state=state)
                complete = current_supplier_capture_review(state, prepared, as_of=at)["complete"]
                # A capture can fail part-way through its page checks. Do
                # not expose partial pending/reused inputs before they all
                # pass this item-local preparation boundary.
                pending_pages = ([] if complete else [entry for entry in prepared
                    if not current_supplier_capture_review(state, [entry], as_of=at)["complete"]])
            except (SafetyError, OSError) as error:
                skipped.append({"capture_id": saved["capture_id"], "state": "not_interpreted",
                    "reason": _interpretation_failure(error, local_code="SUPPLIER_REVIEW_LOCAL_FILE_FAILED")})
                continue
            saved["scoped_prepared"] = prepared
            captured[str(path)] = saved
            if complete:
                reused.append(saved["capture_id"])
            else:
                # Keep page-sized model input separate from the capture's
                # date-bound applications. Already answered siblings need no
                # admission and an identical recapture can apply saved work.
                scoped_pending.extend(pending_pages)
            continue
        captured[str(path)] = saved
        matches = [row for row in state["supplier_assessments"].values()
                   if _current_supplier_record(state, row, saved, as_of=at)]
        if matches:
            reused.append(saved["capture_id"])
        else:
            try:
                failure = _completed_supplier_quote_failure(memory, saved, state, as_of=at)
            except (SafetyError, OSError) as error:
                skipped.append({"capture_id": saved["capture_id"], "state": "not_interpreted",
                    "reason": _interpretation_failure(error, local_code="SUPPLIER_REVIEW_LOCAL_FILE_FAILED")})
                continue
            if failure is not None:
                terminal_inputs[item["id"]] = dict(failure, capture_id=saved["capture_id"])
            else:
                pending.append(saved)
    initial_terminal_ids = set(terminal_inputs)
    selected = [row for row in pending if row["capture_id"] not in set(skip_capture_ids)][:limit]
    result = {"accepted": {}, "failures": {}, "failure_job_ids": {}, "reused": []}
    if selected:
        # Only the small selected legacy group is execution input. One fresh
        # view rechecks its original pages/current scope; a changed sibling
        # does not block an independently valid capture's saved F recovery.
        checked, failures = _validated_supplier_captures(memory,
            {row["capture_path"]: row for row in selected}, memory.snapshot())
        healthy = [row for row in selected if row["capture_path"] in checked]
        if healthy:
            originals = {row["item"]["id"]: row for row in healthy}

            def current_guard(original_item, fresh_state):
                # A completed model reply belongs to its exact original
                # pages, not whatever a product happens to mean at save time.
                # Extraction calls this both before execution/reuse and from
                # the assessment commit's fresh-state validation boundary.
                saved = originals.get(original_item["id"])
                if saved is None or saved["item"] != original_item:
                    raise SafetyError("SUPPLIER_CAPTURE_ORIGINAL_CHANGED")
                _, changed = _validated_supplier_captures(memory,
                    {saved["capture_path"]: saved}, fresh_state)
                if changed:
                    raise SafetyError(changed[saved["capture_path"]])

            result = extract_supplier_terms(memory, [row["item"] for row in healthy], runner,
                source_dates={row["item"]["id"]: row["page_dates"] for row in healthy},
                current_guard=current_guard)
        result["failures"].update({row["item"]["id"]: failures[row["capture_path"]]
            for row in selected if row["capture_path"] in failures})
        skipped.extend(_supplier_capture_failures(failures))
        for path in failures:
            captured.pop(path, None)
    selected_scoped = [row for row in scoped_pending if row["capture_id"] not in set(skip_capture_ids)][:max(0, limit - len(selected))]
    if selected_scoped:
        from .supplier_checks import extract_supplier_candidates
        scoped_result = extract_supplier_candidates(memory, selected_scoped, runner)
        for field in ("accepted", "failures", "failure_job_ids"):
            result[field].update(scoped_result[field])
        result["reused"] += scoped_result["reused"]
    # F records retain their real completion time, which may be after the
    # capture-selection cutoff. A live call can bind its new saved result now
    # without backdating F or rereading a source. Historical callers must not
    # adopt a result completed after their explicitly requested cutoff.
    binding_at = at if as_of is not None else utc_now()
    timestamp(binding_at)
    # A newly ended unusable correction settles only its exact current capture.
    # Keep its failure and original job proof visible; do not turn it into a
    # supplier recommendation or another five-minute model question.
    fresh_state = memory.snapshot() if selected else state
    checked, failures = _validated_supplier_captures(memory,
        {row["capture_path"]: row for row in selected}, fresh_state)
    skipped.extend(_supplier_capture_failures(failures))
    for saved in checked.values():
        if saved["item"]["id"] in result["accepted"]:
            continue
        try:
            failure = _completed_supplier_quote_failure(memory, saved, fresh_state, as_of=binding_at)
        except (SafetyError, OSError) as error:
            result["failures"][saved["item"]["id"]] = _interpretation_failure(
                error, local_code="SUPPLIER_REVIEW_LOCAL_FILE_FAILED")
            continue
        if failure is not None:
            terminal_inputs[saved["item"]["id"]] = dict(failure, capture_id=saved["capture_id"])
    bindings = _bind_supplier_tasks(memory, captured, as_of=binding_at,
        failed_captures=failed_captures, skipped_captures=skipped)
    invalid_capture_ids = {row["capture_id"] for row in skipped}
    reused = [capture_id for capture_id in reused if capture_id not in invalid_capture_ids]
    invalid_terminal = {item_id: proof for item_id, proof in terminal_inputs.items()
                        if proof["capture_id"] in invalid_capture_ids}
    for item_id, proof in invalid_terminal.items():
        terminal_inputs.pop(item_id)
        result["failures"][item_id] = next(row["reason"] for row in skipped
            if row["capture_id"] == proof["capture_id"])
    newly_terminal = {row["item"]["id"] for row in pending} & set(terminal_inputs)
    return {**result, "terminal_inputs": terminal_inputs,
            "reused_capture_ids": reused, "skipped_captures": skipped, "source_bindings": bindings,
            "selected_capture_ids": list(dict.fromkeys(row["capture_id"] for row in selected + selected_scoped)),
            "selected_inputs": [{"capture_id": row["capture_id"], "item_id": row["item"]["id"]} for row in selected + selected_scoped],
            "remaining_inputs": (len(pending) + len(scoped_pending) - len(result["accepted"])
                                 - len(newly_terminal) + len(initial_terminal_ids - set(terminal_inputs))),
            "source_calls": 0, "client_publication_attempted": False}
