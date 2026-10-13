#!/usr/bin/env python3
"""Save genuine referenced product photos; never generate substitute pictures."""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.fit_policy import FIT_POLICY_ID
from product_radar.models import SafetyError, utc_now
from product_radar.product_images import capture_product_image
from product_radar.report_selection import build_report_pool
from product_radar.research_memory import ResearchMemory, key
from product_radar.runtime_storage import runtime_path
from product_radar.storage import Journal


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Fetch actual public photos already referenced by accepted relevant research.")
    parser.add_argument("--limit", type=int, help="Diagnostic limit; remaining products are not marked finished.")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("limit must be positive")
    memory = ResearchMemory(runtime_path(ROOT, "data/research/captured"), Journal(runtime_path(ROOT, "data/benchmarks/state/apify")))
    pool = build_report_pool(memory, fit_policy_id=FIT_POLICY_ID)
    facts = pool["facts_by_concept"]
    counts = Counter("has_candidates" if fact.get("image_candidates") else "no_relevant_photo_link"
                     for fact in facts.values())
    print(json.dumps({"products_examined": len(facts), "photo_inputs": dict(counts)}), flush=True)
    if not args.live:
        return 0
    outcomes = []
    for concept_id, fact in sorted(facts.items())[:args.limit]:
        candidates = sorted(fact.get("image_candidates", []), key=lambda row: row["relationship"] != "exact_product")
        result = {"concept_id": concept_id, "state": "not_attempted", "reason": "No relevant original photo link was captured.", "capture_ids": []}
        for candidate in candidates[:3]:
            try:
                receipt = capture_product_image(memory, concept_id, candidate)
                result.update(state=receipt["state"], reason=receipt["reason"])
                result["capture_ids"].append(receipt["reference"])
                if receipt["state"] == "usable":
                    break
            except (SafetyError, OSError) as error:
                result.update(state="waiting" if str(error) in {"RUNNER_ALREADY_ACTIVE", "SAVED_FILE_UNAVAILABLE", "PRODUCT_IMAGE_READERS_BUSY"} else "failed",
                              reason=str(error) if isinstance(error, SafetyError) else "Photo could not be saved locally.")
        outcomes.append(result)
        print(json.dumps({"photo_progress": result}), flush=True)
    waiting = sum(row["state"] == "waiting" for row in outcomes)
    unexamined = len(facts) - len(outcomes)
    receipt = {"kind": "product_photo_completion", "source": "product_photo", "route": "public_image_http",
               "at": utc_now(), "state": "work_remaining" if waiting or unexamined else "pass_finished", "outcomes": outcomes,
               "counts": dict(Counter(row["state"] for row in outcomes)),
               "remaining_products": unexamined, "waiting_products": waiting,
               "apify_usd": "0", "model_calls": 0}
    reference = key("photo-completion", receipt)
    memory.put("source_checks", reference, dict(receipt, reference=reference))
    print(json.dumps({"completion_receipt": reference, "counts": receipt["counts"],
                      "remaining_products": receipt["remaining_products"], "waiting_products": waiting}), flush=True)
    return 1 if waiting or unexamined else 0


if __name__ == "__main__":
    raise SystemExit(main())
