#!/usr/bin/env python3
"""One explicit free public Trends capture or original-file replay."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.models import SafetyError
from product_radar.trends_controller import build_trends_request, capture_direct_trends, replay_direct_trends


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("capture")
    run.add_argument("--query", required=True)
    run.add_argument("--acquisition-id", required=True)
    run.add_argument("--transport", choices=("auto", "native", "playwright"), default="auto")
    run.add_argument("--timeout", type=int, default=45)
    run.add_argument("--browser-path")
    saved = commands.add_parser("replay")
    saved.add_argument("--folder", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "capture":
            receipt = capture_direct_trends(build_trends_request(args.query), args.acquisition_id,
                transport=args.transport, timeout=args.timeout, browser_path=args.browser_path)
        else:
            receipt = replay_direct_trends(args.folder)
    except (SafetyError, OSError, ValueError) as error:
        print(json.dumps({"state": "failed", "reason": str(error) if isinstance(error, SafetyError) else "DIRECT_TRENDS_LOCAL_OPERATION_FAILED"}))
        return 2
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if receipt["state"] in ("usable", "no_data") else 1


if __name__ == "__main__": raise SystemExit(main())
