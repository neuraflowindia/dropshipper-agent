#!/usr/bin/env python3
"""Local by default; --live stages only two fixed illustrative private files."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.portal_delivery_check import cached_owner_token, check_private_delivery, public_failure


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="Use the existing owner CLI session to stage/read two private illustrative files; never publish.")
    parser.add_argument("--cloudflared-path", type=Path,
                        help="Explicit path to the trusted installed cloudflared binary; no token argument is accepted.")
    args = parser.parse_args(argv)
    try:
        result = check_private_delivery(ROOT, live=args.live,
            token_loader=lambda: cached_owner_token(executable_path=args.cloudflared_path))
    except Exception as error:
        print(json.dumps(public_failure(error), sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
