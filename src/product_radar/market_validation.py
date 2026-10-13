"""Associate saved marketplace evidence without recollection or checklist edits."""

import re
from copy import deepcopy
from datetime import timedelta
from itertools import combinations

from .discovery import latest_observations, normalise, tokens
from .brand_reviews import current_brand
from .models import SafetyError, digest, identifier, prepared_request, request_stage, timestamp, utc_now
from .research_ai import Jobs, MODEL, prompt_version
from .research_flow import problem
from .research_memory import import_saved_attempt, key
from .storage import Journal, load_json, safe_path

REVIEW_VERSION = "marketplace-relevance-v2"
RELEVANCE_PROMPT_VERSION = prompt_version("E", {"mode": "marketplace_relevance_only", "criteria": []})
SOURCE_SUPPORT_VERSION = "captured-source-support-v3"
FUNCTIONAL_MARKET_CONTRACT = "functional-market-scope-v1"
PRIORITY_SUPPORT_CONTRACT = "functional-priority-support-v1"
FUNCTIONAL_MATCH_METHOD = "functional_source_match_v4"
FUNCTIONAL_SUPPORT_METHOD = "functional_source_support_v4"


def source_evidence(state, evidence_id):
    """Resolve an original captured row or chart, never a fabricated observation."""
    if evidence_id in state["observations"]:
        row = state["observations"][evidence_id]
        return row["source"], row["observed_at"], row
    if evidence_id in state["trends_evidence"]:
        row = state["trends_evidence"][evidence_id]
        return "google-trends", row.get("parsed", {}).get("metadata", {}).get("collected_at"), row
    return None, None, None


def current_source_support(state, concept_id, *, as_of):
    """Latest current, reference-bound interpretation; unknown is not negative.

    An interpretation cannot travel to changed source facts merely because a
    title or semantic meaning is reused. Older source-E v2 records retain their
    relevance meaning and do not acquire invented support judgments.
    """
    from .research_ai import SOURCE_SUPPORT_PROMPT_VERSION
    from .opportunity_policy import SOURCE_PLATFORMS
    current_hash, now = digest(state["concepts"][concept_id]["description"]), timestamp(as_of)
    latest = {}
    for insertion, (review_id, review) in enumerate(state["market_reviews"].items()):
        if (review.get("concept_id") != concept_id or review.get("description_hash") != current_hash
                or review.get("review_version") != REVIEW_VERSION or review.get("support_version") != SOURCE_SUPPORT_VERSION
                or review.get("prompt_version") != SOURCE_SUPPORT_PROMPT_VERSION):
            continue
        try:
            reviewed_at = timestamp(review["reviewed_at"])
            if reviewed_at > now:
                continue
            evidence = review["input"]["observations"]
            if review["input_hash"] != digest(review["input"]) or not isinstance(review["evidence_hashes"], dict):
                continue
            context = {}
            for row in evidence:
                source, captured_at, original = source_evidence(state, row["id"])
                if (source != row.get("source") or original is None or captured_at is None
                        or review["evidence_hashes"].get(row["id"]) != digest(original)
                        or not 0 <= (now - timestamp(captured_at)).total_seconds() <= 7 * 86400):
                    raise SafetyError("SOURCE_SUPPORT_EVIDENCE_CHANGED")
                context.setdefault(source, []).append(row["id"])
            if set(review["evidence_hashes"]) != {row["id"] for row in evidence}:
                continue
            scope_ids = {}
            for evidence_id, expected_hash in review.get("scope_evidence_hashes", review["evidence_hashes"]).items():
                source, captured_at, original = source_evidence(state, evidence_id)
                if (original is None or captured_at is None or digest(original) != expected_hash
                        or not 0 <= (now - timestamp(captured_at)).total_seconds() <= 7 * 86400):
                    raise SafetyError("SOURCE_SUPPORT_EVIDENCE_CHANGED")
                scope_ids.setdefault(source, []).append(evidence_id)
            relevant = {row["id"] for row in review["relevance"]
                        if row["relationship"] in ("exact_product", "relevant_family")}
            for support in review["source_support"]:
                source, refs = support["source"], support["references"]
                if (source not in SOURCE_PLATFORMS or source not in context or not isinstance(refs, list)
                        or len(set(refs)) != len(refs) or not set(refs) <= relevant
                        or not set(refs) <= set(context[source])
                        or support["judgment"] not in ("reasonably_positive", "neutral", "weak", "unclear")
                        or not isinstance(support["reason"], str) or not support["reason"].strip()
                        or support["basis"] not in ("observed", "inferred", "not_stated")
                        or support["judgment"] in ("reasonably_positive", "weak")
                        and (not refs or support["basis"] not in ("observed", "inferred"))):
                    continue
                order = reviewed_at, review["input_hash"], insertion
                value = dict(support, review_id=review_id, reviewed_at=review["reviewed_at"],
                             context_ids=sorted(context[source]),
                             scope_ids=sorted(scope_ids.get(source, context[source])),
                             evidence_hashes={ref: review["evidence_hashes"][ref] for ref in context[source]})
                if source not in latest or order > latest[source][0]:
                    latest[source] = order, value
        except (KeyError, TypeError, ValueError, SafetyError):
            # A malformed optional interpretation cannot break independent
            # acquisition lanes or silently certify its opinion.
            continue
    return [latest[source][1] for source in sorted(latest)]


def _functional_originals(state, row):
    return [(ref, source_evidence(state, ref["id"])) for ref in row["references"]
            if ref["table"] in ("observations", "trends_evidence")]


def _functional_match(row, ref):
    if row["method"] == "operator_source_review":
        return True
    from .research_ai import FUNCTIONAL_SOURCE_PROMPT_VERSION
    proof = row.get("job_proof") or {}
    answer = proof.get("accepted_item")
    if (row["method"] != FUNCTIONAL_MATCH_METHOD or row["support"] != "insufficient"
            or proof.get("role") != "E" or proof.get("prompt_version") != FUNCTIONAL_SOURCE_PROMPT_VERSION
            or not isinstance(answer, dict) or digest(answer) != proof.get("accepted_item_hash")):
        return False
    expected = {"id": ref, "scope": row["scope"], "relationship": row["relationship"],
                "reason": row["reason"], "limitations": row["limitations"]}
    return [value for value in answer.get("relevance", []) if value.get("id") == ref] == [expected]


def _functional_support(row):
    """Authenticate the aggregate opinion, not a mere successful model job."""
    from .research_ai import FUNCTIONAL_SOURCE_PROMPT_VERSION
    proof = row.get("job_proof") or {}
    answer = proof.get("accepted_item")
    if (row.get("method") != FUNCTIONAL_SUPPORT_METHOD or proof.get("role") != "E"
            or proof.get("prompt_version") != FUNCTIONAL_SOURCE_PROMPT_VERSION
            or not isinstance(answer, dict) or digest(answer) != proof.get("accepted_item_hash")):
        return None
    source = "google-trends" if row["source"] == "trends" else row["source"]
    refs = sorted(ref["id"] for ref in row["references"] if ref["table"] in ("observations", "trends_evidence"))
    judgments = {"positive": "reasonably_positive", "neutral": "neutral", "not_positive": "weak", "insufficient": "unclear"}
    candidates = [value for value in answer.get("source_support", []) if value.get("source") == source
                  and sorted(value.get("references", [])) == refs and value.get("judgment") == judgments[row["support"]]
                  and value.get("reason") == row["reason"]]
    if len(candidates) != 1:
        return None
    value = candidates[0]
    if (value.get("basis") not in ("observed", "inferred", "not_stated")
            or value["judgment"] in ("reasonably_positive", "weak")
            and (not refs or value["basis"] not in ("observed", "inferred"))):
        return None
    return deepcopy(value)


def functional_market_context(state, concept_id, *, as_of):
    """Read accepted type-scoped source facts without inheriting legacy votes.

    Work coverage and supplier/offer/broad context are retained separately.
    Original source-item replacements and original capture dates govern reuse;
    a later interpretation cannot refresh an old page or split a multi-row
    positive judgment into independent confirmations.
    """
    from .functional_types import effective_functional_definition
    from .research_scope import validated_scoped_evidence
    from .discovery import compute_market_signals
    meaning = effective_functional_definition(state, concept_id)
    if not meaning["accepted"]:
        return None
    now = timestamp(as_of)
    rows = validated_scoped_evidence(state, concept_id, as_of=as_of)
    latest = latest_observations(state["observations"], as_of)
    # A changed chart for the same actual query governs before subject filtering.
    latest_charts = {}
    for evidence_id, evidence in state.get("trends_evidence", {}).items():
        if evidence.get("evidence_kind") == "related_queries":
            continue
        metadata = evidence.get("parsed", {}).get("metadata", {})
        try:
            captured = timestamp(metadata["collected_at"])
        except (KeyError, TypeError, ValueError, SafetyError):
            continue
        if not 0 <= (now - captured).total_seconds() <= 7 * 86400:
            continue
        identity = digest({key: metadata.get(key) for key in ("term", "geo", "time_range", "property", "category")})
        # A later failed export is work history, not a replacement chart. A
        # successfully parsed zero/negative chart does replace an older one.
        order = evidence.get("parsed", {}).get("state") == "Usable", captured, evidence_id
        if identity not in latest_charts or order > latest_charts[identity][0]:
            latest_charts[identity] = order, evidence_id
    current_ids = set(latest) | {row[1] for row in latest_charts.values()}
    matches = {}
    for row in rows:
        if row["method"] not in (FUNCTIONAL_MATCH_METHOD, "operator_source_review"):
            continue
        for ref, (source, captured_at, original) in _functional_originals(state, row):
            if original is None or source == "supplier" or ref["id"] not in current_ids or not _functional_match(row, ref["id"]):
                continue
            order = timestamp(row["reviewed_at"]), row["revision"], row["id"]
            if ref["id"] not in matches or order > matches[ref["id"]][0]:
                matches[ref["id"]] = order, row
    accepted = {ref: row for ref, (_, row) in matches.items()
                if row["scope"] == "narrow_type" and row["admissibility"] == "accepted"
                and row["relationship"] in ("exact_product", "relevant_family")}
    observations = {ref: state["observations"][ref] for ref in accepted if ref in state["observations"]}
    relevance = {row["semantic_id"]: accepted[ref]["relationship"] for ref, row in observations.items()}
    charts = [dict(id=ref, **state["trends_evidence"][ref]) for ref in accepted if ref in state.get("trends_evidence", {})]
    opinions = {}
    for row in rows:
        if row["scope"] != "narrow_type" or row["admissibility"] != "accepted":
            continue
        opinion = _functional_support(row)
        if opinion is None or not opinion["references"] or not set(opinion["references"]) <= set(accepted):
            continue
        originals = _functional_originals(state, row)
        if not originals or any(ref["id"] not in current_ids or captured_at is None
                or not 0 <= (now - timestamp(captured_at)).total_seconds() <= 7 * 86400
                for ref, (_, captured_at, _) in originals):
            continue
        source = opinion["source"]
        order = max(timestamp(value[1][1]) for value in originals), timestamp(row["reviewed_at"]), row["id"]
        value = dict(opinion, review_id=row["id"], reviewed_at=row["reviewed_at"])
        if source not in opinions or order > opinions[source][0]:
            opinions[source] = order, value
    market = compute_market_signals(observations, relevance, as_of, charts,
                                    source_support=[value[1] for value in opinions.values()])
    selected_rows = {row["id"]: row for row in accepted.values()}
    selected_rows.update({value[1]["review_id"]: next(row for row in rows if row["id"] == value[1]["review_id"])
                          for value in opinions.values()})
    dependencies = {table: {} for table in ("source_checks", "observations", "captures", "trends_evidence")}
    for row in selected_rows.values():
        dependencies["source_checks"][row["id"]] = digest({key: value for key, value in row.items() if key != "id"})
        for ref in row["references"]:
            dependencies[ref["table"]][ref["id"]] = ref["hash"]
    return {"contract": FUNCTIONAL_MARKET_CONTRACT, "concept_id": concept_id, "meaning_hash": meaning["meaning_hash"],
            "market": market, "matches": {ref: {"scope_review_id": row["id"], "scope_review_hash": row["record_hash"],
                "relationship": row["relationship"], "scope": row["scope"], "reason": row["reason"],
                "limitations": deepcopy(row["limitations"])} for ref, row in accepted.items()},
            "scoped_evidence": list(selected_rows.values()), "dependencies": dependencies,
            "context_reviews": [row["id"] for row in rows if row["id"] not in selected_rows],
            "scope_hash": digest({"meaning_hash": meaning["meaning_hash"],
                "heads": {row["binding_key"]: row["record_hash"] for row in rows},
                "current_originals": {ref: digest(source_evidence(state, ref)[2]) for ref in sorted(current_ids & set(matches))}})}


def functional_priority_support(state, context, fit, *, as_of):
    """Choose the latest-expiring complete sufficient witness, not extra gates."""
    from .opportunity_policy import evaluate_opportunity
    from .marketplace_browser import verified_list_observation
    opportunity = evaluate_opportunity(fit, context["market"])
    if not opportunity["eligible"]:
        return {"opportunity": opportunity, "priority_support": None,
                "priority_support_refs": [], "support_valid_until": None}
    by_id = {row["id"]: row for row in context["scoped_evidence"]}
    candidates = {}
    for source, signal in context["market"]["sources"].items():
        if not signal["available"]:
            continue
        groups = []
        positive_needed = opportunity["opportunity_priority"] in (3, 4)
        if not positive_needed:
            groups = [[ref] for ref in signal["used_ids"]]
        else:
            if signal.get("positive") is True:
                if source in ("amazon", "flipkart"):
                    groups.extend([[ref] for ref in signal["used_ids"] if ref in state["observations"]
                                   and verified_list_observation(state["observations"][ref])])
                else:
                    groups.append(list(signal["used_ids"]))
            if signal.get("evidence_support") == "reasonably_positive":
                groups.extend([list(row["references"]) for row in context["market"].get("source_support", [])
                               if row["source"] == source and row["judgment"] == "reasonably_positive"])
        choices = []
        for refs in groups:
            originals, reviews = [], set()
            for ref in sorted(set(refs)):
                platform, captured_at, original = source_evidence(state, ref)
                if platform != source or original is None or ref not in context["matches"]:
                    raise SafetyError("FUNCTIONAL_MARKET_WITNESS_REFERENCE_INVALID")
                table = "observations" if ref in state["observations"] else "trends_evidence"
                originals.append({"table": table, "id": ref, "hash": digest(original), "source": source, "observed_at": captured_at})
                reviews.add(context["matches"][ref]["scope_review_id"])
            for support in context["market"].get("source_support", []):
                if positive_needed and support["source"] == source and support["judgment"] == "reasonably_positive" and set(support["references"]) == set(refs):
                    reviews.add(support["review_id"])
            expiry = min(timestamp(ref["observed_at"]) + timedelta(days=7) for ref in originals)
            choices.append({"source": source, "kind": "positive" if positive_needed else "available",
                "references": originals, "scope_reviews": [{"id": rid, "hash": by_id[rid]["record_hash"]} for rid in sorted(reviews)],
                "valid_until": expiry.isoformat()})
        if choices:
            candidates[source] = max(choices, key=lambda row: (timestamp(row["valid_until"]), digest(row)))
    count = 2 if opportunity["opportunity_priority"] in (1, 2) else 1
    groups = [list(values) for values in combinations(candidates.values(), count)]
    if not groups:
        raise SafetyError("FUNCTIONAL_MARKET_WITNESS_REQUIRED")
    chosen = max(groups, key=lambda group: (min(timestamp(row["valid_until"]) for row in group),
                                            tuple(sorted(row["source"] for row in group))))
    chosen.sort(key=lambda row: row["source"])
    witness = {"contract": PRIORITY_SUPPORT_CONTRACT, "meaning_hash": context["meaning_hash"],
               "priority": opportunity["opportunity_priority"], "sources": chosen,
               "valid_until": min(timestamp(row["valid_until"]) for row in chosen).isoformat()}
    validate_priority_support(witness, opportunity=opportunity, meaning_hash=context["meaning_hash"], as_of=as_of)
    return {"opportunity": opportunity, "priority_support": witness,
            "priority_support_refs": sorted({ref["id"] for row in chosen for ref in row["references"]}),
            "support_valid_until": witness["valid_until"]}


def validate_priority_support(witness, *, opportunity, meaning_hash, as_of=None):
    """Validate frozen witness shape, exact dates and sufficient distinct sites.

    Original hash/currentness is rechecked by the selection's source revisions;
    this pure validator is also usable without mutable research storage.
    """
    from .opportunity_policy import CURRENT_OPPORTUNITY_POLICY_ID, opportunity_counts
    try:
        if (not isinstance(witness, dict) or set(witness) != {"contract", "meaning_hash", "priority", "sources", "valid_until"}
                or witness["contract"] != PRIORITY_SUPPORT_CONTRACT or witness["meaning_hash"] != meaning_hash
                or opportunity["opportunity_policy_id"] != CURRENT_OPPORTUNITY_POLICY_ID
                or witness["priority"] != opportunity["opportunity_priority"]):
            raise SafetyError("FUNCTIONAL_MARKET_WITNESS_INVALID")
        opportunity_counts(opportunity)
        needed = 2 if witness["priority"] in (1, 2) else 1 if witness["priority"] in (3, 4) else 0
        sources, seen, seen_refs = witness["sources"], [], set()
        if not needed or not isinstance(sources, list) or len(sources) != needed:
            raise SafetyError("FUNCTIONAL_MARKET_WITNESS_INVALID")
        for row in sources:
            if (set(row) != {"source", "kind", "references", "scope_reviews", "valid_until"}
                    or row["source"] not in opportunity["opportunity_basis"]["source_ids"]
                    or row["kind"] != ("available" if needed == 2 else "positive")
                    or needed == 1 and row["source"] not in opportunity["opportunity_basis"]["positive_source_ids"]
                    or not row["references"] or not row["scope_reviews"]):
                raise SafetyError("FUNCTIONAL_MARKET_WITNESS_INVALID")
            refs = []
            for ref in row["references"]:
                if (set(ref) != {"table", "id", "hash", "source", "observed_at"}
                        or ref["table"] not in ("observations", "trends_evidence") or ref["source"] != row["source"]
                        or ref["id"] not in opportunity["opportunity_basis"]["evidence_by_source"][row["source"]]
                        or ref["id"] in seen_refs or not re.fullmatch(r"[0-9a-f]{64}", ref["hash"])):
                    raise SafetyError("FUNCTIONAL_MARKET_WITNESS_REFERENCE_INVALID")
                identifier(ref["id"]); refs.append(timestamp(ref["observed_at"])); seen_refs.add(ref["id"])
            for proof in row["scope_reviews"]:
                if set(proof) != {"id", "hash"} or not re.fullmatch(r"[0-9a-f]{64}", proof["hash"]):
                    raise SafetyError("FUNCTIONAL_MARKET_WITNESS_REFERENCE_INVALID")
                identifier(proof["id"])
            if timestamp(row["valid_until"]) != min(refs) + timedelta(days=7):
                raise SafetyError("FUNCTIONAL_MARKET_WITNESS_DATE_INVALID")
            seen.append(row["source"])
        if seen != sorted(set(seen)) or timestamp(witness["valid_until"]) != min(timestamp(row["valid_until"]) for row in sources):
            raise SafetyError("FUNCTIONAL_MARKET_WITNESS_INVALID")
        if as_of is not None and (timestamp(as_of) > timestamp(witness["valid_until"])
                or any(timestamp(ref["observed_at"]) > timestamp(as_of) for row in sources for ref in row["references"])):
            raise SafetyError("FUNCTIONAL_MARKET_WITNESS_STALE")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SafetyError("FUNCTIONAL_MARKET_WITNESS_INVALID") from None
    return witness


def _signal_projection(market):
    return {source: {field: deepcopy(row[field]) for field in
            ("available", "positive", "used_ids", "evidence_support") if field in row}
            for source, row in market["sources"].items()}


def freeze_functional_market(state, context, *, as_of):
    """Selected scoped proof and actual originals, not a copy of whole history."""
    from .frozen_records import freeze_original_json
    records = {table: {} for table in ("observations", "captures", "trends_evidence", "source_checks")}
    scoped = {}
    for row in context["scoped_evidence"]:
        scoped[row["id"]] = freeze_original_json({key: deepcopy(value) for key, value in row.items() if key != "id"})
        for ref in row["references"]:
            records[ref["table"]][ref["id"]] = freeze_original_json(state[ref["table"]][ref["id"]])
    return {"contract": FUNCTIONAL_MARKET_CONTRACT, "concept_id": context["concept_id"],
            "meaning_hash": context["meaning_hash"], "as_of": as_of, "scope_hash": context["scope_hash"],
            "scoped_records": scoped, "records": records, "matches": deepcopy(context["matches"]),
            "source_signals": _signal_projection(context["market"]),
            "source_support": deepcopy(context["market"].get("source_support", []))}


def validate_frozen_functional_market(product):
    """Recompute frozen scoped facts, seven-site counts and complete witness.

    This authenticates the packet's internal proof. Canonical latest-head and
    release authority remain the live selection/root checks, not this offline
    projection and not the capture's favourable interpretation.
    """
    from .research_scope import (_verified, _record_id, _source_context, _field_values,
                                 _evidence_contract, _route_source)
    from .research_records import SCOPED_EVIDENCE_KIND
    from .discovery import compute_market_signals
    from .opportunity_policy import evaluate_opportunity, opportunity_fields
    from .fit_policy import FIT_POLICY_ID, evaluate_fit
    from .fit_description import FUNCTIONAL_INFERENCE_KIND
    from .frozen_records import read_original_json
    try:
        proof = product["functional_market_proof"]
        meaning = product["functional_definition"]["meaning_hash"]
        if (product.get("functional_market_contract") != FUNCTIONAL_MARKET_CONTRACT
                or set(proof) != {"contract", "concept_id", "meaning_hash", "as_of", "scope_hash", "scoped_records", "records", "matches", "source_signals", "source_support"}
                or proof["contract"] != FUNCTIONAL_MARKET_CONTRACT or proof["concept_id"] != product["concept_id"]
                or proof["meaning_hash"] != meaning or not re.fullmatch(r"[0-9a-f]{64}", proof["scope_hash"])):
            raise SafetyError("FUNCTIONAL_MARKET_PROOF_INVALID")
        at = timestamp(proof["as_of"])
        if set(proof["records"]) != {"observations", "captures", "trends_evidence", "source_checks"}:
            raise SafetyError("FUNCTIONAL_MARKET_PROOF_INVALID")
        records = {table: {rid: read_original_json(value) for rid, value in values.items()}
                   for table, values in proof["records"].items()}
        rows = {}
        used = {table: set() for table in records}
        for rid, frozen in proof["scoped_records"].items():
            row = read_original_json(frozen)
            _verified(row, SCOPED_EVIDENCE_KIND)
            if (rid != _record_id(row) or row["concept_id"] != product["concept_id"]
                    or row["subject"] != {"kind": "concept", "concept_id": product["concept_id"], "definition_hash": meaning}
                    or timestamp(row["reviewed_at"]) > at or row["source"] != _route_source(row["route"])
                    or row["admissibility"] != _evidence_contract(row["scope"], row["relationship"], row["support"])):
                raise SafetyError("FUNCTIONAL_MARKET_PROOF_INVALID")
            contexts = []
            for ref in row["references"]:
                original = records[ref["table"]][ref["id"]]
                if digest(original) != ref["hash"]:
                    raise SafetyError("FUNCTIONAL_MARKET_ORIGINAL_CHANGED")
                used[ref["table"]].add(ref["id"])
                contexts.append(_source_context(ref["table"], ref["id"], original))
                if ref["table"] == "observations":
                    capture = records["captures"][original["capture_id"]]
                    if any(original.get(field) != capture.get(field) for field in ("source", "acquisition_id", "observed_at")):
                        raise SafetyError("FUNCTIONAL_MARKET_ORIGINAL_CHANGED")
            if (contexts != row["source_contexts"] or any(
                    timestamp(context["observed_at"]) > timestamp(row["reviewed_at"])
                    or context["source"] != row["source"]
                    or context.get("route") is not None and context["route"] != row["route"] for context in contexts)):
                raise SafetyError("FUNCTIONAL_MARKET_ORIGINAL_CHANGED")
            fields = [{"reference": value["reference"], "path": value["path"]} for value in row["usable_fields"]]
            if _field_values(records, fields, row["references"]) != row["usable_fields"]:
                raise SafetyError("FUNCTIONAL_MARKET_ORIGINAL_CHANGED")
            rows[rid] = dict(id=rid, **deepcopy(row))
        if any(set(records[table]) != ids for table, ids in used.items()):
            raise SafetyError("FUNCTIONAL_MARKET_EXTRA_ORIGINALS")
        matches = {}
        for ref, value in proof["matches"].items():
            row = rows[value["scope_review_id"]]
            expected = {"scope_review_id": row["id"], "scope_review_hash": row["record_hash"],
                "relationship": row["relationship"], "scope": row["scope"], "reason": row["reason"], "limitations": row["limitations"]}
            if (value != expected or row["scope"] != "narrow_type" or row["admissibility"] != "accepted"
                    or not _functional_match(row, ref) or ref not in {value["id"] for value in row["references"]}
                    or source_evidence(records, ref)[2] is None):
                raise SafetyError("FUNCTIONAL_MARKET_MATCH_INVALID")
            matches[ref] = expected
        observations = {ref: records["observations"][ref] for ref in matches if ref in records["observations"]}
        relevance = {value["semantic_id"]: matches[ref]["relationship"] for ref, value in observations.items()}
        charts = [dict(id=ref, **records["trends_evidence"][ref]) for ref in matches if ref in records["trends_evidence"]]
        for support in proof["source_support"]:
            row = rows[support["review_id"]]
            opinion = _functional_support(row)
            expected = dict(opinion or {}, review_id=row["id"], reviewed_at=row["reviewed_at"])
            if support != expected or not set(support["references"]) <= set(matches):
                raise SafetyError("FUNCTIONAL_MARKET_SUPPORT_INVALID")
            if any(not 0 <= (at - timestamp(source_evidence(records, ref)[1])).total_seconds() <= 7 * 86400 for ref in support["references"]):
                raise SafetyError("FUNCTIONAL_MARKET_SUPPORT_STALE")
        market = compute_market_signals(observations, relevance, proof["as_of"], charts, source_support=proof["source_support"])
        if _signal_projection(market) != proof["source_signals"]:
            raise SafetyError("FUNCTIONAL_MARKET_SIGNALS_CHANGED")
        expected_checks = [{"source": source, "state": "Available" if signal["available"] else "Insufficient",
            "detail": signal["reason"], "evidence_ids": signal["used_ids"], "url": None}
            for source, signal in market["sources"].items()]
        if product.get("market_checks") != expected_checks or product.get("market_label") != market["source_label"]:
            raise SafetyError("FUNCTIONAL_MARKET_DISPLAY_CHANGED")
        fit = evaluate_fit(product["criteria"], FIT_POLICY_ID, description=product["fit_description"],
                           description_contract=FUNCTIONAL_INFERENCE_KIND)
        opportunity = evaluate_opportunity(fit, market)
        if opportunity_fields(opportunity) != {key: product[key] for key in opportunity_fields(opportunity)}:
            raise SafetyError("FUNCTIONAL_MARKET_PRIORITY_CHANGED")
        context = {"meaning_hash": meaning, "market": market, "matches": matches, "scoped_evidence": list(rows.values())}
        expected = functional_priority_support(records, context, fit, as_of=proof["as_of"])
        for field in ("priority_support", "priority_support_refs", "support_valid_until"):
            if product.get(field) != expected[field]:
                raise SafetyError("FUNCTIONAL_MARKET_WITNESS_CHANGED")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SafetyError("FUNCTIONAL_MARKET_PROOF_INVALID") from None
    return product


def import_saved_marketplace(memory, attempt_id):
    """Reuse a genuine committed Amazon capture, including recorded failures."""
    identifier(attempt_id)
    with memory.journal.locked():
        attempt = memory.journal.state()[1].get(attempt_id)
        if not attempt:
            raise SafetyError("SAVED_CAPTURE_REQUIRED")
        prepared = prepared_request(load_json(safe_path(memory.journal.root, attempt_id) / "prepared.json"))
        if request_stage(prepared) != "amazon":
            raise SafetyError("SAVED_AMAZON_CAPTURE_REQUIRED")
    return import_saved_attempt(memory, attempt_id)


def _retrieval_tokens(value):
    # Only candidate retrieval uses these forms. A lexical hit is never an
    # automatic same-product or positive-market decision.
    words = {word[:-1] if len(word) > 4 and word.endswith("s") and not word.endswith("ss") else word
             for word in tokens(value)}
    # The general plural rule deliberately avoids short words; these actual
    # object forms are safe retrieval aliases, unlike standalone S notation.
    words = {{"mats": "mat", "pots": "pot", "shelve": "shelf"}.get(word, word) for word in words}
    if "airfryer" in words:
        words.update(("air", "fryer"))
    return words - {"kitchen", "home", "baby", "travel", "outdoor", "accessory", "accessories",
                                        "store", "essential", "quality", "premium", "best", "new"}


# Descriptive features can rank an already plausible listing, but must not
# admit it alone. This is a small retrieval vocabulary, not a product taxonomy.
_WEAK_ANCHORS = {
    "cover", "protector", "protect", "protection", "elastic", "stretch", "waterproof", "washable",
    "pack", "set", "model", "piece", "size", "colour", "color", "beige", "black", "white",
    "cotton", "fabric", "silicone", "plastic", "stainless", "steel", "bamboo", "soft", "durable",
    "easy", "convenient", "convenience", "storage", "organizer", "organiser", "holder", "rack",
    "dining", "cleaning", "powered", "hold", "used", "use", "over", "around", "help", "item",
    "people", "want", "small", "large", "keep", "keeps", "save", "space", "protects", "stretching",
    # Observed false anchors: Z/X shapes and S-hook notation, metal/stand
    # descriptions, table surfaces, hands-free draining and anti-slip claims
    # were admitting scales, bags, wall hooks, detergent, clocks and diapers.
    # These may rank a plausible object but do not establish one by themselves.
    "metal", "shape", "round", "cm", "mm", "feet", "inch", "inche", "count", "unit", "pc",
    "stand", "support", "tabletop", "freestanding", "strong", "heavy", "duty",
    "anti", "non", "slip", "free", "from", "hand", "liquid", "above", "floor", "ground",
    # Grammar and negation can remain in source facts, but are not objects:
    # "no-drill" must not match a no-tear shampoo or "no marks" diapers.
    "no", "not", "without", "by", "on", "at", "as", "into", "onto", "under", "after", "before",
    "this", "that", "these", "those", "its", "their", "your", "our", "is", "are", "was", "were",
    "be", "being", "been", "can", "may", "do", "does", "or", "also", "only",
}
_STORAGE_FORMS = {"caddy", "organizer", "organiser", "holder", "rack", "basket", "tray", "shelf", "storage"}


def _strong_tokens(words):
    return {word for word in words - _WEAK_ANCHORS
            if len(word) > 1 and not any(char.isdigit() for char in word)}


def _carrying_bag_anchor(label, function, listing, target_scope, title_scope):
    """Require a carrying object and its use, not colour or 'bag' alone.

    This only applies to the described personal-wash and meal carrying bags.
    Other bags keep their existing retrieval; E still judges mechanism/use.
    """
    forms = {"bag", "bags", "pouch", "pouche", "case", "carrier", "washbag"}
    toiletries = {"toiletry", "toiletrie", "cosmetic", "makeup", "dopp"}
    meals = {"lunch", "lunchbox", "tiffin", "meal"}
    context = label | function
    if not label & forms and not ("dopp" in label and label & {"kit", "kits"}):
        return None
    other_wash = bool((set(target_scope) | context) & {"laundry", "lingerie", "clothes", "clothe"})
    target_travel = bool(set(target_scope) & {"travel", "travelling", "traveling"})
    wash_name = "washbag" in label or ("wash" in label and target_travel)
    personal_wash = bool(context & toiletries or (wash_name and not other_wash))
    meal_bag = bool(context & meals or "cooler" in label)
    if not personal_wash and not meal_bag:
        return None
    # Dustbin/packaging bags do not become carrying bags through an offer,
    # colour, travel claim or generic food/wash word in the same title.
    if listing & {"garbage", "trash", "rubbish", "dustbin", "refuse", "disposable", "ziplock"}:
        return 0
    container = bool(listing & forms)
    if personal_wash:
        dopp_kit = "dopp" in listing and bool(listing & {"kit", "kits"})
        # A refill/sachet's bag is chemical packaging, not a travel carrying
        # product. A named cosmetic/toiletry container can still mention the
        # bottles or sachets it holds; E decides that candidate's relevance.
        if (listing & {"refill", "sachet"}
                and not ((container and listing & toiletries) or dopp_kit)):
            return 0
        if listing & {"laundry", "lingerie", "bra", "clothe"} and not listing & toiletries:
            return 0
        # 'Dopp kit' is itself the conventional name for this carrying
        # product, unlike an unspecified shampoo/body-wash gift kit.
        travel_wash = bool(listing & {"wash", "washing"} and set(title_scope) & {
            "hanging", "travel", "travelling", "traveling"})
        ordinary_washbag = bool("wash" in listing and listing & {"bag", "bags"}
                                and not listing & {"body", "face", "shampoo", "detergent", "cleanser"})
        wash_scope = bool(listing & toiletries or "washbag" in listing or travel_wash or ordinary_washbag)
        return 8 if (container and wash_scope) or dopp_kit else 0
    meal_scope = bool(listing & (meals | {"food", "cooler", "sandwich"}))
    return 8 if container and meal_scope else 0


def _primary_anchor_score(label, function, listing, *, title_scope=(), target_scope=(), title_text=""):
    """Cheap object anchors prevent broad feature matches wasting model rows.

    These aliases only widen retrieval. A seat cushion, sink rack or fryer
    basket still needs E to establish the actual function and mechanism.
    """
    context = label | function
    # Titles such as pink/printed lunch bags and blue travel wash bags need
    # their actual carrying use. These adjectives can only rank a candidate
    # after admission; wash, a colour or a generic bag cannot admit one.
    carrying_bag = _carrying_bag_anchor(label, function, listing, target_scope, title_scope)
    if carrying_bag is not None:
        return carrying_bag
    if "shelf" in label:
        # A shelf is the purchase unit; hooks/liners used on or to hang one
        # are accessories. "Shelf with hooks/brackets" remains a shelf.
        text = normalise(title_text)
        if (re.search(r"\bshel(?:f|ves)\s+(?:liner|lining|paper|bracket|hook)s?\b", text)
                or re.search(r"\b(?:hook|bracket)s?\s+for(?:\s+\w+){0,3}\s+shel(?:f|ves)\b", text)
                or re.search(r"\b(?:without|no)\s+(?:a\s+)?shel(?:f|ves)\b", text)):
            return 0
        if "shelf" in listing:
            return 10
        return 8 if "rack" in listing and listing & {"wall", "hanging", "display", "plant", "planter"} else 0
    nonfood = {"engine", "motor", "lubricant", "lubricating", "lubrication", "automotive",
               "machinery", "fragrance", "perfume", "aromatherapy", "diffuser", "hair", "skin",
               "cosmetic", "skincare", "soap", "detergent", "cleanser", "cleansing", "shampoo", "lotion", "powder"}
    culinary = {"cooking", "cook", "food", "salad", "edible", "culinary", "frying", "grilling", "baking", "bbq"}
    target_scope = set(target_scope) | context
    different_target = bool(target_scope & nonfood) or {"essential", "oil"} <= target_scope
    if ("oil" in label and label & {"spray", "sprayer", "dispenser", "mister"}
            and (not different_target or target_scope & culinary)):
        # Oil ingredients in baby care/cleaning are not an oil-dispensing
        # purchase unit. Preserve raw scope words (notably essential oil),
        # which general retrieval deliberately treats as weak vocabulary.
        title_scope = set(title_scope) | listing
        different_use = bool(title_scope & nonfood) or {"essential", "oil"} <= title_scope
        if different_use and not title_scope & culinary:
            return 0
        return 8 if listing & {"oil", "olive"} and listing & {
            "spray", "sprayer", "spraying", "dispenser", "dispense", "dispensing", "mister", "misting",
            "atomizer", "atomiser", "pourer", "pour", "pouring", "spout", "cruet"} else 0
    if "chain" in label and "hook" in context:
        # A chain-and-hook swing accessory is not an ordinary adhesive wall
        # hook merely because both titles contain "hook". Keep named swing
        # hooks as candidates even when the listing omits "chain".
        return 8 if "chain" in listing or ("hook" in listing and listing & {"swing", "jhula"}) else 0
    if "planter" in label and "stand" in context:
        return 8 if listing & {"plant", "planter", "pot"} and listing & {"stand", "rack", "holder", "shelf"} else 0
    if "rice" in context and "drainer" in label:
        # Food-straining object aliases widen retrieval, not E's eventual
        # same-function/mechanism decision. Liquid detergent is not an alias.
        return 8 if listing & {"drainer", "colander", "strainer"} else 0
    if "mat" in label and context & {"table", "placemat"}:
        # "Table" identifies a surface, not the mat itself. Keep the ordinary
        # placemat spelling without admitting clocks, same-brand decor or a
        # drawer liner merely because its title also uses "mat".
        return 8 if "placemat" in listing or ("mat" in listing and listing & {"table", "dining", "crochet"}) else 0
    if "mat" in label and "shower" in label:
        return 8 if listing & {"mat", "rug"} and listing & {"shower", "bath", "bathroom"} else 0
    if "chair" in label or ("chair" in context and context & {"cover", "protector", "slipcover"}):
        return 8 if listing & {"chair", "seat"} else 0
    if "sink" in context and context & _STORAGE_FORMS:
        storage = bool(listing & _STORAGE_FORMS)
        return 8 if storage and listing & {"sink", "basin", "sponge", "soap"} else 0
    if ("fryer" in label or {"air", "cooker"} <= label
            or (("fryer" in context or {"air", "cooker"} <= context) and context & {"liner", "basket"})):
        return 8 if "fryer" in listing or {"air", "cooker"} <= listing else 0
    shared_label = _strong_tokens(label) & _strong_tokens(listing)
    shared_function = _strong_tokens(function) & _strong_tokens(listing)
    # A shared product noun is useful; functional-only retrieval needs two
    # terms so one vague benefit cannot manufacture a plausible candidate.
    return 6 * len(shared_label) if shared_label else (min(5, 2 * len(shared_function)) if len(shared_function) >= 2 else 0)


def _listing_pool(state, as_of, *, include_context=False):
    from .marketplace_browser import verified_list_observation
    fresh = latest_observations(state["observations"], as_of)
    pool = {}
    for obs_id, observation in fresh.items():
        facts = observation["facts"]
        context = (include_context and (
            observation["source"] in ("amazon", "flipkart") and facts.get("list_type") == "product_search"
            or observation["source"] in ("blinkit", "zepto", "instamart") and facts.get("list_type") == "quickcommerce_context"))
        if not verified_list_observation(observation) and not context:
            continue
        semantic_id = observation["semantic_id"]
        # Relevance meaning reuses a stable original reference, while summary
        # arithmetic independently resolves actual latest acquisition dates.
        original_id, original = next((i, o) for i, o in state["observations"].items()
                                     if o["source"] == observation["source"] and o["semantic_id"] == semantic_id)
        pool[semantic_id] = (original_id, original)
    return pool


def plausible_marketplace_observations(description, observations, *, limit=5):
    """Return a primary-object shortlist; E remains the relevance decision."""
    label = _retrieval_tokens(description["label"])
    function = _retrieval_tokens(" ".join([description.get("function", ""), description.get("mechanism", "")]))
    features = _retrieval_tokens(" ".join(description.get("features", [])))
    target_scope = tokens(" ".join((description["label"], description.get("function", ""), description.get("mechanism", ""))))
    ranked = []
    for semantic_id, (obs_id, observation) in observations.items():
        title = observation["semantic"].get("title", "")
        listing = _retrieval_tokens(title)
        anchor = _primary_anchor_score(label, function, listing, title_scope=tokens(title), target_scope=target_scope, title_text=title)
        if anchor:
            # Tuple ordering keeps any number of material/benefit words below
            # the primary-object score, rather than adding them to admission.
            score = (anchor, len(_strong_tokens(label | function) & listing), min(2, len(features & listing)))
            ranked.append((score, semantic_id, obs_id, observation))
    ranked.sort(key=lambda row: (tuple(-part for part in row[0]), row[1]))
    return {"observations": [{"id": obs_id, "semantic_id": semantic_id, "source_text": observation["semantic"]}
                             for _, semantic_id, obs_id, observation in ranked[:limit]],
            "plausible": len(ranked), "omitted": max(0, len(ranked) - limit),
            "method": "bounded_primary_object_alias_overlap_not_identity"}


def review_marketplace(memory, runner=None, *, concept_ids=None, limit=5, observations_per_concept=5, as_of=None, include_context=False):
    """Review fresh saved listings through E; store only independent relevance.

    Default work targets current fit-passing candidates missing marketplace
    coverage. Explicit known concept IDs permit other useful owner-selected
    comparisons. Exact repeated meanings skip the model. New captures with the
    same title/URL meaning reuse original reference IDs; freshness is not reset.
    """
    if type(include_context) is not bool or type(limit) is not int or not 1 <= limit <= 20 or type(observations_per_concept) is not int or not 1 <= observations_per_concept <= 10:
        raise SafetyError("MARKET_REVIEW_LIMIT_INVALID")
    as_of = as_of or utc_now()
    timestamp(as_of)
    with Journal(memory.root).locked():
        return _review_marketplace(memory, runner, concept_ids, limit, observations_per_concept, as_of, include_context)


def _review_marketplace(memory, runner, concept_ids, limit, observations_per_concept, as_of, include_context=False):
    state = memory.snapshot()
    pool = _listing_pool(state, as_of, include_context=include_context)
    if concept_ids is not None:
        if (not isinstance(concept_ids, list) or len(concept_ids) > 200
                or any(not isinstance(value, str) for value in concept_ids) or len(set(concept_ids)) != len(concept_ids)):
            raise SafetyError("MARKET_CONCEPT_IDS_INVALID")
        for concept_id in concept_ids:
            identifier(concept_id)
            if concept_id not in state["concepts"]:
                raise SafetyError("KNOWN_CONCEPT_REQUIRED")
        chosen = sorted(concept_ids)
    else:
        from .research_flow import current_assessment_fit
        from .service import load_policy
        from .fit_policy import LEGACY_POLICY_ID
        policy_id = load_policy().get("fit_policy_id", LEGACY_POLICY_ID)
        fits = {concept_id: current_assessment_fit(state, concept_id, expected_policy_id=policy_id)
                for concept_id in state["concepts"]}
        excluded_assessments = set()
        for concept_id, assessment in state["assessments"].items():
            if concept_id not in state["concepts"] or assessment.get("fit_policy_id", LEGACY_POLICY_ID) != policy_id:
                continue
            try:
                if current_brand(state, "concept", concept_id, state["concepts"][concept_id]["description"]) == "established":
                    excluded_assessments.add(concept_id)
            except SafetyError:
                continue
        chosen = [concept_id for concept_id in sorted(state["concepts"], key=lambda value:
                  (0 if fits[value] and fits[value]["fit_tier"] == "good" else 1,
                   -fits[value]["kpi_matches"] if fits[value] else 0, value))
                  if (fits[concept_id] and fits[concept_id]["eligible"]) or concept_id in excluded_assessments]
    pending, deferred, reused = [], [], []
    examined = 0
    for concept_id in chosen:
        if len(pending) >= limit:
            break
        examined += 1
        concept = state["concepts"][concept_id]
        description = concept["description"]
        try:
            effective_brand = current_brand(state, "concept", concept_id, description)
        except SafetyError as error:
            deferred.append({"concept_id": concept_id, "reason": str(error)})
            problem(memory, "brand_binding", concept_id, str(error))
            continue
        if effective_brand == "established" or concept.get("status") in ("excluded", "merged", "retired"):
            deferred.append({"concept_id": concept_id, "reason": "Excluded product; no marketplace model review is needed."})
            continue
        description_hash = digest(description)
        old_reviews = [row for row in state["market_reviews"].values()
                       if row["concept_id"] == concept_id and row["description_hash"] == description_hash
                       and row.get("review_version") == REVIEW_VERSION and row.get("model") == MODEL
                       and row.get("prompt_version") == RELEVANCE_PROMPT_VERSION]
        if concept_ids is None and not include_context:
            known_relations = [row for review in old_reviews for row in review["relevance"]]
            known_relations.extend(state["assessments"].get(concept_id, {}).get("result", {}).get("relevance", []))
            if any(row["relationship"] in ("exact_product", "relevant_family")
                   and row["id"] in state["observations"]
                   and state["observations"][row["id"]]["source"] == "amazon"
                   and state["observations"][row["id"]]["semantic_id"] in pool for row in known_relations):
                reused.append(concept_id)
                continue
        reviewed = {semantic_id for row in old_reviews for semantic_id in row["semantic_ids"]}
        remaining = {semantic_id: value for semantic_id, value in pool.items() if semantic_id not in reviewed}
        shortlist = plausible_marketplace_observations(description, remaining, limit=observations_per_concept)
        if not shortlist["observations"]:
            if old_reviews:
                reused.append(concept_id)
            deferred.append({"concept_id": concept_id, "reason": "No unreviewed plausible fresh saved listing was found.",
                             "saved_listings": len(pool), "already_reviewed": len(reviewed),
                             "limitation": "Lexical retrieval can miss different wording; absence is not proof that Amazon has no match."})
            continue
        item = {"id": concept_id, "description": description, "observations": shortlist["observations"]}
        pending.append((item, description_hash, shortlist))
    jobs, accepted, failures = Jobs(memory, runner), {}, {}
    for offset in range(0, len(pending), 5):
        chunk = pending[offset:offset + 5]
        payload = {"mode": "marketplace_relevance_only", "criteria": [],
                   "task": "Classify each supplied listing against its recommendation-level product. Only relevance is requested. Return criteria as an empty list; existing checklist judgments are preserved. relevant_family requires the same primary product use, function and basic mechanism, not merely a shared benefit, material, broad category or word. A product that protects a different thing for a different use is unrelated. Small size, colour, seller, pack and ordinary material variants of the same useful product can be relevant_family; exact supplier SKU identity is not required. If supplied title/text does not establish that scope, return unclear.",
                   "comparison_examples": [
                       {"product": "Dining-chair protector slipcovers", "listing": "Mattress protector or bed-cover sheet",
                        "relationship": "unrelated", "reason": "Protecting furniture or textiles is too broad: the objects and primary uses differ."},
                       {"product": "Dining-chair protector slipcovers", "listing": "Waterproof stretch dining-chair seat cover",
                        "relationship": "relevant_family", "reason": "Same chair-protection use and slipcover mechanism; ordinary material or coverage variation may be useful."},
                       {"product": "Sink sponge organiser caddy", "listing": "Drain-cleaning chemical",
                        "relationship": "unrelated", "reason": "Both concern a sink, but storage and drain treatment are different products."}],
                   "items": [item for item, _, _ in chunk]}
        try:
            job_id, results = jobs.run("E", payload)
        except SafetyError as error:
            for item, _, _ in chunk:
                failures[item["id"]] = str(error)
                problem(memory, "market_relevance", item["id"], str(error), revision=digest(item))
            continue
        for item, description_hash, shortlist in chunk:
            concept_id = item["id"]
            answer = results["accepted"].get(concept_id)
            if answer is None:
                reason = results["failures"][concept_id]
                failures[concept_id] = reason
                problem(memory, "market_relevance", concept_id, reason, job_id=job_id, revision=digest(item))
                continue
            # Do not splice this job into a previous checklist result or claim
            # that the previous assessment used evidence it never received.
            input_hash = digest(item)
            review_id = key("market-review", [concept_id, input_hash, REVIEW_VERSION, MODEL, RELEVANCE_PROMPT_VERSION])
            observations = shortlist["observations"]
            record = {"concept_id": concept_id, "description_hash": description_hash, "input_hash": input_hash,
                      "job_id": job_id, "reviewed_at": as_of,
                      "observation_ids": [o["id"] for o in observations], "semantic_ids": [o["semantic_id"] for o in observations],
                      "observed_at": {o["id"]: state["observations"][o["id"]]["observed_at"] for o in observations},
                      "relevance": answer["relevance"], "assessment_unchanged": True,
                      "review_version": REVIEW_VERSION, "model": MODEL, "prompt_version": RELEVANCE_PROMPT_VERSION,
                      "retrieval": {"reviewed": len(observations), "omitted": shortlist["omitted"], "method": shortlist["method"]}}
            old = memory.snapshot()["market_reviews"].get(review_id)
            if old is None:
                memory.put("market_reviews", review_id, record)
            else:
                record = old
            accepted[concept_id] = dict(id=review_id, **record)
    return {"accepted": accepted, "failures": failures, "reused": reused, "deferred": deferred,
            "remaining_concepts": max(0, len(chosen) - examined), "collector_requests": 0, "client_ready": False}
