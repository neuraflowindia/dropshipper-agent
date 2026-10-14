#!/usr/bin/env python3
"""Plan R10 cutover helper (W13), run by the lead after research is stopped
and the store is backed up.

  check  Read only: report stock frozen under the old Meta rule, old-identity
         Google Trends waits, saved state of retired routes and missing
         configuration files.
  apply  Switch on the Apify account list, release the old Trends waits and
         expire (with a recorded reason) the stock frozen under the old Meta
         rule so it is judged again. Repeating it changes nothing new.

No provider, website or model is called.
"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.models import SafetyError, utc_now
from product_radar.r10_cutover import apply_cutover, cutover_check
from product_radar.research_memory import ResearchMemory
from product_radar.runtime_storage import runtime_path
from product_radar.storage import Journal


def main(argv=None, *, root=ROOT, memory=None, enable_accounts=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("check", "apply"))
    args = parser.parse_args(argv)
    root = Path(root)
    memory = memory or ResearchMemory(runtime_path(root, "data/research/captured"),
                                      Journal(runtime_path(root, "data/benchmarks/state/apify")))
    at = utc_now()
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
