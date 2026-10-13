"""Saved supplier scope -> grouped report facts, without a new sourcing gate.

Company/workflow information and individual offers stay separate.  This module
does not collect pages, call a model, infer company identity, or change legacy F.
"""

from copy import deepcopy

from .frozen_records import MAX_BYTES as MAX_ORIGINAL_BYTES, freeze_original_json, read_original_json
from .models import SafetyError, canonical, digest, timestamp
from .research_memory import TABLES, key
from .supplier_terms import APPLICATION_KIND, VERSION, current_supplier_terms


CONTRACT = "functional-supplier-scoped-proof-v2"
FIELDS = ("supplier_scoped_terms", "supplier_scope_coverage")
MAX_GROUPS = 20
MAX_RECORD_BYTES = 2 * 1024 * 1024
_TABLES = ("concepts", "source_checks", "supplier_assessments", "jobs")


def _group_identity(row):
    # A trading name or host never joins companies. Unknown company identities
    # retain only the exact original lead placement.
    return (["company", row["company_ref"], row["workflow_ref"]] if row["company_ref"]
            else ["lead", row["lead_id"]])


def _groups(rows):
    groups = {}
    for row in rows:
        group_id = key("supplier-report-group", _group_identity(row))
        groups.setdefault(group_id, []).append(row)
    return groups


def _originals(state, concept_id, rows):
    selected = {table: {} for table in _TABLES}
    selected["concepts"][concept_id] = deepcopy(state["concepts"][concept_id])
    for rid, record in state["source_checks"].items():
        if record.get("kind") == "functional-definition-v1" and record.get("concept_id") == concept_id:
            selected["source_checks"][rid] = deepcopy(record)
    for row in rows:
        aid = row["application_id"]
        app = state["source_checks"][aid]
        selected["source_checks"][aid] = deepcopy(app)
        eid = app["binding"]["effort_id"]
        selected["source_checks"][eid] = deepcopy(state["source_checks"][eid])
        sid = row["assessment_id"]
        semantic = state["supplier_assessments"][sid]
        selected["supplier_assessments"][sid] = deepcopy(semantic)
        selected["jobs"][semantic["job_id"]] = deepcopy(state["jobs"][semantic["job_id"]])
        # Hybrid F's literal section selection is the original interpretation
        # owner, not a second judgment or an inferred supplier identity.
        candidate_id = semantic.get("candidate_review_id")
        if candidate_id:
            candidate = state["supplier_assessments"][candidate_id]
            selected["supplier_assessments"][candidate_id] = deepcopy(candidate)
            selected["jobs"][candidate["job_id"]] = deepcopy(state["jobs"][candidate["job_id"]])
    return selected


def _display(state, scoped):
    """One company/workflow lead, with every selected offer kept local."""
    from .report_selection import _url
    groups, refs = [], {}
    for group_id, rows in sorted(_groups(scoped["all"]).items()):
        useful = [row for row in rows if row["relevance"]["relationship"] in ("exact_product", "relevant_company")]
        if not useful:
            continue
        company, offers = [], []
        for row in rows:
            semantic = state["supplier_assessments"][row["assessment_id"]]
            item = semantic["input"]
            value = deepcopy(row)
            value["scope_label"] = (item["offer_identity"]["label"] if item["offer_identity"]
                                    else item["workflow"] or "Supplier information needs review")
            value["source_pages"] = [{"id": page["id"], "url": _url(page["url"]), "title": page["title"]}
                                     for page in item["pages"] if page["id"] in item["scope_page_ids"]]
            (offers if row["scope_kind"] == "product_offer" else company).append(value)
            for page in row["page_refs"]:
                rid = key("supplier-scoped-ref", [row["application_id"], page["page_id"]])
                refs[rid] = {"id": rid, "url": _url(page["url"]), "source": "Supplier page (scoped review)",
                    "observed_at": page["observed_at"], "date_basis": "supplier_page_capture",
                    "original_page_id": page["page_id"], "application_id": row["application_id"]}
        first = useful[0]
        semantic = state["supplier_assessments"][first["assessment_id"]]
        item = semantic["input"]
        page = next((p for p in item["pages"] if p["id"] in item["scope_page_ids"]), item["pages"][0])
        from urllib.parse import urlsplit
        name = (item.get("company_identity") or {}).get("name") or urlsplit(page["url"]).hostname or page["title"]
        limitation = "Company information and each offer's own terms are shown separately."
        # Never flatten an offer's MOQ, quote or dispatch restriction into the
        # entire company. These summaries are intentionally cautious; actual
        # qualified company terms and offer-local terms remain visible below.
        terms = [deepcopy(term) for row in company for term in row["terms"] if term["qualified"]]
        groups.append({"id": group_id, "lead_id": group_id, "original_lead_ids": sorted({r["lead_id"] for r in rows}),
            "name": name, "url": _url(page["url"]), "company_ref": first["company_ref"],
            "workflow_ref": first["workflow_ref"], "relationship": "relevant_company",
            "relevance": {"relationship": "relevant_company", "reason": limitation, "references": []},
            "single_piece_dispatch": "not_stated", "subscription": "not_stated", "min_order": None,
            "ecommerce_support": "Not stated", "reason": limitation,
            "supply_arrangement": "See each offer's own purchase and dispatch terms.", "terms": terms,
            "reviewed_at": max((r["reviewed_at"] for r in rows), key=timestamp),
            "source_capture_at": min((r["source_capture_at"] for r in rows), key=timestamp),
            "current": any(r["current"] for r in rows), "scoped_usable": any(r["scoped_usable"] for r in rows),
            "company_workflows": company, "product_offers": offers,
            "scoped_supplier_contract": VERSION})
    groups.sort(key=lambda r: (not r["current"], not r["scoped_usable"], r["id"]))
    return groups, list(refs.values())


def freeze_functional_supplier_terms(state, concept_id, *, as_of, legacy_context):
    """Freeze complete company/workflow groups under a finite packet budget.

    The legacy context capsule is retained exactly. A large optional group can
    be omitted with an explicit count; individual offers never borrow the
    omitted company's conflicting terms or siblings' prices.
    """
    timestamp(as_of)
    scoped = current_supplier_terms(state, concept_id=concept_id, as_of=as_of)
    grouped = _groups(scoped["all"])
    ordered = sorted(grouped, key=lambda gid: (not any(r["current"] for r in grouped[gid]),
        not any(r["scoped_usable"] for r in grouped[gid]), gid))
    selected, omitted = [], []
    for gid in ordered:
        proposed = selected + [gid]
        rows = [row for group in proposed for row in grouped[group]]
        originals = _originals(state, concept_id, rows)
        if (len(proposed) > MAX_GROUPS or len(canonical(originals)) > MAX_RECORD_BYTES
                or any(len(canonical(record)) > MAX_ORIGINAL_BYTES
                       for table in originals.values() for record in table.values())):
            omitted.append(gid)
        else:
            selected.append(gid)
    originals = _originals(state, concept_id, [row for gid in selected for row in grouped[gid]])
    replay = {table: {} for table in TABLES}
    replay.update(originals)
    selected_scoped = current_supplier_terms(replay, concept_id=concept_id, as_of=as_of)
    proof = {"contract": CONTRACT, "as_of": as_of, "concept_id": concept_id,
        "legacy_context": deepcopy(legacy_context),
        "records": {table: {rid: freeze_original_json(record) for rid, record in sorted(rows.items())}
                    for table, rows in originals.items()}}
    coverage = {"available_groups": len(grouped), "selected_groups": len(selected), "omitted_groups": len(omitted),
        "selected_group_ids": sorted(selected), "omitted_group_ids": sorted(omitted),
        "limit": MAX_GROUPS, "record_byte_limit": MAX_RECORD_BYTES}
    return {"functional_supplier_proof": proof, "supplier_scoped_terms": selected_scoped,
            "supplier_scope_coverage": coverage}


def validate_frozen_functional_supplier_terms(product):
    """Verify scope/date/offer grouping against frozen originals only."""
    try:
        proof = product["functional_supplier_proof"]
        if (set(proof) != {"contract", "as_of", "concept_id", "legacy_context", "records"}
                or proof["contract"] != CONTRACT or proof["concept_id"] != product["concept_id"]
                or proof["as_of"] != product["example_as_of"] or set(proof["records"]) != set(_TABLES)):
            raise SafetyError("REPORT_SCOPED_SUPPLIER_PROOF_INVALID")
        state = {table: {} for table in TABLES}
        for table, rows in proof["records"].items():
            for rid, frozen in rows.items():
                record = read_original_json(frozen)
                if "source_revisions" in product and product["source_revisions"].get(table, {}).get(rid) != digest(record):
                    raise SafetyError("REPORT_SCOPED_SUPPLIER_ORIGINAL_CHANGED")
                state[table][rid] = record
        if len(canonical({table: state[table] for table in _TABLES})) > MAX_RECORD_BYTES:
            raise SafetyError("REPORT_SCOPED_SUPPLIER_PROOF_INVALID")
        scoped = current_supplier_terms(state, concept_id=proof["concept_id"], as_of=proof["as_of"])
        if _originals(state, proof["concept_id"], scoped["all"]) != {table: state[table] for table in _TABLES}:
            raise SafetyError("REPORT_SCOPED_SUPPLIER_PROOF_INVALID")
        if scoped != product["supplier_scoped_terms"]:
            raise SafetyError("REPORT_SCOPED_SUPPLIER_FACTS_CHANGED")
        coverage = product["supplier_scope_coverage"]
        ids = sorted(_groups(scoped["all"]))
        if (set(coverage) != {"available_groups", "selected_groups", "omitted_groups", "selected_group_ids", "omitted_group_ids", "limit", "record_byte_limit"}
                or coverage["limit"] != MAX_GROUPS or coverage["record_byte_limit"] != MAX_RECORD_BYTES
                or coverage["selected_group_ids"] != ids or coverage["selected_groups"] != len(ids)
                or coverage["omitted_group_ids"] != sorted(set(coverage["omitted_group_ids"]))
                or set(ids) & set(coverage["omitted_group_ids"])
                or coverage["omitted_groups"] != len(coverage["omitted_group_ids"])
                or coverage["available_groups"] != len(ids) + coverage["omitted_groups"] or len(ids) > MAX_GROUPS):
            raise SafetyError("REPORT_SCOPED_SUPPLIER_COVERAGE_INVALID")
        groups, refs = _display(state, scoped)
        return {"scoped": scoped, "groups": groups, "source_refs": refs, "state": state, "coverage": coverage}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SafetyError("REPORT_SCOPED_SUPPLIER_PROOF_INVALID") from None
