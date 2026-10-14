#!/usr/bin/env python3
"""Complete the full saved-product practical checklist; no source collection."""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.fit_review import prepare_practical_fit_reviews, run_fit_reviews
from product_radar.models import utc_now
from product_radar.spend_watch_ai import research_codex_runner
from product_radar.research_flow import candidate_summary, write_summary
from product_radar.research_memory import ResearchMemory, key
from product_radar.runtime_storage import runtime_path
from product_radar.storage import Journal, load_json

MODEL_ROUTE_FAILURES = {"AI_SUBSCRIPTION_LIMIT", "AI_RATE_LIMITED", "AI_MODEL_UNAVAILABLE",
                        "AI_CHATGPT_SIGN_IN_REQUIRED", "AI_CODEX_NOT_INSTALLED",
                        "AI_LOCAL_PERMISSION_DENIED", "AI_NETWORK_UNAVAILABLE"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-model", action="store_true", help="Interpret saved public product material with the selected Luna account.")
    args = parser.parse_args(argv)
    memory = ResearchMemory(runtime_path(ROOT, "data/research/captured"), Journal(runtime_path(ROOT, "data/benchmarks/state/apify")))
    prepared = prepare_practical_fit_reviews(memory)
    reviews = [row["review_id"] for row in prepared if row["status"] == "prepared"]
    print(json.dumps({"saved_products": len(prepared), "already_current": sum(row["reused"] for row in prepared),
                      "awaiting_full_review": len(reviews), "not_reviewable": sum(row["status"] == "not_reviewable" for row in prepared)}, indent=2), flush=True)
    if not args.live_model:
        print("Prepared only. No model, collector, purchase, supplier message or client publication.", flush=True)
        return 0
    runner = research_codex_runner(load_json(ROOT / "config/ai/codex_luna.json"), root=ROOT)
    outcomes = []
    stopped_reason = None
    for offset in range(0, len(reviews), 5):
        results = run_fit_reviews(memory, runner, reviews[offset:offset + 5], limit=5)
        outcomes.extend(results)
        counts = Counter(row["status"] for row in outcomes)
        print(json.dumps({"reviewed": len(outcomes), "remaining": len(reviews) - len(outcomes),
                          "results": dict(counts), "last_reasons": sorted({row["reason"] for row in results if "reason" in row})}), flush=True)
        # A common account/connection failure pauses this model route, not the
        # programme. Unstarted siblings remain queued and free source work can
        # continue. Per-product rejection does not stop healthy products.
        failures = [row.get("reason") for row in results if row.get("reason") in MODEL_ROUTE_FAILURES]
        if failures:
            stopped_reason = failures[0]
            break
    # Recover short local-save conflicts through the same saved jobs. The job
    # owner reuses completed responses and never relaunches an uncertain call.
    # One bounded reconciliation pass prevents either abandoned answers or a
    # repeat-until-passing model loop.
    if not stopped_reason:
        retry_ids = [row["review_id"] for row in outcomes
                     if row["status"] in ("pending", "needs_reconciliation")
                     or row.get("reason") in {"RUNNER_ALREADY_ACTIVE", "SAVED_FILE_UNAVAILABLE"}]
        for offset in range(0, len(retry_ids), 5):
            recovered = run_fit_reviews(memory, runner, retry_ids[offset:offset + 5], limit=5)
            by_review = {row["review_id"]: row for row in recovered}
            outcomes = [by_review.get(row["review_id"], row) for row in outcomes]
            print(json.dumps({"reconciliation": dict(Counter(row["status"] for row in recovered)),
                              "last_reasons": sorted({row["reason"] for row in recovered if "reason" in row})}), flush=True)
            failures = [row.get("reason") for row in recovered if row.get("reason") in MODEL_ROUTE_FAILURES]
            if failures:
                stopped_reason = failures[0]
                break
    at = utc_now()
    unresolved = [row["review_id"] for row in outcomes if row["status"] != "accepted"]
    unresolved.extend(reviews[len(outcomes):])
    receipt = {"kind": "practical_fit_completion", "source": "fit", "route": "saved_public_interpretation",
               "at": at, "state": "model_route_waiting" if stopped_reason else "work_remaining" if unresolved else "complete",
               "prepared": prepared, "outcomes": outcomes, "remaining_review_ids": unresolved,
               "reason": stopped_reason, "source_collection_attempted": False}
    reference = key("practical-fit-completion", receipt)
    memory.put("source_checks", reference, dict(receipt, reference=reference))
    write_summary(memory, candidate_summary(memory))
    print(json.dumps({"completion_receipt": reference, "outcomes": dict(Counter(row["status"] for row in outcomes)),
                      "model_route_reason": stopped_reason, "remaining": len(unresolved)}, indent=2), flush=True)
    return 1 if stopped_reason or unresolved else 0


if __name__ == "__main__":
    raise SystemExit(main())
