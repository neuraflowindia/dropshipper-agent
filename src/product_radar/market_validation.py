"""Associate saved marketplace evidence without recollection or checklist edits."""

import re
from contextvars import ContextVar
from copy import deepcopy
from datetime import timedelta

from .discovery import latest_observations, normalise, tokens
from .brand_reviews import current_brand
from .models import SafetyError, digest, identifier, prepared_request, request_stage, timestamp, utc_now
from .research_ai import Jobs, MODEL, prompt_version
from .research_flow import problem
from .research_memory import import_saved_attempt, key
from .storage import Journal, load_json, safe_path

REVIEW_VERSION = "marketplace-relevance-v2"
RELEVANCE_PROMPT_VERSION = prompt_version("E", {"mode": "marketplace_relevance_only", "criteria": []})
FUNCTIONAL_MARKET_CONTRACT = "functional-market-scope-v1"
SCORE_SUPPORT_CONTRACT = "functional-score-support-v1"
FUNCTIONAL_MATCH_METHOD = "functional_source_match_v4"


def source_evidence(state, evidence_id):
    """Resolve an original captured row or chart, never a fabricated observation."""
    if evidence_id in state["observations"]:
        row = state["observations"][evidence_id]
        return row["source"], row["observed_at"], row
    if evidence_id in state["trends_evidence"]:
        row = state["trends_evidence"][evidence_id]
        return "google-trends", row.get("parsed", {}).get("metadata", {}).get("collected_at"), row
    return None, None, None


def _functional_originals(state, row):
    return [(ref, source_evidence(state, ref["id"])) for ref in row["references"]
            if ref["table"] in ("observations", "trends_evidence")]


# Set only by the store-copy score preview (r10_cutover._provisional) to count
# saved answers of an older relevance prompt; empty on every research path.
PREVIEW_MATCH_VERSIONS = ContextVar("preview_match_versions", default=())


def _functional_match(row, ref):
    if row["method"] == "operator_source_review":
        return True
    from .research_ai import FUNCTIONAL_SOURCE_PROMPT_VERSION
    proof = row.get("job_proof") or {}
    answer = proof.get("accepted_item")
    if (row["method"] != FUNCTIONAL_MATCH_METHOD or row["support"] != "insufficient" or proof.get("role") != "E"
            or proof.get("prompt_version") not in (FUNCTIONAL_SOURCE_PROMPT_VERSION, *PREVIEW_MATCH_VERSIONS.get())
            or not isinstance(answer, dict) or digest(answer) != proof.get("accepted_item_hash")):
        return False
    expected = {"id": ref, "scope": row["scope"], "relationship": row["relationship"],
                "reason": row["reason"], "limitations": row["limitations"]}
    return [value for value in answer.get("relevance", []) if value.get("id") == ref] == [expected]


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
    market = compute_market_signals(observations, relevance, as_of, charts)
    selected_rows = {row["id"]: row for row in accepted.values()}
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


def _expiry(at):
    return (timestamp(at) + timedelta(days=7)).isoformat()


def _score_sites(records, opportunity):
    """Market sites whose evidence earns score points: {site: [evidence ids]}.
    Quick-commerce evidence is split by its own app (one site row each)."""
    from .opportunity_score import QUICK_COMMERCE, market_evidence
    sites = market_evidence(opportunity)
    for ref in sites.pop("quick-commerce", []):
        platform = source_evidence(records, ref)[0]
        if platform not in QUICK_COMMERCE:
            raise SafetyError("FUNCTIONAL_MARKET_WITNESS_REFERENCE_INVALID")
        sites.setdefault(platform, []).append(ref)
    return {site: sorted(refs) for site, refs in sorted(sites.items())}


def _presence_at(opportunity):
    parts = opportunity["opportunity_basis"]["parts"]
    platform = parts["want"]["items"]["platform_lists"]
    earned = platform["points"] or parts["early"]["items"]["new_arrivals"]["points"]
    return platform["facts"]["presence_at"] if earned else None


def functional_score_support(records, context, opportunity, *, as_of):
    """The dated evidence behind the score's market and platform points.

    Every market site earning points is cited with its original records and
    scope reviews; the platform presence fact counts when it earns points.
    The support lasts until the earliest of their seven-day validities.
    """
    if not opportunity["eligible"]:
        return {"score_support": None, "score_support_refs": [], "support_valid_until": None}
    by_id = {row["id"]: row for row in context["scoped_evidence"]}
    sources = []
    for site, refs in _score_sites(records, opportunity).items():
        originals, reviews = [], set()
        for ref in refs:
            platform, captured_at, original = source_evidence(records, ref)
            if platform != site or original is None or ref not in context["matches"]:
                raise SafetyError("FUNCTIONAL_MARKET_WITNESS_REFERENCE_INVALID")
            table = "observations" if ref in records["observations"] else "trends_evidence"
            originals.append({"table": table, "id": ref, "hash": digest(original), "source": site, "observed_at": captured_at})
            reviews.add(context["matches"][ref]["scope_review_id"])
        oldest = min(originals, key=lambda row: timestamp(row["observed_at"]))
        sources.append({"source": site, "references": originals,
                        "scope_reviews": [{"id": rid, "hash": by_id[rid]["record_hash"]} for rid in sorted(reviews)],
                        "valid_until": _expiry(oldest["observed_at"])})
    at = _presence_at(opportunity)
    presence = {"at": at, "valid_until": _expiry(at)} if at else None
    dates = [row["valid_until"] for row in sources] + ([presence["valid_until"]] if presence else [])
    if not dates:
        raise SafetyError("FUNCTIONAL_MARKET_WITNESS_REQUIRED")
    witness = {"contract": SCORE_SUPPORT_CONTRACT, "meaning_hash": context["meaning_hash"],
               "score": opportunity["opportunity_score"], "sources": sources, "presence": presence,
               "valid_until": min(dates, key=timestamp)}
    validate_score_support(witness, opportunity=opportunity, meaning_hash=context["meaning_hash"], as_of=as_of)
    return {"score_support": witness,
            "score_support_refs": sorted({ref["id"] for row in sources for ref in row["references"]}),
            "support_valid_until": witness["valid_until"]}


def validate_score_support(witness, *, opportunity, meaning_hash, as_of=None):
    """Validate the frozen support's shape, exact dates and completeness.

    Original hash/currentness is rechecked by the selection's source revisions;
    this pure validator is also usable without mutable research storage.
    """
    from .opportunity_score import POLICY_ID, QUICK_COMMERCE, market_evidence
    try:
        if (not isinstance(witness, dict) or set(witness) != {"contract", "meaning_hash", "score", "sources", "presence", "valid_until"}
                or witness["contract"] != SCORE_SUPPORT_CONTRACT or witness["meaning_hash"] != meaning_hash
                or opportunity["opportunity_policy_id"] != POLICY_ID or opportunity["opportunity_basis"]["eligible"] is not True
                or type(witness["score"]) is not int or witness["score"] != opportunity["opportunity_score"]):
            raise SafetyError("FUNCTIONAL_MARKET_WITNESS_INVALID")
        expected = market_evidence(opportunity)
        sources, seen, cited, dates = witness["sources"], [], {}, []
        if not isinstance(sources, list):
            raise SafetyError("FUNCTIONAL_MARKET_WITNESS_INVALID")
        for row in sources:
            if (set(row) != {"source", "references", "scope_reviews", "valid_until"}
                    or not row["references"] or not row["scope_reviews"]):
                raise SafetyError("FUNCTIONAL_MARKET_WITNESS_INVALID")
            site = "quick-commerce" if row["source"] in QUICK_COMMERCE else row["source"]
            refs = []
            for ref in row["references"]:
                if (set(ref) != {"table", "id", "hash", "source", "observed_at"}
                        or ref["table"] not in ("observations", "trends_evidence") or ref["source"] != row["source"]
                        or ref["id"] not in expected.get(site, []) or ref["id"] in {i for ids in cited.values() for i in ids}
                        or not re.fullmatch(r"[0-9a-f]{64}", ref["hash"])):
                    raise SafetyError("FUNCTIONAL_MARKET_WITNESS_REFERENCE_INVALID")
                identifier(ref["id"]); refs.append(timestamp(ref["observed_at"]))
                cited.setdefault(site, []).append(ref["id"])
            for proof in row["scope_reviews"]:
                if set(proof) != {"id", "hash"} or not re.fullmatch(r"[0-9a-f]{64}", proof["hash"]):
                    raise SafetyError("FUNCTIONAL_MARKET_WITNESS_REFERENCE_INVALID")
                identifier(proof["id"])
            if timestamp(row["valid_until"]) != min(refs) + timedelta(days=7):
                raise SafetyError("FUNCTIONAL_MARKET_WITNESS_DATE_INVALID")
            seen.append(row["source"]); dates.append(row["valid_until"])
        if seen != sorted(set(seen)) or {site: sorted(ids) for site, ids in cited.items()} != expected:
            raise SafetyError("FUNCTIONAL_MARKET_WITNESS_INVALID")
        at, presence = _presence_at(opportunity), witness["presence"]
        if (presence != ({"at": at, "valid_until": _expiry(at)} if at else None)
                or presence is not None and timestamp(presence["valid_until"]) != timestamp(at) + timedelta(days=7)):
            raise SafetyError("FUNCTIONAL_MARKET_WITNESS_INVALID")
        if presence:
            dates.append(presence["valid_until"])
        if not dates or timestamp(witness["valid_until"]) != min(timestamp(value) for value in dates):
            raise SafetyError("FUNCTIONAL_MARKET_WITNESS_INVALID")
        if as_of is not None and (timestamp(as_of) > timestamp(witness["valid_until"])
                or any(timestamp(ref["observed_at"]) > timestamp(as_of) for row in sources for ref in row["references"])
                or presence is not None and timestamp(presence["at"]) > timestamp(as_of)):
            raise SafetyError("FUNCTIONAL_MARKET_WITNESS_STALE")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SafetyError("FUNCTIONAL_MARKET_WITNESS_INVALID") from None
    return witness


def _signal_projection(market):
    return {source: {field: deepcopy(row[field]) for field in ("available", "positive", "used_ids")}
            for source, row in market["sources"].items()}


def freeze_score_facts(state, concept_id):
    """The saved platform presence and crowding facts the score read, frozen
    with their hashes: a report copy re-runs those sub-points from them."""
    from .crowding import crowding_for, fact_id
    from .frozen_records import freeze_original_json
    from .platform_roles import current_platform_presence, presence_record_id
    presence = current_platform_presence(state, concept_id)
    return {"presence": None if presence is None else {"id": presence_record_id(presence),
                                                       "record": freeze_original_json(presence)},
            "crowding": {"id": fact_id(concept_id), "record": freeze_original_json(crowding_for(state, concept_id))}}


def score_fact_inputs(proof, concept_id, meaning_hash):
    """(presence, crowding) score inputs from the frozen facts, each checked
    against its own kind, type, meaning, id and saved hashes."""
    from .crowding import fact_id, validate_crowding_fact
    from .frozen_records import read_original_json
    from .opportunity_score import _utc_at, crowding_input, presence_input
    from .platform_roles import presence_record_id, validate_presence_fact
    try:
        facts = proof["score_facts"]
        if (not isinstance(facts, dict) or set(facts) != {"presence", "crowding"} or facts["crowding"] is None
                or any(row is not None and (not isinstance(row, dict) or set(row) != {"id", "record"}) for row in facts.values())):
            raise SafetyError("FUNCTIONAL_MARKET_SCORE_FACTS_INVALID")
        presence = None
        if facts["presence"] is not None:
            record = validate_presence_fact(read_original_json(facts["presence"]["record"]), concept_id, meaning_hash)
            if (facts["presence"]["id"] != presence_record_id(record) or timestamp(_utc_at(record["at"])) > timestamp(proof["as_of"])):
                raise SafetyError("FUNCTIONAL_MARKET_SCORE_FACTS_INVALID")
            presence = presence_input(record, as_of=proof["as_of"])
        crowding = validate_crowding_fact(read_original_json(facts["crowding"]["record"]), concept_id, meaning_hash)
        if facts["crowding"]["id"] != fact_id(concept_id):
            raise SafetyError("FUNCTIONAL_MARKET_SCORE_FACTS_INVALID")
        return presence, crowding_input(crowding)
    except SafetyError as error:
        if str(error) in ("DROPSHIP_PRESENCE_RECORD_INVALID", "CROWDING_FACT_INVALID", "INVALID_TIME", "INVALID_IDENTIFIER",
                          "OPPORTUNITY_BASIS_INVALID"):
            raise SafetyError("FUNCTIONAL_MARKET_SCORE_FACTS_INVALID") from None
        raise
    except (KeyError, TypeError, AttributeError):
        raise SafetyError("FUNCTIONAL_MARKET_SCORE_FACTS_INVALID") from None


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
            "score_facts": freeze_score_facts(state, context["concept_id"])}


def validate_frozen_functional_market(product):
    """Recompute frozen scoped facts, the market signals, the score and its support.

    This authenticates the packet's internal proof. Canonical latest-head and
    release authority remain the live selection/root checks, not this offline
    projection and not the capture's favourable interpretation.
    """
    from .research_scope import (_verified, _record_id, _source_context, _field_values,
                                 _evidence_contract, _route_source)
    from .research_records import SCOPED_EVIDENCE_KIND
    from .discovery import compute_market_signals
    from .opportunity_score import evaluate_opportunity, opportunity_fields
    from .fit_policy import FIT_POLICY_ID, evaluate_fit
    from .fit_description import FUNCTIONAL_INFERENCE_KIND
    from .frozen_records import read_original_json
    try:
        proof = product["functional_market_proof"]
        meaning = product["functional_definition"]["meaning_hash"]
        if (product.get("functional_market_contract") != FUNCTIONAL_MARKET_CONTRACT
                or set(proof) != {"contract", "concept_id", "meaning_hash", "as_of", "scope_hash", "scoped_records", "records",
                                  "matches", "source_signals", "score_facts"}
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
        market = compute_market_signals(observations, relevance, proof["as_of"], charts)
        if _signal_projection(market) != proof["source_signals"]:
            raise SafetyError("FUNCTIONAL_MARKET_SIGNALS_CHANGED")
        expected_checks = [{"source": source, "state": "Available" if signal["available"] else "Insufficient",
            "detail": signal["reason"], "evidence_ids": signal["used_ids"], "url": None}
            for source, signal in market["sources"].items()]
        if product.get("market_checks") != expected_checks or product.get("market_label") != market["source_label"]:
            raise SafetyError("FUNCTIONAL_MARKET_DISPLAY_CHANGED")
        fit = evaluate_fit(product["criteria"], FIT_POLICY_ID, description=product["fit_description"],
                           description_contract=FUNCTIONAL_INFERENCE_KIND)
        # The market sub-points are recomputed from the frozen originals. Presence
        # and crowding are re-run from their frozen saved facts: each must be
        # internally consistent (kind, type, meaning, id; presence's own change
        # hash; crowding's level and rule re-derived from its stored counts under
        # the current thresholds), so the score cannot disagree with them. What
        # is NOT proven here: that those facts' item ids and counts match the
        # listings behind them (they are not frozen), or that they are still the
        # current answer; reserve and release compare crowding with the store.
        presence, crowding = score_fact_inputs(proof, product["concept_id"], meaning)
        opportunity = evaluate_opportunity(fit, market, presence=presence, crowding=crowding)
        if digest(opportunity_fields(opportunity)) != digest({key: product[key] for key in opportunity_fields(opportunity)}):
            raise SafetyError("FUNCTIONAL_MARKET_SCORE_CHANGED")
        context = {"meaning_hash": meaning, "market": market, "matches": matches, "scoped_evidence": list(rows.values())}
        expected = functional_score_support(records, context, opportunity, as_of=proof["as_of"])
        for field in ("score_support", "score_support_refs", "support_valid_until"):
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
