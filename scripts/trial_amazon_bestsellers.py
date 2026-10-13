#!/usr/bin/env python3
"""Explicit standalone Amazon trial; never starts application research."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.amazon_browser_trial import LANDING, MAX_ENVELOPE, capture, replay, summary, save_browser_capture
from product_radar.models import SafetyError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect-landing", "capture"):
        command = commands.add_parser(name)
        command.add_argument("--trial-id", required=True)
        command.add_argument("--acquisition-id", required=True)
        if name == "capture":
            command.add_argument("--url", required=True)
        command.add_argument("--executable-path")
        command.add_argument("--timeout", type=int, default=45)
    command = commands.add_parser("replay")
    command.add_argument("--trial-id", required=True)
    command.add_argument("--acquisition-id", required=True)
    command = commands.add_parser("summary")
    command.add_argument("--trial-id", required=True)
    command = commands.add_parser("record-browser", help="Save an already completed fresh public browser-tool capture; no navigation")
    command.add_argument("--trial-id", required=True)
    command.add_argument("--acquisition-id", required=True)
    command.add_argument("--input", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in ("inspect-landing", "capture"):
            result = capture(LANDING if args.command == "inspect-landing" else args.url,
                             args.trial_id, args.acquisition_id,
                             kind="landing" if args.command == "inspect-landing" else "list",
                             executable_path=args.executable_path, timeout=args.timeout)
        elif args.command == "replay":
            result = replay(args.trial_id, args.acquisition_id)
        elif args.command == "record-browser":
            path = Path(args.input)
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ENVELOPE:
                raise SafetyError("AMAZON_TRIAL_BROWSER_ENVELOPE_INVALID")
            with path.open("rb") as stream:
                raw = stream.read(MAX_ENVELOPE + 1)
            if len(raw) > MAX_ENVELOPE:
                raise SafetyError("AMAZON_TRIAL_CAPTURE_TOO_LARGE")
            result = save_browser_capture(args.trial_id, args.acquisition_id, json.loads(raw))
        else:
            result = summary(args.trial_id)
    except (SafetyError, OSError, ValueError, TypeError) as error:
        print(json.dumps({"state": "failed", "reason": str(error) if isinstance(error, SafetyError) else "local_trial_operation_failed"}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("state", "usable") in ("usable", "partial") else 1


if __name__ == "__main__":
    raise SystemExit(main())
