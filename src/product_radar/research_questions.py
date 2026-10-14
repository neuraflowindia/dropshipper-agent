"""Pure, local research-question preparation; never collection or admission.

A question identifies what one real concept or accepted catalogue direction
needs to learn. A physical fingerprint identifies the actual requested read,
independently of its consumers. Neither a prepared question nor its declared
fee/channel is authority to dispatch, spend, claim source evidence or vote.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import timezone
import math
import re

from .models import SafetyError, canonical, digest, identifier, timestamp
from .public_pages import _public_url
from .research_memory import TABLES, change, key
from .research_records import RESEARCH_QUESTION_KIND


REQUEST_CONTRACT = "physical-request-v1"
PARSE_CONTRACT = "saved-capture-parse-v1"
MAX_QUESTIONS = 100
MAX_REQUEST_BYTES = 32768
MAX_QUESTION_BYTES = 65536
_HEX = re.compile(r"[0-9a-f]{64}")
_METHODS = frozenset(("GET", "HEAD", "POST"))
_PURPOSES = frozenset(("discovery", "validation", "sourcing"))
_CHANNELS = frozenset(("direct_local", "funded_provider", "saved_replay", "unknown"))
_FEES = frozenset(("known_zero", "reserved_paid", "unknown"))
_DIRECTION_SCOPE = ContextVar("product_radar_catalogue_direction_scope", default=None)
_PRIVATE_KEYS = frozenset(("token", "access_token", "api_token", "api_key", "apikey", "password",
                           "secret", "client_secret", "session_token", "authorization", "cookie",
                           "cookies", "set-cookie", "x-api-key", "x-auth-token"))
# Only explicit physical request fields belong here. Source-specific parameters
# can be placed in options/filters/body and are preserved, not silently dropped.
# Consumer IDs, programme/week, desired freshness and parser version do NOT.
_REQUEST_FIELDS = frozenset((
    "schema_version", "version", "route", "platform", "provider_platform", "transport",
    "url", "endpoint", "urls", "query", "queries", "term", "department", "node", "nodes",
    "country", "language", "locale", "timezone", "location", "geo", "geography", "postcode",
    "date", "date_scope", "date_from", "date_to", "timeframe", "property", "category", "sort",
    "filter", "filters", "pagination", "page", "pages", "depth", "limit", "offset", "cursor",
    "options", "body", "headers",
))
_QUESTION_FIELDS = frozenset((
    "subject", "route", "purpose", "request", "method", "capture_options", "question_options",
    "wording_candidates", "dependencies", "parent_question_id", "freshness_reason", "programme_id",
    "week", "collection_channel", "incremental_collection_fee",
))


def _hash(value, code):
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise SafetyError(code)
    return value


def _text(value, *, limit=4096, code="RESEARCH_QUESTION_TEXT_INVALID"):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise SafetyError(code)
    return value


def _json(value, *, depth=0):
    """Bounded actual JSON only; no coercion of numbers, keys or ordered lists."""
    if depth > 12:
        raise SafetyError("RESEARCH_REQUEST_JSON_INVALID")
    if value is None or type(value) in (bool, int):
        return deepcopy(value)
    if type(value) is float:
        if not math.isfinite(value):
            raise SafetyError("RESEARCH_REQUEST_JSON_INVALID")
        return value
    if isinstance(value, str):
        if len(value) > 16384 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise SafetyError("RESEARCH_REQUEST_JSON_INVALID")
        return value
    if isinstance(value, list) and len(value) <= 1000:
        return [_json(item, depth=depth + 1) for item in value]
    if isinstance(value, dict) and len(value) <= 128:
        result = {}
        for field, item in value.items():
            if (not isinstance(field, str) or not field or len(field) > 160
                    or any(ord(c) < 32 or ord(c) == 127 for c in field)):
                raise SafetyError("RESEARCH_REQUEST_JSON_INVALID")
            if field.casefold() in _PRIVATE_KEYS:
                raise SafetyError("RESEARCH_REQUEST_CREDENTIALS_REFUSED")
            result[field] = _json(item, depth=depth + 1)
        return result
    raise SafetyError("RESEARCH_REQUEST_JSON_INVALID")


def _object(value):
    if not isinstance(value, dict):
        raise SafetyError("RESEARCH_REQUEST_JSON_INVALID")
    return _json(value)


def _route(value, *, history=False):
    # Lazy import: the future dispatcher may itself import these pure helpers.
    # A closed route is accepted only when re-verifying saved history.
    from .source_coverage import CLOSED_ROUTES, ROUTES
    if not isinstance(value, str) or value not in ROUTES and not (history and value in CLOSED_ROUTES):
        raise SafetyError("RESEARCH_QUESTION_ROUTE_INVALID")
    return value


def _request_contract(route, request, method, capture_options, *, supporting=False, history=False):
    # Supporting reads have physical ownership, but must never enter the
    # fourteen demand/source question routes or acquire market votes.
    if not supporting or route != "public_image_http":
        _route(route, history=history)
    if not isinstance(method, str) or method not in _METHODS:
        raise SafetyError("RESEARCH_REQUEST_METHOD_INVALID")
    value = _object(request)
    if not value or not set(value) <= _REQUEST_FIELDS:
        raise SafetyError("RESEARCH_REQUEST_FIELD_UNSUPPORTED")
    if "route" in value and value["route"] != route:
        raise SafetyError("RESEARCH_REQUEST_ROUTE_MISMATCH")
    if "schema_version" in value and (type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise SafetyError("RESEARCH_REQUEST_VERSION_UNSUPPORTED")
    if "version" in value and value["version"] != "quickcommerce-location-v1":
        raise SafetyError("RESEARCH_REQUEST_VERSION_UNSUPPORTED")
    if not any(value.get(field) for field in ("url", "endpoint", "urls", "query", "queries", "term", "department", "node", "nodes", "body")):
        raise SafetyError("RESEARCH_REQUEST_SCOPE_MISSING")
    for field in ("url", "endpoint"):
        if field in value:
            value[field] = _public_url(value[field])[0]
    if "urls" in value:
        if not isinstance(value["urls"], list) or not value["urls"]:
            raise SafetyError("RESEARCH_REQUEST_URLS_INVALID")
        # These are destination URLs, not URL-looking literals inside a body.
        value["urls"] = [_public_url(item)[0] for item in value["urls"]]
    for field in ("query", "term", "department", "node"):
        if field in value and value[field] is not None:
            _text(value[field], code="RESEARCH_REQUEST_SCOPE_INVALID")
    for field in ("queries", "nodes"):
        if field in value:
            if not isinstance(value[field], list) or not value[field]:
                raise SafetyError("RESEARCH_REQUEST_SCOPE_INVALID")
            for item in value[field]:
                _text(item, code="RESEARCH_REQUEST_SCOPE_INVALID")
    if "country" in value and (not isinstance(value["country"], str) or not re.fullmatch(r"[A-Z]{2}", value["country"])):
        raise SafetyError("RESEARCH_REQUEST_GEOGRAPHY_INVALID")
    for field in ("page", "pages", "depth", "limit"):
        if field in value and (type(value[field]) is not int or not 1 <= value[field] <= 1000000):
            raise SafetyError("RESEARCH_REQUEST_RANGE_INVALID")
    if "offset" in value and (type(value["offset"]) is not int or not 0 <= value["offset"] <= 1000000000):
        raise SafetyError("RESEARCH_REQUEST_RANGE_INVALID")
    for field in ("options", "filters", "filter", "pagination", "geography", "date_scope", "headers"):
        if field in value and not isinstance(value[field], dict):
            raise SafetyError("RESEARCH_REQUEST_OPTIONS_INVALID")
    if "headers" in value and any(not isinstance(item, str) for item in value["headers"].values()):
        raise SafetyError("RESEARCH_REQUEST_HEADERS_INVALID")
    if "location" in value:
        location = value["location"]
        if not isinstance(location, dict) or not location:
            raise SafetyError("RESEARCH_REQUEST_LOCATION_INVALID")
        for coordinate, bound in (("lat", 90), ("lon", 180)):
            if coordinate in location:
                number = location[coordinate]
                try:
                    if isinstance(number, bool) or not math.isfinite(float(number)) or not -bound <= float(number) <= bound:
                        raise ValueError
                except (TypeError, ValueError, OverflowError):
                    raise SafetyError("RESEARCH_REQUEST_LOCATION_INVALID") from None
    capture = _object({} if capture_options is None else capture_options)
    # Only documented builder markers are omitted; actual API version options
    # nested in body/options remain physical and change the fingerprint.
    physical = {name: item for name, item in value.items() if name not in ("schema_version", "version", "route")}
    contract = {"contract": REQUEST_CONTRACT, "route": route, "method": method,
                "request": physical, "capture_options": capture}
    if len(canonical(contract)) > MAX_REQUEST_BYTES:
        raise SafetyError("RESEARCH_REQUEST_SIZE_LIMIT")
    return value, capture, contract


def physical_request_fingerprint(route, request, *, method="GET", capture_options=None, history=False):
    """Hash complete physical scope; this does not prove access or freshness.

    Inputs must describe the actual adapter request, including any transport,
    paid endpoint/body, date/filter/list node and depth which affects the read.
    Consumer-only information is rejected, not heuristically stripped. URL host
    case and fragments use the existing public destination normalization; query
    strings, words, numbers, option values and list order are otherwise exact.
    ``history`` re-verifies a saved record of a closed route; never a new read.
    """
    return digest(_request_contract(route, request, method, capture_options, supporting=True, history=history)[2])


def parse_fingerprint(capture_hash, parser_contract):
    """A changed parser reparses original saved bytes; it is not a new read."""
    _hash(capture_hash, "RESEARCH_CAPTURE_HASH_INVALID")
    if isinstance(parser_contract, str):
        contract = _text(parser_contract, limit=160, code="RESEARCH_PARSER_CONTRACT_INVALID")
    elif isinstance(parser_contract, dict) and parser_contract:
        contract = _object(parser_contract)
    else:
        raise SafetyError("RESEARCH_PARSER_CONTRACT_INVALID")
    if len(canonical(contract)) > MAX_REQUEST_BYTES:
        raise SafetyError("RESEARCH_PARSER_CONTRACT_INVALID")
    return digest({"contract": PARSE_CONTRACT, "capture_hash": capture_hash, "parser_contract": contract})


@contextmanager
def _catalogue_direction_scope(state, catalogue, current):
    """Reuse one validated view during a pure finite candidate enumeration.

    Only the exact input objects participate. No prepared question, dispatch,
    persisted cache or subsequent state can inherit this temporary view.
    ``current`` is the caller's effective_catalogue result, not the base with
    durable additions applied a second time. Return family copies to callers.
    """
    token = _DIRECTION_SCOPE.set((state, catalogue, {row["id"]: row for row in current["families"]}))
    try:
        yield
    finally:
        _DIRECTION_SCOPE.reset(token)


def _accepted_family(state, family_id, catalogue):
    from .catalogue import load_catalogue
    from .catalogue_expansion import effective_catalogue
    identifier(family_id)
    scope = _DIRECTION_SCOPE.get()
    if scope is not None and scope[0] is state and scope[1] is catalogue:
        family = scope[2].get(family_id)
        if family is None or family["disposition"] != "usable":
            raise SafetyError("RESEARCH_DIRECTION_NOT_ACCEPTED")
        return deepcopy(family)
    current = effective_catalogue(load_catalogue() if catalogue is None else catalogue, state)
    family = next((item for item in current["families"] if item["id"] == family_id), None)
    if family is None or family["disposition"] != "usable":
        raise SafetyError("RESEARCH_DIRECTION_NOT_ACCEPTED")
    return family


def catalogue_direction_subject(state, family_id, *, catalogue=None, qualifiers=None):
    """Freeze a real accepted discovery direction without creating a concept."""
    family = _accepted_family(state, family_id, catalogue)
    scope = {field: deepcopy(family[field]) for field in ("category", "definition", "included", "excluded")}
    scope["qualifiers"] = _object({} if qualifiers is None else qualifiers)
    if len(canonical(scope)) > MAX_REQUEST_BYTES:
        raise SafetyError("RESEARCH_DIRECTION_SCOPE_INVALID")
    return {"kind": "catalogue_direction", "family_id": family_id, "scope": scope, "scope_hash": digest(scope)}


def validate_subject(state, subject, *, catalogue=None):
    """Validate current meaning or frozen accepted direction; never migrate."""
    if not isinstance(subject, dict):
        raise SafetyError("RESEARCH_QUESTION_SUBJECT_INVALID")
    if subject.get("kind") == "concept":
        from .functional_types import effective_functional_definition
        if not {"kind", "concept_id", "definition_hash"} <= set(subject) or not set(subject) <= {"kind", "concept_id", "definition_hash", "definition_basis"}:
            raise SafetyError("RESEARCH_QUESTION_SUBJECT_INVALID")
        concept_id = identifier(subject["concept_id"])
        _hash(subject["definition_hash"], "RESEARCH_QUESTION_MEANING_INVALID")
        effective = effective_functional_definition(state, concept_id)
        if subject["definition_hash"] != effective["meaning_hash"]:
            raise SafetyError("RESEARCH_QUESTION_MEANING_CHANGED")
        if "definition_basis" in subject and subject["definition_basis"] != effective["basis"]:
            raise SafetyError("RESEARCH_QUESTION_MEANING_CHANGED")
        # Acceptance/provenance is labelled on the prepared row, not included
        # in meaning identity. Accepting an identical legacy definition must
        # not manufacture another logical question or physical acquisition.
        return {"kind": "concept", "concept_id": concept_id, "definition_hash": effective["meaning_hash"]}
    if subject.get("kind") == "catalogue_direction":
        if set(subject) != {"kind", "family_id", "scope", "scope_hash"} or not isinstance(subject["scope"], dict):
            raise SafetyError("RESEARCH_QUESTION_SUBJECT_INVALID")
        _hash(subject["scope_hash"], "RESEARCH_DIRECTION_SCOPE_INVALID")
        expected = catalogue_direction_subject(state, subject["family_id"], catalogue=catalogue,
                                              qualifiers=subject["scope"].get("qualifiers"))
        if subject != expected:
            raise SafetyError("RESEARCH_DIRECTION_SCOPE_CHANGED")
        return expected
    raise SafetyError("RESEARCH_QUESTION_SUBJECT_INVALID")


def _dependencies(state, values):
    if not isinstance(values, list) or len(values) > 64:
        raise SafetyError("RESEARCH_QUESTION_DEPENDENCY_INVALID")
    result = []
    for value in values:
        if not isinstance(value, dict) or set(value) != {"table", "id", "hash"}:
            raise SafetyError("RESEARCH_QUESTION_DEPENDENCY_INVALID")
        if value["table"] not in TABLES:
            raise SafetyError("RESEARCH_QUESTION_DEPENDENCY_INVALID")
        record_id = identifier(value["id"])
        expected = _hash(value["hash"], "RESEARCH_QUESTION_DEPENDENCY_INVALID")
        record = state.get(value["table"], {}).get(record_id)
        if record is None or digest(record) != expected:
            raise SafetyError("RESEARCH_DEPENDENCY_CHANGED")
        if value not in result:
            result.append(deepcopy(value))
    return sorted(result, key=lambda item: (item["table"], item["id"], item["hash"]))


def _prepare(state, value, as_of, catalogue):
    if not isinstance(value, dict) or not {"subject", "route", "purpose", "request"} <= set(value) or not set(value) <= _QUESTION_FIELDS:
        raise SafetyError("RESEARCH_QUESTION_FIELDS_INVALID")
    subject = validate_subject(state, value["subject"], catalogue=catalogue)
    route = _route(value["route"])
    purpose = value["purpose"]
    if not isinstance(purpose, str) or purpose not in _PURPOSES:
        raise SafetyError("RESEARCH_QUESTION_PURPOSE_INVALID")
    method = value.get("method", "GET")
    request, capture, physical = _request_contract(route, value["request"], method, value.get("capture_options"))
    physical_hash = digest(physical)
    options = _object(value.get("question_options", {}))
    dependencies = _dependencies(state, value.get("dependencies", []))
    parent_id = value.get("parent_question_id")
    if parent_id is not None:
        identifier(parent_id)
        parent = state.get("query_tasks", {}).get(parent_id)
        if parent is None or parent.get("kind") != RESEARCH_QUESTION_KIND or parent.get("subject") != subject:
            raise SafetyError("RESEARCH_QUESTION_PARENT_INVALID")
        validate_question_record(parent)
    candidates = value.get("wording_candidates", [])
    if not isinstance(candidates, list) or len(candidates) > 8:
        raise SafetyError("RESEARCH_QUESTION_WORDINGS_INVALID")
    wording_rows = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or "text" not in candidate or not set(candidate) <= {"text", "request", "method", "capture_options"}:
            raise SafetyError("RESEARCH_QUESTION_WORDINGS_INVALID")
        text = _text(candidate["text"], limit=180)
        candidate_method = candidate.get("method", method)
        candidate_request, candidate_capture, candidate_physical = _request_contract(
            route, candidate.get("request", request), candidate_method, candidate.get("capture_options", capture))
        row = {"text": text, "request": candidate_request, "method": candidate_method,
               "capture_options": candidate_capture, "physical_request_fingerprint": digest(candidate_physical)}
        row["id"] = key("question-wording", [text, row["physical_request_fingerprint"]])
        if row not in wording_rows:
            wording_rows.append(row)
    binding = {"contract": RESEARCH_QUESTION_KIND, "subject": subject, "route": route, "purpose": purpose,
               "physical_request_fingerprint": physical_hash, "question_options": options,
               "wording_candidates": [{field: row[field] for field in ("id", "text", "physical_request_fingerprint")}
                                      for row in wording_rows],
               "dependencies": dependencies, "parent_question_id": parent_id}
    question_id = key("research-question", binding)
    provenance = {field: identifier(value[field]) for field in ("programme_id", "week") if field in value}
    freshness_reason = value.get("freshness_reason")
    if freshness_reason is not None:
        _text(freshness_reason, limit=2000)
    channel, fee = value.get("collection_channel", "unknown"), value.get("incremental_collection_fee", "unknown")
    if not isinstance(channel, str) or channel not in _CHANNELS or not isinstance(fee, str) or fee not in _FEES:
        raise SafetyError("RESEARCH_QUESTION_CLASSIFICATION_INVALID")
    row = {"id": question_id, "kind": RESEARCH_QUESTION_KIND, "binding": binding, "binding_hash": digest(binding),
           "subject": subject, "route": route, "purpose": purpose, "request": request, "method": method,
           "capture_options": capture, "physical_request_fingerprint": physical_hash, "wording_candidates": wording_rows,
           "prepared_at": as_of, "provenance": provenance, "freshness_reason": freshness_reason,
           "collection_channel": channel, "incremental_collection_fee": fee,
           "progress": {"acquisition": "prepared", "interpretation": "not_started", "scheduling": "ready"}}
    if subject["kind"] == "concept":
        from .functional_types import effective_functional_definition
        row["subject_basis"] = effective_functional_definition(state, subject["concept_id"])["basis"]
    if len(canonical(row)) > MAX_QUESTION_BYTES:
        raise SafetyError("RESEARCH_QUESTION_SIZE_LIMIT")
    return row


def validate_question_record(saved):
    """Verify immutable saved binding without declaring its meaning current."""
    if not isinstance(saved, dict) or saved.get("kind") != RESEARCH_QUESTION_KIND:
        raise SafetyError("RESEARCH_QUESTION_BINDING_CONFLICT")
    binding = saved.get("binding")
    if (not isinstance(binding, dict) or binding.get("contract") != RESEARCH_QUESTION_KIND
            or saved.get("binding_hash") != digest(binding) or saved.get("id") != key("research-question", binding)
            or any(saved.get(field) != binding.get(field) for field in ("subject", "route", "purpose", "physical_request_fingerprint"))
            or physical_request_fingerprint(saved.get("route"), saved.get("request"), method=saved.get("method"),
                                             capture_options=saved.get("capture_options"), history=True)
               != binding.get("physical_request_fingerprint")):
        raise SafetyError("RESEARCH_QUESTION_BINDING_CONFLICT")
    wordings = saved.get("wording_candidates")
    if not isinstance(wordings, list):
        raise SafetyError("RESEARCH_QUESTION_BINDING_CONFLICT")
    references = []
    for wording in wordings:
        if not isinstance(wording, dict) or set(wording) != {"id", "text", "request", "method", "capture_options", "physical_request_fingerprint"}:
            raise SafetyError("RESEARCH_QUESTION_BINDING_CONFLICT")
        fingerprint = physical_request_fingerprint(saved["route"], wording["request"], method=wording["method"],
                                                   capture_options=wording["capture_options"], history=True)
        if (wording["physical_request_fingerprint"] != fingerprint
                or wording["id"] != key("question-wording", [wording["text"], fingerprint])):
            raise SafetyError("RESEARCH_QUESTION_BINDING_CONFLICT")
        references.append({field: wording[field] for field in ("id", "text", "physical_request_fingerprint")})
    if references != binding.get("wording_candidates"):
        raise SafetyError("RESEARCH_QUESTION_BINDING_CONFLICT")
    return deepcopy(saved)


def _same_binding(saved, prepared):
    validate_question_record(saved)
    if (not isinstance(saved, dict) or saved.get("kind") != RESEARCH_QUESTION_KIND
            or saved.get("binding") != prepared["binding"] or saved.get("binding_hash") != prepared["binding_hash"]
            or any(saved.get(field) != prepared[field] for field in ("id", "subject", "route", "purpose", "physical_request_fingerprint"))):
        raise SafetyError("RESEARCH_QUESTION_BINDING_CONFLICT")


def prepare_research_questions(memory, questions, *, as_of, catalogue=None):
    """Prepare <=100 questions atomically, without launch, migration or reset.

    ``questions`` contains dictionaries with subject/route/purpose/request.
    Concept subjects bind effective_functional_definition(...).meaning_hash;
    direction subjects come from catalogue_direction_subject. Optional exact
    dependencies are {table,id,hash}. Different consumers may share one physical
    fingerprint. Later programme/week ownership is a dispatcher responsibility:
    replay keeps original preparation/provenance and mutable progress intact.
    """
    if not isinstance(questions, list) or len(questions) > MAX_QUESTIONS:
        raise SafetyError("RESEARCH_QUESTION_BATCH_INVALID")
    at = timestamp(as_of).astimezone(timezone.utc).isoformat()
    # Freeze caller inputs before the lock's dependency check; caller mutation
    # cannot change what that check or the canonical event actually means.
    inputs, base = deepcopy(questions), deepcopy(catalogue)
    for attempt in range(3):
        state = memory.snapshot()
        rows = {}
        for value in inputs:
            row = _prepare(state, value, at, base)
            rows.setdefault(row["id"], row)
        new = {record_id: row for record_id, row in rows.items() if record_id not in state["query_tasks"]}
        for record_id in rows.keys() - new.keys():
            _same_binding(state["query_tasks"][record_id], rows[record_id])
        if new:
            def validate(current):
                for record_id, row in rows.items():
                    if validate_subject(current, row["subject"], catalogue=base) != row["subject"]:
                        raise SafetyError("RESEARCH_DEPENDENCY_CHANGED")
                    _dependencies(current, row["binding"]["dependencies"])
                    parent_id = row["binding"]["parent_question_id"]
                    if parent_id is not None:
                        parent = validate_question_record(current["query_tasks"].get(parent_id))
                        if parent["subject"] != row["subject"]:
                            raise SafetyError("RESEARCH_QUESTION_PARENT_INVALID")
                    saved = current["query_tasks"].get(record_id)
                    if record_id in new:
                        if saved is not None:
                            raise SafetyError("RESEARCH_DEPENDENCY_CHANGED")
                    else:
                        _same_binding(saved, row)
            changes = [change("query_tasks", record_id, row) for record_id, row in sorted(new.items())]
            try:
                memory.commit(key("prepare-research-questions", changes), changes, validate=validate)
            except SafetyError as error:
                # A racing *local preparation* may already have created the
                # same binding. Re-read, never dispatch or overwrite progress.
                if str(error) != "RESEARCH_DEPENDENCY_CHANGED" or attempt == 2:
                    raise
                continue
        saved_state = memory.snapshot()
        records = []
        for record_id, row in rows.items():
            saved = saved_state["query_tasks"].get(record_id)
            _same_binding(saved, row)
            records.append(deepcopy(saved))
        return {"question_ids": list(rows), "created": len(new), "reused": len(rows) - len(new),
                "records": records, "collector_dispatched": False, "source_attempts_created": 0,
                "model_calls_started": 0}
    raise SafetyError("RESEARCH_DEPENDENCY_CHANGED")
