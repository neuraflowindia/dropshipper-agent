#!/usr/bin/env python3
"""Preview local storage adoption; explicit execution preserves saved originals.

No sources, models, spending, client publication, task installation or backup
copy is performed. The optional backup setting change preserves enablement.
"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.local_storage_adoption import adopt_local_storage
from product_radar.models import SafetyError


def main(argv=None, *, root=ROOT, memory=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Adopt local storage under the existing programme writer.")
    parser.add_argument("--adopt-backup-settings", action="store_true",
        help="After both stores verify, upgrade existing backup settings without enabling a disabled backup.")
    parser.add_argument("--expected-plan-hash", help="Require the exact earlier read-only preview before making changes.")
    args = parser.parse_args(argv)
    if args.adopt_backup_settings and not args.execute:
        parser.error("--adopt-backup-settings requires --execute")
    try:
        result = adopt_local_storage(root, execute=args.execute, adopt_backup_settings=args.adopt_backup_settings,
            expected_plan_hash=args.expected_plan_hash, memory=memory)
    except (SafetyError, OSError) as error:
        result = {"state": "waiting", "reason": str(error) if isinstance(error, SafetyError) else "LOCAL_STORAGE_FILE_UNAVAILABLE",
            "execute": args.execute, "provider_calls": 0, "model_calls": 0, "publication_attempted": False}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["state"] in {"preview", "complete"} and result.get("receipt_saved") is not False else 1


if __name__ == "__main__":
    raise SystemExit(main())
