"""Shared, description-bound product-type search wording.

Profiles guide retrieval only. They neither merge products nor establish demand,
supplier terms or an exact quote. Existing product descriptions and AI jobs are
unchanged; profiles are additive records in the existing research event store.
"""

from copy import deepcopy
import re

from .discovery import normalise
from .models import SafetyError, canonical, digest, identifier, timestamp, utc_now
from .research_memory import change, key
from .storage import Journal, atomic_json, load_json, safe_path

VERSION = "product-query-profile-v1"
MODE = "product_query_profile"
PROMPT_VERSION = "research-c-query-profile-v2"
LEGACY_PROMPT_VERSION = "research-c-query-profile-v1"
REPAIR_VERSION = "product-query-contract-repair-v1"
FUNCTIONAL_VERSION = "product-query-profile-v2"
FUNCTIONAL_CONTRACT = "functional-query-context-v1"
FUNCTIONAL_PROMPT_VERSION = "research-c-query-profile-v3"
SOURCES = ("meta", "marketplace", "trends", "supplier", "quickcommerce")
SOURCE_ALIASES = {
    "amazon": "marketplace", "amazon_search": "marketplace", "flipkart": "marketplace",
    "flipkart_search": "marketplace", "blinkit": "quickcommerce", "zepto": "quickcommerce",
    "instamart": "quickcommerce", "google": "supplier", "google_search": "supplier",
    "google_maps": "supplier", "indiamart": "supplier", "tradeindia": "supplier",
}
MAX_EVIDENCE = 12
MAX_TERMS = 3


def _phrase(value, *, empty=False):
    if (not isinstance(value, str) or (not empty and not value.strip()) or len(value) > 120
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or re.search(r"https?://|www\.|[<>]", value, re.I)):
        raise SafetyError("PRODUCT_QUERY_PHRASE_INVALID")
    return " ".join(value.split())


def _unique(values, *, maximum):
    if not isinstance(values, list) or len(values) > maximum:
        raise SafetyError("PRODUCT_QUERY_TERMS_INVALID")
    output, seen = [], set()
    for value in values:
        cleaned = _phrase(value)
        fingerprint = normalise(cleaned)
        if fingerprint in seen:
            raise SafetyError("PRODUCT_QUERY_TERMS_DUPLICATED")
        seen.add(fingerprint)
        output.append(cleaned)
    return output


def query_profile_input(state, concept_id):
    """Prepare bounded real saved context; no query, provider or model call."""
    identifier(concept_id)
    concept = state.get("concepts", {}).get(concept_id)
    if not concept or not isinstance(concept.get("description"), dict):
        raise SafetyError("KNOWN_CONCEPT_REQUIRED")
    from .functional_types import functional_role_context
    context = functional_role_context(state, concept_id)
    if context is not None:
        return {"id": concept_id, "functional_contract": FUNCTIONAL_CONTRACT,
                "meaning_hash": context["meaning_hash"], "description": context["definition"],
                "description_ref": key("query-functional-definition", [concept_id, context["meaning_hash"]]),
                "observations": context["examples"][:MAX_EVIDENCE]}
    description = deepcopy(concept["description"])
    if (not isinstance(description.get("label"), str) or not description["label"].strip()
            or len(description["label"]) > 4000):
        raise SafetyError("PRODUCT_QUERY_DESCRIPTION_INVALID")
    semantic_ids = {row.get("semantic_id") for row in state.get("associations", {}).values()
                    if row.get("concept_id") == concept_id and row.get("status") == "resolved"}
    # Stable meaning IDs reuse original references rather than changing a query
    # profile whenever the same listing is collected on a later date.
    original = {}
    for observation_id, row in state.get("observations", {}).items():
        if row.get("semantic_id") in semantic_ids:
            original.setdefault(row["semantic_id"], {"id": observation_id, "source_text": deepcopy(row["semantic"])})
    observations = [original[semantic_id] for semantic_id in sorted(original)[:MAX_EVIDENCE]]
    return {"id": concept_id, "description": description,
            "description_ref": key("query-description", [concept_id, digest(description)]),
            "observations": observations}


def _validate_query_item(item):
    if isinstance(item, dict) and "functional_contract" in item:
        from .functional_types import MEANING_FIELDS, definition_meaning_hash
        if (set(item) != {"id", "description", "description_ref", "observations", "functional_contract", "meaning_hash"}
                or item["functional_contract"] != FUNCTIONAL_CONTRACT or not isinstance(item["id"], str)
                or not isinstance(item["description"], dict) or set(item["description"]) != set(MEANING_FIELDS)
                or item["meaning_hash"] != definition_meaning_hash(dict(item["description"], report_title="Functional type"))
                or item["description_ref"] != key("query-functional-definition", [item["id"], item["meaning_hash"]])
                or not isinstance(item["observations"], list) or len(item["observations"]) > MAX_EVIDENCE):
            raise SafetyError("PRODUCT_QUERY_INPUT_INVALID")
        for row in item["observations"]:
            if (not isinstance(row, dict) or set(row) != {"id", "source_text"}
                    or not isinstance(row["source_text"], dict)
                    or set(row["source_text"]) != {"function", "mechanism", "features", "audience"}
                    or row["id"] != key("functional-role-example", [item["id"], item["meaning_hash"], row["source_text"]])):
                raise SafetyError("PRODUCT_QUERY_INPUT_INVALID")
        if len({row["id"] for row in item["observations"]}) != len(item["observations"]):
            raise SafetyError("PRODUCT_QUERY_INPUT_INVALID")
        return
    if (not isinstance(item, dict) or set(item) != {"id", "description", "description_ref", "observations"}
            or not isinstance(item["id"], str) or not isinstance(item["description"], dict)
            or item["description_ref"] != key("query-description", [item["id"], digest(item["description"])])
            or not isinstance(item["observations"], list) or len(item["observations"]) > MAX_EVIDENCE
            or any(not isinstance(row, dict) or set(row) != {"id", "source_text"}
                   or not isinstance(row["id"], str) or not isinstance(row["source_text"], dict)
                   for row in item["observations"])):
        raise SafetyError("PRODUCT_QUERY_INPUT_INVALID")
    if len({row["id"] for row in item["observations"]}) != len(item["observations"]):
        raise SafetyError("PRODUCT_QUERY_INPUT_INVALID")


def validate_query_input(payload):
    functional = isinstance(payload, dict) and payload.get("query_contract") == FUNCTIONAL_CONTRACT
    fields = {"mode", "items", "query_contract"} if functional else {"mode", "items"}
    if (not isinstance(payload, dict) or set(payload) != fields or payload["mode"] != MODE
            or not isinstance(payload["items"], list) or not 1 <= len(payload["items"]) <= 10):
        raise SafetyError("PRODUCT_QUERY_INPUT_INVALID")
    for item in payload["items"]:
        _validate_query_item(item)
        if (item.get("functional_contract") == FUNCTIONAL_CONTRACT) != functional:
            raise SafetyError("PRODUCT_QUERY_INPUT_INVALID")
    if len({item["id"] for item in payload["items"]}) != len(payload["items"]):
        raise SafetyError("PRODUCT_QUERY_INPUT_INVALID")


def validate_query_profile(item, value):
    """Validate wording/scope and actual references, not commercial success."""
    _validate_query_item(item)
    fields = {"id", "core_type", "subtype", "aliases", "mechanism_anchors", "broad_context",
              "queries", "references", "reason"}
    functional = item.get("functional_contract") == FUNCTIONAL_CONTRACT
    if functional:
        fields.add("meaning_hash")
    if (not isinstance(value, dict) or set(value) != fields or value["id"] != item["id"]
            or (functional and value.get("meaning_hash") != item["meaning_hash"])
            or not isinstance(value["reason"], str) or not value["reason"].strip()
            or len(value["reason"]) > 2000):
        raise SafetyError("PRODUCT_QUERY_PROFILE_INVALID")
    core = _phrase(value["core_type"])
    subtype = _phrase(value["subtype"], empty=True)
    aliases = _unique(value["aliases"], maximum=4)
    anchors = _unique(value["mechanism_anchors"], maximum=4)
    broad = _unique(value["broad_context"], maximum=2)
    refs = value["references"]
    allowed = {item["description_ref"], *[row["id"] for row in item["observations"]]}
    if (not isinstance(refs, list) or not refs or not all(isinstance(ref, str) for ref in refs)
            or len(set(refs)) != len(refs) or not set(refs) <= allowed):
        raise SafetyError("PRODUCT_QUERY_REFERENCE_INVALID")
    if not isinstance(value["queries"], list) or len(value["queries"]) > len(SOURCES) * MAX_TERMS:
        raise SafetyError("PRODUCT_QUERY_QUERIES_INVALID")
    queries, seen = [], set()
    for row in value["queries"]:
        if (not isinstance(row, dict) or set(row) != {"source", "text", "scope", "reason"}
                or row["source"] not in SOURCES or row["scope"] not in ("subtype", "family", "context")
                or not isinstance(row["reason"], str) or not row["reason"].strip() or len(row["reason"]) > 2000):
            raise SafetyError("PRODUCT_QUERY_QUERIES_INVALID")
        phrase = _phrase(row["text"])
        identity = (row["source"], normalise(phrase))
        if identity in seen:
            raise SafetyError("PRODUCT_QUERY_TERMS_DUPLICATED")
        if row["scope"] == "context" and normalise(phrase) not in {normalise(term) for term in broad}:
            raise SafetyError("PRODUCT_QUERY_CONTEXT_INVALID")
        if functional and row["scope"] != "context" and normalise(phrase) in {normalise(term) for term in broad}:
            raise SafetyError("PRODUCT_QUERY_CONTEXT_INVALID")
        seen.add(identity)
        queries.append(dict(row, text=phrase))
    # A missed optional query slot does not prevent useful wording from the
    # accepted core/subtype. Filling is deterministic and introduces no facts.
    for source in SOURCES:
        if not any(row["source"] == source and row["scope"] != "context" for row in queries):
            if functional and (not (subtype or core) or normalise(subtype or core) in {normalise(term) for term in broad}):
                raise SafetyError("PRODUCT_QUERY_NARROW_WORDING_REQUIRED")
            queries.append({"source": source, "text": subtype or core,
                            "scope": "subtype" if subtype else "family",
                            "reason": "Search wording derived from the reviewed product type."})
        if sum(row["source"] == source for row in queries) > MAX_TERMS:
            raise SafetyError("PRODUCT_QUERY_SOURCE_LIMIT_INVALID")
    return dict(value, core_type=core, subtype=subtype, aliases=aliases,
                mechanism_anchors=anchors, broad_context=broad, queries=queries, references=list(refs))


def _from_type(item, label, *, method, family_id=None):
    label = _phrase(label)
    return {"id": item["id"], "core_type": label, "subtype": "", "aliases": [],
            "mechanism_anchors": [], "broad_context": [],
            "queries": [{"source": source, "text": label, "scope": "family",
                         "reason": "Use the accepted short product-family wording, not a retail title."}
                        for source in SOURCES],
            "references": [item["description_ref"]],
            "reason": "Accepted catalogue family." if family_id else "Short accepted product label; retrieval hint only."}


def _catalogue_profile(state, item, catalogue):
    if item.get("functional_contract") == FUNCTIONAL_CONTRACT:
        return None
    if catalogue is None:
        from .catalogue import load_catalogue
        catalogue = load_catalogue()
    from .catalogue import validate_catalogue
    from .catalogue_expansion import effective_catalogue
    effective = effective_catalogue(validate_catalogue(catalogue), state)
    category = item["description"].get("category")
    families = [row for row in effective["families"] if row["category"] == category]
    description_hash = digest(item["description"])
    # A current accepted C family admission is a stronger meaning binding than
    # containment of a generic word in a manufacturer title.
    additions = [row for row in state.get("catalogue_additions", {}).values()
                 if row.get("concept_id") == item["id"] and row.get("outcome") in ("accepted", "merged")
                 and isinstance(row.get("input"), dict)
                 and digest(row["input"].get("description")) == description_hash]
    accepted_ids = {row.get("family_id") for row in additions}
    label = normalise(item["description"]["label"])
    candidates = [row for row in families if row["id"] in accepted_ids or label in {
        normalise(term) for term in [row["label"], *row["aliases"]]}]
    if len(candidates) != 1:
        return None
    family = candidates[0]
    # Catalogue descriptions can be much longer than a useful search phrase.
    # Reuse an actually accepted label/alias; never truncate one into a new
    # product meaning or let an awkward family hold up its healthy siblings.
    phrases = []
    for text in [family["label"], *family["aliases"]]:
        try:
            phrase = _phrase(text)
        except SafetyError:
            continue
        if normalise(phrase) not in {normalise(value) for value in phrases}:
            phrases.append(phrase)
    if not phrases:
        return None
    profile = _from_type(item, phrases[0], method="catalogue", family_id=family["id"])
    profile["aliases"] = phrases[1:5]
    return validate_query_profile(item, profile)


def _short_label(item):
    if item.get("functional_contract") == FUNCTIONAL_CONTRACT:
        return None
    label = item["description"].get("label")
    if (not isinstance(label, str) or len(label) > 80 or not 1 <= len(label.split()) <= 5
            or re.search(r"[,;!?<>]|\b(?:buy|sale|discount|offer|premium|store|brand)\b", label, re.I)):
        return None
    return validate_query_profile(item, _from_type(item, label, method="short_label"))


def _wording(value):
    """Punctuation-insensitive phrase comparison, not synonym invention."""
    return " ".join(re.findall(r"\w+", normalise(value)))


def _grounded_catalogue_profile(state, item, catalogue):
    """Use an actual accepted type phrase visible in saved product wording.

    This is a broad retrieval fallback, not a catalogue identity/admission. A
    lone shared adjective or noun is insufficient, and ambiguous unrelated
    phrases remain pending. No title is blindly shortened or word-stripped.
    """
    if item.get("functional_contract") == FUNCTIONAL_CONTRACT:
        return None
    if catalogue is None:
        from .catalogue import load_catalogue
        catalogue = load_catalogue()
    from .catalogue import validate_catalogue
    from .catalogue_expansion import effective_catalogue
    families = effective_catalogue(validate_catalogue(catalogue), state)["families"]
    description = item["description"]
    label = " " + _wording(description["label"]) + " "
    candidates = []
    for family in families:
        if family["category"] != description.get("category") or family["disposition"] != "usable":
            continue
        for supplied in [family["label"], *family["aliases"]]:
            try:
                phrase = _phrase(supplied)
            except SafetyError:
                continue
            words = _wording(phrase)
            if 2 <= len(words.split()) <= 6 and " " + words + " " in label:
                candidates.append((len(words.split()), len(words), family["id"], phrase))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    winner = candidates[0]
    # A longer compound genuinely contains the shorter type; other unrelated
    # type matches mean this title may be a bundle and need model interpretation.
    winning_words = " " + _wording(winner[3]) + " "
    if any(family_id != winner[2] and " " + _wording(phrase) + " " not in winning_words
           for _, _, family_id, phrase in candidates):
        return None
    profile = _from_type(item, winner[3], method="grounded_catalogue")
    profile["reason"] = "Accepted catalogue wording appears in the saved product title; a retrieval hint only."
    return validate_query_profile(item, profile)


def _grounded_model_fallback(item, value):
    """Recover only a compact type actually stated in the saved title."""
    if item.get("functional_contract") == FUNCTIONAL_CONTRACT:
        return None
    if not isinstance(value, dict) or value.get("id") != item["id"]:
        return None
    label = " " + _wording(item["description"]["label"]) + " "
    for field in ("subtype", "core_type"):
        try:
            phrase = _phrase(value.get(field))
        except SafetyError:
            continue
        words = _wording(phrase)
        if 2 <= len(words.split()) <= 6 and " " + words + " " in label:
            profile = _from_type(item, phrase, method="grounded_description")
            profile["reason"] = "This compact wording is present in the saved product title; a retrieval hint only."
            return validate_query_profile(item, profile)
    return None


def repair_query_contract(item, value):
    """One bounded local shape repair; never change core/subtype or add facts.

    Optional context/list errors must not suppress valid product-type wording.
    Foreign references are discarded only when an actually supplied reference
    remains. A shortened hash is never completed or treated as evidence.
    """
    _validate_query_item(item)
    if not isinstance(value, dict) or value.get("id") != item["id"]:
        raise SafetyError("PRODUCT_QUERY_PROFILE_INVALID")
    repaired, actions = deepcopy(value), []
    for field, maximum in (("aliases", 4), ("mechanism_anchors", 4), ("broad_context", 2)):
        values = repaired.get(field)
        if not isinstance(values, list):
            raise SafetyError("PRODUCT_QUERY_TERMS_INVALID")
        retained, seen = [], set()
        for supplied in values:
            phrase = _phrase(supplied)
            fingerprint = normalise(phrase)
            if fingerprint not in seen and len(retained) < maximum:
                retained.append(phrase)
            seen.add(fingerprint)
        if retained != values:
            actions.append("bounded_optional_" + field)
            repaired[field] = retained
    supplied_refs = repaired.get("references")
    allowed = {item["description_ref"], *[row["id"] for row in item["observations"]]}
    if not isinstance(supplied_refs, list) or any(not isinstance(ref, str) for ref in supplied_refs):
        raise SafetyError("PRODUCT_QUERY_REFERENCE_INVALID")
    refs = list(dict.fromkeys(ref for ref in supplied_refs if ref in allowed))
    if not refs:
        raise SafetyError("PRODUCT_QUERY_REFERENCE_INVALID")
    if refs != supplied_refs:
        repaired["references"] = refs
        actions.append("retained_only_actual_supplied_references")
    queries = repaired.get("queries")
    if not isinstance(queries, list):
        raise SafetyError("PRODUCT_QUERY_QUERIES_INVALID")
    broad = {normalise(term) for term in repaired["broad_context"]}
    retained_queries = []
    for row in queries:
        if (isinstance(row, dict) and row.get("scope") == "context"
                and isinstance(row.get("text"), str) and normalise(row["text"]) not in broad):
            actions.append("omitted_unlisted_optional_context")
            continue
        retained_queries.append(row)
    repaired["queries"] = retained_queries
    return validate_query_profile(item, repaired), list(dict.fromkeys(actions))


def _profile_refs_available(state, item, profile):
    """Only used source meanings are dependencies; new captures do not expire wording."""
    if item.get("functional_contract") == FUNCTIONAL_CONTRACT:
        from .functional_types import functional_role_context
        actual = functional_role_context(state, item["id"])
        if (actual is None or actual["meaning_hash"] != item["meaning_hash"]
                or actual["definition"] != item["description"]):
            return False
        current = {row["id"]: row["source_text"] for row in actual["examples"]}
        supplied = {row["id"]: row["source_text"] for row in item["observations"]}
        return all(ref in current and current[ref] == supplied.get(ref)
                   for ref in set(profile["references"]) - {item["description_ref"]})
    semantic_ids = {row.get("semantic_id") for row in state.get("associations", {}).values()
                    if isinstance(row, dict) and row.get("concept_id") == item["id"]
                    and row.get("status") == "resolved"}
    observations = state.get("observations", {})
    used = set(profile["references"]) - {item["description_ref"]}
    for row in item["observations"]:
        if row["id"] not in used:
            continue
        actual = observations.get(row["id"])
        if (not isinstance(actual, dict) or actual.get("semantic_id") not in semantic_ids
                or actual.get("semantic") != row["source_text"]):
            return False
    return True


def current_query_profile(state, concept_id, *, records=None):
    """Read an applicable saved profile; its date is not source collection."""
    item = query_profile_input(state, concept_id)
    return _current_query_profile_for_input(state, item, records=records)


def _current_query_profile_for_input(state, item, *, records=None):
    """Reuse this decision's already-built context, never across state reads.

    Both callers build the item with query_profile_input on the supplied
    state. Saved original references and every profile are still validated
    below; this only avoids constructing the identical context twice.
    """
    concept_id = item["id"]
    candidates = [row for row in (state.get("source_checks", {}).values() if records is None else records)
                  if row.get("kind") == "product_query_profile" and row.get("concept_id") == concept_id
                  and row.get("description_hash") == digest(item["description"])]
    valid = []
    for row in candidates:
        saved = row.get("input")
        functional = item.get("functional_contract") == FUNCTIONAL_CONTRACT
        version = FUNCTIONAL_VERSION if functional else VERSION
        if (row.get("version") != version or not isinstance(saved, dict)
                or saved.get("description") != item["description"] or saved.get("id") != concept_id
                or saved.get("description_ref") != item["description_ref"]
                or row.get("input_hash") != digest(saved)):
            continue
        if functional:
            if (row.get("meaning_hash") != item["meaning_hash"]
                    or saved.get("meaning_hash") != item["meaning_hash"]
                    or row.get("record_hash") != digest({k: v for k, v in row.items() if k != "record_hash"})
                    or row.get("proof_hash") != digest(row.get("proof"))
                    or row.get("reference") != key("query-profile", [FUNCTIONAL_VERSION,
                        row.get("input_hash"), row.get("profile"), row.get("method"), row.get("job_id"), row.get("proof_hash")])):
                continue
        # Supplied observation refs are real immutable records in this memory.
        elif not isinstance(saved.get("observations"), list) or any(not isinstance(ref, dict) or ref.get("id") not in state.get("observations", {}) or
               state["observations"][ref["id"]].get("semantic") != ref.get("source_text")
               for ref in saved.get("observations", [])):
            continue
        try:
            profile = validate_query_profile(saved, row["profile"])
            timestamp(row["at"])
            if not _profile_refs_available(state, saved, profile):
                continue
        except (SafetyError, KeyError, TypeError):
            continue
        valid.append((timestamp(row["at"]), row.get("reference", ""), profile))
    return deepcopy(max(valid, key=lambda row: (row[0], row[1]))[2]) if valid else None


def resolve_query_profile(state, concept_id, *, catalogue=None):
    """One pure currentness/fallback rule for every query-planning consumer.

    An accepted functional boundary cannot fall through an older raw retail
    label or catalogue-family equivalence. Its own saved C wording can still
    contain expressly labelled broader context, never a new demand vote.
    """
    item = query_profile_input(state, concept_id)
    return (_current_query_profile_for_input(state, item) or _catalogue_profile(state, item, catalogue)
            or _short_label(item) or _grounded_catalogue_profile(state, item, catalogue))


def query_profile_scope_hash(state, concept_id):
    """Stable retry/scheduling key; legacy descriptions keep their exact key."""
    from .functional_types import functional_role_context
    if functional_role_context(state, concept_id) is None:
        # A malformed legacy label still needs a stable progress key so its
        # repeated failure cannot starve a healthy sibling.
        return digest(state.get("concepts", {}).get(concept_id, {}).get("description"))
    return digest(query_profile_input(state, concept_id))


def _payload(items):
    payload = {"mode": MODE, "items": items}
    if items and items[0].get("functional_contract") == FUNCTIONAL_CONTRACT:
        payload["query_contract"] = FUNCTIONAL_CONTRACT
    return payload


def product_query_terms(memory, concept_id, *, source, limit=MAX_TERMS, include_context=False, catalogue=None):
    """Pure consumer: no hidden model call, fetch, write or paid fallback."""
    source = SOURCE_ALIASES.get(source, source)
    if source not in SOURCES or type(limit) is not int or not 1 <= limit <= MAX_TERMS or type(include_context) is not bool:
        raise SafetyError("PRODUCT_QUERY_SOURCE_INVALID")
    state = memory.snapshot()
    profile = resolve_query_profile(state, concept_id, catalogue=catalogue)
    if profile is None:
        raise SafetyError("PRODUCT_QUERY_PROFILE_REQUIRED")
    ranked = [row for row in profile["queries"] if row["source"] == source
              and (include_context or row["scope"] != "context")]
    ranked.sort(key=lambda row: {"subtype": 0, "family": 1, "context": 2}[row["scope"]])
    return deepcopy(ranked[:limit])


def _record(memory, item, profile, method, at, *, job_id=None, repair_reference=None):
    profile = validate_query_profile(item, profile)
    functional = item.get("functional_contract") == FUNCTIONAL_CONTRACT
    version = FUNCTIONAL_VERSION if functional else VERSION
    proof = None
    if functional:
        from .functional_types import functional_role_context
        context = functional_role_context(memory.snapshot(), item["id"])
        if context is None or context["meaning_hash"] != item["meaning_hash"]:
            raise SafetyError("PRODUCT_QUERY_DESCRIPTION_CHANGED")
        used = set(profile["references"]) - {item["description_ref"]}
        proof = {"definition_id": context["proof"]["definition_id"],
                 "definition_record_hash": context["proof"]["definition_record_hash"],
                 "examples": {ref: context["proof"]["examples"].get(ref) for ref in sorted(used)}}
    identity = [version, digest(item), profile, method, job_id]
    if functional:
        identity.append(digest(proof))
    record_id = key("query-profile", identity)
    value = {"kind": "product_query_profile", "version": version, "source": "query_profile",
             "route": "saved_query_planning", "state": "usable", "at": at, "reference": record_id,
             "concept_id": item["id"], "description_hash": digest(item["description"]),
             "input_hash": digest(item), "input": deepcopy(item), "profile": profile,
             "method": method, "job_id": job_id, "reason": profile["reason"]}
    if repair_reference is not None:
        value["repair_reference"] = repair_reference
    if functional:
        value.update(meaning_hash=item["meaning_hash"], proof=proof, proof_hash=digest(proof))
        value["record_hash"] = digest(value)
    def unchanged(state):
        actual = query_profile_input(state, item["id"])
        if actual["description"] != item["description"] or actual["description_ref"] != item["description_ref"]:
            raise SafetyError("PRODUCT_QUERY_DESCRIPTION_CHANGED")
        if not _profile_refs_available(state, item, profile):
            raise SafetyError("PRODUCT_QUERY_REFERENCES_CHANGED")
        if functional:
            context = functional_role_context(state, item["id"])
            current_proof = {"definition_id": context["proof"]["definition_id"],
                            "definition_record_hash": context["proof"]["definition_record_hash"],
                            "examples": {ref: context["proof"]["examples"].get(ref) for ref in sorted(used)}}
            if current_proof != proof:
                raise SafetyError("PRODUCT_QUERY_REFERENCES_CHANGED")
    memory.commit(record_id, [change("source_checks", record_id, value)], validate=unchanged)
    return value


class _SavedOnlyRunner:
    """The exact frozen runner identity allows Jobs to reuse, never call it."""
    def __init__(self, identity):
        self.identity = identity

    def __call__(self, *args, **kwargs):
        raise SafetyError("PRODUCT_QUERY_SAVED_RESULT_REQUIRED")


def _local_code(error):
    return str(error) if isinstance(error, SafetyError) else "PRODUCT_QUERY_LOCAL_IO_FAILED"


def _repair_receipt(memory, job_id, item, saved, profile, actions):
    record_id = key("query-contract-repair", [REPAIR_VERSION, job_id, digest(item), digest(saved)])
    path = safe_path(memory.root, "query_profiles", "repairs", record_id) / "repair.json"
    receipt = {"version": REPAIR_VERSION, "job_id": job_id, "input_hash": digest(item),
               "source_result_hash": digest(saved), "actions": actions, "profile_hash": digest(profile),
               "state": "usable", "provider_requests": 0, "model_requests": 0}
    if path.exists():
        if load_json(path) != receipt:
            raise SafetyError("PRODUCT_QUERY_REPAIR_CONFLICT")
    else:
        atomic_json(path, receipt)
    return str(path)


def _recover_saved_profile(memory, state, item, *, catalogue=None, runner=None, current_items=None):
    """Reapply a current saved C answer before creating any new model job.

    Recovery keeps the original prompt, input batch, observation references and
    runner identity. New observations do not force an unchanged product to be
    analysed again. An unfinished original intent stays unfinished, not retried
    under a new prompt version or batch size.
    """
    from .research_ai import Jobs, MAX_JOB_BYTES, current_attempt
    from .model_recovery import run_current_model_job
    from .work_turns import active_work_turn
    for job_id, job in reversed(list(state.get("jobs", {}).items())):
        request = job.get("request", {})
        payload = request.get("payload", {})
        if (request.get("role") != "C" or payload.get("mode") != MODE
                or request.get("prompt_version") not in (LEGACY_PROMPT_VERSION, PROMPT_VERSION, FUNCTIONAL_PROMPT_VERSION)):
            continue
        originals = [row for row in payload.get("items", []) if row.get("id") == item["id"]
                     and row.get("description") == item["description"]
                     and row.get("description_ref") == item["description_ref"]
                     and row.get("functional_contract") == item.get("functional_contract")
                     and row.get("meaning_hash") == item.get("meaning_hash")]
        if len(originals) != 1:
            continue
        original = originals[0]
        folder = current_attempt(safe_path(memory.root, "ai_jobs", job_id))
        cooperative = runner is not None and getattr(runner, "closed_retry_policy", None) is not None and active_work_turn(memory) is not None
        if not (folder / "result.json").exists() and not (folder / "completion.json").exists():
            if cooperative:
                try:
                    allowed = current_items(payload) if callable(current_items) else current_items
                    recovered_job, result = run_current_model_job(memory, runner, "C", payload,
                        declared_version=request["prompt_version"], current_items=allowed)
                    if recovered_job != job_id: raise SafetyError("PRODUCT_QUERY_SAVED_JOB_CONFLICT")
                except (SafetyError, OSError) as error:
                    return {"job_id": job_id, "failure": _local_code(error)}
            elif (folder / "intent.json").exists():
                return {"job_id": job_id, "failure": "AI_JOB_PENDING_RECONCILIATION"}
            else:
                # A prepared but never-started request is safe for normal Jobs
                # to resume; do not mistake it for a completed response.
                continue
        try:
            if not cooperative or (folder / "result.json").exists() or (folder / "completion.json").exists():
                recovered_job, result = Jobs(memory, _SavedOnlyRunner(request["runner"])).run(
                    "C", payload, declared_version=request["prompt_version"])
            if recovered_job != job_id:
                raise SafetyError("PRODUCT_QUERY_SAVED_JOB_CONFLICT")
            if item["id"] in result["accepted"]:
                if (item.get("functional_contract") == FUNCTIONAL_CONTRACT
                        and not _profile_refs_available(state, original, result["accepted"][item["id"]])):
                    # The completed original is immutable, but a removed used
                    # example no longer supports this answer. A genuinely new
                    # semantic item may be planned; never rebind the old one.
                    continue
                return {"job_id": job_id, "item": original, "profile": result["accepted"][item["id"]],
                        "method": "model_review", "repair_reference": None}
            saved = load_json(folder / "result.json", MAX_JOB_BYTES)
            rows = [row for row in saved["response"]["results"]
                    if isinstance(row, dict) and row.get("id") == item["id"]]
            if len(rows) != 1:
                return {"job_id": job_id, "failure": result["failures"][item["id"]]}
            try:
                profile, actions = repair_query_contract(original, rows[0])
                method = "model_contract_repair"
            except SafetyError as error:
                # Do not conceal fabricated evidence. A separately grounded
                # title/catalogue fallback is allowed for malformed optional
                # slots, not an entirely unsupported reference envelope.
                if str(error) == "PRODUCT_QUERY_REFERENCE_INVALID":
                    return {"job_id": job_id, "failure": str(error)}
                profile = (_grounded_catalogue_profile(state, original, catalogue)
                           or _grounded_model_fallback(original, rows[0]))
                if profile is None:
                    return {"job_id": job_id, "failure": str(error)}
                actions = ["used_compact_wording_grounded_in_saved_title"]
                method = "grounded_description"
            if (item.get("functional_contract") == FUNCTIONAL_CONTRACT
                    and not _profile_refs_available(state, original, profile)):
                continue
            reference = _repair_receipt(memory, job_id, original, saved, profile, actions)
            return {"job_id": job_id, "item": original, "profile": profile,
                    "method": method, "repair_reference": reference}
        except (SafetyError, OSError) as error:
            return {"job_id": job_id, "failure": _local_code(error)}
    return None


def _progress_path(memory, concept_id, description_hash):
    return safe_path(memory.root, "query_profiles", "progress",
                     key("query-progress", [concept_id, description_hash])) / "progress.json"


def _saved_progress(memory, state, selected):
    """Local progress survives a busy canonical writer; it is not evidence."""
    output = []
    for concept_id in selected:
        try:
            scope_hash = query_profile_scope_hash(state, concept_id)
        except SafetyError:
            continue
        path = _progress_path(memory, concept_id, scope_hash)
        if not path.exists():
            continue
        try:
            value = load_json(path)
            if (value.get("concept_id") == concept_id and value.get("scope_hash", value.get("description_hash")) == scope_hash
                    and type(value.get("attempt_order")) is int and value["attempt_order"] > 0):
                output.append(value)
        except (SafetyError, OSError):
            # A damaged optional progress cache cannot invalidate product facts
            # or hide canonical pending records.
            continue
    return output


def ensure_query_profiles(memory, runner=None, *, concept_ids=None, catalogue=None, limit=20, as_of=None):
    """Prepare useful missing profiles in bounded batches through existing C.

    Catalogue-backed and compact accepted labels need no model. Awkward unknown
    family names use one versioned, cached C request when a runner is supplied.
    With no runner, awkward labels remain pending rather than full-title searches.
    Callers continue other questions; pending planning is never zero demand.
    """
    if type(limit) is not int or not 1 <= limit <= 200:
        raise SafetyError("PRODUCT_QUERY_LIMIT_INVALID")
    at = as_of or utc_now()
    timestamp(at)
    if concept_ids is not None:
        if (not isinstance(concept_ids, (list, tuple)) or any(not isinstance(value, str) for value in concept_ids)
                or len(set(concept_ids)) != len(concept_ids)):
            raise SafetyError("PRODUCT_QUERY_CONCEPTS_INVALID")
        if not concept_ids:
            # An explicit empty scope is not None/all products. It has no
            # original answers to recover and no progress to read or save.
            return {"records": [], "reused": [], "pending": [], "job_ids": [],
                    "recovered_job_ids": [], "contract_repairs": [], "local_failures": [],
                    "provider_requests": 0, "client_ready": False}
    with Journal(memory.root / "query_profiles").locked():
        state = memory.snapshot()
        selected = sorted(state["concepts"]) if concept_ids is None else concept_ids
        if concept_ids is None and (not isinstance(selected, (list, tuple))
                or any(not isinstance(value, str) for value in selected)
                or len(set(selected)) != len(selected)):
            raise SafetyError("PRODUCT_QUERY_CONCEPTS_INVALID")
        # Give never-tried siblings a turn before retrying a failed planning
        # question. An ordinal, not the wall clock, keeps this fair even when
        # a resumed batch uses the same frozen date or several calls are fast.
        attempts = ([row for row in state["source_checks"].values()
                     if row.get("kind") == "product_query_profile_pending"]
                    + _saved_progress(memory, state, selected))
        attempt_order = max([row.get("attempt_order", 0) for row in attempts], default=0)
        if concept_ids is None:
            prior_order = {}
            for row in attempts:
                concept = state["concepts"].get(row.get("concept_id"))
                try:
                    scope_hash = query_profile_scope_hash(state, row.get("concept_id")) if concept else None
                except SafetyError:
                    continue
                if concept and row.get("scope_hash", row.get("description_hash")) == scope_hash:
                    prior_order[row["concept_id"]] = max(prior_order.get(row["concept_id"], 0),
                                                         row.get("attempt_order", 0))
            selected.sort(key=lambda concept_id: (concept_id in prior_order,
                           prior_order.get(concept_id, 0), concept_id))
        records, pending, reused, failures = [], [], [], []
        allowed_ids = set(selected)
        def current_original_items(payload):
            # Bound work to actual original siblings (Jobs has at most10), not
            # another inventory scan or permission to research outside scope.
            return [query_profile_input(state, row["id"]) for row in payload["items"] if row["id"] in allowed_ids]
        recovered_jobs, contract_repairs, local_failures = [], [], []
        processed = 0
        for concept_id in selected:
            if processed >= limit:
                break
            try:
                item = query_profile_input(state, concept_id)
            except SafetyError as error:
                failures.append({"concept_id": concept_id, "reason": str(error)})
                processed += 1
                continue
            saved = current_query_profile(state, concept_id)
            if saved:
                reused.append(concept_id)
                if concept_ids is not None:
                    processed += 1
                continue
            processed += 1
            try:
                local = (_catalogue_profile(state, item, catalogue) or _short_label(item)
                         or _grounded_catalogue_profile(state, item, catalogue))
            except SafetyError as error:
                failures.append({"concept_id": concept_id, "reason": str(error)})
                continue
            if local is not None:
                try:
                    records.append(_record(memory, item, local, "catalogue" if local["reason"] == "Accepted catalogue family." else "short_label", at))
                except (SafetyError, OSError) as error:
                    failures.append({"concept_id": concept_id, "reason": _local_code(error)})
            else:
                recovered = _recover_saved_profile(memory, state, item, catalogue=catalogue, runner=runner,
                                                  current_items=current_original_items)
                if recovered is None:
                    pending.append(item)
                    continue
                recovered_jobs.append(recovered["job_id"])
                if "failure" in recovered:
                    failures.append({"concept_id": concept_id, "reason": recovered["failure"]})
                    continue
                try:
                    record = _record(memory, recovered["item"], recovered["profile"], recovered["method"], at,
                                     job_id=recovered["job_id"], repair_reference=recovered["repair_reference"])
                    records.append(record)
                    if recovered["repair_reference"]:
                        contract_repairs.append({"concept_id": concept_id, "reference": recovered["repair_reference"]})
                except (SafetyError, OSError) as error:
                    failures.append({"concept_id": concept_id, "reason": _local_code(error)})
        model_jobs = []
        if runner is not None:
            from .research_ai import MAX_JOB_BYTES
            from .model_recovery import run_current_model_job
            groups = {}
            for item in pending:
                groups.setdefault(item.get("functional_contract"), []).append(item)
            batches = []
            for contract, values in groups.items():
                if contract != FUNCTIONAL_CONTRACT:
                    batches.extend(values[offset:offset + 10] for offset in range(0, len(values), 10))
                    continue
                chunk = []
                for item in values:
                    if len(canonical(_payload([item]))) > MAX_JOB_BYTES:
                        failures.append({"concept_id": item["id"], "reason": "PRODUCT_QUERY_INPUT_SIZE_LIMIT"})
                        continue
                    if chunk and (len(chunk) == 10 or len(canonical(_payload(chunk + [item]))) > MAX_JOB_BYTES):
                        batches.append(chunk); chunk = []
                    chunk.append(item)
                if chunk:
                    batches.append(chunk)
            for items in batches:
                try:
                    job_id, result = run_current_model_job(memory, runner, "C", _payload(items),
                        current_items=groups[items[0].get("functional_contract")])
                    model_jobs.append(job_id)
                except (SafetyError, OSError) as error:
                    failures.extend({"concept_id": item["id"], "reason": _local_code(error)} for item in items)
                    continue
                for item in items:
                    if item["id"] in result["accepted"]:
                        try:
                            records.append(_record(memory, item, result["accepted"][item["id"]], "model_review", at, job_id=job_id))
                        except (SafetyError, OSError) as error:
                            failures.append({"concept_id": item["id"], "reason": _local_code(error)})
                    else:
                        # Interpret the saved raw answer once locally, without
                        # making a second model call for contract shape.
                        recovered = _recover_saved_profile(memory, memory.snapshot(), item, catalogue=catalogue)
                        if recovered and "profile" in recovered:
                            try:
                                records.append(_record(memory, recovered["item"], recovered["profile"],
                                    recovered["method"], at, job_id=recovered["job_id"],
                                    repair_reference=recovered["repair_reference"]))
                                if recovered["repair_reference"]:
                                    contract_repairs.append({"concept_id": item["id"], "reference": recovered["repair_reference"]})
                                continue
                            except (SafetyError, OSError) as error:
                                failures.append({"concept_id": item["id"], "reason": _local_code(error)})
                                continue
                        failures.append({"concept_id": item["id"], "reason": recovered.get("failure")
                            if recovered else result["failures"][item["id"]]})
        else:
            failures.extend({"concept_id": item["id"], "reason": "PRODUCT_QUERY_PROFILE_REQUIRED"} for item in pending)
        for failure in failures:
            description = state["concepts"].get(failure["concept_id"], {}).get("description")
            try:
                scope_hash = query_profile_scope_hash(state, failure["concept_id"])
                functional = query_profile_input(state, failure["concept_id"]).get("functional_contract") == FUNCTIONAL_CONTRACT
            except SafetyError:
                scope_hash, functional = digest(description), False
            version = FUNCTIONAL_VERSION if functional else VERSION
            attempt_order += 1
            record_id = key("query-profile-pending", [version, failure, scope_hash, attempt_order])
            value = {"kind": "product_query_profile_pending", "version": version,
                    "source": "query_profile", "route": "saved_query_planning", "state": "insufficient", "at": at,
                    "reference": record_id, "concept_id": failure["concept_id"],
                    "description_hash": digest(description), "attempt_order": attempt_order,
                    "reason": failure["reason"]}
            if functional:
                value["scope_hash"] = scope_hash
            # The independent cache means a busy writer cannot erase fair
            # ordering or abort the entire programme while saving its error.
            try:
                atomic_json(_progress_path(memory, failure["concept_id"], scope_hash), value)
            except (SafetyError, OSError) as error:
                local_failures.append({"concept_id": failure["concept_id"], "stage": "local_progress",
                                       "reason": _local_code(error)})
            try:
                memory.put("source_checks", record_id, value)
            except (SafetyError, OSError) as error:
                local_failures.append({"concept_id": failure["concept_id"], "stage": "canonical_progress",
                                       "reason": _local_code(error)})
        return {"records": records, "reused": reused, "pending": failures,
                "job_ids": list(dict.fromkeys([*recovered_jobs, *model_jobs])),
                "recovered_job_ids": list(dict.fromkeys(recovered_jobs)),
                "contract_repairs": contract_repairs, "local_failures": local_failures,
                "provider_requests": 0, "client_ready": False}
