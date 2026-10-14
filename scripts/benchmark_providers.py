#!/usr/bin/env python3
"""Bounded source trials: offline by default; network actions require --live."""

import argparse
from contextlib import nullcontext
from datetime import date
import json
from pathlib import Path
import sys
from urllib.parse import urlencode
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.models import PreparedMeta, SafetyError, digest, identifier, utc_now
from product_radar.research_sources import prepare_source, AMAZON_CASES
from product_radar.providers.apify import map_meta_rows
from product_radar.runtime_storage import runtime_path
from product_radar.storage import Journal, atomic_json, safe_path
from product_radar.accounts import (bind_account, load_binding, load_account_registry, bind_registered_account,
                                    account_pool_status, authorise_credential_recovery)
from product_radar.service import load_policy, calendar_plan
from product_radar.benchmark import MetaTrial
from product_radar.providers.apify import ApifyTransport

CONFIG = ROOT / "config/benchmarks/meta_first.json"


def prepare(config, attempt_id):
    required = {"schema_version", "stage", "purpose", "term", "actor", "build", "expected_build_id",
                "reserved_usd", "stage_cap_usd", "trial_cap_usd", "reviewed_at", "metadata"}
    if set(config) != required or config["schema_version"] != 1 or config["stage"] != "T1-meta" or config["purpose"] not in ("discovery", "validation") or config["stage_cap_usd"] != "0.20" or config["trial_cap_usd"] != "0.75":
        raise SafetyError("UNREVIEWED_CONFIG")
    query = {"active_status": "active", "ad_type": "all", "country": "IN", "q": config["term"],
             "search_type": "keyword_unordered", "media_type": "all"}
    request = PreparedMeta(
        attempt_id=identifier(attempt_id), actor=config["actor"], build=config["build"],
        expected_build_id=config["expected_build_id"], purpose=config["purpose"],
        input={"urls": [{"url": "https://www.facebook.com/ads/library/?" + urlencode(query)}],
               "limitPerSource": 5 if config["purpose"] == "discovery" else 10,
               "scrapeAdDetails": False, "runTag": attempt_id},
        options={"build": config["build"], "timeout": 300, "maxTotalChargeUsd": config["reserved_usd"], "restartOnError": "false"},
        reserved_usd=config["reserved_usd"], reviewed_at=config["reviewed_at"], public_metadata_hash=digest(config["metadata"]))
    return {"schema_version": 1, "execution_mode": "offline_preparation", "request": request.to_dict(),
            "config_hash": request.config_hash, "remote_attempt_created": False, "credits_used": False,
            "ready_for_live": False, "blocking_checks": ["owner_credential_confirmation", "account_identity_period_balance",
                "fresh_account_bound_schema_pricing_build_review", "runtime_charge_and_output_access_preflight"],
            "local_capture_bounds": {"page_bytes": 5242880, "pages": 10, "records": 1000},
            "bounded_meta_sample": True, "metadata": config["metadata"]}


def prepare_planned_action(plan, action, *, production=True):
    """Save or recover the exact plan request without making a provider call."""
    from product_radar.benchmark import load_preparation
    from product_radar.models import as_production_request, prepared_request
    from product_radar.storage import load_json
    existing = action.get("existing_task")
    if existing is not None:
        request = load_preparation(ROOT, existing["attempt_id"])
        if request.config_hash != existing["config_hash"]:
            raise SafetyError("SAVED_PREPARATION_CONFLICT")
        return request
    identifier(plan["id"]); identifier(action["action_id"])
    attempt_id = "meta-" + digest([plan["id"], action["action_id"]])[:32]
    config = load_json(CONFIG)
    config.update(term=action["request"]["term"], purpose=action["request"]["purpose"])
    envelope = prepare(config, attempt_id)
    request = prepared_request(envelope["request"])
    if production:
        request = as_production_request(request)
        envelope.update(request=request.to_dict(), config_hash=request.config_hash)
    destination = runtime_path(ROOT, "data/benchmarks/2026-10-03") / attempt_id / "prepared.json"
    if destination.exists() and load_json(destination, 1024 * 1024) != envelope:
        raise SafetyError("SAVED_PREPARATION_CONFLICT")
    atomic_json(destination, envelope)
    return request


def load_visual_followup_requests(input_name):
    """Check a bounded operator input before opening research state or a model."""
    from product_radar.storage import load_json
    if not isinstance(input_name, str) or not input_name.strip() or "\x00" in input_name:
        raise SafetyError("VISUAL_INPUT_PATH_INVALID")
    path = safe_path(ROOT / input_name)
    if not path.resolve().is_relative_to(ROOT.resolve()):
        raise SafetyError("VISUAL_INPUT_PATH_INVALID")
    requests = load_json(path, 256 * 1024)
    if (not isinstance(requests, list) or not 1 <= len(requests) <= 5
            or any(not isinstance(row, dict) or set(row) != {"child_id", "images"}
                   or not isinstance(row["images"], list) or len(row["images"]) > 4
                   or any(not isinstance(image, dict) for image in row["images"]) for row in requests)):
        raise SafetyError("VISUAL_FOLLOWUP_REQUESTS_INVALID")
    ids = [identifier(row["child_id"]) for row in requests]
    if len(set(ids)) != len(ids):
        raise SafetyError("VISUAL_FOLLOWUP_REQUESTS_INVALID")
    return requests


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("prepare", "status", "replay", "bind", "bind-account", "account-status", "authorise-recovery", "preflight", "run", "resume", "review", "settle", "settle-renewed-read", "resolve", "apply-policy", "adopt-production", "calendar", "refresh-costs", "research-import", "research-summary", "research-run", "research-retry", "research-repair-checklist", "research-visual-followup", "research-catalogue", "research-plan", "research-validate", "research-prepare", "research-import-trends", "research-expand", "research-suppliers", "research-market", "research-batch", "research-continue", "research-ready", "research-allocate", "research-brand-review", "research-project-fit", "research-review-fit", "research-capture-supplier", "research-plan-trends", "research-capture-trends", "research-discover-suppliers"), default="prepare")
    parser.add_argument("--account-id", help="Private configured profile ID; bind-account or same-account read recovery only.")
    parser.add_argument("--original-generation", help="Original saved key generation; authorise-recovery only, never launches or moves a job.")
    parser.add_argument("--read-id", help="Exact current-period funded-read envelope; settle-renewed-read only, no network.")
    parser.add_argument("--job-id", help="Exact saved model job for execution retry preparation or one-item checklist repair.")
    parser.add_argument("--failure-id", help="Exact saved per-item checklist rejection; research-repair-checklist only.")
    parser.add_argument("--review-id", action="append", help="Exact frozen saved-input fit review; research-review-fit only. Repeat for a bounded group.")
    parser.add_argument("--reason", help="Reason for a model retry, unsubmitted batch continuation, or named validation query refinement.")
    parser.add_argument("--live-model", action="store_true", help="Use the existing ChatGPT sign-in for bounded research interpretation; no collector calls.")
    parser.add_argument("--item-limit", type=int, help="Maximum items per research stage in this invocation (1–50; default 10).")
    parser.add_argument("--term", help="Reviewed Meta query for offline preparation or a named research-validate refinement.")
    parser.add_argument("--purpose", choices=("discovery", "validation"))
    parser.add_argument("--source", choices=("meta", "amazon", "trends"), help="Source for offline preparation; defaults to Meta.")
    parser.add_argument("--case", choices=tuple(AMAZON_CASES), help="One reviewed Amazon India list.")
    parser.add_argument("--start", help="First local publication date, inclusive; calendar or research-allocate.")
    parser.add_argument("--end", help="Last local date, exclusive; calendar or research-allocate.")
    parser.add_argument("--available-usd", help="Remaining allocated research money; calendar, research-plan or research-validate.")
    parser.add_argument("--as-of", help="Explicit timezone-aware planning time; research-plan or research-validate.")
    parser.add_argument("--slots", type=int, help="Finite choices: research-plan 0–100, research-validate 1–5 (default 3).")
    parser.add_argument("--plan-id", help="Saved working plan; research-prepare, research-batch or research-continue.")
    parser.add_argument("--action-limit", type=int, help="Maximum planned actions in one research-batch (1–8; default 8).")
    parser.add_argument("--production", action="store_true", help="Prepare routine collection using existing production allocation, not a fresh trial allowance.")
    parser.add_argument("--action-id", help="One discovery action within the saved plan; research-prepare only.")
    parser.add_argument("--concept-id", help="Known product for Trends import or a named research-validate query refinement.")
    parser.add_argument("--csv", help="Saved Google Trends CSV; research-import-trends only.")
    parser.add_argument("--receipt", help="Saved JSON capture receipt with verified scope; research-import-trends only.")
    parser.add_argument("--supplier-input", help="Saved supplier interpretation inputs, or a known-lead capture request.")
    parser.add_argument("--brand-review-input", help="Saved source-backed established-brand correction; research-brand-review only.")
    parser.add_argument("--visual-input", help="Saved JSON list of 1–5 child_id/images requests; research-visual-followup only. No photograph fetching.")
    parser.add_argument("--check-id", help="Exact saved Trends follow-up request; research-capture-trends only.")
    parser.add_argument("--browser-path", help="Installed Chromium executable for Trends or supplier search.")
    parser.add_argument("--acquisition-id", action="append", help="Limit research-run to one or more actual saved acquisitions.")
    parser.add_argument("--fixture", help="Reviewed synthetic fixture under tests/fixtures/benchmarks.")
    parser.add_argument("--live", action="store_true", help="Explicit provider access; never replaces account confirmation.")
    parser.add_argument("--attempt-id", help="Exact saved preparation ID; resume never creates a second launch.")
    args = parser.parse_args(argv)
    try:
        network = args.action in ("bind", "bind-account", "preflight", "run", "resume", "refresh-costs", "research-batch", "research-capture-supplier", "research-capture-trends", "research-discover-suppliers")
        if (args.account_id is not None) != (args.action in ("bind-account", "authorise-recovery")):
            raise SafetyError("ACCOUNT_PROFILE_FLAG_REQUIRED_OR_NOT_ALLOWED")
        if (args.original_generation is not None) != (args.action == "authorise-recovery"):
            raise SafetyError("CREDENTIAL_RECOVERY_FLAG_REQUIRED_OR_NOT_ALLOWED")
        if (args.read_id is not None) != (args.action == "settle-renewed-read"):
            raise SafetyError("READ_FUNDING_FLAG_REQUIRED_OR_NOT_ALLOWED")
        if args.read_id is not None:
            identifier(args.read_id)
        if args.account_id is not None:
            identifier(args.account_id)
        if args.original_generation is not None:
            identifier(args.original_generation)
        if network and not args.live:
            raise SafetyError("EXPLICIT_LIVE_REQUIRED")
        if args.live and not network:
            raise SafetyError("LIVE_FLAG_NOT_ALLOWED_FOR_OFFLINE_ACTION")
        if args.action in ("preflight", "run", "resume", "review", "settle", "settle-renewed-read", "resolve", "research-import"):
            identifier(args.attempt_id)
        elif args.attempt_id is not None:
            raise SafetyError("ATTEMPT_FLAG_NOT_ALLOWED")
        if args.fixture is not None and args.action != "replay":
            raise SafetyError("FIXTURE_FLAG_NOT_ALLOWED")
        if (args.term is not None and args.action not in ("prepare", "research-validate", "research-plan-trends")
                or args.purpose is not None and args.action != "prepare"):
            raise SafetyError("QUERY_FLAG_NOT_ALLOWED")
        if (args.source is not None or args.case is not None) and args.action != "prepare":
            raise SafetyError("SOURCE_FLAG_NOT_ALLOWED")
        source = args.source or "meta"
        if ((source != "meta" and (args.term is not None or args.purpose is not None))
                or (source != "amazon" and args.case is not None)):
            raise SafetyError("SOURCE_FLAG_CONFLICT")
        if any(v is not None for v in (args.start, args.end)) and args.action not in ("calendar", "research-allocate"):
            raise SafetyError("CALENDAR_FLAG_NOT_ALLOWED")
        if args.available_usd is not None and args.action not in ("calendar", "research-plan", "research-validate"):
            raise SafetyError("BUDGET_FLAG_NOT_ALLOWED")
        if (args.as_of is not None and args.action not in ("research-plan", "research-validate", "research-plan-trends")
                or args.slots is not None and args.action not in ("research-plan", "research-validate")):
            raise SafetyError("PLANNING_FLAG_NOT_ALLOWED")
        if args.plan_id is not None and args.action not in ("research-prepare", "research-batch", "research-continue"):
            raise SafetyError("PLAN_ACTION_FLAG_NOT_ALLOWED")
        if args.action_id is not None and args.action != "research-prepare":
            raise SafetyError("PLAN_ACTION_FLAG_NOT_ALLOWED")
        if args.action_limit is not None and (args.action != "research-batch" or not 1 <= args.action_limit <= 8):
            raise SafetyError("BATCH_LIMIT_INVALID")
        if args.production and args.action != "research-prepare":
            raise SafetyError("PRODUCTION_FLAG_NOT_ALLOWED")
        if (args.concept_id is not None and args.action not in ("research-import-trends", "research-validate", "research-plan-trends", "research-discover-suppliers", "research-repair-checklist", "research-project-fit")
                or any(v is not None for v in (args.csv, args.receipt)) and args.action != "research-import-trends"):
            raise SafetyError("TRENDS_IMPORT_FLAG_NOT_ALLOWED")
        if (args.check_id is not None and args.action != "research-capture-trends"
                or args.browser_path is not None and args.action not in ("research-capture-trends", "research-discover-suppliers")):
            raise SafetyError("TRENDS_CAPTURE_FLAG_NOT_ALLOWED")
        model_actions = ("research-run", "research-expand", "research-suppliers", "research-market", "research-batch", "research-repair-checklist", "research-visual-followup", "research-review-fit")
        if args.action == "research-repair-checklist" and args.item_limit is not None:
            raise SafetyError("RESEARCH_REPAIR_SINGLE_ITEM_REQUIRED")
        if args.action == "research-visual-followup" and args.item_limit is not None:
            raise SafetyError("VISUAL_SAVED_INPUT_DEFINES_LIMIT")
        if (args.live_model and args.action not in model_actions or args.item_limit is not None and args.action not in (*model_actions, "research-project-fit")):
            raise SafetyError("RESEARCH_FLAG_NOT_ALLOWED")
        if args.action in model_actions and not args.live_model:
            raise SafetyError("EXPLICIT_LIVE_MODEL_REQUIRED")
        if (args.review_id is not None) != (args.action == "research-review-fit"):
            raise SafetyError("FIT_REVIEW_IDS_REQUIRED_OR_NOT_ALLOWED")
        if args.review_id is not None:
            if len(set(args.review_id)) != len(args.review_id) or len(args.review_id) > (args.item_limit or 10):
                raise SafetyError("FIT_REVIEW_BATCH_INVALID")
            for review_id in args.review_id:
                identifier(review_id)
        if args.action == "research-project-fit" and args.concept_id is not None:
            identifier(args.concept_id)
        if args.supplier_input is not None and args.action not in ("research-suppliers", "research-capture-supplier"):
            raise SafetyError("SUPPLIER_INPUT_FLAG_NOT_ALLOWED")
        if (args.brand_review_input is not None) != (args.action == "research-brand-review"):
            raise SafetyError("BRAND_REVIEW_INPUT_REQUIRED_OR_NOT_ALLOWED")
        if (args.visual_input is not None) != (args.action == "research-visual-followup"):
            raise SafetyError("VISUAL_INPUT_REQUIRED_OR_NOT_ALLOWED")
        if args.acquisition_id is not None and args.action != "research-run":
            raise SafetyError("ACQUISITION_SCOPE_FLAG_NOT_ALLOWED")
        if args.item_limit is not None and not 1 <= args.item_limit <= 50:
            raise SafetyError("RESEARCH_BATCH_LIMIT_INVALID")
        if ((args.job_id is not None and args.action not in ("research-retry", "research-repair-checklist"))
                or (args.reason is not None and args.action not in ("research-retry", "research-continue", "research-validate"))):
            raise SafetyError("RESEARCH_RETRY_FLAG_NOT_ALLOWED")
        if args.failure_id is not None and args.action != "research-repair-checklist":
            raise SafetyError("CHECKLIST_REPAIR_FLAG_NOT_ALLOWED")
        if args.action == "research-repair-checklist":
            for value in (args.concept_id, args.failure_id, args.job_id):
                identifier(value)
        refinement = (args.concept_id, args.term, args.reason)
        if args.action == "research-validate" and any(value is not None for value in refinement) and any(value is None for value in refinement):
            raise SafetyError("META_VALIDATION_REFINEMENT_REQUIRED")
        visual_requests = load_visual_followup_requests(args.visual_input) if args.action == "research-visual-followup" else None
        if args.action == "calendar":
            if None in (args.start, args.end, args.available_usd):
                raise SafetyError("CALENDAR_ARGUMENTS_REQUIRED")
            print(json.dumps(calendar_plan(args.start, args.end, args.available_usd), indent=2))
            return 0
        journal = Journal(runtime_path(ROOT, "data/benchmarks/state/apify"))
        if args.action.startswith("research-"):
            from product_radar.research_memory import ResearchMemory, import_saved_attempt, key
            from product_radar.research_flow import candidate_summary, run_flow, write_summary
            from product_radar.research_ai import prepare_retry
            from product_radar.spend_watch_ai import research_codex_runner
            from product_radar.storage import load_json
            memory = ResearchMemory(runtime_path(ROOT, "data/research/captured"), journal)
            if args.action == "research-project-fit":
                from product_radar.fit_review import project_saved_fit
                from product_radar.fit_policy import FIT_POLICY_ID, LEGACY_POLICY_ID
                from product_radar.brand_reviews import current_brand
                state = memory.snapshot()
                outcomes = []
                targets = [args.concept_id] if args.concept_id else []
                if args.concept_id is None:
                    for concept_id, record in sorted(state["assessments"].items()):
                        concept = state["concepts"].get(concept_id)
                        if record.get("fit_policy_id", LEGACY_POLICY_ID) != LEGACY_POLICY_ID or not concept or (
                                concept.get("status") in ("excluded", "merged", "retired") or concept.get("identity_conflicted")):
                            continue
                        try:
                            if current_brand(state, "concept", concept_id, concept["description"]) == "established":
                                continue
                        except SafetyError as error:
                            outcomes.append({"concept_id": concept_id, "status": "pending", "applied": False, "reason": str(error)})
                            continue
                        targets.append(concept_id)
                applied_count = 0
                for concept_id in targets:
                    try:
                        outcome = project_saved_fit(memory, concept_id)
                        outcomes.append(dict(concept_id=concept_id, **outcome))
                        if outcome.get("status") in ("projected", "already_current", "already_applied", "review_base_recovered"):
                            applied_count += 1
                            if applied_count >= (args.item_limit or 50):
                                break
                    except SafetyError as error:
                        outcomes.append({"concept_id": concept_id, "status": "pending", "applied": False, "reason": str(error)})
                print(json.dumps({"fit_policy_id": FIT_POLICY_ID, "outcomes": outcomes}, indent=2), flush=True)
                write_summary(memory, candidate_summary(memory))
                print("Saved rows projected only. No collector, model, reservation or publication call.")
            elif args.action == "research-review-fit":
                from product_radar.fit_review import run_fit_reviews
                runner = research_codex_runner(load_json(ROOT / "config/ai/codex_luna.json"), root=ROOT)
                print("Reviewing only the named saved public fit questions; no collector searches.", flush=True)
                result = run_fit_reviews(memory, runner, args.review_id, limit=args.item_limit or 10)
                print(json.dumps({"outcomes": result}, indent=2), flush=True)
                write_summary(memory, candidate_summary(memory))
                print("Only saved public research was interpreted. No supplier contact, paid collector or publication call.")
            elif args.action == "research-visual-followup":
                from product_radar.visual_followups import resolve_saved_visual_followups
                result = resolve_saved_visual_followups(memory, visual_requests,
                    research_codex_runner(load_json(ROOT / "config/ai/codex_luna.json"), root=ROOT))
                write_summary(memory, candidate_summary(memory))
                print(json.dumps(result, indent=2))
                print("Only explicitly selected saved photographs were checked. No photo fetch, collector, supplier contact or publication call.")
            elif args.action == "research-repair-checklist":
                from product_radar.checklist_repair import repair_checklist_item
                result = repair_checklist_item(memory, research_codex_runner(load_json(ROOT / "config/ai/codex_luna.json"), root=ROOT),
                    concept_id=args.concept_id, failure_id=args.failure_id, job_id=args.job_id)
                write_summary(memory, candidate_summary(memory))
                print(json.dumps(result, indent=2))
                print("Only the named checklist rejection was repaired or reused. No collector, supplier contact or publication call.")
            elif args.action == "research-discover-suppliers":
                from product_radar.supplier_discovery import (discover_saved_supplier_leads,
                    capture_discovered_supplier_pages, supplier_search_check)
                from product_radar.supplier_search_browser import fetch_google_search_page
                identifier(args.concept_id)
                search = discover_saved_supplier_leads(memory, args.concept_id, route="google-browser-html",
                    fetcher=lambda item: fetch_google_search_page(item, browser_path=args.browser_path))
                check = supplier_search_check(search)
                memory.put("source_checks", key("supplier-search-check", [search["search_id"], check]), check)
                destinations = capture_discovered_supplier_pages(memory, search)
                atomic_json(memory.root / "supplier_search_next.json", destinations)
                print("Supplier search: " + search["result"]["state"] + "; candidate pages: " + str(search["result"]["candidate_count"]))
                print("Saved bundles awaiting interpretation: " + str(len(destinations["pending_interpretation_paths"])))
                for path in destinations["pending_interpretation_paths"]:
                    print("Supplier interpretation input: " + str(path))
                print("No model call, paid collector, supplier contact or publication occurred.")
            elif args.action == "research-plan-trends":
                from product_radar.market_followups import prepare_trends_check
                result = prepare_trends_check(memory, concept_id=args.concept_id, term=args.term, as_of=args.as_of)
                print("Saved useful Trends check: " + result["id"])
                print("No browser, model or paid collector was started.")
            elif args.action == "research-capture-trends":
                from product_radar.market_followups import execute_trends_check
                from product_radar.trends_capture import capture
                identifier(args.check_id)
                request = load_json(safe_path(memory.root, "trends_checks", args.check_id) / "request.json")
                if not isinstance(request, dict) or request.get("id") != args.check_id:
                    raise SafetyError("TRENDS_CHECK_ID_MISMATCH")
                result = execute_trends_check(memory, request, root=ROOT, capture=capture, browser_path=args.browser_path)
                print(json.dumps(result, indent=2))
                write_summary(memory, candidate_summary(memory))
                print("Public browser check only. No Apify, model or publication call.")
            elif args.action in ("research-ready", "research-allocate"):
                from product_radar.report_selection import build_report_pool
                dates = None
                if args.action == "research-allocate":
                    if args.start is None or args.end is None:
                        raise SafetyError("ISSUE_DATE_WINDOW_REQUIRED")
                    dates = [row["date"] for row in calendar_plan(args.start, args.end, "0")["issues"]]
                    if not dates:
                        raise SafetyError("ISSUE_DATE_WINDOW_EMPTY")
                pool = build_report_pool(memory, fit_policy_id=load_policy().get("fit_policy_id", "fit-13-v1"))
                atomic_json(memory.root / "report_pool.json", pool)
                print("Products ready for report allocation: " + str(len(pool["eligible"])))
                print("Useful remaining checks: market " + str(len(pool["needed_market_work"]))
                      + "; supplier " + str(len(pool["needed_supplier_work"]))
                      + "; facts " + str(len(pool["needed_fact_work"])))
                if args.action == "research-allocate":
                    from product_radar.issue_allocation import allocate_issues
                    state = memory.snapshot()
                    drafts = allocate_issues(pool["eligible"], dates, state["published_history"], state["issue_reservations"], policy=load_policy())
                    atomic_json(memory.root / "report_drafts.json", {"pool_hash": digest(pool), "allocation": drafts})
                    complete = sum(issue["ready_for_review"] for issue in drafts["issues"])
                    print("Complete internal drafts: " + str(complete) + " of " + str(len(dates)))
                    print("Open: " + str(memory.root / "report_drafts.json"))
                print("No reservations, approval, upload or client publication occurred.")
            elif args.action == "research-validate":
                from product_radar.research_operations import plan_meta_validation
                refinement_args = dict(concept_id=args.concept_id, term=args.term, reason=args.reason) if args.concept_id is not None else {}
                plan = plan_meta_validation(memory, as_of=args.as_of or utc_now(),
                                            max_actions=3 if args.slots is None else args.slots,
                                            budget_usd=args.available_usd, **refinement_args)
                print("Product-specific follow-up plan: " + plan["id"])
                print("Selected checks: " + str(len(plan["actions"])))
                print("Open: " + str(memory.root / "next_searches.html"))
                print("No collector, model, reservation or publication was started.")
            elif args.action == "research-continue":
                from product_radar.research_batch import prepare_continuation
                identifier(args.plan_id)
                result = prepare_continuation(memory, args.plan_id, reason=args.reason)
                if result is None:
                    print("No blocked or deferred work remains that has never been submitted.")
                else:
                    print("Continuation plan: " + result["id"])
                    print("Never-submitted actions retained: " + str(len(result["actions"])))
                    print("Existing exact preparations and the original money allowance are preserved.")
                print("No collector, model, upload or publication was started.")
            elif args.action == "research-batch":
                from product_radar.research_batch import execute_plan
                identifier(args.plan_id)
                print("Working through the saved research batch; existing captures are reused before new collection.", flush=True)
                controller = MetaTrial(ROOT, journal, announce=lambda message: print(message, flush=True))
                result = execute_plan(memory, args.plan_id, prepare_action=prepare_planned_action,
                    controller_factory=lambda: controller,
                    runner=research_codex_runner(load_json(ROOT / "config/ai/codex_luna.json"), root=ROOT),
                    item_limit=args.item_limit or 10, action_limit=args.action_limit or 8)
                write_summary(memory, result["summary"])
                for action in result["actions"]:
                    print(action["family_id"] + ": " + action["status"] + ". " + action["reason"])
                print("Working candidates: " + str(len(result["summary"]["products"]))
                      + "; pending actions: " + str(result["pending_actions"])
                      + "; blocked actions: " + str(result.get("blocked_actions", 0)))
                if result["analysis_reason"]:
                    print("Saved analysis check: " + result["analysis_reason"])
                print("Open: " + str(memory.root / "candidate_summary.html"))
                print("Internal research only. No client report was approved or published.")
            elif args.action == "research-expand":
                from product_radar.catalogue import load_catalogue
                from product_radar.catalogue_expansion import run_expansion, effective_catalogue
                from product_radar.research_operations import write_catalogue_view
                result = run_expansion(memory, load_catalogue(), research_codex_runner(load_json(ROOT / "config/ai/codex_luna.json"), root=ROOT), limit=args.item_limit or 5)
                write_catalogue_view(memory.root, effective_catalogue(load_catalogue(), memory.snapshot()))
                print("Saved catalogue decisions: " + str(len(result["records"])) + "; pending: " + str(len(result["pending"])))
                if result.get("problem"):
                    print("Unresolved check: " + result["problem"])
                print("Saved research directions only. No collector search or client recommendation was created.")
            elif args.action == "research-capture-supplier":
                from product_radar.supplier_capture_store import capture_saved_supplier_bundle
                if args.supplier_input is None:
                    raise SafetyError("SUPPLIER_INPUT_REQUIRED")
                saved = capture_saved_supplier_bundle(memory, load_json(safe_path(ROOT / args.supplier_input), 16 * 1024))
                bundle = saved["bundle"]
                print("Supplier capture: " + bundle["state"] + "; reused: " + str(saved["reused"]))
                print("Saved: " + saved["path"])
                print("Usable pages: " + str(len(bundle["item"]["pages"]) if bundle["item"] else 0)
                      + "; recorded failures: " + str(len(bundle["failures"])))
                print("Known-lead capture only. No general search, model, supplier contact or publication.")
            elif args.action == "research-suppliers":
                from product_radar.suppliers import extract_supplier_terms
                if args.supplier_input is None:
                    raise SafetyError("SUPPLIER_INPUT_REQUIRED")
                input_path = safe_path(ROOT / args.supplier_input)
                payload = load_json(input_path, 8 * 1024 * 1024)
                source_dates = None
                if "bundle" in payload:
                    from product_radar.supplier_capture_store import load_saved_supplier_input
                    captured = load_saved_supplier_input(memory, input_path)
                    payload = {"items": [captured["item"]]}
                    source_dates = {captured["item"]["id"]: captured["page_dates"]}
                elif "items" not in payload:
                    from product_radar.research_operations import supplier_capture_inputs
                    payload = {"items": supplier_capture_inputs(memory, payload)}
                result = extract_supplier_terms(memory, payload["items"], research_codex_runner(load_json(ROOT / "config/ai/codex_luna.json"), root=ROOT),
                                                source_dates=source_dates)
                atomic_json(memory.root / "supplier_summary.json", result)
                print("Saved supplier interpretations: " + str(len(result["accepted"])) + "; unresolved: " + str(len(result["failures"])))
                print("These are cited website claims, not confirmed supplier agreements. No supplier was contacted.")
            elif args.action == "research-brand-review":
                from product_radar.research_flow import exclude_established_brand
                result = exclude_established_brand(memory, load_json(safe_path(ROOT / args.brand_review_input), 16 * 1024))
                write_summary(memory, candidate_summary(memory))
                print(json.dumps(result, indent=2))
                print("Source-backed operator correction saved. No model, collector or publication call.")
            elif args.action == "research-market":
                from product_radar.market_validation import review_marketplace
                result = review_marketplace(memory, research_codex_runner(load_json(ROOT / "config/ai/codex_luna.json"), root=ROOT), limit=args.item_limit or 5)
                print(json.dumps(result, indent=2))
                write_summary(memory, candidate_summary(memory))
                print("Only saved marketplace evidence was reviewed. No paid collector search or publication.")
            elif args.action in ("research-catalogue", "research-plan"):
                from product_radar.catalogue import load_catalogue
                from product_radar.catalogue_expansion import effective_catalogue
                from product_radar.research_operations import plan_research, write_catalogue_view
                catalogue = effective_catalogue(load_catalogue(), memory.snapshot())
                counts = write_catalogue_view(memory.root, catalogue)
                print("Research areas: " + str(counts["distinct_usable_families"]) + "; search phrases: " + str(counts["phrase_variants"]))
                if args.action == "research-plan":
                    plan = plan_research(memory, catalogue, as_of=args.as_of or utc_now(),
                                         slots=8 if args.slots is None else args.slots,
                                         budget_usd=args.available_usd, summary=candidate_summary(memory, as_of=args.as_of))
                    print("Saved plan: " + plan["id"] + "; discovery choices: " + str(len(plan["actions"])))
                    print("Open: " + str(memory.root / "next_searches.html"))
                else:
                    print("Open: " + str(memory.root / "catalogue.html"))
                print("No collector searches, model calls or financial changes.")
            elif args.action == "research-prepare":
                from product_radar.models import prepared_request
                from product_radar.research_operations import bind_prepared_action
                identifier(args.plan_id); identifier(args.action_id)
                plan = memory.snapshot()["batch_plans"].get(args.plan_id)
                action = next((a for a in plan["actions"] if a["action_id"] == args.action_id), None) if plan else None
                if action is None or action["source"] != "meta" or action["kind"] != "collect":
                    raise SafetyError("PLANNED_META_ACTION_REQUIRED")
                from product_radar.query_dispatch import find_existing_task
                existing = find_existing_task(memory, action)
                if existing is not None:
                    print("Reused saved search task: " + existing["task_id"] + "; attempt: " + existing["attempt_id"])
                    print(existing["reuse_reason"] + " No new preparation or launch.")
                    return 0
                request = prepare_planned_action(plan, action, production=args.production)
                task = bind_prepared_action(memory, args.plan_id, args.action_id, request)
                print("Prepared one saved search task: " + task["id"] + "; attempt: " + request.attempt_id)
                print("No launch or credit usage; repeating this action reuses this same preparation.")
            elif args.action == "research-import-trends":
                from product_radar.research_operations import import_trends_evidence
                identifier(args.concept_id)
                if args.csv is None or args.receipt is None:
                    raise SafetyError("TRENDS_CAPTURE_FILES_REQUIRED")
                csv_path = safe_path(ROOT / args.csv)
                raw = csv_path.read_bytes() if csv_path.stat().st_size <= 5 * 1024 * 1024 else b""
                if not raw:
                    raise SafetyError("TRENDS_CAPTURE_EMPTY_OR_OVERSIZED")
                metadata = load_json(safe_path(ROOT / args.receipt))
                receipt_id = "csv-" + digest([str(csv_path), metadata])[:40]
                result = import_trends_evidence(memory, concept_id=args.concept_id, acquisition_id=receipt_id,
                                               csv_bytes=raw, metadata=metadata)
                print("Saved Trends evidence: " + result["id"] + "; state: " + result["parsed"]["state"])
                print("No browser request, collector call or new product created.")
            elif args.action == "research-retry":
                attempt = prepare_retry(memory, args.job_id, args.reason)
                print("Prepared model retry " + str(attempt) + "; original failure retained. No model call made.")
            elif args.action == "research-import":
                result = import_saved_attempt(memory, args.attempt_id)
                from product_radar.research_operations import sync_query_feedback
                sync_query_feedback(memory)
                print("Saved research imported: " + str(result["new_observations"]) + " new observations; no downloads or model calls.")
            else:
                if args.action == "research-run":
                    print("Interpreting saved research with the approved model; no paid collector searches.", flush=True)
                    summary = run_flow(memory, research_codex_runner(load_json(ROOT / "config/ai/codex_luna.json"), root=ROOT), limit=args.item_limit or 10,
                                       acquisition_ids=args.acquisition_id, fit_policy_id=load_policy().get("fit_policy_id", "fit-13-v1"))
                    from product_radar.research_operations import sync_query_feedback
                    sync_query_feedback(memory)
                else:
                    summary = candidate_summary(memory)
                write_summary(memory, summary)
                print("Products in working summary: " + str(len(summary["products"]))
                      + "; unresolved issues: " + str(summary["unresolved_issues"])
                      + "; recovered earlier issues: " + str(summary["recovered_issues"]))
                print("Open: " + str(memory.root / "candidate_summary.html"))
                print("Internal research only. Supplier checks and client publication are not part of this action.")
            return 0
        registry = load_account_registry(ROOT)
        controller = MetaTrial(ROOT, journal, account_registry=registry, announce=lambda message: print(message, flush=True))
        pool_network = registry["pool_enabled"] and args.action in ("preflight", "run", "resume", "refresh-costs")
        with nullcontext() if pool_network or args.action == "bind-account" else journal.locked():
            if args.action == "adopt-production":
                binding = load_binding(ROOT / "config/local/apify_binding.json")
                journal.adopt_production_scope(binding.namespace, {"meta": "3.00", "amazon": "0.75"},
                    "Approved routine research from the existing US$3.75 earmark; Trends uses the local route.",
                    "owner-approved-continuous-execution-20261006")
                print("Existing production allocation assigned: Meta US$3.00; marketplace US$0.75. No new funds, reset, key read or provider launch.")
            elif args.action == "apply-policy":
                policy = load_policy()
                binding = load_binding(ROOT / "config/local/apify_binding.json")
                journal.apply_policy(binding.namespace, policy["initial_future_allocation_usd"], "owner-20261004-cap450")
                print("Spending policy recorded: approximately US$4.50 use and US$0.50 buffer; five-cent automatic tolerance.")
                print("US$3.75 reserved for future research; trial allowance is at most US$0.75 including earlier trial commitments.")
                print("Existing charges and holds preserved. No key read or network request.")
            elif args.action == "account-status":
                print(json.dumps(account_pool_status(ROOT, journal, registry), indent=2))
                print("No key was read, no provider request was made and no balance was reset.")
            elif args.action == "authorise-recovery":
                authorise_credential_recovery(ROOT, journal, registry, args.account_id, args.original_generation)
                print("Verified replacement credential authorised for the original account's saved reads only. No launch or money reset.")
            elif args.action == "status":
                summary = journal.summary()
                attempts = summary["attempts"]
                print("Saved provider attempts: " + str(len(attempts)))
                from decimal import Decimal
                actual = sum((Decimal(a["charge"]) for a in attempts.values() if a["charge"] is not None), Decimal(0))
                actual_reads = sum((Decimal(a["read_charge"]) for a in attempts.values() if a["read_charge"] is not None), Decimal(0))
                held_reads = sum((Decimal(a["read_hold"]) for a in attempts.values() if a["read_charge"] is None), Decimal(0))
                print("Latest recorded collection charges: US$" + str(actual) + "; known read charges: US$" + str(actual_reads))
                print("Unsettled read allowances held: US$" + str(held_reads) + ". Holds are not confirmed fees or a live Console balance.")
                print("No key was read and no network request was made.")
            elif args.action == "prepare":
                journal.state()  # refuse corruption, even during preparation
                if source != "meta":
                    result = prepare_source(source, args.case, source + "-" + uuid.uuid4().hex, utc_now())
                    destination = safe_path(runtime_path(ROOT, "data/benchmarks"), date.today().isoformat(), result["request"]["attempt_id"])
                    atomic_json(destination / "prepared.json", result)
                    print("Prepared bounded " + source + " request; NOT cleared for live execution.")
                    print("Saved: " + str(destination / "prepared.json"))
                    print("Collection cap: US$" + result["request"]["reserved_usd"] + "; reads reserved separately. No network request or credit use.")
                    return 0
                with CONFIG.open("rb") as handle:
                    raw = handle.read(32769)
                if len(raw) > 32768:
                    raise SafetyError("CONFIG_SIZE_LIMIT")
                config = json.loads(raw)
                if args.term is not None:
                    config["term"] = args.term
                if args.purpose is not None:
                    config["purpose"] = args.purpose
                result = prepare(config, "meta-" + uuid.uuid4().hex)
                destination = safe_path(runtime_path(ROOT, "data/benchmarks"), date.today().isoformat(), result["request"]["attempt_id"])
                atomic_json(destination / "prepared.json", result)
                print("Prepared bounded Meta " + config["purpose"] + " request; NOT cleared for live execution.")
                print("Saved: " + str(destination / "prepared.json"))
                print("Cap: US$0.03. No reservation, key read, network request or credit use.")
            elif args.action == "replay":
                if args.fixture is None:
                    raise SafetyError("FIXTURE_REQUIRED")
                identifier(args.fixture)
                path = safe_path(ROOT / "tests/fixtures/benchmarks") / (args.fixture + ".json")
                if path.is_symlink():
                    raise SafetyError("SYMLINK_PATH_REFUSED")
                with path.open("rb") as handle:
                    raw = handle.read(1048577)
                if len(raw) > 1048576:
                    raise SafetyError("FIXTURE_SIZE_LIMIT")
                fixture = json.loads(raw)
                if fixture.get("evidence_type") != "synthetic":
                    raise SafetyError("REPLAY_REQUIRES_SYNTHETIC_FIXTURE")
                result = map_meta_rows(fixture["rows"], purpose=fixture["purpose"], query_reference=args.fixture)
                if result != fixture["expected"]:
                    raise SafetyError("FIXTURE_EXPECTATION_MISMATCH")
                print("PASS: synthetic fixture replay. Not live provider evidence.")
            elif args.action == "bind":
                bind_account(ROOT, journal, ApifyTransport)
                print("Account access bound privately using the saved key. No Actor was launched.")
            elif args.action == "bind-account":
                bind_registered_account(ROOT, journal, args.account_id, ApifyTransport, registry=registry)
                print("One supplied account profile was verified and bound privately. Existing funds and all original jobs are preserved; no Actor was launched.")
            else:
                if args.action == "refresh-costs":
                    result = controller.refresh_costs()
                    print("Existing terminal jobs checked: " + str(result["checked_attempts"]) + ". No new jobs or product downloads.")
                elif args.action == "preflight":
                    result = controller.preflight(args.attempt_id)
                elif args.action == "review":
                    result = controller.review(args.attempt_id)
                elif args.action == "settle":
                    result = controller.settle(args.attempt_id)
                elif args.action == "settle-renewed-read":
                    result = controller.settle_renewed_reads(args.attempt_id, args.read_id)
                elif args.action == "resolve":
                    result = controller.resolve_non_submission(args.attempt_id)
                else:
                    result = controller.run(args.attempt_id, resume_only=args.action == "resume")
                verdict = result.get("verdict", "Preflight ready")
                if verdict == "QualityPass":
                    verdict = ("Data quality accepted; download-cost allowance remains reserved" if result.get("read_actual_usd") is None
                               else "Data quality accepted; download cost recorded, final accounting review pending")
                print("Result: " + verdict)
                if "code" in result:
                    print("Check: " + result["code"])
                if result.get("http_status") is not None:
                    print("Provider response: HTTP " + str(result["http_status"]))
                if result.get("phase"):
                    print("Stage: " + result["phase"])
                if result.get("next_action"):
                    print("Next: " + {"resume_same_attempt_or_resolve_recorded_issue_no_new_launch": "Resume this saved attempt after resolving the check above; do not launch another.",
                          "inspect_saved_sample_and_settle_read_costs": "Codex will inspect the saved sample and reconcile the read-cost provision.",
                          "remaining_bounded_meta_discovery_and_validation": "Continue the remaining capped Meta searches; this is not all of T1.",
                          "consult_current_benchmark_record": "Sample accepted. Follow the current benchmark record for the next stage; do not automatically repeat searches.",
                          "review_source_limits_and_independent_trials": "Source limitations recorded. Other trials may continue after their integrity and spending checks pass; do not automatically repeat this search.",
                          "resolve_recorded_sample_defects": "Resolve the defects recorded in this saved sample before another launch."}.get(result["next_action"], "Inspect the saved trial record."))
                if result.get("remote_may_run"):
                    print("The original remote job may still run. Do not start a new one.")
                print("Saved trial folder: " + str(journal.root / args.attempt_id if args.attempt_id else journal.root))
                if result.get("verdict") == "Blocked":
                    return 2
        return 0
    except SafetyError as error:
        print("Stopped safely: " + str(error))
        if str(error) == "ACCOUNT_HANDOVER_REQUIRED":
            print("Next: confirm the current account's identity, credit period and remaining balance. Reuse the saved key; no replacement is required.")
    except KeyboardInterrupt:
        print("Stopped locally. A submitted job may still run; resume the same attempt, never repost it.")
    except Exception:
        print("Stopped safely: LOCAL_OPERATION_FAILED. No private diagnostic was printed.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
