"""Bounded, saved-evidence catalogue admission; never collects new source data.

Semantic distinctions are model judgments, not guarantees. Code binds references,
reuses unchanged decisions and retains existing family IDs and scheduling history.
"""

from copy import deepcopy
from urllib.parse import parse_qs, urlparse

from .brand_reviews import current_brand
from .catalogue import validate_catalogue
from .discovery import normalise, tokens
from .models import SafetyError, digest, identifier, timestamp, utc_now
from .research_ai import MODEL, _item_bindings, bind_model_retry_leader, prompt_version, text
from .model_recovery import run_current_model_job
from .research_memory import key
from .service import CATEGORIES
from .storage import Journal

MAX_CONCEPTS = 5
MAX_NEARBY = 8
MAX_EVIDENCE = 12


def validate_expansion_input(payload):
    items = payload.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_CONCEPTS:
        raise SafetyError("CATALOGUE_EXPANSION_INPUT_INVALID")
    for item in items:
        if (not isinstance(item, dict) or not isinstance(item.get("description"), dict)
                or item["description"].get("category") not in CATEGORIES
                or not isinstance(item.get("candidates"), list) or len(item["candidates"]) > MAX_NEARBY
                or not isinstance(item.get("evidence"), list) or len(item["evidence"]) > MAX_EVIDENCE
                or not isinstance(item.get("branches"), list) or not item["branches"]):
            raise SafetyError("CATALOGUE_EXPANSION_INPUT_INVALID")
        for name in ("candidates", "evidence", "branches"):
            if any(not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in item[name]):
                raise SafetyError("CATALOGUE_EXPANSION_INPUT_INVALID")
            if len({row["id"] for row in item[name]}) != len(item[name]):
                raise SafetyError("CATALOGUE_EXPANSION_INPUT_INVALID")
        if any(row.get("category") != item["description"]["category"] for row in [*item["candidates"], *item["branches"]]):
            raise SafetyError("CATALOGUE_EXPANSION_INPUT_INVALID")


def validate_proposal(item, value):
    """Shape is checked by Jobs; this checks bounded supplied references."""
    category = item["description"]["category"]
    if value["category"] != category:
        raise SafetyError("CATALOGUE_EXPANSION_CATEGORY_INVALID")
    parents = {row["id"] for row in item["branches"] if row["category"] == category}
    if value["parent_id"] not in parents:
        raise SafetyError("CATALOGUE_EXPANSION_PARENT_INVALID")
    candidates = {row["id"] for row in item["candidates"]}
    if ((value["decision"] == "same_family" and value["match_id"] not in candidates)
            or (value["decision"] != "same_family" and value["match_id"] is not None)):
        raise SafetyError("AI_MATCH_REFERENCE_INVALID")
    refs = {row["id"] for row in item["evidence"]}
    if len(set(value["evidence_ids"])) != len(value["evidence_ids"]) or not set(value["evidence_ids"]) <= refs:
        raise SafetyError("AI_EVIDENCE_REFERENCE_INVALID")
    if value["basis"] == "observed" and not value["evidence_ids"]:
        raise SafetyError("CATALOGUE_EXPANSION_OBSERVATION_REQUIRED")
    for field in ("label", "problem", "distinction", "reason"):
        text(value[field])
        if "://" in value[field]:
            raise SafetyError("CATALOGUE_EXPANSION_URL_INVALID")
    if len(value["alternative_phrases"]) > 2:
        raise SafetyError("CATALOGUE_EXPANSION_PHRASE_LIMIT")
    if value["primary_phrase"]["source"] != "meta" or value["primary_phrase"]["purpose"] != "discovery":
        raise SafetyError("CATALOGUE_EXPANSION_PRIMARY_INVALID")
    for phrase in [value["primary_phrase"], *value["alternative_phrases"]]:
        for field in ("text", "reason"):
            text(phrase[field])
            if "://" in phrase[field]:
                raise SafetyError("CATALOGUE_EXPANSION_PHRASE_INVALID")
        if "://" in phrase["text"] or any(ord(char) < 32 for char in phrase["text"]):
            raise SafetyError("CATALOGUE_EXPANSION_PHRASE_INVALID")
    return value


def _exact_match(catalogue, label):
    wording = normalise(label)
    # Search every family, including retired families and currently cooled-down
    # families. Scheduling availability must not determine identity.
    return next((family for family in sorted(catalogue["families"], key=lambda row: row["id"])
                 if wording in {normalise(value) for value in [family["label"], *family["aliases"]]}), None)


def effective_catalogue(base_catalogue, state):
    """Return a fresh view of static research plus durable accepted additions."""
    result = deepcopy(validate_catalogue(base_catalogue))
    families = {row["id"]: row for row in result["families"]}
    sources = {row["id"]: row for row in result["sources"]}
    records = sorted(state.get("catalogue_additions", {}).values(), key=lambda row: (row["at"], row["id"]))
    # Install admitted families before merging aliases: wall-clock timestamps
    # alone must not determine whether a dependent alias can be replayed.
    for record in records:
        if record["outcome"] != "accepted":
            continue
        for source in record["sources"]:
            old = sources.get(source["id"])
            if old is not None and old != source:
                raise SafetyError("CATALOGUE_ADDITION_SOURCE_CONFLICT")
            sources[source["id"]] = deepcopy(source)
        family = record["family"]
        old = families.get(family["id"])
        if old is not None and old != family:
            raise SafetyError("CATALOGUE_ADDITION_FAMILY_CONFLICT")
        families[family["id"]] = deepcopy(family)
    for record in records:
        if record["outcome"] != "merged":
            continue
        family = families.get(record["family_id"])
        if family is None:
            raise SafetyError("CATALOGUE_ADDITION_MATCH_MISSING")
        for label in record.get("aliases", []):
            if normalise(label) not in {normalise(value) for value in [family["label"], *family["aliases"]]}:
                family["aliases"].append(label)
        # Keep the original disposition, identity and season. New words do not
        # reactivate a retired family or reset its query history/cooldown.
        existing = {digest(phrase) for phrase in family["phrases"]}
        family["phrases"].extend(deepcopy(phrase) for phrase in record.get("phrases", []) if digest(phrase) not in existing)
    result["families"] = list(families.values())
    result["sources"] = list(sources.values())
    return validate_catalogue(result)


def _concept_evidence(state, concept_id):
    semantics = {row["semantic_id"] for row in state["associations"].values()
                 if row.get("concept_id") == concept_id and row.get("status") == "resolved"}
    return [dict(id=record_id, **row) for record_id, row in sorted(state["observations"].items())
            if row["semantic_id"] in semantics]


def exploration_description(state, concept_id):
    """Worth exploring is independent of recommendation fit or market proof.

    Canonical A/identity work supplies the meaning. This is only a small shape,
    scope and active-identity check, not a second semantic or score judgment.
    """
    concept = state["concepts"][concept_id]
    if (concept.get("status") in ("excluded", "merged", "retired")
            or concept.get("identity_conflicted")):
        return None
    description = concept.get("description")
    if (not isinstance(description, dict) or description.get("category") not in CATEGORIES
            or any(not isinstance(description.get(field), str) or not description[field].strip()
                   for field in ("label", "function", "mechanism"))):
        return None
    try:
        if current_brand(state, "concept", concept_id, description) == "established":
            return None
    except SafetyError:
        # A changed/ambiguous brand binding belongs to this concept only.
        # It cannot make that seed usable or stop unrelated exploration.
        return None
    return description


def _nearby(catalogue, description):
    label_words = tokens(description["label"])
    function_words = tokens(description.get("function", "") + " " + description.get("mechanism", ""))
    def rank(family):
        names = tokens(" ".join([family["label"], *family["aliases"]]))
        meaning = tokens(family["definition"] + " " + family["problem"])
        # Lexical ranking is only retrieval, never a semantic merge verdict.
        score = 3 * len(label_words & names) + 2 * len(function_words & meaning) + len((label_words | function_words) & (names | meaning))
        return (-score, family["id"])
    return [{name: family[name] for name in ("id", "label", "category", "parent_id", "definition", "problem", "aliases", "disposition")}
            for family in sorted((family for family in catalogue["families"]
                                  if family["category"] == description["category"]), key=rank)[:MAX_NEARBY]]


def _expansion_item(state, catalogue, concept_id):
    description = exploration_description(state, concept_id)
    if description is None: return None
    evidence = _concept_evidence(state, concept_id)
    selected = sorted(evidence, key=lambda row: (timestamp(row["observed_at"]), row["id"]), reverse=True)[:MAX_EVIDENCE]
    return {"id": concept_id, "description": description, "evidence": selected,
            "candidates": _nearby(catalogue, description),
            "branches": [row for row in catalogue["branches"] if row["category"] == description["category"]]}


def _public_source(observation):
    semantic = observation["semantic"]
    url = observation.get("facts", {}).get("url") or semantic.get("product_url") or semantic.get("link_url")
    if not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
    except ValueError:
        return None
    forbidden = {"token", "api_key", "apikey", "access_token", "secret", "password", "authorization"}
    if (parsed.scheme not in ("https", "http") or not hostname or parsed.username or parsed.password
            or forbidden & {name.casefold() for name in parse_qs(parsed.query)}):
        return None
    # Preserve supplied wording, not a model-generated summary masquerading as
    # a quote. Public source references do not establish demand or safe use.
    excerpts = [semantic[field][:2000] for field in ("title", "body", "link_description")
                if isinstance(semantic.get(field), str) and semantic[field].strip()]
    if not excerpts:
        return None
    return {"id": key("page", observation["id"]), "url": url,
            "title": semantic.get("title") or "Saved " + observation["source"] + " observation",
            "checked_at": observation["observed_at"], "excerpts": excerpts}


def _admit(catalogue, item, proposal, at):
    match = _exact_match(catalogue, proposal["label"]) or _exact_match(catalogue, item["description"]["label"])
    if match is None and proposal["decision"] == "same_family":
        match = next(family for family in catalogue["families"] if family["id"] == proposal["match_id"])
    if match is not None:
        return {"outcome": "merged", "family_id": match["id"],
                "aliases": list(dict.fromkeys([proposal["label"], item["description"]["label"]])),
                "phrases": [proposal["primary_phrase"], *proposal["alternative_phrases"]],
                "reason": proposal["reason"]}
    if proposal["decision"] == "unclear":
        return {"outcome": "deferred", "reason": proposal["reason"]}
    refs = set(proposal["evidence_ids"])
    sources = [source for observation in item["evidence"] if observation["id"] in refs
               if (source := _public_source(observation)) is not None]
    if proposal["basis"] == "observed" and not sources:
        return {"outcome": "deferred", "reason": "Cited saved observations have no usable public page reference."}
    source_ids = [source["id"] for source in sources]
    family_id = key("family", [proposal["category"], proposal["parent_id"], normalise(proposal["label"])])
    family = {"id": family_id, "parent_id": proposal["parent_id"], "category": proposal["category"],
              "label": proposal["label"], "definition": proposal["distinction"], "problem": proposal["problem"],
              "included": [item["description"]["label"]], "excluded": ["Seller, colour or ordinary size changes alone are not distinct families."],
              "aliases": [], "overlaps": [], "examples": [{"label": item["description"]["label"],
                   "basis": proposal["basis"], "sources": source_ids}], "basis": proposal["basis"],
              "sources": source_ids, "rationale": proposal["reason"],
              "phrases": [proposal["primary_phrase"], *proposal["alternative_phrases"]],
              "season": {"kind": "unknown", "months": []}, "review_note": "Accepted research direction; not verified demand or a recommendation.",
              "disposition": "usable", "author": "bounded-role-c", "reviewed_at": at,
              "next_review": "After measured searches or materially changed evidence."}
    return {"outcome": "accepted", "family_id": family_id, "family": family, "sources": sources, "reason": proposal["reason"]}


def _save(memory, records):
    """One event for a call's new records (replays reach the same record IDs)."""
    from .research_memory import change
    if records:
        memory.commit(key("additions", [row["id"] for row in records]),
                      [change("catalogue_additions", row["id"], row) for row in records])


def run_expansion(memory, base_catalogue, runner=None, *, concept_ids=None, limit=5, as_of=None):
    """Admit at most five saved ideas, preserving every previous outcome.

    Explicit IDs retain the caller's order and unchanged saved C decisions.
    Otherwise coherent active in-scope concepts are considered before fit,
    market or supplier assessment. Neither route performs proof searches or
    releases client recommendations. The call's new records are saved together
    in one event; a failed save leaves them for the saved job's replay.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_CONCEPTS:
        raise SafetyError("CATALOGUE_EXPANSION_LIMIT_INVALID")
    at = as_of or utc_now()
    timestamp(at)
    with Journal(memory.root).locked() as leader, bind_model_retry_leader(memory, leader):
        state = memory.snapshot()
        catalogue = effective_catalogue(base_catalogue, state)
        automatic = concept_ids is None
        if automatic:
            concept_ids = sorted(state["concepts"])
        if not isinstance(concept_ids, (list, tuple)) or any(not isinstance(value, str) for value in concept_ids):
            raise SafetyError("CATALOGUE_EXPANSION_CONCEPTS_INVALID")
        for concept_id in concept_ids:
            identifier(concept_id)
        if len(set(concept_ids)) != len(concept_ids):
            raise SafetyError("CATALOGUE_EXPANSION_CONCEPTS_INVALID")
        pending, reused, inputs, saved = [], [], {}, []
        processed = 0
        for concept_id in concept_ids:
            if processed >= limit:
                break
            if concept_id not in state["concepts"]:
                raise SafetyError("CATALOGUE_EXPANSION_CONCEPT_UNKNOWN")
            description = exploration_description(state, concept_id)
            if description is None:
                continue
            evidence = _concept_evidence(state, concept_id)
            input_hash = digest({"description": description, "evidence": evidence})
            previous = next((record for record in state["catalogue_additions"].values()
                             if record["concept_id"] == concept_id and record["input_hash"] == input_hash
                             and not record.get("resumable", False)), None)
            if previous:
                if not automatic:
                    reused.append(previous)
                    processed += 1
                continue
            processed += 1
            # Newest evidence is included first; omitted evidence does not get
            # silently cited by the model. Full input hash still detects change.
            item = _expansion_item(state, catalogue, concept_id)
            inputs[concept_id] = input_hash
            exact = _exact_match(catalogue, description["label"])
            if exact:
                record_id = key("addition", [concept_id, input_hash, "exact", exact["id"]])
                record = {"id": record_id, "concept_id": concept_id, "input_hash": input_hash,
                          "job_id": None, "at": at, "outcome": "merged", "family_id": exact["id"],
                          "aliases": [], "phrases": [], "reason": "Exact existing family label or accepted alias.", "proposal": None,
                          "input": item}
                saved.append(record)
                reused.append(record)
            else:
                pending.append(item)
        records = list(reused)
        if pending:
            try:
                payload = {"items": pending}
                current_items = list(pending)
                if runner is not None and getattr(runner, "closed_retry_policy", None) is not None:
                    # Application limit1 must not strand a real original2-item
                    # batch. Rebuild only its actual siblings still in this
                    # caller's scope; never claim historical inputs are current.
                    bindings = _item_bindings("C", payload, MODEL, prompt_version("C", payload),
                        getattr(runner, "identity", "codex-chatgpt-high-v1"))
                    owners = {value: jid for jid, saved in state["jobs"].items()
                        for value in saved["binding_keys"].values()}
                    original_ids = {owners[value] for value in bindings.values() if value in owners}
                    sibling_ids = {item["id"] for jid in original_ids
                        for item in state["jobs"][jid]["request"]["payload"]["items"]
                        if item["id"] in concept_ids}
                    known = {item["id"] for item in current_items}
                    for cid in sorted(sibling_ids - known):
                        item = _expansion_item(state, catalogue, cid)
                        if item is not None: current_items.append(item)
                job_id, result = run_current_model_job(memory, runner, "C", payload, current_items=current_items)
            except SafetyError as error:
                # Runner/access failures are resumable. Keep the roadblock and
                # exact inputs without treating this as a completed idea review.
                for item in pending:
                    record_id = key("addition", [item, inputs[item["id"]], str(error)])
                    record = {"id": record_id, "concept_id": item["id"], "input_hash": inputs[item["id"]],
                              "job_id": None, "at": at, "outcome": "deferred", "reason": str(error),
                              "resumable": True, "proposal": None, "input": item}
                    if record_id not in state["catalogue_additions"]:
                        saved.append(record)
                    records.append(record)
                _save(memory, saved)
                return {"records": records, "pending": [item["id"] for item in pending],
                        "problem": str(error), "effective_families": len(catalogue["families"])}
            # Each admission sees this call's earlier ones without another full read.
            view = dict(state, catalogue_additions=dict(state["catalogue_additions"], **{row["id"]: row for row in saved}))
            for item in pending:
                concept_id = item["id"]
                proposal = result["accepted"].get(concept_id)
                outcome = (_admit(effective_catalogue(base_catalogue, view), item, proposal, at)
                           if proposal is not None else {"outcome": "rejected", "reason": result["failures"][concept_id]})
                record_id = key("addition", [job_id, concept_id, inputs[concept_id]])
                record = dict(outcome, id=record_id, concept_id=concept_id, input_hash=inputs[concept_id],
                              job_id=job_id, proposal=proposal, at=at, input=item)
                if record_id not in view["catalogue_additions"]:
                    saved.append(record)
                    view["catalogue_additions"][record_id] = record
                records.append(record)
        _save(memory, saved)
        view = dict(state, catalogue_additions=dict(state["catalogue_additions"], **{row["id"]: row for row in saved}))
        return {"records": records, "pending": [], "problem": None,
                "effective_families": len(effective_catalogue(base_catalogue, view)["families"])}
