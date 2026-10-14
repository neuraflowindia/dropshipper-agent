"""Code-owned, current-scope supplier effort from verified saved receipts.

The canonical source check is a projection of a finite saved search/page pass.
It proves an attempt, never stock, a supplier promise or absence of suppliers.
Readers use this record rather than scanning arbitrary capture folders. Legacy
receipts remain untouched and require real HTTP/navigation evidence.
"""

from copy import deepcopy
import re

from .models import SafetyError, digest, identifier, timestamp, utc_now
from .public_pages import _public_url
from .research_memory import key

VERSION = "supplier-effort-v1"
CLIENT_NO_RESULTS = "Supplier search did not produce any usable results."
# Supplier routes the programme no longer prepares or sends (owner decision,
# 14 October 2026). Google serves its "enable JavaScript" gate to the plain
# reader and "unusual traffic" refusals to the browser, and the owner will not
# bypass Google or buy a search service. Saved records of a retired route stay
# as history; they are not current supplier effort, a reason a product waits
# or a failed route for new work. This is the single list of retired routes.
RETIRED_SUPPLIER_ROUTES = {
    "google-suppliers": "Not available: Google requires JavaScript for automated reading; no approved official route.",
}
# Older effort records name the same routes differently.
SUPPLIER_ROUTE_ALIASES = {"google-public-html": "google-suppliers", "google-browser-html": "google-suppliers",
                          "known-supplier-pages": "company-pages"}
_SCOPE_FIELDS = ("concept_id", "label", "category", "function", "mechanism", "features")
_OUTCOMES = {"completed_empty", "blocked", "failed", "partial", "interpretation_pending", "usable_leads", "prelaunch_failed"}
# How much a current original read achieved, best first. A product's supplier
# search is "blocked" only when every current original failed or was refused.
_OUTCOME_RANK = {"usable_leads": 6, "interpretation_pending": 6, "completed_empty": 5, "partial": 4,
                 "blocked": 3, "failed": 2, "prelaunch_failed": 1}
_PARSER_FILE = re.compile(r"capture-parser-([1-9][0-9]{0,3})(?:-[0-9a-f]{16})?\.json")
_FIELDS = {"schema_version", "kind", "source", "route", "at", "state", "reason", "reference", "concept_id",
           "description_hash", "sourcing_scope", "sourcing_scope_hash", "pass_id", "receipts_hash", "receipt_refs",
           "actual_attempt_refs", "last_attempt_at", "internal_outcome", "accepted_lead_refs", "effort_satisfied",
           "supplier_search_status"}
_PRELAUNCH_CODES = {"PUBLIC_FETCH_WORKERS_BUSY", "PUBLIC_INPUT_INVALID", "PUBLIC_URL_INVALID",
                    "SUPPLIER_BROWSER_INPUT_INVALID", "SUPPLIER_BROWSER_QUERY_INVALID",
                    "SUPPLIER_BROWSER_DEPENDENCY_UNAVAILABLE", "SUPPLIER_BROWSER_EXECUTABLE_INVALID"}


def supplier_scope(concept_id, description):
    """Identity/use fields define sourcing; a brand judgment does not."""
    identifier(concept_id)
    if not isinstance(description, dict):
        raise SafetyError("SUPPLIER_EFFORT_SCOPE_INVALID")
    product = dict(description, concept_id=concept_id)
    return {field: deepcopy(product.get(field)) for field in _SCOPE_FIELDS}


def failed_reader_receipt(item, reason, error=None):
    """Sanitise wrapper failure while retaining an owned receipt if supplied.

    An arbitrary exception is unknown dispatch, not proof of an attempt. An
    instrumented reader may attach ``execution_receipt`` to its exception;
    only the normal receipt verifier below can accept that stage evidence.
    """
    supplied = getattr(error, "execution_receipt", None)
    if (isinstance(supplied, dict) and supplied.get("id") == item["id"]
            and supplied.get("requested_url") == item["url"]):
        kept = {field: deepcopy(supplied.get(field)) for field in ("schema_version", "id", "requested_url", "final_url",
                 "checked_at", "http_status", "execution_stage", "attempt_started_at", "execution_ref", "body_complete")}
        candidate = {**kept, "state": "unavailable", "reason": reason}
        # Attribute data is not trusted merely because an exception carries it.
        # Reject private/credential-bearing URLs and malformed metadata before
        # retaining it in a durable capture; never save arbitrary error text.
        if _receipt_ref(candidate, "owned-reader-exception") is not None:
            return candidate
    return {"id": item["id"], "state": "unavailable", "reason": reason,
            "requested_url": item["url"], "final_url": None, "checked_at": utc_now(),
            "dispatch_known": False}


def _receipt_ref(receipt, receipt_reference):
    """Return a bounded audit row and whether there is real dispatch proof."""
    if not isinstance(receipt, dict):
        return None
    try:
        receipt_id = identifier(receipt.get("id"))
        requested = _public_url(receipt.get("requested_url"))[0]
        checked = receipt.get("checked_at")
        timestamp(checked)
        final = receipt.get("final_url")
        if final is not None:
            final = _public_url(final)[0]
        stage, started, execution_ref, proof = "unknown", None, None, "none"
        if receipt.get("schema_version") == 2 and type(receipt.get("schema_version")) is int:
            stage = receipt.get("execution_stage")
            if stage not in ("not_dispatched", "attempt_started", "response_received", "unknown"):
                raise ValueError
            started, execution_ref = receipt.get("attempt_started_at"), receipt.get("execution_ref")
            if stage in ("not_dispatched", "unknown"):
                if started is not None or execution_ref is not None:
                    raise ValueError
            else:
                timestamp(started)
                if timestamp(started) > timestamp(checked):
                    raise ValueError
                identifier(execution_ref)
                if (not re.fullmatch(r"(?:public-fetch|supplier-browser)-[0-9a-f]{32}", execution_ref)
                        or receipt.get("reason") in _PRELAUNCH_CODES or receipt.get("no_request") is True):
                    raise ValueError
                if stage == "response_received" and not (type(receipt.get("http_status")) is int
                                                           and 100 <= receipt["http_status"] <= 599):
                    raise ValueError
                proof = "owned_reader_execution"
        elif receipt.get("schema_version") not in (None, 1) or isinstance(receipt.get("schema_version"), bool):
            raise ValueError
        elif (receipt.get("no_request") is not True and receipt.get("reason") not in _PRELAUNCH_CODES
              and type(receipt.get("http_status")) is int and 100 <= receipt["http_status"] <= 599 and final):
            # An actual HTTP response, including a 403/429, is stronger than a
            # requested URL/date. Its original capture date stays unchanged.
            stage, started, proof = "response_received", checked, "legacy_http_response"
        return {"id": receipt_id, "hash": digest(receipt), "reference": receipt_reference,
                "requested_url": requested, "final_url": final, "checked_at": checked,
                "execution_stage": stage, "attempt_started_at": started, "execution_ref": execution_ref,
                "http_status": receipt.get("http_status") if type(receipt.get("http_status")) is int else None,
                "proof": proof}
    except (SafetyError, TypeError, ValueError):
        return None


def _validated_record(record):
    try:
        if (not isinstance(record, dict) or set(record) != _FIELDS or record.get("kind") != VERSION
                or type(record.get("schema_version")) is not int or record["schema_version"] != 2
                or record["source"] != "supplier" or record["internal_outcome"] not in _OUTCOMES
                or not isinstance(record["sourcing_scope"], dict) or set(record["sourcing_scope"]) != set(_SCOPE_FIELDS)
                or record["sourcing_scope"].get("concept_id") != record["concept_id"]
                or record["sourcing_scope_hash"] != digest(record["sourcing_scope"])
                or not isinstance(record["receipt_refs"], list) or len(record["receipt_refs"]) > 10
                or not isinstance(record["actual_attempt_refs"], list)
                or not isinstance(record["accepted_lead_refs"], list)
                or record["accepted_lead_refs"] != sorted(set(record["accepted_lead_refs"]))):
            raise ValueError
        identifier(record["concept_id"]); identifier(record["pass_id"])
        timestamp(record["at"])
        for value in (record["description_hash"], record["receipts_hash"]):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError
        if not isinstance(record["reference"], str) or not record["reference"] or len(record["reference"]) > 4096:
            raise ValueError
        actual = []
        seen = set()
        for ref in record["receipt_refs"]:
            if (not isinstance(ref, dict) or set(ref) != {"id", "hash", "reference", "requested_url", "final_url",
                    "checked_at", "execution_stage", "attempt_started_at", "execution_ref", "http_status", "proof"}
                    or not isinstance(ref["hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", ref["hash"])
                    or ref["reference"] != record["reference"] or ref["id"] in seen):
                raise ValueError
            identifier(ref["id"]); timestamp(ref["checked_at"])
            _public_url(ref["requested_url"])
            seen.add(ref["id"])
            if ref["final_url"] is not None:
                _public_url(ref["final_url"])
            if ref["proof"] != "none":
                if ref["proof"] not in ("owned_reader_execution", "legacy_http_response"):
                    raise ValueError
                timestamp(ref["attempt_started_at"])
                if timestamp(ref["attempt_started_at"]) > timestamp(ref["checked_at"]):
                    raise ValueError
                if ref["execution_stage"] not in ("attempt_started", "response_received"):
                    raise ValueError
                if ref["proof"] == "owned_reader_execution":
                    if (not isinstance(ref["execution_ref"], str)
                            or not re.fullmatch(r"(?:public-fetch|supplier-browser)-[0-9a-f]{32}", ref["execution_ref"])):
                        raise ValueError
                    if ref["execution_stage"] == "response_received" and (type(ref["http_status"]) is not int
                                                                                     or not 100 <= ref["http_status"] <= 599):
                        raise ValueError
                elif (ref["execution_stage"] != "response_received" or ref["execution_ref"] is not None
                      or ref["final_url"] is None or type(ref["http_status"]) is not int
                      or not 100 <= ref["http_status"] <= 599):
                    raise ValueError
                actual.append(ref["id"])
            elif (ref["execution_stage"] not in ("unknown", "not_dispatched")
                  or ref["attempt_started_at"] is not None or ref["execution_ref"] is not None):
                raise ValueError
        if record["actual_attempt_refs"] != sorted(actual) or record["effort_satisfied"] is not bool(actual):
            raise ValueError
        if (not actual and record["internal_outcome"] != "prelaunch_failed"
                or record["accepted_lead_refs"] and (not actual or record["internal_outcome"] != "usable_leads")):
            raise ValueError
        at = max((ref["attempt_started_at"] for ref in record["receipt_refs"] if ref["proof"] != "none"),
                 key=timestamp, default=None)
        if record["last_attempt_at"] != at:
            raise ValueError
        for lead_ref in record["accepted_lead_refs"]:
            identifier(lead_ref)
        outcome = record["internal_outcome"]
        expected_state = ("usable" if outcome in ("usable_leads", "completed_empty") else "failed" if outcome == "failed"
                          else "blocked" if outcome in ("blocked", "prelaunch_failed") else "insufficient")
        expected_status = ("complete" if outcome in ("usable_leads", "completed_empty") else
                           "blocked" if outcome in ("blocked", "failed", "prelaunch_failed") else "attempted")
        if (record["state"] != expected_state or record["supplier_search_status"] != expected_status
                or not isinstance(record["route"], str) or not record["route"] or len(record["route"]) > 100
                or not isinstance(record["reason"], str) or not record["reason"] or len(record["reason"]) > 1000):
            raise ValueError
        return record
    except (KeyError, TypeError, ValueError, SafetyError):
        raise SafetyError("SUPPLIER_EFFORT_RECORD_INVALID") from None


def record_supplier_effort(memory, *, concept_id, description, pass_id, receipts, receipt_reference,
                           outcome, accepted_lead_refs=(), route="known-supplier-pages", expected_requests=None):
    """Append/reuse a verified finite-pass projection, without fetching again.

    This is called only after a wrapper has checked its saved request/result
    bindings. It never accepts supplier F text as a declaration of effort.
    Scope changes preserve the prior record and refuse current applicability.
    """
    scope = supplier_scope(concept_id, description)
    identifier(pass_id)
    if (not isinstance(receipts, list) or len(receipts) > 10 or outcome not in _OUTCOMES
            or not isinstance(receipt_reference, str) or not receipt_reference or len(receipt_reference) > 4096
            or not isinstance(route, str) or not route or len(route) > 100
            or not isinstance(accepted_lead_refs, (list, tuple)) or len(accepted_lead_refs) > 20):
        raise SafetyError("SUPPLIER_EFFORT_INPUT_INVALID")
    accepted = sorted(set(identifier(ref) for ref in accepted_lead_refs))
    if expected_requests is not None:
        if not isinstance(expected_requests, dict) or len(expected_requests) > 10:
            raise SafetyError("SUPPLIER_EFFORT_INPUT_INVALID")
        for request_id, requested_url in expected_requests.items():
            identifier(request_id)
            if _public_url(requested_url)[0] != requested_url:
                raise SafetyError("SUPPLIER_EFFORT_INPUT_INVALID")
    refs = [ref for receipt in receipts if (ref := _receipt_ref(receipt, receipt_reference)) is not None
            and (expected_requests is None or expected_requests.get(ref["id"]) == ref["requested_url"])]
    if len({ref["id"] for ref in refs}) != len(refs):
        raise SafetyError("SUPPLIER_EFFORT_RECEIPT_CONFLICT")
    actual = sorted(ref["id"] for ref in refs if ref["proof"] != "none")
    latest = max((ref["attempt_started_at"] for ref in refs if ref["proof"] != "none"), key=timestamp, default=None)
    if accepted and not actual:
        raise SafetyError("SUPPLIER_EFFORT_LEADS_WITHOUT_ATTEMPT")
    if not actual:
        outcome = "prelaunch_failed"
    elif accepted:
        outcome = "usable_leads"
    binding = {"kind": VERSION, "concept_id": concept_id, "description_hash": digest(description),
               "sourcing_scope": scope, "sourcing_scope_hash": digest(scope), "pass_id": pass_id,
               "receipts_hash": digest(receipts), "reference": receipt_reference, "receipt_refs": refs,
               "actual_attempt_refs": actual, "last_attempt_at": latest, "internal_outcome": outcome,
               "accepted_lead_refs": accepted, "effort_satisfied": bool(actual), "route": route}
    check_id = key("supplier-effort", binding)
    state = memory.snapshot()
    concept = state["concepts"].get(concept_id)
    if concept is None or supplier_scope(concept_id, concept.get("description")) != scope:
        raise SafetyError("SUPPLIER_EFFORT_SCOPE_CHANGED")
    existing = state["source_checks"].get(check_id)
    if existing is not None:
        _validated_record(existing)
        if any(existing.get(field) != value for field, value in binding.items()):
            raise SafetyError("SUPPLIER_EFFORT_RECORD_CONFLICT")
        return {"check_id": check_id, "record": deepcopy(existing), "reused": True}
    checked_dates = [ref["checked_at"] for ref in refs]
    at = max(checked_dates, key=timestamp, default=None) or utc_now()
    record = {**binding, "schema_version": 2, "source": "supplier", "at": at,
              "state": "usable" if outcome in ("usable_leads", "completed_empty") else "failed" if outcome == "failed"
                       else "blocked" if outcome in ("blocked", "prelaunch_failed") else "insufficient",
              "reason": "Finite saved supplier pass; commercial capability interpretation remains separate.",
              "supplier_search_status": "complete" if outcome in ("usable_leads", "completed_empty") else
                                        "blocked" if outcome in ("blocked", "failed", "prelaunch_failed") else "attempted"}
    _validated_record(record)
    memory.put("source_checks", check_id, record, event_id=key("supplier-effort-record", check_id))
    return {"check_id": check_id, "record": deepcopy(record), "reused": False}


def _parser_rank(record):
    """The saved interpretation version a record was projected from.

    A parser repair re-reads the same original receipts into
    capture-parser-<N>.json; the original capture.json (or any other saved
    page reference) is older than every such repair of the same original.
    """
    match = _PARSER_FILE.fullmatch(record["reference"].replace("\\", "/").rsplit("/", 1)[-1])
    return int(match[1]) if match else 0


def _current_outcome(records):
    """Aggregate current originals; a reparse supersedes only its own original.

    Records of one original read (same route and pass) differ only by parser
    version: the newest interpretation wins, independent of record IDs. Across
    originals and routes, a later failed or refused read does not erase what
    another read already produced, so "blocked"/"failed" remains only when
    every current original failed or was refused.
    """
    current = {}
    for check_id, record in records:
        slot = (record["route"], record["pass_id"])
        order = (_parser_rank(record), timestamp(record["at"]), check_id)
        if slot not in current or order > current[slot][0]:
            current[slot] = (order, record["internal_outcome"])
    if not current:
        return "not_attempted"
    best = max((outcome for _, outcome in current.values()), key=_OUTCOME_RANK.__getitem__)
    # Leads not accepted by the caller's reviewed interpretation are pending.
    return "interpretation_pending" if best == "usable_leads" else best


def current_supplier_effort(state, concept_id, description, *, accepted_lead_refs=(), as_of=None):
    """Read canonical current-scope evidence; date/unknown is never proof.

    Retired routes (RETIRED_SUPPLIER_ROUTES) are saved history only: their
    records are neither current effort nor an outcome for this product.
    """
    scope = supplier_scope(concept_id, description)
    deadline = timestamp(as_of) if as_of is not None else None
    accepted = sorted(set(identifier(ref) for ref in accepted_lead_refs))
    records, actual = [], {}
    for check_id, record in state.get("source_checks", {}).items():
        if record.get("kind") != VERSION or record.get("concept_id") != concept_id:
            continue
        _validated_record(record)
        if (record["sourcing_scope"] != scope
                or SUPPLIER_ROUTE_ALIASES.get(record["route"], record["route"]) in RETIRED_SUPPLIER_ROUTES):
            continue
        if deadline is not None and timestamp(record["at"]) > deadline:
            continue
        records.append((check_id, record))
        for ref in record["receipt_refs"]:
            if ref["proof"] == "none":
                continue
            if deadline is not None and (timestamp(ref["attempt_started_at"]) > deadline or timestamp(ref["checked_at"]) > deadline):
                continue
            actual[(ref["id"], ref["hash"])] = {**deepcopy(ref), "check_id": check_id, "pass_id": record["pass_id"]}
    return {"check_ids": sorted(check_id for check_id, _ in records),
            "actual_attempt_refs": sorted(actual.values(), key=lambda ref: (timestamp(ref["attempt_started_at"]), ref["id"], ref["hash"])),
            "last_attempt_at": max((ref["attempt_started_at"] for ref in actual.values()), key=timestamp, default=None),
            "internal_outcome": "usable_leads" if accepted else _current_outcome(records),
            "effort_satisfied": bool(actual), "usable_lead_count": len(accepted)}


def frozen_supplier_effort_valid(product):
    """Check new frozen supplier facts without files, calls or invented proof.

    A received/successful page or a usable lead is not required. Real failed,
    blocked and interpretation-pending attempts are sufficient. Existing F or
    recognised empty-search evidence has its own explicit legacy binding;
    it is not converted into a fictional new reader receipt. Historical
    thirteen-criterion products stay under their original validator.
    """
    try:
        proof = product.get("functional_supplier_proof") if isinstance(product, dict) else None
        if isinstance(proof, dict) and proof.get("contract") == "functional-supplier-scoped-proof-v2":
            if (product.get("recommendation_contract") != "functional-type-examples-v1"
                    or product.get("fit_policy_id") != "fit-10-v1"):
                return False
            from .report_selection import _validate_frozen_functional_suppliers
            return _validate_frozen_functional_suppliers(product)["effort_satisfied"] is True
        if not isinstance(product, dict):
            return False
        if product.get("fit_policy_id") in (None, "fit-13-v1"):
            return True
        if product.get("fit_policy_id") != "fit-10-v1" or product.get("supplier_effort_satisfied") is not True:
            return False
        effort, revisions = product.get("supplier_effort"), product.get("source_revisions")
        if not isinstance(effort, dict) or not isinstance(revisions, dict):
            return False

        def ids(value):
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value) or len(set(value)) != len(value):
                raise ValueError
            return {identifier(item) for item in value}

        def bound(table):
            values = revisions.get(table, {})
            if (not isinstance(values, dict) or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                                                   for value in values.values())):
                raise ValueError
            return {identifier(item) for item in values}

        canonical = ids(effort.get("check_ids"))
        legacy_f = ids(effort.get("legacy_interpretation_ids", []))
        legacy_search = ids(effort.get("legacy_search_check_ids", []))
        frozen_f = ids(product.get("supplier_assessment_ids"))
        frozen_search = ids(product.get("supplier_search_check_ids"))
        if (legacy_f != frozen_f or not legacy_f <= bound("supplier_assessments")
                or canonical & legacy_search or canonical | legacy_search != frozen_search
                or not frozen_search <= bound("source_checks")):
            return False
        suppliers = product.get("suppliers")
        count, found = effort.get("usable_lead_count"), product.get("supplier_leads_found")
        if (not isinstance(suppliers, list) or any(not isinstance(lead, dict) for lead in suppliers)
                or type(count) is not int or type(found) is not int or count < 0
                or count != found or count != len(suppliers) or count > len(frozen_f)
                or len({identifier(lead.get("lead_id")) for lead in suppliers}) != count):
            return False
        outcome = effort.get("internal_outcome")
        if (outcome not in _OUTCOMES | {"not_attempted"} or (count > 0) != (outcome == "usable_leads")
                or product.get("supplier_search_status") not in ("attempted", "complete", "blocked")):
            return False
        actual = effort.get("actual_attempt_refs")
        if not isinstance(actual, list) or type(effort.get("effort_satisfied")) is not bool:
            return False
        if effort["effort_satisfied"] is not bool(actual):
            return False
        seen, dates = set(), []
        for ref in actual:
            if not isinstance(ref, dict) or identifier(ref.get("check_id")) not in canonical:
                return False
            identifier(ref.get("id")); identifier(ref.get("pass_id"))
            if not isinstance(ref.get("hash"), str) or not re.fullmatch(r"[0-9a-f]{64}", ref["hash"]):
                return False
            identity = (ref["id"], ref["hash"])
            if identity in seen:
                return False
            seen.add(identity)
            if not isinstance(ref.get("reference"), str) or not ref["reference"] or len(ref["reference"]) > 4096:
                return False
            _public_url(ref.get("requested_url"))
            if ref.get("final_url") is not None:
                _public_url(ref["final_url"])
            start, checked = timestamp(ref.get("attempt_started_at")), timestamp(ref.get("checked_at"))
            if start > checked or ref.get("execution_stage") not in ("attempt_started", "response_received"):
                return False
            dates.append(ref["attempt_started_at"])
            if ref.get("proof") == "owned_reader_execution":
                if (not isinstance(ref.get("execution_ref"), str)
                        or not re.fullmatch(r"(?:public-fetch|supplier-browser)-[0-9a-f]{32}", ref["execution_ref"])):
                    return False
                if ref["execution_stage"] == "response_received" and (type(ref.get("http_status")) is not int
                                                                      or not 100 <= ref["http_status"] <= 599):
                    return False
            elif ref.get("proof") == "legacy_http_response":
                if (ref["execution_stage"] != "response_received" or ref.get("execution_ref") is not None
                        or ref.get("final_url") is None or type(ref.get("http_status")) is not int
                        or not 100 <= ref["http_status"] <= 599):
                    return False
            else:
                return False
        if effort.get("last_attempt_at") != max(dates, key=timestamp, default=None):
            return False
        if actual and outcome == "not_attempted":
            return False
        return bool(actual or legacy_f or legacy_search)
    except (KeyError, TypeError, ValueError, SafetyError):
        return False
