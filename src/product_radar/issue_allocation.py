"""Fair internal draft allocation and durable holds; never uploads or publishes.

The eligible pool is supplied by upstream research. Count validation here does
not certify supplier terms, page layout or owner approval. Publication history
is recorded only from a verified release-index receipt supplied by the caller.
"""

from copy import deepcopy
from datetime import date
import re

from .models import SafetyError, digest, identifier, timestamp, utc_now
from .material_updates import update_baseline_current
from .research_memory import change, key
from .service import CATEGORIES, _validate_policy, load_policy, validate_issue, product_fit, policy_for_fit, issue_fit_policy
from .storage import Journal
from .opportunity_score import (OPPORTUNITY_FIELDS, OPPORTUNITY_POLICY_IDS, POLICY_ID as SCORE_POLICY_ID, crowding_level,
                                load_settings as load_score_settings, opportunity_sort_key, validate_opportunity)
from .report_examples import RECOMMENDATION_CONTRACT, FUNCTIONAL_REPORT_FIELDS, validate_functional_examples

ACTIVE_STATES = {"reserved", "uploaded", "published"}


def selection_fingerprint(issue):
    """Legacy selection bytes stay exact; new packets also bind owned assets."""
    fields = ("issue_id", "client_id", "revision", "date", "products", "evidence_fingerprint")
    selected = {field: issue[field] for field in fields}
    marker = issue.get("recommendation_contract")
    if marker is not None:
        if marker != RECOMMENDATION_CONTRACT:
            raise SafetyError("ISSUE_RECOMMENDATION_CONTRACT_INVALID")
        selected["recommendation_contract"] = marker
        if "report_asset_receipt" in issue:
            selected["report_asset_receipt"] = issue["report_asset_receipt"]
    elif "report_asset_receipt" in issue:
        raise SafetyError("ISSUE_RECOMMENDATION_CONTRACT_INVALID")
    if "identity_context_hash" in issue:
        from .functional_aliases import _HASH
        if not isinstance(issue["identity_context_hash"], str) or not _HASH.fullmatch(issue["identity_context_hash"]):
            raise SafetyError("ISSUE_IDENTITY_CONTEXT_INVALID")
        selected["identity_context_hash"] = issue["identity_context_hash"]
    return digest(selected)


def _crowding_current(state, product):
    """A score-marked frozen type's crowding answer still holds in ``state``
    (level, rule, thresholds version and meaning only; ``crowding_unchanged``)."""
    if product.get("opportunity_policy_id") != SCORE_POLICY_ID:
        return True
    from .crowding import crowding_unchanged
    try:
        facts = product["opportunity_basis"]["parts"]["room"]["items"]["crowding"]["facts"]
        return crowding_unchanged(state, product["concept_id"], facts, product["functional_definition"]["meaning_hash"])
    except (KeyError, TypeError):
        return False


def _fresh_at_release(product, issue, *, as_of=None, state=None):
    """Support still valid on the release morning; with ``state``, also the
    type's crowding answer (a type that turned High is not released as Low)."""
    if state is not None and product.get("recommendation_contract") == RECOMMENDATION_CONTRACT and not _crowding_current(state, product):
        return False
    if product.get("recommendation_contract") == RECOMMENDATION_CONTRACT:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from .market_validation import validate_score_support
        release = datetime.combine(date.fromisoformat(issue["date"]), datetime.min.time(),
                                   tzinfo=ZoneInfo("Asia/Kolkata")).replace(hour=9)
        if as_of is not None:
            release = max(release, timestamp(as_of))
        try:
            validate_score_support(product["score_support"], opportunity=product,
                meaning_hash=product["functional_definition"]["meaning_hash"], as_of=release.isoformat())
        except (SafetyError, KeyError, TypeError):
            return False
        return True
    return not product.get("usable_until") or product["usable_until"][:10] >= issue["date"]


def _records(values):
    if isinstance(values, dict):
        return list(values.values())
    if isinstance(values, list):
        return values
    raise SafetyError("ISSUE_RECORDS_INVALID")


def allocation_snapshot_hash(state):
    value = {name: state.get(name, {}) for name in ("issue_reservations", "published_history")}
    from .functional_aliases import alias_binding
    binding = alias_binding(state)
    if binding is not None: value["identity_context_hash"] = binding
    return digest(value)


def _dates(values):
    if not isinstance(values, list) or not values:
        raise SafetyError("ISSUE_DATES_INVALID")
    output, seen = [], set()
    for item in values:
        value = {"date": item, "issue_id": "issue-" + item, "revision": 1, "client_id": "default"} if isinstance(item, str) else deepcopy(item)
        try:
            parsed = date.fromisoformat(value["date"])
            issue_id = identifier(value["issue_id"])
            identifier(value.get("client_id", "default"))
            revision = value.get("revision", 1)
            if type(revision) is not int or revision < 1 or issue_id in seen:
                raise SafetyError("ISSUE_DATES_INVALID")
            value.update(revision=revision, client_id=value.get("client_id", "default"))
            seen.add(issue_id)
            output.append(value)
        except (KeyError, TypeError, ValueError):
            raise SafetyError("ISSUE_DATES_INVALID") from None
    output.sort(key=lambda value: (value["date"], value["issue_id"]))
    return output


def _published(history, client_id="default", exclude_issue_id=None, *, aliases=None):
    values = {identifier(record["concept_id"]) for record in _records(history) if record.get("status") == "published"
              and record.get("client_id", "default") == client_id and record.get("issue_id") != exclude_issue_id}
    if aliases:
        from .functional_aliases import expand_identities
        values = expand_identities(values, aliases)
        # Only parent exposure flows downwards. Child exposure does not mark
        # its parent or a disjoint sibling seen.
        values.update(cid for cid, row in aliases.items() if set(row.get("ancestor_ids", [])) & values)
    return values


def _held(reservations, *, own_issue_id=None, client_id="default", aliases=None, history=None):
    exposed = {(r.get("issue_id"), r.get("concept_id")) for r in _records(history)
               if r.get("status") == "published" and r.get("client_id", "default") == client_id} if history is not None else set()
    values = {identifier(product["concept_id"]) for record in _records(reservations)
            if record.get("state") in ACTIVE_STATES and record.get("client_id", "default") == client_id
            and record.get("issue_id") != own_issue_id for product in record.get("products", [])
            if record.get("state") != "published" or (record.get("issue_id"), product.get("concept_id")) not in exposed}
    if aliases:
        from .functional_aliases import expand_identities
        values = expand_identities(values, aliases)
        from .functional_lineage import hold_overlap
        originals = set(values)
        values.update(cid for cid in aliases if any(held in aliases and hold_overlap(cid, held, aliases)
                      for held in originals))
    return values


def draft_ordinal(issue_id, issue_date, history, reservations, *, client_id="default", planned=()):
    """Distinct logical issues count; revisions, translations and downloads do not.

    ``planned`` must contain only actually produced sets (ready_for_review or
    later). An unfilled or missed release date is not a set: it must not take
    one of the first two bilingual positions from a set that is produced.

    A held (reserved/uploaded) set keeps the position saved in its hold; a
    new set is numbered after every held/published position, in date order
    among the new sets. A later or back-filled allocation therefore cannot
    move an existing set, so at most two sets are ever bilingual. Holds saved
    without a position keep the original date-ordered rule.
    """
    published = {record["issue_id"] for record in _records(history)
                 if record.get("status") == "published" and record.get("client_id", "default") == client_id}
    if issue_id in published:
        own = [record for record in _records(history) if record.get("issue_id") == issue_id]
        known = [record.get("ordinal") for record in own if type(record.get("ordinal")) is int]
        return min(known) if known else len(published)
    held = {record["issue_id"]: record for record in _records(reservations)
            if record.get("state") in ACTIVE_STATES and record.get("client_id", "default") == client_id
            and record["issue_id"] not in published}
    positions = {ref: row.get("projected_ordinal") for ref, row in held.items()}
    if all(type(value) is int and value >= 1 for value in positions.values()):
        if issue_id in held:
            return positions[issue_id]
        published_positions = [record["ordinal"] for record in _records(history)
                               if record.get("status") == "published" and record.get("client_id", "default") == client_id
                               and type(record.get("ordinal")) is int]
        base = max([len(published), *published_positions, *positions.values()])
        new = {record["issue_id"]: record["date"] for record in planned
               if record.get("client_id", "default") == client_id
               and record["issue_id"] not in published and record["issue_id"] not in held}
        new[issue_id] = issue_date
        return base + sorted(new, key=lambda value: (new[value], value)).index(issue_id) + 1
    queue = {ref: record["date"] for ref, record in held.items()}
    for record in planned:
        if record.get("client_id", "default") == client_id and record["issue_id"] not in published:
            queue[record["issue_id"]] = record["date"]
    queue[issue_id] = issue_date
    return len(published) + sorted(queue, key=lambda value: (queue[value], value)).index(issue_id) + 1


def _fingerprint(products):
    return digest([{ "concept_id": product["concept_id"],
                    "evidence": product.get("evidence_fingerprint", product.get("evidence_refs", [])),
                    "kpi_states": product["kpi_states"], "status": product["status"],
                    "update_reason": product.get("update_reason"),
                    **({field: product[field] for field in ("fit_policy_id", "criterion_ids", "kpi_total", "kpi_matches", "fit_tier")}
                       if product.get("fit_policy_id") == "fit-10-v1" else {}),
                    **({field: deepcopy(product[field]) for field in OPPORTUNITY_FIELDS}
                       if any(field in product for field in OPPORTUNITY_FIELDS) else {}),
                    **({field: deepcopy(product[field]) for field in FUNCTIONAL_REPORT_FIELDS if field in product}
                       if product.get("recommendation_contract") == RECOMMENDATION_CONTRACT else {}),
                    **({"display_asset": deepcopy(product["display_asset"])} if "display_asset" in product else {})}
                   for product in products])


def allocate_issues(eligible, issue_dates, history, reservations, policy=None, *, identity_state=None):
    explicit_policy = policy is not None
    policy = load_policy() if policy is None else _validate_policy(policy)
    issues = _dates(issue_dates)
    if len({issue["client_id"] for issue in issues}) != 1:
        raise SafetyError("ISSUE_BATCH_CLIENTS_DIFFER")
    if not isinstance(eligible, list):
        raise SafetyError("ISSUE_POOL_INVALID")
    expected_policy = policy.get("fit_policy_id", "fit-13-v1") if explicit_policy else (
        "fit-10-v1" if any(isinstance(product, dict) and product.get("fit_policy_id") == "fit-10-v1" for product in eligible)
        else "fit-13-v1")
    from .functional_aliases import alias_binding, alias_projection, expand_identities, selected_alias_binding
    aliases = alias_projection(identity_state) if identity_state is not None and alias_binding(identity_state) is not None else {}
    published = _published(history, issues[0]["client_id"], aliases=aliases)
    own_published = {record["concept_id"] for record in _records(history) if record.get("status") == "published"
                     and record.get("client_id", "default") == issues[0]["client_id"]
                     and record.get("issue_id") in {issue["issue_id"] for issue in issues}}
    own_published = expand_identities(own_published, aliases)
    own_published.update(cid for cid, row in aliases.items() if set(row.get("ancestor_ids", [])) & own_published)
    pool, decisions, seen = [], [], set()
    for original in sorted(eligible, key=digest):
        if not isinstance(original, dict):
            decisions.append({"concept_id": "invalid-candidate-" + digest(original)[:24], "decision": "excluded",
                "reason": "Saved candidate is not a product record; healthy products can continue.", "diagnostic_code": "ISSUE_POOL_INVALID"})
            continue
        product = deepcopy(original)
        try:
            concept_id = identifier(product.get("concept_id"))
            if product.get("usable_until") is not None:
                date.fromisoformat(product["usable_until"][:10])
        except (SafetyError, TypeError, ValueError) as error:
            decisions.append({"concept_id": "invalid-candidate-" + digest(original)[:24], "decision": "excluded",
                "reason": "Saved candidate identity or date needs correction; healthy products can continue.",
                "diagnostic_code": str(error) if isinstance(error, SafetyError) else "ISSUE_EVIDENCE_DATE_INVALID"})
            continue
        reason = None
        if concept_id in seen:
            reason = "Duplicate canonical concept in the pool."
        seen.add(concept_id)
        if product.get("identity_reviewed") is not True or product.get("identity_conflicted") is True:
            reason = "Identity is pending or conflicted; unrelated products can continue."
        elif product.get("category") not in CATEGORIES:
            reason = "Product category is outside the client categories."
        try:
            fit = product_fit(product, expected_policy)
            validate_opportunity(product, matches=fit["kpi_matches"])
            if "recommendation_contract" in product:
                if product["recommendation_contract"] != RECOMMENDATION_CONTRACT:
                    raise SafetyError("ISSUE_RECOMMENDATION_CONTRACT_INVALID")
                validate_functional_examples(product, concept_id=concept_id)
            if not fit["eligible"]:
                reason = "The reviewed fit points are below the inclusion threshold."
        except SafetyError:
            reason = "The reviewed checklist states or declared fit policy are invalid."
        product["status"] = "Updated" if concept_id in published else "New"
        if product["status"] == "Updated" and concept_id not in own_published and (product.get("material_update_reviewed") is not True
                or not isinstance(product.get("update_reason"), str) or not product["update_reason"].strip()):
            reason = "Previously published product has no supported useful update."
        if product["status"] == "Updated" and not update_baseline_current(product, history, reservations, issues[0]["client_id"],
                member_ids=aliases.get(concept_id, {}).get("member_ids")):
            reason = "The useful-update comparison needs review against the client's latest published facts."
        if product.get("eligible") is False:
            reason = "Upstream research has not made this product eligible."
        decisions.append({"concept_id": concept_id, "decision": "excluded" if reason else "available", "reason": reason or "Reviewed upstream candidate."})
        if not reason:
            pool.append(product)
    if aliases:
        # Do not let a weaker/unfinished alias hide a stronger usable member
        # merely because its record hash sorted first. Evidence stays local to
        # the chosen member; only the duplicate-prevention key is shared.
        used, selected = set(), []
        for product in sorted(pool, key=lambda p: (*opportunity_sort_key(p), p["concept_id"])):
            cid = product["concept_id"]
            identity = aliases.get(cid, {}).get("canonical_id", cid)
            if identity in used:
                for decision in decisions:
                    if decision["concept_id"] == cid and decision["decision"] == "available":
                        decision.update(decision="excluded", reason="Another usable member of the same reviewed functional type is preferred.")
            else:
                used.add(identity); selected.append(product)
        pool = selected
    pool.sort(key=lambda product: (product["status"] != "New", product["concept_id"]))
    allocations = {issue["issue_id"]: [] for issue in issues}
    assigned = set()
    def assigned_identities(cid):
        from .functional_lineage import hold_overlap
        related = expand_identities({cid}, aliases)
        if cid in aliases:
            related.update(other for other in aliases if hold_overlap(cid, other, aliases))
        return related
    # Keep enough new products for later complete minima when useful updates
    # exist. For example, 24 New + 6 Updated must become three 8+2 issues.
    available = [product for product in pool if any(product["concept_id"] not in _held(reservations, own_issue_id=issue["issue_id"], client_id=issue["client_id"], aliases=aliases, history=history)
                 and _fresh_at_release(product, issue) for issue in issues)]
    new_count = sum(product["status"] == "New" for product in available)
    minimum = policy["minimum_products"]
    complete_count = min(len(issues), len(available) // minimum, new_count // (minimum - minimum // 5))
    intended_new = min(new_count, complete_count * minimum)
    new_targets = {issue["issue_id"]: minimum for issue in issues}
    if complete_count:
        share, extra = divmod(intended_new, complete_count)
        for index, issue in enumerate(issues[:complete_count]):
            new_targets[issue["issue_id"]] = share + (index < extra)

    packet_cohorts = {}
    mixed_packets = len({product.get("recommendation_contract") for product in pool}) > 1

    def choose_packet_cohort(issue):
        # A short new-format cohort must not consume an issue's first row and
        # strand a complete legacy cohort, or vice versa. Never mix their facts
        # to make ten. Keep legacy-only allocation byte-for-byte compatible.
        groups = {}
        held = _held(reservations, own_issue_id=issue["issue_id"], client_id=issue["client_id"], aliases=aliases, history=history)
        for product in pool:
            if product["concept_id"] not in assigned and product["concept_id"] not in held and _fresh_at_release(product, issue):
                groups.setdefault(product.get("recommendation_contract"), []).append(product)
        options = []
        required_new = minimum - minimum // 5
        for marker, group in groups.items():
            new = sum(product["status"] == "New" for product in group)
            capacity = min(len(group) // minimum, new // required_new)
            options.append((not bool(capacity), min(opportunity_sort_key(product) for product in group),
                            -min(len(group), minimum), str(marker), marker, new, capacity))
        if options:
            _, _, _, _, marker, new, capacity = min(options, key=lambda row: row[:4])
            packet_cohorts[issue["issue_id"]] = marker
            if capacity:
                remaining_slots = len(issues) - issues.index(issue)
                capacity = min(capacity, remaining_slots)
                new_targets[issue["issue_id"]] = min(minimum, new - (capacity - 1) * required_new)

    high_only_below = load_score_settings()["selection"]["high_crowding_only_below"]

    def high_crowding(product):
        return product.get("opportunity_policy_id") == SCORE_POLICY_ID and crowding_level(product) == "high"

    def choose(issue):
        products = allocations[issue["issue_id"]]
        held = _held(reservations, own_issue_id=issue["issue_id"], client_id=issue["client_id"], aliases=aliases, history=history)
        own_held = {identifier(product["concept_id"]) for record in _records(reservations)
                    if record.get("state") in ACTIVE_STATES
                    and record.get("client_id", "default") == issue["client_id"]
                    and record.get("issue_id") == issue["issue_id"]
                    for product in record.get("products", [])}
        own_held = expand_identities(own_held, aliases)
        published_elsewhere = _published(history, issue["client_id"], exclude_issue_id=issue["issue_id"], aliases=aliases)
        # Owner rule (A2): High-crowding types fill an issue only while fewer
        # than ten Low/Medium types are ready for it: already in it, or ones it
        # can still actually take (New, plus Updated within the update cap).
        ready_new = ready_updated = 0
        allowed = []
        for candidate in pool:
            product = dict(candidate, status="Updated" if candidate["concept_id"] in published_elsewhere else "New")
            if mixed_packets and product.get("recommendation_contract") != packet_cohorts.get(issue["issue_id"]):
                continue
            if product["concept_id"] in assigned or product["concept_id"] in held:
                continue
            if product["status"] == "Updated" and (product.get("material_update_reviewed") is not True
                    or not isinstance(product.get("update_reason"), str) or not product["update_reason"].strip()):
                continue
            if not _fresh_at_release(product, issue):
                continue
            if products and product.get("recommendation_contract") != products[0].get("recommendation_contract"):
                continue
            if not high_crowding(product):
                ready_new += product["status"] == "New"; ready_updated += product["status"] == "Updated"
            updates = sum(value["status"] == "Updated" for value in products)
            # While filling ten, at most two supported updates can be reserved.
            # Final count rounding is validated again before extras/commit.
            cap = policy["minimum_products"] // 5 if len(products) < policy["minimum_products"] else (len(products) + 1) // 5
            if product["status"] == "Updated" and updates + 1 > cap:
                continue
            counts = {category: sum(value["category"] == category for value in products) for category in CATEGORIES}
            balance = counts[product["category"]] >= 2
            freshness = product.get("usable_until") or "9999-12-31"
            need_new = sum(value["status"] == "New" for value in products) < new_targets[issue["issue_id"]]
            preferred_status = "New" if need_new or len(products) >= minimum else "Updated"
            allowed.append((product["status"] != preferred_status,
                            # Keep valid own-held stock before spending shared
                            # stock on the same New/Updated choice. Otherwise
                            # an unused own hold is stranded for later issues
                            # and can return as an earlier issue's extra.
                            product["concept_id"] not in own_held,
                            *opportunity_sort_key(product),
                            balance, counts[product["category"]], freshness, product["concept_id"], product))
        updates = sum(value["status"] == "Updated" for value in products)
        cap = policy["minimum_products"] // 5 if len(products) < policy["minimum_products"] else (len(products) + 1) // 5
        ready = (sum(not high_crowding(value) for value in products) + ready_new
                 + min(ready_updated, max(0, cap - updates)))
        if ready >= high_only_below:
            allowed = [value for value in allowed if not high_crowding(value[-1])]
        return min(allowed, key=lambda value: value[:-1])[-1] if allowed else None

    # Earlier issues get a complete minimum before a partial later issue.
    for issue in issues:
        if mixed_packets:
            choose_packet_cohort(issue)
        while len(allocations[issue["issue_id"]]) < policy["minimum_products"]:
            product = choose(issue)
            if product is None:
                break
            allocations[issue["issue_id"]].append(product)
            assigned.update(assigned_identities(product["concept_id"]))
    # Extras are balanced across complete issues, never used to hide a shortfall.
    complete = [issue for issue in issues if len(allocations[issue["issue_id"]]) >= policy["minimum_products"]]
    while complete:
        progress = False
        for issue in sorted(complete, key=lambda value: (len(allocations[value["issue_id"]]), value["date"], value["issue_id"])):
            if len(allocations[issue["issue_id"]]) >= policy["target_products"]:
                continue
            product = choose(issue)
            if product is None:
                continue
            allocations[issue["issue_id"]].append(product)
            assigned.update(assigned_identities(product["concept_id"]))
            progress = True
        if not progress:
            break
    input_value = {"eligible": sorted(deepcopy(eligible), key=digest), "issue_dates": issues,
                         "history": sorted(deepcopy(_records(history)), key=digest),
                         "reservations": sorted(deepcopy(_records(reservations)), key=digest), "policy": policy}
    if aliases: input_value["identity_context_hash"] = alias_binding(identity_state)
    input_hash = digest(input_value)
    output = []
    # Only complete sets occupy language positions. A short (unfilled) date is
    # not produced and must not push a produced set past the bilingual trial.
    produced = [issue for issue in issues if len(allocations[issue["issue_id"]]) >= policy["minimum_products"]]
    for issue in issues:
        products = allocations[issue["issue_id"]]
        ready = len(products) >= policy["minimum_products"]
        counts = validate_issue(products, _published(history, issue["client_id"], exclude_issue_id=issue["issue_id"], aliases=aliases), _held(reservations, own_issue_id=issue["issue_id"], client_id=issue["client_id"], aliases=aliases, history=history), policy if explicit_policy else policy_for_fit(expected_policy)) if ready else {
            "products": len(products), "new": sum(value["status"] == "New" for value in products), "updates": sum(value["status"] == "Updated" for value in products)}
        ordinal = draft_ordinal(issue["issue_id"], issue["date"], history, reservations, client_id=issue["client_id"], planned=produced)
        frozen = dict(issue, products=products, counts=counts, input_hash=input_hash, ready_for_review=ready,
                      shortfall=max(0, policy["minimum_products"] - len(products)), evidence_fingerprint=_fingerprint(products),
                      projected_ordinal=ordinal, language_state="bilingual_trial" if ordinal <= 2 else "language_pending" if policy["ongoing_pdf_language"] is None else "selected_language")
        if products and all(product.get("fit_policy_id") == "fit-10-v1" for product in products):
            frozen.update(fit_policy_id="fit-10-v1", criterion_ids=deepcopy(products[0]["criterion_ids"]), kpi_total=10)
        priorities = {product.get("opportunity_policy_id") for product in products}
        if products and len(priorities) == 1 and next(iter(priorities)) in OPPORTUNITY_POLICY_IDS:
            frozen["opportunity_policy_id"] = next(iter(priorities))
        if products and all(product.get("recommendation_contract") == RECOMMENDATION_CONTRACT for product in products):
            frozen["recommendation_contract"] = RECOMMENDATION_CONTRACT
        if aliases:
            binding = selected_alias_binding(identity_state, [p["concept_id"] for p in products], aliases=aliases)
            if binding is not None: frozen["identity_context_hash"] = binding
        frozen["selection_hash"] = selection_fingerprint(frozen)
        frozen["client_ready"] = False
        output.append(frozen)
    for decision in decisions:
        if decision["decision"] == "available":
            decision.update(decision="allocated" if decision["concept_id"] in assigned else "deferred",
                            reason="Assigned once to this batch." if decision["concept_id"] in assigned else "Reserved elsewhere, stale at release, or outside this finite issue capacity.")
    decisions.sort(key=lambda value: (value["concept_id"], value["decision"], value["reason"]))
    return {"schema_version": 1, "input_hash": input_hash, "issues": output, "decisions": decisions}


def _receipt(receipt, issue, *, as_of):
    if not isinstance(receipt, dict) or receipt.get("verified") is not True or receipt.get("authority") != "release_index":
        raise SafetyError("ISSUE_REMOTE_CONFIRMATION_REQUIRED")
    for field in ("issue_id", "revision", "selection_hash"):
        if receipt.get(field) != issue.get(field):
            raise SafetyError("ISSUE_REMOTE_REVISION_MISMATCH")
    identifier(receipt.get("receipt_id"))
    try:
        if timestamp(receipt["server_observed_at"]) > timestamp(as_of):
            raise SafetyError("ISSUE_REMOTE_RECEIPT_FROM_FUTURE")
    except KeyError:
        raise SafetyError("ISSUE_PUBLICATION_RECEIPT_INVALID") from None
    return receipt


def _availability(receipt):
    try:
        activation, scheduled, observed = (timestamp(receipt[field]) for field in ("server_activated_at", "scheduled_at", "server_observed_at"))
        deactivated = timestamp(receipt["server_deactivated_at"]) if receipt.get("server_deactivated_at") else None
        if (receipt.get("remote_state") not in ("active", "withdrawn")
                or not isinstance(receipt.get("package_hash"), str) or not re.fullmatch(r"[0-9a-f]{64}", receipt["package_hash"])
                or (receipt["remote_state"] == "withdrawn" and (deactivated is None or deactivated > observed))
                or (receipt["remote_state"] == "active" and deactivated is not None)):
            raise SafetyError("ISSUE_PUBLICATION_RECEIPT_INVALID")
        available_at = max(activation, scheduled)
        return available_at if available_at <= observed and (deactivated is None or deactivated > available_at) else None
    except KeyError:
        raise SafetyError("ISSUE_PUBLICATION_RECEIPT_INVALID") from None


def _publication_changes(state, issue, receipt, available_at):
    changes = []
    if available_at is None:
        return changes
    for product in issue["products"]:
        record_id = key("published", [issue["client_id"], issue["issue_id"], issue["revision"], product["concept_id"]])
        if record_id in state["published_history"]:
            continue
        record = {"schema_version": 1, "status": "published", "client_id": issue["client_id"], "issue_id": issue["issue_id"],
                  "concept_id": product["concept_id"], "revision": issue["revision"], "date": issue["date"],
                  "ordinal": issue["projected_ordinal"], "available_at": available_at.isoformat(),
                  "evidence_fingerprint": issue["evidence_fingerprint"], "activation_receipt": deepcopy(receipt)}
        from .material_updates import publication_baseline
        baseline = publication_baseline(issue, product, receipt)
        if baseline is not None:
            record["publication_baseline"] = baseline
        changes.append(change("published_history", record_id, record))
    return changes


def reserve_issue(memory, issue, *, expected_input_hash, expected_snapshot_hash, remote_confirmation=None, as_of=None,
                  report_root=None):
    """Commit a checked draft and its holds together, or leave the old draft intact."""
    issue_fit_policy(issue)
    with Journal(memory.root).locked():
        state = memory.snapshot()
        if allocation_snapshot_hash(state) != expected_snapshot_hash or issue.get("input_hash") != expected_input_hash:
            raise SafetyError("ISSUE_ALLOCATION_INPUT_CHANGED")
        issue_id = identifier(issue["issue_id"])
        from .functional_aliases import alias_binding, alias_projection, selected_alias_binding
        aliases = alias_projection(state) if alias_binding(state) is not None else {}
        if issue.get("identity_context_hash") != selected_alias_binding(state,
                [p["concept_id"] for p in issue["products"]], aliases=aliases):
            raise SafetyError("ISSUE_IDENTITY_CONTEXT_CHANGED")
        if len({aliases.get(p["concept_id"], {}).get("canonical_id", p["concept_id"])
                for p in issue["products"]}) != len(issue["products"]):
            raise SafetyError("ISSUE_DUPLICATE_FUNCTIONAL_IDENTITY")
        from .functional_lineage import hold_overlap
        selected_ids = [p["concept_id"] for p in issue["products"]]
        if aliases and any(hold_overlap(left, right, aliases)
               for offset, left in enumerate(selected_ids) for right in selected_ids[offset + 1:]):
            raise SafetyError("ISSUE_DUPLICATE_FUNCTIONAL_IDENTITY")
        old = state["issue_reservations"].get(issue_id)
        if issue.get("evidence_fingerprint") != _fingerprint(issue["products"]):
            raise SafetyError("ISSUE_FROZEN_EVIDENCE_CHANGED")
        expected_selection = selection_fingerprint(issue)
        if issue.get("selection_hash") != expected_selection:
            raise SafetyError("ISSUE_SELECTION_HASH_INVALID")
        if issue.get("recommendation_contract") == RECOMMENDATION_CONTRACT:
            if report_root is None:
                raise SafetyError("ISSUE_REPORT_ASSETS_REQUIRED")
            from .runtime_storage import runtime_path
            from .storage import load_json, safe_path
            from .live_outputs import build_issue_dataset, validate_dataset
            frozen = load_json(safe_path(safe_path(runtime_path(report_root, "data", "outputs"), issue_id,
                "revision-" + str(issue["revision"])) / "package-input.json"), 8 * 1024 * 1024)
            if (not isinstance(frozen, dict) or frozen.get("issue") != issue
                    or frozen.get("evidence_type") != memory.evidence_type):
                raise SafetyError("ISSUE_FROZEN_REPORT_INPUT_CHANGED")
            dataset = build_issue_dataset(issue, frozen.get("product_facts"), evidence_type=memory.evidence_type)
            validate_dataset(issue, dataset, root=report_root)
            if any(not _crowding_current(state, product) for product in issue["products"]):
                raise SafetyError("ISSUE_CROWDING_CHANGED")
            if any(not _fresh_at_release(product, issue, as_of=as_of or utc_now()) for product in issue["products"]):
                raise SafetyError("ISSUE_SUPPORT_EXPIRED")
        if old and old.get("selection_hash") == issue.get("selection_hash") and old.get("state") in ACTIVE_STATES:
            return old
        if old and old.get("state") in ("uploaded", "published"):
            receipt = _receipt(remote_confirmation, old, as_of=as_of or utc_now())
            if receipt.get("remote_state") != "withdrawn":
                raise SafetyError("ISSUE_REMOTE_CONFIRMATION_REQUIRED")
            if _publication_changes(state, old, receipt, _availability(receipt)):
                raise SafetyError("ISSUE_PUBLICATION_RECONCILIATION_REQUIRED")
        if old and issue.get("revision", 0) <= old["revision"]:
            raise SafetyError("ISSUE_REVISION_NOT_ADVANCED")
        published = _published(state["published_history"], issue["client_id"], exclude_issue_id=issue_id, aliases=aliases)
        counts = validate_issue(issue["products"], published, _held(state["issue_reservations"], own_issue_id=issue_id, client_id=issue["client_id"], aliases=aliases, history=state["published_history"]))
        if issue.get("counts") != counts or issue.get("shortfall") != 0 or issue.get("ready_for_review") is not True:
            raise SafetyError("ISSUE_FROZEN_COUNTS_CHANGED")
        for product in issue["products"]:
            if product["status"] == "Updated" and not update_baseline_current(
                    product, state["published_history"], state["issue_reservations"], issue["client_id"],
                    member_ids=aliases.get(product["concept_id"], {}).get("member_ids")):
                raise SafetyError("ISSUE_USEFUL_UPDATE_BASELINE_CHANGED")
            if product.get("source_scope_hash") is not None:
                from .report_selection import selection_scope
                if digest(selection_scope(state, product["concept_id"],
                        source_route_contract=product.get("source_route_contract"))) != product["source_scope_hash"]:
                    raise SafetyError("ISSUE_SOURCE_FACTS_CHANGED")
            revisions = product.get("source_revisions", {})
            if not isinstance(revisions, dict):
                raise SafetyError("ISSUE_SOURCE_REVISION_INVALID")
            for table, records in revisions.items():
                if table not in state or not isinstance(records, dict):
                    raise SafetyError("ISSUE_SOURCE_REVISION_INVALID")
            from .report_source_facts import source_revisions_current
            if not source_revisions_current(state, product):
                raise SafetyError("ISSUE_SOURCE_FACTS_CHANGED")
        value = dict(deepcopy(issue), client_ready=False, state="reserved", reserved_at=utc_now())
        memory.commit(key("reserve-issue", [issue_id, issue["revision"], issue["selection_hash"]]), [change("issue_reservations", issue_id, value, old)])
        return value


def mark_issue_uploaded(memory, issue_id, receipt, *, as_of):
    with Journal(memory.root).locked():
        state = memory.snapshot()
        old = state["issue_reservations"].get(identifier(issue_id))
        if not old:
            raise SafetyError("ISSUE_RESERVATION_MISSING")
        if old.get("state") == "cancelled":
            # An archived/cancelled draft is never revived by a late receipt.
            raise SafetyError("ISSUE_RESERVATION_CANCELLED")
        _receipt(receipt, old, as_of=as_of)
        if receipt.get("remote_state") != "active":
            raise SafetyError("ISSUE_REMOTE_CONFIRMATION_REQUIRED")
        value = dict(old, state="uploaded" if old["state"] != "published" else "published", activation_receipt=deepcopy(receipt))
        if value == old:
            return old
        memory.commit(key("uploaded-issue", [issue_id, receipt]), [change("issue_reservations", issue_id, value, old)])
        return value


def cancel_issue(memory, issue_id, *, as_of, remote_confirmation=None):
    with Journal(memory.root).locked():
        state = memory.snapshot()
        old = state["issue_reservations"].get(identifier(issue_id))
        if not old:
            raise SafetyError("ISSUE_RESERVATION_MISSING")
        if old["state"] == "cancelled":
            return old
        if old["state"] in ("uploaded", "published"):
            receipt = _receipt(remote_confirmation, old, as_of=as_of)
            if receipt.get("remote_state") != "withdrawn":
                raise SafetyError("ISSUE_REMOTE_CONFIRMATION_REQUIRED")
            history_changes = _publication_changes(state, old, receipt, _availability(receipt))
        else:
            history_changes = []
        value = dict(old, state="cancelled", cancelled_at=as_of)
        memory.commit(key("cancel-issue", [issue_id, old["revision"]]), history_changes + [change("issue_reservations", issue_id, value, old)])
        return value


def cancel_unpublished_issues(memory, expected, *, as_of, reason):
    """Release several never-activated private drafts in one canonical event.

    ``expected`` maps each issue ID to the selection hash the caller inspected.
    Every hold must still be that exact reserved draft, with no activation
    receipt and no published history. Any refusal changes nothing. Report files
    are never touched; the cancelled record keeps its products and the reason.
    """
    timestamp(as_of)
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise SafetyError("ISSUE_CANCEL_REASON_INVALID")
    if (not isinstance(expected, dict) or not expected
            or any(not isinstance(value, str) for value in expected.values())):
        raise SafetyError("ISSUE_CANCEL_SELECTION_INVALID")
    ids = sorted(identifier(issue_id) for issue_id in expected)
    with Journal(memory.root).locked():
        state = memory.snapshot()
        published = {record.get("issue_id") for record in _records(state["published_history"])}
        changes, values = [], {}
        for issue_id in ids:
            old = state["issue_reservations"].get(issue_id)
            if not old:
                raise SafetyError("ISSUE_RESERVATION_MISSING")
            if old.get("state") != "reserved" or "activation_receipt" in old:
                raise SafetyError("ISSUE_CANCEL_NOT_PRIVATE_DRAFT")
            if issue_id in published:
                raise SafetyError("ISSUE_CANCEL_PUBLISHED_HISTORY")
            if old.get("selection_hash") != expected[issue_id]:
                raise SafetyError("ISSUE_CANCEL_SELECTION_CHANGED")
            values[issue_id] = dict(old, state="cancelled", cancelled_at=as_of, cancel_reason=reason)
            changes.append(change("issue_reservations", issue_id, values[issue_id], old))
        memory.commit(key("cancel-unpublished-issues", [[issue_id, expected[issue_id]] for issue_id in ids]), changes)
        return values


def record_publication_receipt(memory, issue_id, receipt, *, as_of):
    """Record proven availability, not a future date, a download or local draft."""
    with Journal(memory.root).locked():
        state = memory.snapshot()
        old = state["issue_reservations"].get(identifier(issue_id))
        if not old:
            raise SafetyError("ISSUE_RESERVATION_MISSING")
        _receipt(receipt, old, as_of=as_of)
        available_at = _availability(receipt)
        if available_at is None:
            return {"published": False, "reason": "No proven availability interval has occurred."}
        changes = _publication_changes(state, old, receipt, available_at)
        value = dict(old, state="cancelled" if receipt["remote_state"] == "withdrawn" else "published", activation_receipt=deepcopy(receipt))
        if value != old:
            changes.append(change("issue_reservations", issue_id, value, old))
        if changes:
            memory.commit(key("publication", [issue_id, receipt]), changes)
        return {"published": True, "products": len(old["products"]), "available_at": available_at.isoformat()}
