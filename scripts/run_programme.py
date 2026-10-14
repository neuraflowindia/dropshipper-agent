#!/usr/bin/env python3
"""Show or continue one saved programme; no approval, outreach or publication.

Default is read-only. --prepare freezes a current pool without source/model
calls. --execute advances the saved pool until only waiting work remains.
--tick is the recurring-task entry: reuse unfinished work, do not start parallel
programmes or accumulate separate paid allowances for missed research weeks.
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from product_radar.models import SafetyError, timestamp, utc_now
from product_radar.programme import (advance_programme, advance_ready_programme_turn, due_programme, load_programme,
    prepare_programme, programme_progress, programme_owner_summary, programme_service_state,
    run_programme_housekeeping, set_programme_service_state)
from product_radar.research_memory import ResearchMemory
from product_radar.runtime_storage import runtime_path
from product_radar.source_coverage import installed_source_handlers
from product_radar.storage import Journal, load_json


def compact_progress(row):
    """Counts/states only: original research stays in its saved records."""
    compact = {name: row[name] for name in ("programme_id", "review_scope_id", "stage", "cycle", "phase_state",
               "frozen_products", "maximum_group_size", "role", "supplied_items", "error_type") if name in row}
    for name in ("reason",):
        if isinstance(row.get(name), str):
            compact[name] = row[name][:160]
    source = row.get("source_progress")
    if isinstance(source, dict):
        compact["source"] = {name: source[name] for name in ("task_id", "route", "state", "dispatch_started") if name in source}
        if isinstance(source.get("reason"), str):
            compact["source"]["reason"] = source["reason"][:160]
    result = row.get("result")
    if isinstance(result, dict):
        compact["counts"] = {name: value for name, value in result.items() if type(value) is int}
        for name in ("records", "reused", "pending", "job_ids", "outcomes", "accepted", "failures",
                     "selected_capture_ids", "skipped_captures", "references", "contract_repairs", "local_failures",
                     "recovered_job_ids", "new_concept_ids", "deferred", "pending_actions"):
            if isinstance(result.get(name), (list, dict)):
                compact["counts"][name] = len(result[name])
        for name in ("state", "status", "tick_state"):
            if isinstance(result.get(name), str):
                compact[name] = result[name][:120]
        if isinstance(result.get("reason"), str):
            compact["reason"] = result["reason"][:160]
        states = Counter(item.get("state", item.get("status", "unspecified")) for item in result.get("outcomes", [])
                         if isinstance(item, dict))
        if states:
            compact["outcome_states"] = dict(states)
        for name in ("counts", "selected_fit_states"):
            if isinstance(result.get(name), dict) and all(isinstance(key, str) and type(value) is int
                                                         for key, value in result[name].items()):
                compact[name] = result[name]
    if isinstance(row.get("control_save"), dict):
        compact["control_save"] = {name: row["control_save"][name] for name in ("saved", "reason") if name in row["control_save"]}
    return compact


def _main(argv=None, *, memory=None, runner=None, handlers=None, steps=None, discovery_step=None,
         private_staging=None, backup_step=None, platform_step=None, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--programme-id")
    parser.add_argument("--activity", action="store_true", help="Read the small current-activity record; no research history or provider calls.")
    parser.add_argument("--meter", action="store_true", help="Read the owner meter since the saved fresh start: time, money, research counts, reports, errors and waits. No provider, model or research write.")
    parser.add_argument("--source-run-id", help="Resume the existing all-source acquisition identity.")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--tick", action="store_true")
    parser.add_argument("--cooperative-turns", action="store_true",
        help="Opt in to saved finite turns and fair due-week rotation; installed legacy commands stay unchanged.")
    control = parser.add_mutually_exclusive_group()
    control.add_argument("--pause", action="store_true", help="Pause new research stages; preserve the current run and in-flight work.")
    control.add_argument("--resume", action="store_true", help="Allow the saved programme to continue; never reset its funds or attempts.")
    parser.add_argument("--live-model", action="store_true", help="Only the already-approved signed-in account and saved public inputs.")
    parser.add_argument("--funded-quickcommerce-fallback", action="store_true", help="Existing credits only; never buy or upgrade.")
    parser.add_argument("--max-cycles", type=int, help="Optional execution slice; unfinished work stays saved.")
    parser.add_argument("--source-actions", type=int,
        help="Shared source-callback limit per uninterrupted acquisition pass across the original and follow-on pools "
             "(default: source_reads in config/research/round_limits.json).")
    parser.add_argument("--task-xml", action="store_true", help="Print an uninstalled Windows recurring-task definition.")
    parser.add_argument("--compare-task-xml", action="store_true", help="Read-only semantic comparison of two definitions supplied privately on stdin.")
    parser.add_argument("--plan-cooperative-upgrade", action="store_true", help="Print an exact read-only task upgrade plan supplied privately on stdin; never install or run it.")
    parser.add_argument("--distribution")
    parser.add_argument("--windows-user", help="Windows owner SID for the optional task definition.")
    args = parser.parse_args(argv)
    if args.activity:
        if any((args.execute, args.prepare, args.tick, args.pause, args.resume, args.task_xml,
                args.compare_task_xml, args.plan_cooperative_upgrade, args.cooperative_turns)):
            parser.error("--activity is read-only")
        from product_radar.operations import read_operation_status
        print(json.dumps(read_operation_status(root), indent=2), flush=True)
        return 0
    if args.meter:
        if any((args.activity, args.execute, args.prepare, args.tick, args.pause, args.resume, args.task_xml,
                args.compare_task_xml, args.plan_cooperative_upgrade, args.cooperative_turns, args.programme_id)):
            parser.error("--meter is read-only")
        from product_radar.programme import programme_meter
        root = Path(root)
        try:
            memory = memory or ResearchMemory(runtime_path(root, "data/research/captured"),
                                              Journal(runtime_path(root, "data/benchmarks/state/apify")))
            print(json.dumps(programme_meter(memory, root=root), indent=2), flush=True)
            return 0
        except (SafetyError, OSError) as error:
            print(json.dumps({"state": "unreadable", "reason": str(error) if isinstance(error, SafetyError)
                              else "METER_LOCAL_FILE_FAILED"}), flush=True)
            return 1
    if args.cooperative_turns and (not (args.execute or args.task_xml) or args.compare_task_xml or args.plan_cooperative_upgrade or args.pause or args.resume):
        parser.error("--cooperative-turns requires execution or an uninstalled task definition")
    if args.source_actions is None:
        from product_radar.round_limits import round_limits
        try:
            args.source_actions = round_limits()["source_reads"]
        except (SafetyError, OSError) as error:
            parser.error("round limits unreadable: " + (str(error) if isinstance(error, SafetyError) else "ROUND_LIMITS_UNREADABLE"))
    if args.cooperative_turns and args.source_actions > 1000:
        parser.error("cooperative source actions must not exceed 1000 per saved turn")
    if args.compare_task_xml or args.plan_cooperative_upgrade:
        if args.compare_task_xml and args.plan_cooperative_upgrade or args.prepare or args.execute or args.tick or args.task_xml or args.pause or args.resume:
            parser.error("--compare-task-xml is read-only")
        from product_radar.programme_schedule import compare_native_task, plan_cooperative_task_upgrade
        try:
            supplied = sys.stdin.read(512 * 1024 + 1)
            payload = json.loads(supplied)
            if len(supplied.encode("utf-8")) > 512 * 1024 or not isinstance(payload, dict) or set(payload) != {"actual_xml", "expected_xml"}:
                raise SafetyError("PROGRAMME_NATIVE_TASK_XML_INVALID")
            operation = plan_cooperative_task_upgrade if args.plan_cooperative_upgrade else compare_native_task
            print(json.dumps(operation(payload["actual_xml"], payload["expected_xml"])), flush=True)
            return 0
        except (SafetyError, ValueError, TypeError):
            print(json.dumps({"matches": False, "differences": ["task_definition_invalid"]}), flush=True)
            return 1
    if (args.pause or args.resume) and (args.prepare or args.execute or args.tick or args.task_xml or args.programme_id or args.source_run_id):
        parser.error("--pause/--resume only change the local research switch")
    if args.task_xml:
        if args.prepare or args.execute or args.tick:
            parser.error("--task-xml only prints a definition; it cannot execute work")
        from product_radar.programme_schedule import native_task_xml
        print(native_task_xml(str(root), args.distribution, args.windows_user,
              live_model=args.live_model, funded_quickcommerce_fallback=args.funded_quickcommerce_fallback,
              cooperative_turns=args.cooperative_turns))
        return 0
    if args.tick and (not args.execute or args.programme_id or args.prepare or args.source_run_id):
        parser.error("--tick requires --execute and selects its own saved programme")
    if not args.tick and not args.programme_id and not (args.pause or args.resume):
        parser.error("--programme-id is required outside an automatic tick")
    if args.source_run_id and not args.prepare:
        parser.error("--source-run-id is only needed when the programme is first prepared")
    if (args.max_cycles is not None and args.max_cycles < 1) or args.source_actions < 1:
        parser.error("execution slices must be positive")
    root = Path(root)
    memory = memory or ResearchMemory(runtime_path(root, "data/research/captured"),
                                      Journal(runtime_path(root, "data/benchmarks/state/apify")))
    try:
        if args.pause or args.resume:
            print(json.dumps(set_programme_service_state(memory, enabled=args.resume)), flush=True)
            return 0
        if args.tick and not programme_service_state(memory)["enabled"]:
            print(json.dumps({"state": "owner_paused", "new_research_started": False}), flush=True)
            return 0
        checked_state = None
        if args.tick and args.cooperative_turns:
            record = None  # Selection and admission happen together under the programme writer.
        elif args.tick:
            record = due_programme(memory)
        elif args.prepare:
            record = prepare_programme(memory, args.programme_id, source_run_id=args.source_run_id)
        else:
            checked_state = memory.snapshot() if not args.execute else None
            record = load_programme(memory, args.programme_id, state=checked_state)
        programme_id = record["programme_id"] if record is not None else None
        if not args.execute:
            checked_state = memory.snapshot() if checked_state is None else checked_state
            checked_at = utc_now()
            progress = programme_progress(memory, programme_id, handlers=handlers, record=record,
                                          state=checked_state, as_of=checked_at)
            progress["owner_summary"] = programme_owner_summary(memory, record, root=root,
                state=checked_state, as_of=checked_at)
            print(json.dumps(progress, indent=2), flush=True)
            return 0
        # The outer kernel lock is authoritative. This CLI never unlocks a
        # sibling programme by PID/age and never starts a second paid route.
        if backup_step is None:
            from product_radar.operational_backup_schedule import create_backup_housekeeping_step
            backup_step = create_backup_housekeeping_step(root)
        if args.tick and not args.cooperative_turns and record.get("next_retry_at") and timestamp(record["next_retry_at"]) > timestamp(utc_now()):
            housekeeping = run_programme_housekeeping(memory, programme_id, root=root, backup_step=backup_step)
            print(json.dumps({"programme_id": programme_id, "state": "waiting_for_saved_retry",
                              "next_retry_at": record["next_retry_at"],
                              "local_backup": housekeeping.get("local_backup")}), flush=True)
            return 0
        setup_failures = {}
        if runner is None and args.live_model:
            try:
                from product_radar.spend_watch_ai import research_codex_runner
                # Usage windows are flagged only; the money guards keep paid Codex credits unspent.
                runner = research_codex_runner(load_json(root / "config/ai/codex_luna.json"), root=root,
                                               stop_on_interrupt=True)
            except (SafetyError, OSError) as error:
                setup_failures["model"] = str(error) if isinstance(error, SafetyError) else "AI_CONFIGURATION_UNREADABLE"
        installed_sources = handlers is None
        if handlers is None:
            client = None
            if args.funded_quickcommerce_fallback and not args.cooperative_turns:
                # Paid checks run only in the cooperative turns, where every
                # question passes admission and the credit gate before a call.
                setup_failures["funded_quickcommerce"] = "QUICKCOMMERCE_COOPERATIVE_TURNS_REQUIRED"
            elif args.funded_quickcommerce_fallback:
                try:
                    from product_radar.quickcommerce import QuickCommerceClient, load_key
                    client = QuickCommerceClient(load_key())
                except (SafetyError, OSError) as error:
                    setup_failures["funded_quickcommerce"] = str(error) if isinstance(error, SafetyError) else "QUICKCOMMERCE_KEY_UNREADABLE"
            shared_options = {"marketplace_options": {"shared_acquisitions": True},
                              "meta_options": {"shared_acquisitions": True},
                              "trends_options": {"shared_acquisitions": True},
                              "supplier_options": {"respect_access_waits": True, "shared_acquisitions": True}} if args.cooperative_turns else {}
            # Paid quick-commerce checks pass admission and the credit gate; the
            # credit records live with this project's spend watch.
            handlers = installed_source_handlers(memory, quickcommerce_client=client,
                                                 quickcommerce_options={"spend_root": root}, **shared_options)
        if discovery_step is None:
            try:
                from product_radar.catalogue_discovery import create_catalogue_discovery_step
                from product_radar.catalogue_seed import load_seed_inventory
                # The repository's grounded branch inventory, not a replacement
                # product pool. Each store prepares its own seed manifest from
                # it through the ordinary staging route the first time the lane
                # runs; a missing source page is a named seed-lane wait inside
                # the callback and cannot stop independent current-product review.
                seed_options = {}
                try:
                    seed_options = {"seed_inventory": load_seed_inventory(), "seed_source_root": root}
                except (SafetyError, OSError) as error:
                    setup_failures["catalogue_seed"] = str(error) if isinstance(error, SafetyError) else "CATALOGUE_INVENTORY_UNREADABLE"
                # W8: the wider-discovery lane follows the heading searches of each turn.
                from product_radar.wider_discovery import wider_step
                discovery_options = ({"shared_marketplace": True, "renewable_waves": True, "wider_step": wider_step}
                                     if args.cooperative_turns else {})
                discovery_step = create_catalogue_discovery_step(**seed_options, **discovery_options)
            except (SafetyError, OSError) as error:
                setup_failures["catalogue_discovery"] = str(error) if isinstance(error, SafetyError) else "DISCOVERY_CATALOGUE_UNREADABLE"
        if platform_step is None and installed_sources:
            # With the installed sources, every round also advances the four
            # dropshipping platforms (W4) in their own lane, whatever discovery does.
            from product_radar.platform_roles import platform_step
        if private_staging is None:
            try:
                from product_radar.draft_automation import load_draft_automation_storage, private_staging_callback
                private_staging = private_staging_callback(root, load_draft_automation_storage(root))
            except (SafetyError, OSError) as error:
                setup_failures["private_staging"] = str(error) if isinstance(error, SafetyError) else "PORTAL_DRAFT_SERVICE_CONFIGURATION_UNREADABLE"
        try:
            from render_live_pdf import render, inspect_pdf
        except ImportError:
            render = inspect_pdf = None
            setup_failures["native_renderer"] = "REPORT_NATIVE_RENDERER_NOT_INSTALLED"
        if setup_failures:
            print(json.dumps({"lane_setup_waiting": setup_failures,
                              "healthy_lanes_continue": True}), flush=True)
        if args.cooperative_turns:
            from product_radar.round_limits import round_policy
            policy = round_policy(args.source_actions)  # the configured AI-call allowance, too
            completed = 0
            while args.max_cycles is None or completed < args.max_cycles:
                result = advance_ready_programme_turn(memory, programme_id=programme_id,
                    root=root, runner=runner, handlers=handlers, source_limit=args.source_actions, steps=steps,
                    turn_policy=policy, discovery_step=discovery_step, platform_step=platform_step,
                    private_staging=private_staging,
                    backup_step=backup_step, render_pdf=render, inspect_pdf=inspect_pdf, setup_failures=setup_failures,
                    on_progress=lambda row: print(json.dumps({"programme_progress": compact_progress(row)}), flush=True))
                if result.get("round_completed"):
                    completed += 1
                    print(json.dumps({"cooperative_turn": {"programme_id": result["programme_id"],
                        "completed_rounds": completed, "work_turn": result["work_turn"],
                        "meaningful_progress": result["meaningful_progress"], "next_action": result["next_action"]}}), flush=True)
                if not result.get("round_completed") or result.get("tick_state") == "owner_paused":
                    if result.get("state") == "all_waiting" and result.get("programme_id"):
                        housekeeping = run_programme_housekeeping(memory, result["programme_id"], root=root, backup_step=backup_step)
                        result["local_backup"] = housekeeping.get("local_backup")
                    break
            print(json.dumps(dict(result, cooperative_turns=True, completed_rounds_this_invocation=completed), indent=2), flush=True)
            return 0
        result = advance_programme(memory, programme_id, root=root, runner=runner, handlers=handlers,
            max_cycles=args.max_cycles, source_limit=args.source_actions, steps=steps,
            discovery_step=discovery_step, platform_step=platform_step,
            private_staging=private_staging,
            backup_step=backup_step,
            render_pdf=render, inspect_pdf=inspect_pdf,
            setup_failures=setup_failures,
            on_progress=lambda row: print(json.dumps({"programme_progress": compact_progress(row)}), flush=True))
        print(json.dumps(result, indent=2), flush=True)
        return 0
    except (SafetyError, OSError) as error:
        from product_radar.operations import record_error
        record_error("programme_stopped_for_recovery", error, state="waiting")
        code = str(error) if isinstance(error, SafetyError) else "PROGRAMME_LOCAL_FILE_FAILED"
        print(json.dumps({"state": "waiting_for_recovery", "reason": code,
                          "client_publication_attempted": False}), flush=True)
        # A recurring task that loses the leader race is healthy. It will
        # resume the same work later, rather than launch a competing programme.
        return 0 if code == "RUNNER_ALREADY_ACTIVE" else 1


def main(argv=None, **kwargs):
    actual = sys.argv[1:] if argv is None else argv
    if not any(value.startswith("--exec") for value in actual):
        return _main(argv, **kwargs)
    from product_radar.operations import operation_run, record_error
    with operation_run(kwargs.get("root", ROOT)) as recorder:
        try:
            result = _main(argv, **kwargs)
        except (KeyboardInterrupt, SystemExit) as error:
            recorder.emit("invocation_interrupted", state="interrupted", error_type=type(error).__name__)
            raise
        except BaseException as error:
            record_error("invocation_failed", error, state="failed")
            raise
        recorder.emit("invocation_finished", exit_code=result)
        return result


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        # A desktop Stop saves owner intent separately. An implementation
        # reload or another signal is not proof that the owner chose Stop.
        # Preserve original dispatch intents for normal recovery.
        print(json.dumps({"state": "stopped", "reason": "run_interrupted",
                          "client_publication_attempted": False}), flush=True)
        raise SystemExit(130)
