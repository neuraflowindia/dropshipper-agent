"""The research funnel, step by step in pipeline order, from saved records only.

Every product-type judgement (checklist pass, score, label, report-ready codes,
the official meter counts) comes from the programme's own functions
(``build_report_pool``, ``meter_counts``, ``crowding_summary``,
``runway_summary``, ``functional_type_inventory``), applied to the state
replayed in memory. This module only counts and groups what they return and
what the saved records already say, with the reason for every stop.

"Work" counts (searches, results, questions, AI answers) cover records first
saved at or after ``since``. "Standing" counts (checklist, scores, labels,
report-ready) describe every product type as of ``as_of``.
"""

from collections import Counter, defaultdict
from copy import deepcopy
from statistics import median
from types import SimpleNamespace

from . import trace_labels as words
from .models import SafetyError
from .trace_records import at_or_after, day_of, day_span

ANSWERED = ("acquired", "completed_no_results", "insufficient")
PLAIN_PREPARED = "Prepared; no source attempt yet."
SCORE_PARTS = ("want", "early", "room", "product")
PART_WORDS = {"want": "Want (signs people want it, max 20)", "early": "Early (signs it is new or rising, max 30)",
              "room": "Room (how crowded Amazon/Flipkart is, max 25)", "product": "Product (checklist strength, max 25)"}
IDENTITY_CODES = ("identity_pending", "out_of_scope", "established_brand", "saved_facts_invalid")
# The owner meter's own tolerance: a failing part is named, the rest still shows.
JUDGEMENT_ERRORS = (SafetyError, KeyError, TypeError, ValueError, AttributeError, ArithmeticError)


def entry(key, text, value):
    return {"key": key, "label": text, "value": value}


def table(title, counter, labeller=words.reason, *, note=None, by_key=False, key_table=False):
    """A counted breakdown. ``key_table``: also shown in the short summary."""
    order = (lambda item: (str(type(item[0])), item[0])) if by_key else (lambda item: (-item[1], str(item[0])))
    rows = [{"code": code, "label": labeller(code), "value": value} for code, value in sorted(counter.items(), key=order)]
    return {"title": title, "rows": rows, **({"note": note} if note else {}), **({"key": True} if key_table else {})}


def step(key, title, counts=(), tables=(), notes=()):
    return {"key": key, "title": title, "counts": list(counts), "tables": [t for t in tables if t["rows"]],
            "notes": list(notes)}


def judge(store, as_of, *, pool_builder=None, counter=None):
    """The programme's own report pool and meter counts on the replayed state.

    Returns ``{"pool", "meter", "errors"}``. A failure leaves only that part
    empty and names it, like the owner meter does.
    """
    from .fit_policy import FIT_POLICY_ID
    from .programme import programme_records
    from .programme_summary import meter_counts
    from .report_examples import RECOMMENDATION_CONTRACT
    from .report_selection import build_report_pool
    pool_builder = pool_builder or build_report_pool
    counter = counter or meter_counts
    errors, pool, meter = {}, None, None
    state = store.state
    source = SimpleNamespace(snapshot=lambda: deepcopy(state), evidence_type="captured")
    try:
        pool = pool_builder(source, as_of=as_of, fit_policy_id=FIT_POLICY_ID,
                            recommendation_contract=RECOMMENDATION_CONTRACT)
    except JUDGEMENT_ERRORS as error:
        errors["report_pool"] = str(error) if isinstance(error, SafetyError) else type(error).__name__
    try:
        meter = counter(state, pool, programme_records(state), store.data_root, RECOMMENDATION_CONTRACT)
    except JUDGEMENT_ERRORS as error:
        errors["meter_counts"] = str(error) if isinstance(error, SafetyError) else type(error).__name__
    return {"pool": pool, "meter": meter, "errors": errors}


def _new(store, table_name, record_id, since):
    return at_or_after(store.first_at(table_name, record_id) or "", since)


def _kinds(store, table_name, kind):
    return [(record_id, row) for record_id, row in store.state[table_name].items() if row.get("kind") == kind]


def headings_step(store, since, as_of, *, catalogue_loader=None, map_config=None):
    from .heading_order import first_served_times
    from .map_topup import usable_headings
    from .product_map import runway_summary
    try:
        if catalogue_loader is None:
            from .catalogue import load_catalogue as catalogue_loader
        if map_config is None:
            from .map_materials import settings
            map_config = settings()
        base = catalogue_loader()
        summary = runway_summary(base, store.state, as_of, map_config)
        headings = usable_headings(base, store.state)
    except (SafetyError, KeyError, TypeError, ValueError, OSError) as error:
        return step("headings", "1. Product-map headings", notes=[
            "Could not read the product map: " + words.reason(str(error) if isinstance(error, SafetyError) else type(error).__name__)])
    first = first_served_times(store.state)
    searched_since = sum(1 for family_id in headings if family_id in first and at_or_after(first[family_id], since))
    return step("headings", "1. Product-map headings (the search list)", counts=[
        entry("total", "Headings on the map", summary["headings"]),
        entry("searched", "Headings searched at least once", summary["headings"] - summary["unsearched"]),
        entry("searched_since", "Headings first searched in this period", searched_since),
        entry("unsearched", "Headings never searched yet", summary["unsearched"]),
        entry("pace_per_day", "Recent pace (headings first searched per day)", summary["pace_per_day"])])


def discovery_step(store, since):
    planned, actions = Counter(), defaultdict(Counter)
    for wave_id, wave in _kinds(store, "batch_runs", "discovery-wave-v2"):
        if not _new(store, "batch_runs", wave_id, since):
            continue
        done = wave.get("actions") or {}
        for action in wave.get("planned_actions") or []:
            for question in action.get("questions") or []:
                route = question.get("route") or "unknown"
                planned[route] += 1
                actions[route][_wave_outcome(done.get(question.get("id")))] += 1
    wider, wider_found = defaultdict(Counter), Counter()
    for record_id, row in _kinds(store, "batch_runs", "wider-discovery-read-v1"):
        if _new(store, "batch_runs", record_id, since):
            name = f"{row.get('route')} ({row.get('lane')})"
            wider[name][row.get("state")] += 1
            wider_found[name] += row.get("candidates") or 0
    leads = Counter(row.get("route") for record_id, row in _kinds(store, "source_checks", "dropship-platform-lead-v2")
                    if _new(store, "source_checks", record_id, since))
    tables = [table("Heading searches planned, by website", planned, words.route)]
    for route, counter in sorted(actions.items()):
        tables.append(table("Heading searches on " + words.route(route) + ", by result", counter, _outcome_label))
    for name, counter in sorted(wider.items()):
        tables.append(table(f"Wider searches on {name}, by state (listings nominated: {wider_found[name]})",
                            counter, words.state))
    tables.append(table("Dropshipping-platform listings linked to a product type (leads), by platform", leads, words.route))
    platform_rows = []
    for route, value in sorted(store.platforms.items()):
        refresh = value["refresh"]
        full = (refresh.get("completed") or {}).get("full") or {}
        platform_rows.append(entry(route, words.route(route) + ": catalogue pages read / products listed in last full read",
                                   f"{len(value['reads'])} / {full.get('items', 'not finished')}"))
    return step("discovery", "2. Searches run to find product ideas (discovery)", counts=[
        entry("heading_searches", "Heading searches planned (Meta ads, Google Trends)", sum(planned.values())),
        entry("wider_searches", "Wider searches (supplier listings, rising ads)", sum(sum(c.values()) for c in wider.values())),
        *platform_rows], tables=tables)


def _wave_outcome(result):
    """One heading search's saved result, with its reason when it waits or failed."""
    if result is None:
        return "not asked yet"
    state, reason = result.get("state") or "unknown", result.get("reason")
    if state in ("waiting", "blocked", "failed") and isinstance(reason, str):
        return f"{state}: {reason}"
    return state


def raw_results_step(store, since):
    by_source, by_list = Counter(), Counter()
    for record_id, row in store.state["observations"].items():
        if _new(store, "observations", record_id, since):
            by_source[row.get("source")] += 1
            facts = row.get("facts") if isinstance(row.get("facts"), dict) else {}
            by_list[facts.get("list_type") or "not stated"] += 1
    pages = Counter(row.get("source") for record_id, row in store.state["captures"].items()
                    if _new(store, "captures", record_id, since))
    return step("raw_results", "3. Raw results seen (every listing, ad or result card saved)", counts=[
        entry("results", "Raw results saved", sum(by_source.values())),
        entry("pages", "Result pages saved", sum(pages.values()))],
        tables=[table("Raw results by website", by_source, _source),
                table("Raw results by kind of list", by_list, str)],
        notes=["Results the programme read but did not save (for example cards it could not recognise) are not recorded, so they cannot be counted."])


def _source(code):
    return words.source(code)


def wave_creators(store):
    """Types created by a heading-search wave: the wave saves only its new type
    ids, so the type is credited to the wave's websites and headings."""
    created = {}
    for record_id, wave in store.state["batch_runs"].items():
        if wave.get("kind") != "discovery-wave-v2":
            continue
        routes = sorted({question.get("route") for action in wave.get("planned_actions") or []
                         for question in action.get("questions") or [] if question.get("route")})
        headings = [action.get("family_id") for action in wave.get("planned_actions") or [] if action.get("family_id")]
        for concept_id in wave.get("new_concept_ids") or []:
            created.setdefault(concept_id, {"wave": record_id, "routes": routes, "headings": headings})
    return created


def naming_step(store, since):
    kinds, products = Counter(), 0
    for record_id, row in store.state["descriptions"].items():
        if _new(store, "descriptions", record_id, since):
            result = row.get("result") or {}
            kinds[result.get("kind")] += 1
            products += len(result.get("products") or [])
    blocked = Counter(row.get("reason") for record_id, row in store.state["problems"].items()
                      if row.get("stage") == "description" and _new(store, "problems", record_id, since))
    return step("naming", "4. AI names the products in the listings", counts=[
        entry("listings", "Listings the AI looked at", sum(kinds.values())),
        entry("products", "Products the AI named in them", products)],
        tables=[table("AI answer per listing", kinds, words.description_kind),
                table("Naming that could not run, by reason (one line per stop; the same listing can stop again later)",
                      blocked)])


def _semantic_sources(store):
    sources = {}
    for row in store.state["observations"].values():
        sources.setdefault(row.get("semantic_id"), row.get("source"))
    return sources


def candidates_step(store, since):
    sources = _semantic_sources(store)
    created_from, matching = Counter(), Counter()
    creators = {}
    for record_id, row in store.state["associations"].items():
        if row.get("status") == "resolved" and row.get("method") == "no_known_match" and row.get("concept_id"):
            creators.setdefault(row["concept_id"], sources.get(row.get("semantic_id"), "unknown"))
        if _new(store, "associations", record_id, since):
            status = row.get("status")
            matching[row.get("method") if status == "resolved" else row.get("reason") if status == "excluded"
                     else "unclear" if status == "deferred" else status] += 1
    waves = wave_creators(store)
    new_ids = [cid for cid in store.state["concepts"] if _new(store, "concepts", cid, since)]
    for cid in new_ids:
        if cid in creators:
            created_from[creators[cid]] += 1
        elif cid in waves:
            created_from["heading search on " + " + ".join(waves[cid]["routes"])] += 1
        else:
            created_from["not recorded"] += 1
    statuses = Counter(store.state["concepts"][cid].get("status") for cid in new_ids)
    identity = Counter(row.get("reason") for record_id, row in store.state["problems"].items()
                       if row.get("stage") == "identity" and _new(store, "problems", record_id, since))
    return step("candidates", "5. New product types (candidates) created", counts=[
        entry("created", "Product types created", len(new_ids))],
        tables=[table("Created from", created_from, _source,
                      note="'heading search on ...': the search wave saves only which types it created, not which listing."),
                table("What happened to each named product (match step)", matching, _matching),
                table("Product type status", statuses, str),
                table("Matching that could not finish, by reason", identity)])


def _matching(code):
    if code == "unclear":
        return "AI could not tell if it is the same as an existing type (left undecided) [deferred]"
    return words.reason(code)


def definitions_step(store, judged):
    from .functional_types import functional_type_inventory
    inventory = functional_type_inventory(store.state)
    reviews = Counter(bool(row.get("accepted")) for record_id, row in _kinds(store, "source_checks", "functional-definition-v1"))
    codes = Counter()
    if judged["pool"]:
        for decision in judged["pool"]["decisions"]:
            codes.update(code for code in decision["codes"] if code in IDENTITY_CODES)
    return step("definitions", "6. AI defines each product type (identity)", counts=[
        entry("active", "Active product types", inventory["active_concepts"]),
        entry("accepted", "Definition accepted (narrow type)", inventory["accepted_definitions"]),
        entry("pending", "Definition not accepted yet (pending)", inventory["legacy_views"]),
        entry("unreadable", "Records that could not be read", len(inventory["unresolved"])),
        entry("inactive", "Merged, retired or excluded types", inventory["inactive_concepts"]),
        entry("definition_answers", "AI definition answers saved (accepted / not accepted)",
              f"{reviews[True]} / {reviews[False]}")],
        tables=[table("Stopped at identity, by reason", codes)])


def checklist_step(store, judged):
    meter, pool = judged["meter"] or {}, judged["pool"]
    meets, codes = Counter(), Counter()
    if pool:
        assessed = set(store.state["assessments"])
        for cid, fact in pool["facts_by_concept"].items():
            if cid in assessed and isinstance(fact.get("kpi_matches"), int):
                meets[fact["kpi_matches"]] += 1
        for decision in pool["decisions"]:
            codes.update(code for code in decision["codes"]
                         if code in ("below_checklist_threshold", "fit_information_pending", "checklist_review_required"))
    blocked = Counter(row.get("reason") for row in store.state["problems"].values() if row.get("stage") == "fit_review")
    return step("checklist", "7. AI fills the 10-point checklist (5 or more Meets passes)", counts=[
        entry("assessed", "Types with a current checklist", meter.get("assessed_types")),
        entry("passing", "Types that pass (5+ Meets)", meter.get("assessment_passing_types")),
        entry("below", "Below the pass mark", codes["below_checklist_threshold"]),
        entry("pending", "Information still pending", codes["fit_information_pending"]),
        entry("not_filled", "Checklist not filled yet", codes["checklist_review_required"])],
        tables=[table("Number of Meets per type (out of 10)", meets, lambda code: f"{code} Meets", by_key=True),
                table("Checklist work that could not run, by reason (one line per stop)", blocked)])


def supplier_step(store, judged, since):
    pool = judged["pool"]
    status, routes = Counter(), defaultdict(Counter)
    needed = found = checked = with_leads = 0
    if pool:
        for decision in pool["decisions"]:
            needed += "supplier_search_required" in decision["codes"]
            found += decision.get("supplier_leads_found") or 0
            checked += decision.get("supplier_leads_checked") or 0
            with_leads += bool(decision.get("supplier_leads_found"))
        status.update(fact.get("supplier_search_status") or "not recorded" for fact in pool["facts_by_concept"].values())
    for record_id, row in _kinds(store, "query_tasks", "all_source_task_v1"):
        if row.get("source") == "supplier" and _new(store, "query_tasks", record_id, since):
            routes[row["route"]][_task_outcome(row)] += 1
    tables = [table("Supplier search status per type", status, str)]
    for route, counter in sorted(routes.items()):
        tables.append(table("Supplier questions to " + words.route(route), counter, _outcome_label))
    return step("suppliers", "8. Supplier search (1-3 supplier leads per type)", counts=[
        entry("needed", "Types still needing a supplier search", needed),
        entry("with_leads", "Types with at least one supplier lead", with_leads),
        entry("leads_found", "Supplier leads found (all types)", found),
        entry("leads_checked", "Supplier leads checked (all types)", checked)], tables=tables)


def photo_step(store, judged):
    pool = judged["pool"]
    facts = pool["facts_by_concept"].values() if pool else []
    images = _kinds(store, "source_checks", "public_product_image")
    reads = Counter((row.get("state"), row.get("reason") if row.get("state") != "usable" else None) for _, row in images)
    downloaded = {row.get("concept_id") for _, row in images if row.get("state") == "usable" and row.get("concept_id")}
    return step("photos", "9. Product photos", counts=[
        entry("downloaded", "Types with a photo downloaded", len(downloaded)),
        entry("candidates", "Types with photo candidates in their report facts",
              sum(bool(fact.get("image_candidates")) for fact in facts) if pool else None),
        entry("chosen", "Types with a photo chosen for the report", sum(bool(fact.get("image_path")) for fact in facts)
              if pool else None),
        entry("downloads", "Photo downloads saved", sum(reads.values()))],
        tables=[table("Photo downloads by result", Counter({f"{a}{'' if not b else ': ' + b}": n for (a, b), n in reads.items()}),
                      str)])


def _task_outcome(row):
    status = row.get("status")
    reason = row.get("reason")
    if status == "prepared":
        return "not asked yet" if reason in (None, PLAIN_PREPARED) else "waiting: " + str(reason)
    if status in ("blocked", "failed") and isinstance(reason, str) and words.reason(reason).endswith("]"):
        return f"{status}: {reason}"
    return status


def _outcome_label(code):
    if code == "not asked yet":
        return "Not asked yet [prepared]"
    head, _, tail = code.partition(": ")
    if tail:
        return words.state(head if head != "waiting" else "waiting") + " — " + words.reason(tail)
    return words.state(code)


def market_step(store, since):
    routes, judged, created = defaultdict(Counter), defaultdict(Counter), Counter()
    for record_id, row in _kinds(store, "query_tasks", "all_source_task_v1"):
        if row.get("source") == "supplier" or not _new(store, "query_tasks", record_id, since):
            continue
        route = row["route"]
        created[route] += 1
        routes[route][_task_outcome(row)] += 1
        for result in (row.get("target_results") or {}).values():
            if isinstance(result, dict):
                judged[route][result.get("state")] += 1
    asked = {route: sum(n for code, n in counter.items() if not code.startswith(("not asked", "waiting")))
             for route, counter in routes.items()}
    tables = [table("Questions created, by website", created, words.route),
              table("Questions actually asked (sent or answered), by website", Counter(asked), words.route)]
    for route in sorted(routes):
        tables.append(table("Questions to " + words.route(route) + ", by outcome", routes[route], _outcome_label))
        tables.append(table("Answers from " + words.route(route) + ", by AI judgement", judged[route], words.state))
    return step("market", "10. Market checks per website (one question per product type)", counts=[
        entry("created", "Market questions created", sum(created.values())),
        entry("asked", "Market questions asked", sum(asked.values()))], tables=tables)


def crowding_step(store):
    from .crowding import crowding_summary
    try:
        summary = crowding_summary(store.state)
    except (SafetyError, KeyError, TypeError, ValueError) as error:
        return step("crowding", "11. Amazon/Flipkart crowding level", notes=[
            "Could not read: " + words.reason(str(error) if isinstance(error, SafetyError) else type(error).__name__)])
    return step("crowding", "11. Amazon/Flipkart crowding level", counts=[
        entry(level, f"{level.replace('_', ' ').capitalize()}", count) for level, count in summary["levels"].items()],
        notes=[summary["text"]] + ([summary["alert"]] if summary.get("alert") else []))


def _bases(judged):
    pool = judged["pool"]
    return [decision for decision in (pool["decisions"] if pool else []) if decision.get("opportunity_basis")]


def score_step(judged):
    scored = _bases(judged)
    tables, counts = [], []
    for part in SCORE_PARTS:
        points = [decision["opportunity_basis"]["parts"][part]["points"] for decision in scored
                  if part in decision["opportunity_basis"].get("parts", {})]
        items = Counter()
        for decision in scored:
            for name, item in decision["opportunity_basis"]["parts"].get(part, {}).get("items", {}).items():
                if item.get("points"):
                    items[name] += 1
        counts.append(entry(part, PART_WORDS[part] + ": types with any points / median points",
                            f"{sum(1 for p in points if p)} of {len(points)} / {median(points) if points else '-'}"))
        tables.append(table(PART_WORDS[part] + " — points, number of types", Counter(points), str, by_key=True))
        tables.append(table(PART_WORDS[part] + " — which signals gave points (types)", items, str))
    totals = Counter(decision.get("opportunity_score") for decision in scored)
    return step("scores", "12. Opportunity score parts (out of 100)", counts=[
        entry("scored", "Types with a score", len(scored)), *counts],
        tables=[table("Total score, number of types", totals, str, by_key=True), *tables])


def label_step(judged):
    pool = judged["pool"]
    labels = Counter(decision.get("opportunity_label") if decision.get("opportunity_basis") else "not scored"
                     for decision in (pool["decisions"] if pool else []))
    return step("labels", "13. Labels", tables=[table("Types per label", labels,
                lambda code: "Not scored yet (checklist or crowding missing)" if code == "not scored" else words.label(code),
                key_table=True)])


def codes_step(judged):
    pool = judged["pool"]
    codes, combos = Counter(), Counter()
    for decision in (pool["decisions"] if pool else []):
        codes.update(decision["codes"])
        combos[" + ".join(decision["codes"]) or "none (report-ready)"] += 1
    return step("codes", "14. Why types are not report-ready (a type can have several reasons)", counts=[
        entry("types", "Product types judged", len(pool["decisions"]) if pool else None),
        entry("several", "Types with more than one reason", sum(n for combo, n in combos.items() if " + " in combo))],
        tables=[table("Reason (types)", codes, key_table=True), table("Exact reason combinations (types)", combos, str)])


def eligible_step(store, judged):
    pool = judged["pool"]
    from .trace_product import type_name
    names = [f"{type_name(store.state, row['concept_id'])} ({row['concept_id']})" for row in (pool["eligible"] if pool else [])]
    return step("eligible", "15. Report-ready types (passed every gate)", counts=[
        entry("eligible", "Report-ready types", len(pool["eligible"]) if pool else None),
        entry("meter", "Owner meter count (same rule)", (judged["meter"] or {}).get("report_eligible_types"))],
        notes=names)


def allocation_step(store, judged):
    reservations = Counter(row.get("status") for row in store.state["issue_reservations"].values())
    published = Counter(row.get("status") for row in store.state["published_history"].values())
    return step("allocation", "16. Allocated to reports, and reports produced", counts=[
        entry("allocated", "Types allocated to a report", sum(reservations.values())),
        entry("published", "Types published to the client", published.get("published", 0)),
        entry("reports", "Report sets produced", (judged["meter"] or {}).get("report_sets_produced"))],
        tables=[table("Report allocations by status", reservations, str)])


def per_day(store, since, until):
    """What was done on each owner day (India time), from first-saved times."""
    from .heading_order import first_served_times
    days = {day: Counter() for day in day_span(since, until)}

    def add(at, name, amount=1):
        if at and at_or_after(at, since):
            day = day_of(at)
            if day in days:
                days[day][name] += amount
    for at in first_served_times(store.state).values():
        add(at, "headings_first_searched")
    for (table_name, record_id), at in store.timeline.first.items():
        row = store.state[table_name][record_id]
        kind = row.get("kind")
        if table_name == "observations":
            add(at, "raw_results")
        elif table_name == "descriptions":
            add(at, "listings_named_by_ai")
        elif table_name == "concepts":
            add(at, "product_types_created")
        elif table_name == "assessments":
            add(at, "checklists_saved")
        elif kind == "all_source_task_v1":
            add(at, "questions_created")
        elif kind == "crowding-fact-v1":
            add(at, "crowding_results")
        elif kind == "public_product_image" and row.get("state") == "usable":
            add(at, "photos_saved")
        elif kind == "source-acquisition-stage-v1" and row.get("stage_type") == "dispatch_intent":
            add(at, "website_reads_sent")
        elif kind == "functional-definition-v1" and row.get("accepted"):
            add(at, "definitions_accepted")
    for (table_name, record_id), history in store.timeline.history.items():
        if table_name == "query_tasks":
            answered = next((at for at, _, summary in history if summary.get("status") in ANSWERED), None)
            add(answered, "questions_answered")
    for job in store.ai_jobs:
        completed = (job.get("result") or {}).get("completed_at")
        add(completed, "ai_jobs_finished")
    for row in store.run_rows:
        if row["event"] == "round_finished":
            add(row["at"], "rounds_finished")
    return [{"day": day, **{name: counter[name] for name in DAY_FIELDS}} for day, counter in days.items()]


DAY_FIELDS = ("rounds_finished", "headings_first_searched", "website_reads_sent", "raw_results", "listings_named_by_ai",
              "product_types_created", "definitions_accepted", "checklists_saved", "questions_created",
              "questions_answered", "crowding_results", "photos_saved", "ai_jobs_finished")
DAY_WORDS = {"rounds_finished": "Rounds finished", "headings_first_searched": "Headings first searched",
             "website_reads_sent": "Website reads sent", "raw_results": "Raw results saved",
             "listings_named_by_ai": "Listings named by AI", "product_types_created": "New product types",
             "definitions_accepted": "Definitions accepted", "checklists_saved": "Checklists saved",
             "questions_created": "Market/supplier questions created", "questions_answered": "Questions answered",
             "crowding_results": "Crowding results", "photos_saved": "Photos saved", "ai_jobs_finished": "AI jobs finished"}


def funnel(store, judged, *, since, as_of, until=None, catalogue_loader=None, map_config=None):
    """Every step in pipeline order, plus the per-day breakdown."""
    steps = [headings_step(store, since, as_of, catalogue_loader=catalogue_loader, map_config=map_config),
             discovery_step(store, since), raw_results_step(store, since), naming_step(store, since),
             candidates_step(store, since), definitions_step(store, judged), checklist_step(store, judged),
             supplier_step(store, judged, since), photo_step(store, judged), market_step(store, since),
             crowding_step(store), score_step(judged), label_step(judged), codes_step(judged),
             eligible_step(store, judged), allocation_step(store, judged)]
    meter = judged["meter"] or {}
    return {"kind": "research-trace-funnel-v1", "since": since, "as_of": as_of,
            "meter": {name: meter.get(name) for name in ("candidates", "accepted_narrow_types", "assessed_types",
                      "assessment_passing_types", "report_eligible_types", "report_sets_produced", "ai_jobs")},
            "errors": judged["errors"], "steps": steps,
            "per_day": per_day(store, since, until or store.facts.get("last_event_at") or as_of)}
