#!/usr/bin/env python3
"""Explicit bounded free public marketplace capture; no model or paid call."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.marketplace_browser import ROUTES, build_marketplace_request, capture_marketplace, replay_marketplace
from product_radar.models import SafetyError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("capture")
    command.add_argument("--route", choices=ROUTES, required=True)
    command.add_argument("--acquisition-id", required=True)
    command.add_argument("--department")
    command.add_argument("--term")
    command.add_argument("--limit", type=int, default=30)
    command.add_argument("--timeout", type=int, default=45)
    command.add_argument("--transport", choices=("auto", "native", "playwright"), default="auto")
    command.add_argument("--browser-path")
    command.add_argument("--headed", action="store_true")
    command = commands.add_parser("replay")
    command.add_argument("--folder", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "capture":
            request = build_marketplace_request(args.route, department=args.department, term=args.term, limit=args.limit)
            result = capture_marketplace(request, args.acquisition_id, transport=args.transport, timeout=args.timeout,
                                         browser_path=args.browser_path, headed=args.headed)
        else:
            result = replay_marketplace(args.folder)
    except (SafetyError, OSError, ValueError) as error:
        print(json.dumps({"state": "failed", "reason": str(error) if isinstance(error, SafetyError) else "MARKETPLACE_LOCAL_OPERATION_FAILED"}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["state"] in ("usable", "partial", "empty") else 1


if __name__ == "__main__":
    raise SystemExit(main())
