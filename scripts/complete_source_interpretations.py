#!/usr/bin/env python3
"""Finish saved source matching without recollection or client publication."""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.models import SafetyError, utc_now
from product_radar.research_ai import CodexRunner
from product_radar.research_memory import ResearchMemory, key
from product_radar.source_interpretation import _questions, review_source_results
from product_radar.runtime_storage import runtime_path
from product_radar.storage import Journal, load_json


MODEL_ROUTE_FAILURES = {"AI_SUBSCRIPTION_LIMIT", "AI_RATE_LIMITED", "AI_MODEL_UNAVAILABLE",
                       "AI_CHATGPT_SIGN_IN_REQUIRED", "AI_CODEX_NOT_INSTALLED", "AI_LOCAL_PERMISSION_DENIED",
                       "AI_NETWORK_UNAVAILABLE", "AI_CLI_CONFIGURATION_REJECTED"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", help="Interpret acquisitions from this existing finite source run.")
    parser.add_argument("--live-model", action="store_true", help="Use the approved signed-in Luna account for saved public material only.")
    args = parser.parse_args(argv)
    memory = ResearchMemory(runtime_path(ROOT, "data/research/captured"), Journal(runtime_path(ROOT, "data/benchmarks/state/apify")))
    questions = _questions(memory.snapshot(), run_id=args.run_id)
    print(json.dumps({"pending_product_source_questions": len(questions),
                      "unreviewed_returned_rows": sum(len(row["unknown_ids"]) for row in questions)}), flush=True)
    runner = CodexRunner(load_json(ROOT / "config/ai/codex_luna.json")) if args.live_model else None
    skip_targets, outcomes, reason, passes = set(), [], None, 0
    # Each successful pass accepts at least one supplied row or settles a
    # question. A failed target is deferred within this pass, never allowed to
    # starve other products/sources. Uncertain saved jobs cannot be relaunched.
    maximum = 1 + len(questions) + sum(len(row["unknown_ids"]) for row in questions)
    for _ in range(maximum):
        result = review_source_results(memory, runner, run_id=args.run_id, skip_targets=skip_targets)
        passes += 1; outcomes.extend(result["outcomes"])
        print(json.dumps({"interpretation_progress": {"pass": passes,
            "outcomes": dict(Counter(row["state"] for row in result["outcomes"])),
            "reused_bindings": len(result["reused"]), "remaining_questions": result["remaining_questions"],
            "reasons": sorted({row["reason"] for row in result["outcomes"] if "reason" in row})}}), flush=True)
        for row in result["outcomes"]:
            if row["state"] == "waiting":
                skip_targets.add((row["task_id"], row["concept_id"]))
                if row["reason"] in MODEL_ROUTE_FAILURES:
                    reason = row["reason"]
        if result["remaining_questions"] == 0 or reason:
            break
        if not result["outcomes"] and not result["reused"]:
            reason = "Saved targets need recorded recovery; other programme lanes can continue."
            break
    remaining = _questions(memory.snapshot(), run_id=args.run_id)
    receipt = {"kind": "source_interpretation_completion", "source": "source_interpretation",
        "route": "saved_public_interpretation", "run_id": args.run_id, "at": utc_now(),
        "state": "complete" if not remaining else "work_remaining", "reason": reason,
        "passes": passes, "outcomes": outcomes,
        "remaining_targets": [{"task_id": row["task_id"], "concept_id": row["concept_id"],
                                "unknown_rows": len(row["unknown_ids"])} for row in remaining],
        "source_calls": 0, "client_publication_attempted": False}
    reference = key("source-interpretation-completion", receipt)
    memory.put("source_checks", reference, dict(receipt, reference=reference))
    print(json.dumps({"completion_receipt": reference, "remaining_questions": len(remaining), "reason": reason}), flush=True)
    return 1 if remaining else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SafetyError as error:
        print(json.dumps({"state": "needs_recovery", "reason": str(error), "client_publication_attempted": False}), flush=True)
        raise SystemExit(1)
