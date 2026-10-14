#!/usr/bin/env python3
"""Show, prepare or execute the finite all-product source work.

Default is a read-only coverage summary. --prepare creates public question
records, not searches. --execute runs explicitly installed public handlers;
neither command contacts suppliers, orders products or publishes client reports.
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.models import SafetyError
from product_radar.research_memory import ResearchMemory
from product_radar.source_coverage import (continue_source_run, execute_source_run, installed_source_handlers,
    prepare_all_source_tasks, product_source_request_builder, source_coverage, source_run_progress)
from product_radar.runtime_storage import runtime_path
from product_radar.storage import Journal, atomic_json


def main(argv=None, *, memory=None, handlers=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", help="Existing finite week/envelope identity; does not create money.")
    parser.add_argument("--prepare", action="store_true", help="Prepare all active products against agreed routes without collecting.")
    parser.add_argument("--execute", action="store_true", help="Execute due registered public acquisitions in that finite run.")
    parser.add_argument("--continue", dest="continue_work", action="store_true", help="Prepare all current products and keep newly discovered page reads moving until only waiting or interpretation work remains.")
    parser.add_argument("--action-limit", type=int, help="Calls in this pass; remaining tasks stay queued, not discarded.")
    parser.add_argument("--output", type=Path, help="Optional derived coverage JSON; not source proof or a client report.")
    args = parser.parse_args(argv)
    if (args.prepare or args.execute) and not args.run_id:
        parser.error("--run-id is required for preparing or executing work")
    if args.continue_work and not args.execute:
        parser.error("--continue requires --execute")
    memory = memory or ResearchMemory(runtime_path(ROOT, "data/research/captured"), Journal(runtime_path(ROOT, "data/benchmarks/state/apify")))
    try:
        if handlers is None:
            # No paid quick-commerce client here: those checks run only in the
            # programme's cooperative turns, behind admission and the credit gate.
            handlers = installed_source_handlers(memory)
        if args.prepare:
            prepared = prepare_all_source_tasks(memory, run_id=args.run_id,
                request_builder=product_source_request_builder(run_id=args.run_id))
            print(json.dumps({key: value for key, value in prepared.items() if key not in {"task_ids", "pending_preparation"}}, indent=2), flush=True)
            print(json.dumps({"pending_preparation_reasons": dict(Counter(row["reason"] for row in prepared["pending_preparation"]))}), flush=True)
        if args.execute:
            def progress(row):
                print(json.dumps({"source_progress": row}), flush=True)
            if args.continue_work:
                result = continue_source_run(memory, args.run_id,
                    request_builder=product_source_request_builder(run_id=args.run_id), handlers=handlers,
                    action_limit=args.action_limit, on_progress=progress,
                    on_pass=lambda status: print(json.dumps({"source_run_progress": status}), flush=True))
            else:
                result = execute_source_run(memory, args.run_id, handlers=handlers,
                                            action_limit=args.action_limit, on_progress=progress)
                print(json.dumps({"source_run_progress": source_run_progress(memory, args.run_id)}), flush=True)
            print(json.dumps({key: value for key, value in result.items() if key != "results"}, indent=2), flush=True)
        coverage = source_coverage(memory, handlers=handlers)
        if args.output:
            atomic_json(args.output, coverage)
        print(json.dumps({key: value for key, value in coverage.items() if key != "rows"}, indent=2), flush=True)
        return 0
    except (SafetyError, OSError, UnicodeError) as error:
        reason = str(error) if isinstance(error, SafetyError) else "SOURCE_LOCAL_FILE_READ_FAILED"
        print(json.dumps({"status": "needs_recovery", "reason": reason,
                          "client_publication_attempted": False}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
