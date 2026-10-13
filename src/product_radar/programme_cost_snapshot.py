"""Owner-only cost context over supplied verified inputs; no I/O or billing claims.

Project collection charges include unsuccessful acquisitions. Captured supporting
lineage is a narrower subtotal, never the denominator for a cheaper forecast.
Completed logical drafts, not allocations, editions or revisions, supply the
explicit shared-project average. The unfinished batch is not an exclusive bill.
"""

from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING
from pathlib import PurePosixPath
import re
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from .models import SafetyError, digest, identifier, timestamp, usd
from .research_costs import collection_costs, collection_cost_totals, yield_forecast
from .service import _validate_policy

VERSION = "programme-owner-cost-snapshot-v1"
BASELINE_VERSION = "historical-collection-baseline-v1"
MAX_ROWS = 100000
CAVEATS = (
    "The selected reference is minimum-known shared project collection expense, including failed acquisitions; unresolved read holds are not included as bills.",
    "The reference used the historical thirteen-point product policy. It does not measure the current ten-point policy or the new direct-source mix.",
    "Five percent and three percent are declared planning cases, not measured current useful-product yield.",
    "The calculation holds historical collection cost per candidate constant and rounds candidate needs up separately for each logical report.",
    "AI subscription or additional invoices, computer work, hosting and unresolved charges are separate; all-in cost remains unknown.",
    "A release calendar is not a provider billing-period reset, proof of current funding, approval or publication.",
)


def _unknown(reason):
    return {"state": "unknown", "reason": reason}


def _count(value, *, minimum=0, maximum=MAX_ROWS):
    if type(value) is not int or not minimum <= value <= maximum:
        raise SafetyError("COST_SNAPSHOT_COUNT_INVALID")
    return value


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise SafetyError("COST_SNAPSHOT_HASH_INVALID")
    return value


def _mapping(value):
    if not isinstance(value, dict) or len(value) > MAX_ROWS:
        raise SafetyError("COST_SNAPSHOT_MAPPING_INVALID")
    return value


def _ids(value):
    if not isinstance(value, list) or len(value) > MAX_ROWS:
        raise SafetyError("COST_SNAPSHOT_IDS_INVALID")
    return {identifier(item) for item in value}


def _part(function, *args):
    try:
        return function(*args)
    except (SafetyError, KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return _unknown("COST_SNAPSHOT_INPUTS_MISSING_OR_INCONSISTENT")


def _ledger(financial_state):
    if financial_state is None:
        raise SafetyError("COST_SNAPSHOT_LEDGER_NOT_SUPPLIED")
    if not isinstance(financial_state, (list, tuple)) or len(financial_state) != 2:
        raise SafetyError("COST_SNAPSHOT_LEDGER_INVALID")
    # This pair is supplied by a verified canonical Journal reader. Lifetime
    # history is not a bounded packet or one untrusted parser input; keeping
    # every original must not eventually turn valid accounting into unknown.
    costs = collection_cost_totals(financial_state)
    for name in ("known_actual_usd", "unsettled_hold_usd"):
        total = Decimal(costs[name])
        if not total.is_finite() or total < 0:
            raise SafetyError("COST_SNAPSHOT_TOTAL_INVALID")
    by_source = Counter()
    for stage, count in costs["acquisitions_by_source"].items():
        by_source[stage if stage in ("meta", "amazon", "trends") else "other"] += count
    return {"state": "known", "scope": "shared_project_collection_so_far",
        "physical_acquisitions": costs["physical_acquisitions"],
        "acquisitions_by_source": dict(sorted(by_source.items())),
        "known_incurred_usd": costs["known_actual_usd"],
        "unsettled_hold_usd": costs["unsettled_hold_usd"],
        "accounting_complete": costs["accounting_complete"],
        "all_in_cost_usd": None,
        "note": "All saved project collection attempts, including failed attempts, count once. Holds are not bills. This is not an exclusive programme or report invoice."}


def _manifest_path(value, issue_id, revision, project_root):
    relative = f"data/outputs/{issue_id}/revision-{revision}/delivery-manifest.json"
    if not isinstance(value, str) or len(value) > 4096 or "\\" in value or "\x00" in value:
        raise SafetyError("COST_SNAPSHOT_MANIFEST_PATH_INVALID")
    path = PurePosixPath(value)
    if value != str(path) or any(piece in (".", "..") for piece in path.parts):
        raise SafetyError("COST_SNAPSHOT_MANIFEST_PATH_INVALID")
    if value == relative:
        return relative
    if project_root is not None:
        root = PurePosixPath(str(project_root))
        if (not root.is_absolute() or str(project_root) != str(root)
                or ".." in root.parts or "\\" in str(root) or len(str(root)) > 4096):
            raise SafetyError("COST_SNAPSHOT_PROJECT_ROOT_INVALID")
        if value == str(root / relative):
            return relative
    raise SafetyError("COST_SNAPSHOT_MANIFEST_PATH_INVALID")


def _private_binding(record, issue_id, revision, package_hash, client_id):
    """A retained packet may have lost its path in the newer partial status."""
    for receipt in _mapping(record.get("private_staging_receipts", {})).values():
        if not isinstance(receipt, dict) or type(receipt.get("revision")) is not int or (receipt.get("issue_id"), receipt.get("revision"),
                receipt.get("package_hash"), receipt.get("client_id")) != (issue_id, revision, package_hash, client_id):
            continue
        if (receipt.get("state") != "private_bytes_verified" or receipt.get("index_unchanged") is not True
                or receipt.get("client_publication_attempted") is not False):
            continue
        _hash(receipt.get("manifest_hash"))
        files = receipt.get("files")
        if not isinstance(files, list) or not 4 <= len(files) <= 5:
            continue
        names = set()
        for row in files:
            if not isinstance(row, dict):
                raise SafetyError("COST_SNAPSHOT_RETAINED_BINDING_INVALID")
            name = row.get("name")
            if name not in {"shortlist.xlsx", "final_report_data.json", "delivery-manifest.json", "report-en.pdf", "report-hi-Latn.pdf"}:
                raise SafetyError("COST_SNAPSHOT_RETAINED_BINDING_INVALID")
            names.add(name); _hash(row.get("sha256")); _count(row.get("size"), minimum=1, maximum=100 * 1024 * 1024)
        outcomes = receipt.get("outcomes")
        if not isinstance(outcomes, list) or len(outcomes) != len(files):
            continue
        expected = {row["name"]: row for row in files}
        checked = set()
        for row in outcomes:
            if not isinstance(row, dict) or row.get("name") not in expected:
                raise SafetyError("COST_SNAPSHOT_RETAINED_BINDING_INVALID")
            item = expected[row["name"]]
            if (row.get("state") != "verified" or row.get("sha256") != item["sha256"]
                    or type(row.get("size")) is not int or row["size"] != item["size"]):
                raise SafetyError("COST_SNAPSHOT_RETAINED_BINDING_INVALID")
            checked.add(row["name"])
        if (len(names) == len(files) and checked == names
                and {"shortlist.xlsx", "final_report_data.json", "delivery-manifest.json"} <= names
                and names & {"report-en.pdf", "report-hi-Latn.pdf"}
                and _count(receipt.get("verified_file_count"), maximum=5) == len(files)):
            return True
    return False


def _generated(record, project_root):
    if record is None:
        return _unknown("COST_SNAPSHOT_PROGRAMME_NOT_SUPPLIED")
    slots = record["release_slots"]
    if not isinstance(slots, list) or len(slots) > 3660:
        raise SafetyError("COST_SNAPSHOT_RELEASE_SLOTS_INVALID")
    slot_ids = {identifier(row["issue_id"]) for row in slots}
    if len(slot_ids) != len(slots):
        raise SafetyError("COST_SNAPSHOT_RELEASE_SLOTS_INVALID")
    drafts = _mapping(record["drafts"])
    client_id = identifier(record["client_id"])
    for slot in slots:
        if slot.get("client_id", client_id) != client_id:
            raise SafetyError("COST_SNAPSHOT_RELEASE_SLOTS_INVALID")
        date.fromisoformat(slot["date"])
    issues, invalid, current, retained, states = [], 0, 0, 0, Counter()
    for issue_id, row in drafts.items():
        try:
            identifier(issue_id)
            if issue_id not in slot_ids or not isinstance(row, dict):
                raise SafetyError("COST_SNAPSHOT_GENERATED_SCOPE_INVALID")
            status = row.get("state", "waiting")
            if not isinstance(status, str) or not re.fullmatch(r"[a-z_]{1,80}", status):
                raise SafetyError("COST_SNAPSHOT_DRAFT_STATUS_INVALID")
            states[status] += 1
            for flag in ("complete", "retained_complete"):
                if flag in row and type(row[flag]) is not bool:
                    raise SafetyError("COST_SNAPSHOT_DRAFT_FLAG_INVALID")
            if row.get("complete") is not True and row.get("retained_complete") is not True:
                continue  # A proposed or partial issue is not a generated bill denominator.
            if row.get("issue_id") != issue_id or row.get("client_id", client_id) != client_id:
                raise SafetyError("COST_SNAPSHOT_GENERATED_BINDING_INVALID")
            complete = row.get("complete") is True
            protected_original = (not complete and "retained_revision" not in row
                                  and row.get("state") == "protected_issue")
            if protected_original:
                # The report's protected branch preserves its original complete
                # revision/path/hash, rather than creating a replacement. Reuse
                # only that exact metadata; never guess a future retained path.
                revision = _count(row.get("revision"), minimum=1)
                if row.get("retained_package_hash", row.get("package_hash")) != row.get("package_hash"):
                    raise SafetyError("COST_SNAPSHOT_GENERATED_BINDING_INVALID")
            else:
                revision = _count(row.get("revision") if complete else row.get("retained_revision"), minimum=1)
            package_hash = _hash(row.get("package_hash") if complete else row.get("retained_package_hash", row.get("package_hash")))
            try:
                path = _manifest_path(row.get("manifest_path"), issue_id, revision, project_root)
            except SafetyError:
                if complete or protected_original or not _private_binding(record, issue_id, revision, package_hash, client_id):
                    raise
                path = f"data/outputs/{issue_id}/revision-{revision}/delivery-manifest.json"
            issues.append({"issue_id": issue_id, "revision": revision, "package_hash": package_hash,
                "manifest_path": path, "basis": "current_complete" if complete else "retained_complete"})
            current += int(complete); retained += int(not complete)
        except (SafetyError, KeyError, TypeError, ValueError):
            invalid += 1
    return {"state": "partial" if invalid else "known", "logical_release_slots": len(slot_ids),
        "saved_draft_records": len(drafts), "current_draft_states": dict(sorted(states.items())),
        "current_complete_generated_issues": current, "retained_only_generated_issues": retained,
        "known_generated_logical_issues": len(issues),
        "complete_generated_logical_issues": None if invalid else len(issues),
        "invalid_or_unbound_draft_records": invalid, "issues": sorted(issues, key=lambda row: row["issue_id"]),
        "note": "One logical report counts once across languages and revisions. A retained finished packet is not a current-ready replacement. Allocations and partial files do not count."}


def generated_draft_summary(record, *, project_root=None):
    """Project-root-safe metadata projection; the caller has verified its record."""
    return _part(_generated, record, project_root)


def _supporting(record, state, financial_state, at):
    if record is None or state is None:
        return _unknown("COST_SNAPSHOT_CAPTURE_LINEAGE_NOT_SUPPLIED")
    scope = _ids(record["concept_ids"]) | _ids(record.get("reporting_inventory_concept_ids", []))
    from .inventory_intake import reporting_intake_concept_ids, current_intake_records
    scope |= reporting_intake_concept_ids(state, record)
    for child in _mapping(record.get("follow_on_reviews", {})).values():
        scope |= _ids(child["concept_ids"])
    semantics = {row["semantic_id"] for row in _mapping(state["associations"]).values()
                 if row.get("concept_id") in scope and row.get("status") == "resolved"}
    captures = _mapping(state["captures"])
    capture_ids = {row["capture_id"] for row in _mapping(state["observations"]).values()
                   if row.get("semantic_id") in semantics and timestamp(row["observed_at"]) <= at}
    for intake in current_intake_records(state, record):
        dependencies = intake["binding"].get("support_dependencies", {})
        for ref, expected in dependencies.get("observations", {}).items():
            row = state["observations"].get(ref)
            if row is not None and digest(row) == expected and timestamp(row["observed_at"]) <= at:
                capture_ids.add(row["capture_id"])
        for ref, expected in dependencies.get("captures", {}).items():
            if ref in captures and digest(captures[ref]) == expected:
                capture_ids.add(ref)
    ids = set()
    for capture_id in capture_ids:
        capture = captures[capture_id]
        if timestamp(capture["observed_at"]) > at:
            continue
        reference = capture.get("reference")
        if isinstance(reference, dict) and reference.get("attempt_id") is not None:
            ids.add(identifier(reference["attempt_id"]))
    result = {"state": "known", "paid_attempt_ids": sorted(ids), "physical_acquisitions": len(ids),
        "scope": "associated_saved_capture_lineage", "known_incurred_usd": None, "unsettled_hold_usd": None,
        "note": "Only original financial attempt IDs explicitly referenced by associated canonical captures count here. Dataset IDs are not financial attempt IDs. Failed or uncaptured project work remains in the full project total, not this narrower subtotal."}
    if financial_state is None:
        return dict(result, state="partial", reason="COST_SNAPSHOT_LINEAGE_LEDGER_NOT_SUPPLIED")
    _ledger(financial_state)
    if not ids <= set(financial_state[1]):
        return dict(result, state="partial", reason="COST_SNAPSHOT_LINEAGE_ATTEMPT_NOT_SAVED")
    costs = collection_costs(SimpleNamespace(state=lambda: financial_state), sorted(ids))
    return dict(result, known_incurred_usd=costs["known_actual_usd"], unsettled_hold_usd=costs["unsettled_hold_usd"],
                accounting_complete=costs["accounting_complete"])


def _baseline(wrapper, at):
    if wrapper is None:
        raise SafetyError("COST_SNAPSHOT_BASELINE_NOT_SUPPLIED")
    if (not isinstance(wrapper, dict) or set(wrapper) != {"schema_version", "selector", "baseline"}
            or type(wrapper["schema_version"]) is not int or wrapper["schema_version"] != 1):
        raise SafetyError("COST_SNAPSHOT_BASELINE_INVALID")
    value, selector = wrapper["baseline"], wrapper["selector"]
    fields = {"schema_version", "version", "baseline_id", "observed_at", "baseline_collection_usd",
              "baseline_candidates", "reference", "source_mix", "fit_policy_id"}
    if (not isinstance(value, dict) or set(value) != fields or type(value["schema_version"]) is not int
            or value["schema_version"] != 1 or value["version"] != BASELINE_VERSION
            or value["fit_policy_id"] != "fit-13-v1"):
        raise SafetyError("COST_SNAPSHOT_BASELINE_INVALID")
    identifier(value["baseline_id"])
    if (timestamp(value["observed_at"]) > at or usd(value["baseline_collection_usd"]) <= 0
            or not isinstance(value["baseline_collection_usd"], str)):
        raise SafetyError("COST_SNAPSHOT_BASELINE_INVALID")
    _count(value["baseline_candidates"], minimum=1)
    reference = value["reference"]
    if (not isinstance(reference, str) or not 1 <= len(reference) <= 512 or reference.startswith("/") or "\\" in reference
            or "://" in reference or any(ord(char) < 32 for char in reference)
            or ".." in PurePosixPath(reference.split("#")[0]).parts):
        raise SafetyError("COST_SNAPSHOT_BASELINE_REFERENCE_INVALID")
    if (not isinstance(value["source_mix"], list) or not value["source_mix"]
            or len(value["source_mix"]) != len(set(value["source_mix"]))
            or any(source not in ("meta", "amazon", "trends") for source in value["source_mix"])):
        raise SafetyError("COST_SNAPSHOT_BASELINE_INVALID")
    if (not isinstance(selector, dict) or set(selector) != {"baseline_id", "fingerprint"}
            or selector["baseline_id"] != value["baseline_id"] or _hash(selector["fingerprint"]) != digest(value)):
        raise SafetyError("COST_SNAPSHOT_BASELINE_SELECTOR_MISMATCH")
    return value, selector["fingerprint"]


def _forecast(wrapper, policy, at):
    if wrapper is None:
        return dict(_unknown("COST_SNAPSHOT_BASELINE_NOT_SUPPLIED"), assumed_yields=["0.05", "0.03"])
    try:
        value, fingerprint = _baseline(wrapper, at)
    except (SafetyError, KeyError, TypeError, ValueError, AttributeError):
        return dict(_unknown("COST_SNAPSHOT_BASELINE_INVALID_OR_UNBOUND"), assumed_yields=["0.05", "0.03"])
    if policy is None:
        return _unknown("COST_SNAPSHOT_RELEASE_POLICY_NOT_SUPPLIED")
    _validate_policy(policy)
    local_date = at.astimezone(ZoneInfo(policy["timezone"])).date()
    if not 2000 <= local_date.year <= 2100:
        raise SafetyError("COST_SNAPSHOT_CALENDAR_RANGE_INVALID")
    start = local_date.replace(day=1)
    end = date(start.year + int(start.month == 12), 1 if start.month == 12 else start.month + 1, 1)
    days = [start + timedelta(days=n) for n in range((end - start).days)]
    count = sum(day.weekday() in policy["publication_weekdays"] for day in days)
    inputs = {"baseline_collection_usd": value["baseline_collection_usd"],
        "baseline_candidates": value["baseline_candidates"],
        "new_products_per_report": policy["minimum_products"], "report_count": count}
    forecast = yield_forecast(**inputs, yields=("0.05", "0.03"))
    return dict(forecast, state="known", inputs=inputs, baseline_id=value["baseline_id"],
        baseline_fingerprint=fingerprint, baseline_reference=value["reference"],
        baseline_observed_at=value["observed_at"], baseline_source_mix=list(value["source_mix"]),
        baseline_fit_policy_id=value["fit_policy_id"], assumed_yields=["0.05", "0.03"],
        month=start.strftime("%Y-%m"), calendar_start=start.isoformat(), calendar_end_exclusive=end.isoformat(),
        calendar_timezone=policy["timezone"], calendar_issue_count=count,
        new_products_basis="minimum_10_all_new_planning_case_not_actual_allocation",
        allowance_fit_not_inferred=True, caveats=list(CAVEATS))


def build_owner_cost_snapshot(record, state, financial_state, *, baseline=None, policy=None,
                              account_status=None, as_of=None, project_root=None, approved_accounts=3,
                              report_pool=None, model_effort=None):
    """Use already-verified arguments only; no keys, scans, locks or remote calls."""
    raw_time = as_of or (record.get("updated_at", record.get("created_at")) if isinstance(record, dict) else None)
    try:
        at = timestamp(raw_time)
    except SafetyError:
        at = None
    ledger = _part(_ledger, financial_state)
    generated = generated_draft_summary(record, project_root=project_root)
    supporting = (_part(_supporting, record, state, financial_state, at) if at is not None
                  else _unknown("COST_SNAPSHOT_TIME_NOT_SUPPLIED"))
    forecast = (_part(_forecast, baseline, policy, at) if at is not None
                else _unknown("COST_SNAPSHOT_TIME_NOT_SUPPLIED"))
    from .research_cohort import current_research_cohort, current_cohort_forecast
    cohort = (_part(lambda: current_research_cohort(record, state, financial_state, report_pool, as_of=raw_time, model_effort=model_effort))
              if at is not None else _unknown("COST_SNAPSHOT_TIME_NOT_SUPPLIED"))
    current_forecast = (_part(lambda: current_cohort_forecast(cohort, policy, as_of=raw_time))
                        if at is not None and policy is not None else _unknown("COST_SNAPSHOT_RELEASE_POLICY_NOT_SUPPLIED"))
    average = {"state": "unknown", "reason": "COST_SNAPSHOT_COMPLETE_GENERATED_ISSUES_NOT_KNOWN",
        "scope": "shared_project_collection_per_completed_logical_draft_so_far", "known_project_collection_usd": None,
        "completed_generated_logical_issues": generated.get("complete_generated_logical_issues"),
        "known_shared_average_usd": None, "exclusive_per_issue_cash_cost_usd": None,
        "all_in_per_issue_cost_usd": None, "unfinished_batch": True,
        "note": "Known shared project research spending so far divided by distinct completed generated logical drafts. This includes shared and failed research, is a minimum-known average of an unfinished batch, and is not an exclusive report bill, marginal price or forecasting unit cost."}
    if ledger.get("state") == "known":
        average["known_project_collection_usd"] = ledger["known_incurred_usd"]
        average["unsettled_hold_usd"] = ledger["unsettled_hold_usd"]
        average["accounting_complete"] = ledger["accounting_complete"]
        count = generated.get("complete_generated_logical_issues")
        if type(count) is int and count > 0:
            amount = Decimal(ledger["known_incurred_usd"]) / count
            average.update(state="known", reason=None, known_shared_average_usd=format(
                amount.quantize(Decimal("0.00000001"), rounding=ROUND_CEILING), "f"))
        elif count == 0:
            average["reason"] = "COST_SNAPSHOT_NO_COMPLETE_GENERATED_ISSUES"
    warnings = []
    if ledger.get("state") != "known":
        warnings.append({"code": "PROJECT_COLLECTION_ACCOUNTING_UNKNOWN"})
    elif not ledger["accounting_complete"]:
        warnings.append({"code": "PROJECT_COLLECTION_CHARGES_UNSETTLED", "hold_usd": ledger["unsettled_hold_usd"]})
    if generated.get("state") != "known":
        warnings.append({"code": "COMPLETED_GENERATED_REPORT_COUNT_UNCERTAIN"})
    if forecast.get("state") != "known":
        warnings.append({"code": "CONDITIONAL_MONTHLY_FORECAST_UNKNOWN"})
    warnings.append({"code": "ALL_IN_RESEARCH_COST_UNKNOWN"})
    try:
        approved = _count(approved_accounts, minimum=1, maximum=100)
        configured = _count(account_status["configured_financial_accounts"], maximum=100)
        rows = account_status["accounts"]
        if not isinstance(rows, list) or len(rows) > 100:
            raise SafetyError("COST_SNAPSHOT_ACCOUNT_STATUS_INVALID")
        funded = sum(row.get("enabled") is True and row.get("state") == "saved_account"
                     and usd(row["opening_usd"]) > 0 for row in rows)
        funding = {"state": "known", "approved_accounts": approved, "configured_accounts": configured,
                   "saved_funded_accounts": funded, "currently_verified_funded_accounts": None}
        if configured < approved or funded < approved:
            warnings.append({"code": "APPROVED_ACCOUNT_POOL_NOT_FULLY_FUNDED", "approved_accounts": approved,
                "configured_accounts": configured, "saved_funded_accounts": funded})
    except (SafetyError, TypeError, KeyError, AttributeError):
        funding = _unknown("COST_SNAPSHOT_ACCOUNT_STATUS_NOT_KNOWN")
    warnings.append({"code": "CURRENT_PROVIDER_FUNDING_NOT_VERIFIED"})
    return {"schema_version": 1, "version": VERSION, "owner_only": True,
        "as_of": raw_time if at is not None else None, "shared_project_collection": ledger,
        "generated_reports": generated, "supporting_paid_capture_lineage": supporting,
        "shared_project_cost_average": average, "conditional_forecast": forecast,
        "current_research_cohort": cohort, "current_forecast": current_forecast,
        "historical_forecast_is_not_current_source_cost": True,
        "funding": funding, "warnings": warnings, "per_issue_cash_cost_usd": None,
        "client_publication_attempted": False}
