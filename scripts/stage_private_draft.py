#!/usr/bin/env python3
"""Prepare by default; --live uploads and downloads an existing private draft."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.models import SafetyError
from product_radar.portal_delivery_check import ORIGIN, cached_owner_token
from product_radar.portal_storage import PortalStorage
from product_radar.private_drafts import stage_private_draft, verify_owner_draft_downloads
from product_radar.runtime_storage import runtime_path
from product_radar.storage import atomic_json, safe_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue-id", required=True)
    parser.add_argument("--revision", required=True, type=int)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--verify-downloads", action="store_true", help="Read the owner page and its file links without uploading anything; requires --live.")
    parser.add_argument("--cloudflared-path", type=Path)
    args = parser.parse_args(argv)
    if args.verify_downloads and not args.live:
        parser.error("--verify-downloads requires --live")
    try:
        prepared = stage_private_draft(ROOT, args.issue_id, args.revision)
        storage = None
        if args.live:
            token = cached_owner_token(executable_path=args.cloudflared_path)
            storage = PortalStorage(ORIGIN, prepared["client_id"], token)
        receipt = verify_owner_draft_downloads(ROOT, args.issue_id, args.revision, storage=storage) if args.verify_downloads else stage_private_draft(ROOT, args.issue_id, args.revision, storage=storage) if args.live else prepared
        if args.verify_downloads:
            folder = safe_path(runtime_path(ROOT, "data/outputs"), args.issue_id, "revision-" + str(args.revision))
            atomic_json(folder / "private-owner-download-receipt.json", receipt)
        print(json.dumps(receipt, sort_keys=True), flush=True)
        return 0 if receipt["state"] in ("prepared", "private_bytes_verified", "owner_downloads_verified") else 1
    except Exception as error:
        code = str(error) if isinstance(error, SafetyError) and str(error).startswith(("PRIVATE_", "DELIVERY_", "PORTAL_")) else "PRIVATE_DRAFT_CHECK_FAILED"
        print(json.dumps({"state": "work_remaining", "reason": code, "publication_attempted": False, "index_write_attempted": False}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
