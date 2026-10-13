#!/usr/bin/env python3
"""Connect one product-family question to all three local catalogue platforms.

Only --live uses the already-selected provider's existing credits. This never
buys a pack, places an order, changes billing, uses Apify or publishes a report.
Saved captures are replayed on the same run ID without a fresh provider search.
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from check_quickcommerce_connection import load_key
from product_radar.models import SafetyError
from product_radar.product_queries import ensure_query_profiles, product_query_terms
from product_radar.quickcommerce import PLATFORMS, QuickCommerceClient, adapter
from product_radar.research_memory import ResearchMemory
from product_radar.source_coverage import execute_source_run, prepare_source_tasks
from product_radar.runtime_storage import runtime_path
from product_radar.storage import Journal

LOCATION = {"name": "Mumbai reference point", "lat": "19.0760", "lon": "72.8777"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--concept-id", required=True)
    parser.add_argument("--query", help="An explicitly chosen short family phrase, not a seller's complete title.")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    memory = ResearchMemory(runtime_path(ROOT, "data/research/captured"), Journal(runtime_path(ROOT, "data/benchmarks/state/apify")))
    if args.query is None:
        ensure_query_profiles(memory, concept_ids=[args.concept_id])
    questions = []
    for route in PLATFORMS:
        query = args.query or product_query_terms(memory, args.concept_id, source=route, limit=1)[0]["text"]
        questions.append({"route": route, "request": {"query": query, "location": LOCATION},
                          "concept_ids": [args.concept_id]})
    prepared = prepare_source_tasks(memory, questions, run_id=args.run_id)
    print(json.dumps(prepared), flush=True)
    if not args.live:
        print("Prepared only. No platform search or account change was made.", flush=True)
        return 0
    client = QuickCommerceClient(load_key())
    collect = adapter(memory, client)
    handlers = {route: {"handler": collect, "reconcile": collect} for route in PLATFORMS}
    result = execute_source_run(memory, args.run_id, handlers=handlers, timeout_seconds=45)
    tasks = memory.snapshot()["query_tasks"]
    rows = [{"route": tasks[row["task_id"]]["route"], "state": row["state"],
             "actual_attempt": row.get("actual_attempt", False), "reason": row.get("reason") or tasks[row["task_id"]].get("reason"),
             "target_state": tasks[row["task_id"]].get("target_results", {}).get(args.concept_id, {}).get("state")}
            for row in result["results"]]
    print(json.dumps({"results": rows, "counts": dict(Counter(row["state"] for row in rows)),
                      "location": LOCATION, "apify_usd": "0", "purchase_made": False,
                      "product_matching_still_required": True}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError):
        print("Saved QuickCommerce connection could not be read; no credentials were displayed.")
        raise SystemExit(2)
    except SafetyError as error:
        print(str(error))
        raise SystemExit(2)
