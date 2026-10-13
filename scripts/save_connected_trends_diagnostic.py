#!/usr/bin/env python3
"""Preserve original supervised browser output and verify its downloaded CSV.

This does not launch a browser, attach a product, or claim unattended support.
Input is the original public capture object returned by the connected browser.
"""

from hashlib import sha256
import argparse
import base64
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.models import SafetyError, canonical, identifier
from product_radar.runtime_storage import runtime_path
from product_radar.storage import safe_path
from product_radar.trends_controller import _interpret, _write, build_trends_request


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload-base64")
    args=parser.parse_args()
    raw=base64.b64decode(args.payload_base64,validate=True).decode() if args.payload_base64 else sys.stdin.read(2 * 1024 * 1024)
    if len(raw)>2*1024*1024: raise SafetyError("CONNECTED_TRENDS_DIAGNOSTIC_SIZE_INVALID")
    result = json.loads(raw)
    receipt = result["receipt"]
    identifier(receipt["acquisition_id"])
    if receipt.get("route") != "connected_playwright_browser_csv" or receipt.get("supervised_only") is not True:
        raise SafetyError("CONNECTED_TRENDS_DIAGNOSTIC_INVALID")
    folder = safe_path(runtime_path(ROOT, "data/research/connected-trends"), receipt["acquisition_id"])
    folder.mkdir(parents=True, exist_ok=True)
    snapshot = result.get("snapshot")
    if isinstance(snapshot, str): snapshot = json.loads(snapshot)
    _write(folder / "scope.json", canonical(snapshot))
    raw = None
    if receipt.get("csv_path"):
        path = safe_path(receipt["csv_path"])
        if path != folder / "multiTimeline.csv" or path.stat().st_size > 5 * 1024 * 1024:
            raise SafetyError("CONNECTED_TRENDS_CSV_INVALID")
        raw = path.read_bytes(); receipt["csv_sha256"] = sha256(raw).hexdigest()
    interpreted = _interpret(snapshot, build_trends_request(receipt["term"]), receipt.get("navigation_status"), raw, receipt["collected_at"])
    _write(folder / "interpretation.json", canonical(interpreted))
    receipt["scope_verified"] = interpreted["scope_verified"]
    if receipt["state"] in ("Captured", "Insufficient", "No data"):
        receipt.update(state=interpreted["state"], reason=interpreted["reason"])
    _write(folder / "receipt.json", canonical(receipt))
    print(json.dumps({"id":receipt["acquisition_id"],"state":receipt["state"],"rows":len(snapshot.get("rows",[])) if snapshot else 0,
        "intervals":len(interpreted["parsed"]["series"]) if interpreted["parsed"] else 0,"csv_sha256":receipt.get("csv_sha256"),
        "supervised_only":True,"scope_verified":receipt["scope_verified"]}))
    return 0


if __name__ == "__main__": raise SystemExit(main())
