#!/usr/bin/env python3
"""Cutover step (Plan R10 W13): switch on the Apify account list.

Writes config/local/apify_accounts.json with the one funded account (the four
existing private files). It reads no key, calls no provider and adds no
unfunded account. With the list on, each new Apify month is recorded
automatically by the deeper Meta read and earlier months' jobs keep their own
period. Run once from the project folder: python scripts/enable_apify_account_list.py
"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.accounts import enable_account_list
from product_radar.models import SafetyError


def main(argv=None):
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    try:
        print(json.dumps(enable_account_list(ROOT)))
        return 0
    except SafetyError as error:
        print(json.dumps({"written": False, "reason": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
