#!/usr/bin/env python3
"""Owner sign-in to the research sites in the program's own Chrome profile.

open    starts plain Chrome (no remote control, sync off) on the lasting
        research profile with one tab per site; the owner signs in and closes it.
status  reports each site as signed in / sign-in needed / expired / not checked,
        from what the latest reads saw (no site is visited).

Passwords and cookies are never read, stored or printed.
"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.models import SafetyError
from product_radar.signed_in_browser import open_sign_in_window, sign_in_status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("open", "status"))
    args = parser.parse_args(argv)
    try:
        if args.action == "open":
            result = {"state": open_sign_in_window(ROOT), **sign_in_status(ROOT)}
        else:
            result = {"state": "status", **sign_in_status(ROOT)}
        print(json.dumps(result), flush=True)
        return 0
    except (SafetyError, OSError) as error:
        reason = str(error) if isinstance(error, SafetyError) else "SIGN_IN_LOCAL_IO_FAILED"
        message = ("A research read is using the browser. Try again in a minute." if reason == "SIGNED_IN_BROWSER_BUSY"
                   else "The sign-in window could not be handled. See the project log.")
        print(json.dumps({"state": "error", "reason": reason, "message": message}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
