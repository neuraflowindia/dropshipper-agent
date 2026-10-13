#!/usr/bin/env python3
"""Local tray control entry; status/start/stop never request external research."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.desktop_control import run_worker, start, status, stop
from product_radar.models import SafetyError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "start", "stop", "worker"))
    parser.add_argument("research_arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.action != "worker" and args.research_arguments:
        parser.error("Only the worker accepts saved research options")
    try:
        result = run_worker(ROOT, args.research_arguments) if args.action == "worker" else {
            "status": status, "start": start, "stop": stop}[args.action](ROOT)
        print(json.dumps(result), flush=True)
        return 0 if result.get("exit_code", 0) in (0, 130, -2) else 1
    except (SafetyError, OSError) as error:
        print(json.dumps({"state": "error", "message": "The research control could not complete. See the project log.",
                          "reason": str(error) if isinstance(error, SafetyError) else "DESKTOP_LOCAL_IO_FAILED"}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
