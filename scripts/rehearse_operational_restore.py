#!/usr/bin/env python3
"""Save exact records and prove a local temporary restore without external calls."""

import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.models import SafetyError
from product_radar.operational_audit import audit_operational_checkpoint
from product_radar.operational_backup import create_operational_backup, restore_operational_backup
from product_radar.research_memory import ResearchMemory
from product_radar.runtime_storage import runtime_path
from product_radar.storage import Journal, atomic_json


def main(argv=None, *, memory=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-id", required=True, help="One immutable checkpoint identity; an existing ID remains its original checkpoint.")
    parser.add_argument("--backup-root", type=Path, default=ROOT / "data/operations/backups")
    parser.add_argument("--source-file", type=Path, action="append", default=[])
    parser.add_argument("--model-file", type=Path, action="append", default=[])
    parser.add_argument("--report-file", type=Path, action="append", default=[])
    parser.add_argument("--image-file", type=Path, action="append", default=[])
    parser.add_argument("--direct-receipt", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    memory = memory or ResearchMemory(runtime_path(ROOT, "data/research/captured"), Journal(runtime_path(ROOT, "data/benchmarks/state/apify")))
    try:
        started = time.monotonic()
        attachments = [{"path": path.absolute(), "kind": kind}
                       for paths, kind in ((args.source_file, "source_capture"), (args.model_file, "model_receipt"),
                                           (args.report_file, "report_package"), (args.image_file, "public_image"))
                       for path in paths]
        backup = create_operational_backup(memory, args.backup_root, backup_id=args.backup_id, attachments=attachments)
        with tempfile.TemporaryDirectory(prefix="neuraflow-record-restore-") as directory:
            restored = restore_operational_backup(backup["folder"], Path(directory))
        # This audit has its own checked date; concurrent healthy work may have
        # advanced after the earlier immutable backup checkpoint was captured.
        audit = audit_operational_checkpoint(memory, direct_receipts=args.direct_receipt)
        receipt = {"schema_version": 1, "kind": "local_operational_restore_rehearsal", "backup_folder": backup["folder"],
                   "replayed_backup": backup["replayed"], "restore": restored, "audit": audit,
                   "elapsed_seconds": round(time.monotonic() - started, 3),
                   "temporary_restore_removed_after_verification": True, "independent_backup_proved": False,
                   "next_backup_input": "Select a permanent independent owner-controlled backup destination."}
        target = Path(backup["folder"]) / "restore-rehearsal.json"
        # A later rehearsal is separate proof, never an overwrite of the first.
        if target.exists():
            from product_radar.models import digest
            target = target.with_name("restore-rehearsal-" + digest(receipt)[:24] + ".json")
        atomic_json(target, receipt)
        print(json.dumps({"proof": str(target), "research_events_restored": restored["research_events"],
                          "financial_events_restored": restored["financial_events"], "files_verified": restored["files_verified"],
                          "bytes_verified": restored["bytes_verified"], "elapsed_seconds": receipt["elapsed_seconds"],
                          "known_collection_usd": audit["collection"]["known_actual_usd"],
                          "unsettled_hold_usd": audit["collection"]["unsettled_hold_usd"],
                          "provider_calls": 0, "model_calls": 0, "independent_backup_proved": False}, indent=2))
        return 0
    except (SafetyError, OSError, ValueError) as error:
        print(json.dumps({"state": "backup_or_restore_needs_recovery", "reason": str(error) if isinstance(error, SafetyError)
                          else "LOCAL_BACKUP_OPERATION_FAILED", "provider_calls": 0, "model_calls": 0,
                          "working_records_removed": False, "independent_backup_proved": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
