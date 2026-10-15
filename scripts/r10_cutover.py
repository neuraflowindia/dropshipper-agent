#!/usr/bin/env python3
"""Plan R10 cutover helper (W13), run by the lead after research is stopped
and the store is backed up.

  check  Read only: report stock frozen under the old Meta rule, old-identity
         Google Trends waits, saved state of retired routes and missing
         configuration files.
  apply  Switch on the Apify account list, release the old Trends waits and
         expire (with a recorded reason) the stock frozen under the old Meta
         rule or ranked by the retired priority tiers, so it is judged again.
         Repeating it changes nothing new.
  score-preview --store <copy>
         On a COPY of a store (never the configured runtime store, a store named
         in PRODUCT_RADAR_LIVE_STORE, or a store whose own records point into
         it): work out the crowding check from the copy's saved pages and the
         opportunity score of every accepted type; print the counts, the top 15
         types, the types still waiting for the current relevance review (with a
         rounds estimate) and a clearly labelled provisional count that treats
         saved older relevance answers as current. It writes only into the copy.

No provider, website or model is called.
"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.models import SafetyError, utc_now
from product_radar.r10_cutover import apply_cutover, cutover_check, preview_store_refused, score_preview
from product_radar.research_memory import ResearchMemory
from product_radar.runtime_storage import runtime_path
from product_radar.storage import Journal


def main(argv=None, *, root=ROOT, memory=None, enable_accounts=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("check", "apply", "score-preview"))
    parser.add_argument("--store", help="score-preview only: the copied store folder (holds data/research/captured)")
    args = parser.parse_args(argv)
    root = Path(root)
    if (args.action == "score-preview") != (args.store is not None):
        parser.error("--store is required by score-preview and used by nothing else")
    at = utc_now()
    if args.action == "score-preview":
        refused = preview_store_refused(args.store, root)
        if refused:
            print(json.dumps({"state": "stopped", "reason": refused}))
            return 1
        store = Path(args.store).resolve()
        memory = memory or ResearchMemory(store.joinpath("data", "research", "captured"),
                                          Journal(store.joinpath("data", "benchmarks", "state", "apify")))
        try:
            print(json.dumps(score_preview(memory, store=store, as_of=at), indent=2))
            return 0
        except SafetyError as error:
            print(json.dumps({"state": "stopped", "reason": str(error)}))
            return 1
    memory = memory or ResearchMemory(runtime_path(root, "data/research/captured"),
                                      Journal(runtime_path(root, "data/benchmarks/state/apify")))
    try:
        if args.action == "check":
            print(json.dumps(cutover_check(memory, root=root, as_of=at), indent=2))
            return 0
        result, ok = apply_cutover(memory, root=root, as_of=at, enable_accounts=enable_accounts)
        print(json.dumps(result, indent=2))
        return 0 if ok else 1
    except SafetyError as error:
        print(json.dumps({"state": "stopped", "reason": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
