#!/usr/bin/env python3
"""Check QuickCommerceAPI credits without searching or displaying credentials.

The balance check itself is the library's ``QuickCommerceClient.credits``;
this command only shows its validated totals. It records nothing.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.models import SafetyError
from product_radar.quickcommerce import QuickCommerceClient, load_key

MESSAGES = {
    "QUICKCOMMERCE_AUTH_REJECTED": "Authentication rejected (HTTP 401). Check the saved key.",
    "QUICKCOMMERCE_ACCESS_DENIED": "Access denied (HTTP 403). Review account restrictions; do not assume an invalid key.",
    "QUICKCOMMERCE_CREDITS_CHECK_REFUSED": "Provider returned HTTP 402 on the documented free credit endpoint; investigate before purchasing.",
    "QUICKCOMMERCE_RATE_LIMITED": "Rate limit reached (HTTP 429). No automatic retry was attempted.",
    "QUICKCOMMERCE_NETWORK_FAILED": "Network or TLS connection failed. Credential validity remains unverified.",
    "QUICKCOMMERCE_CREDITS_RESPONSE_INVALID": "Unexpected credit response; connection not confirmed.",
}


def check_connection(client) -> tuple[bool, str, dict | None]:
    try:
        reading = client.credits()
    except SafetyError as error:
        return False, MESSAGES.get(str(error), "Provider check failed. No raw response was logged."), None
    summary = {field: reading[field] for field in ("total_available", "total_used", "active_packs")}
    return True, "Authenticated credit check succeeded (HTTP 200). No product search was made.", summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Perform one free authenticated credit check.")
    args = parser.parse_args(argv)
    if not args.live:
        print("No network request made. Use --live for one free credit-balance check.")
        return 2
    try:
        client = QuickCommerceClient(load_key())
    except SafetyError:
        print("CHECK FAILED: Save one valid QUICKCOMMERCE_API_KEY in config/local/quickcommerce.env. File contents were not displayed.")
        return 2
    except Exception:
        print("CHECK FAILED: Unexpected local error; details suppressed to protect credentials.")
        return 2
    success, message, summary = check_connection(client)
    print(datetime.now(timezone.utc).isoformat(timespec="seconds"))
    print(("PASS: " if success else "CHECK FAILED: ") + message)
    if success:
        print(json.dumps(summary, sort_keys=True))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
