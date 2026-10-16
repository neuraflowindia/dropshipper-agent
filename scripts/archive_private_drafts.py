#!/usr/bin/env python3
"""Archive never-published older-format private drafts; dry run by default.

Each requested draft must still be an older-format reservation with no
activation receipt and no published history. --execute cancels them together
in one canonical event with the recorded reason, under the programme writer
guard (refused while research holds it). Report files are never changed or
deleted; their bytes are fingerprinted before and after. The dry run reads
saved state only: it takes no lock and cannot contend with a running writer.
"""

import argparse
from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.issue_allocation import ACTIVE_STATES, cancel_unpublished_issues, draft_ordinal
from product_radar.models import SafetyError, digest, identifier, utc_now
from product_radar.programme import _binding, programme_records, release_slots, research_week
from product_radar.research_memory import ResearchMemory
from product_radar.service import load_policy
from product_radar.runtime_storage import runtime_path, runtime_root
from product_radar.storage import Journal, safe_path

DEFAULT_REASON = ("Owner-approved archive: older-format private draft that was never activated or "
                  "published. Its files stay as an archive; its unpublished product holds are released "
                  "for current-format reports.")


def draft_refusals(state, issue_id):
    """Every reason this hold cannot be archived; an empty list means eligible."""
    row = state["issue_reservations"].get(issue_id)
    if row is None:
        return ["ISSUE_RESERVATION_MISSING"]
    reasons = []
    if row.get("state") != "reserved":
        reasons.append("ISSUE_NOT_RESERVED")
    if row.get("recommendation_contract") is not None:
        reasons.append("ISSUE_NOT_OLDER_FORMAT")
    if "activation_receipt" in row:
        reasons.append("ISSUE_ACTIVATION_RECEIPT_PRESENT")
    if any(record.get("issue_id") == issue_id for record in state["published_history"].values()):
        reasons.append("ISSUE_PUBLISHED_HISTORY_PRESENT")
    return reasons


def _language(ordinal, policy):
    return ("bilingual_trial" if ordinal <= 2 else "language_pending"
            if policy["ongoing_pdf_language"] is None else "selected_language")


def projection(state, programmes, *, as_of, policy):
    """Ordinals for open dates under the produced-sets rule, without writing.

    ``if_next`` is the position if that date is the next set produced;
    ``if_all_open_produced`` assumes every listed open date is produced.
    """
    slots = {}
    for record in programmes:
        for slot in record["release_slots"]:
            slots[slot["issue_id"]] = dict(slot, programme_id=record["programme_id"])
    following = (date.fromisoformat(research_week(as_of)) + timedelta(days=7)).isoformat()
    if not any(record["week"] == following for record in programmes):
        for slot in release_slots(following):
            slots.setdefault(slot["issue_id"], dict(slot, programme_id=None))
    rows = sorted(slots.values(), key=lambda slot: (slot["date"], slot["issue_id"]))
    history, reservations = state["published_history"], state["issue_reservations"]
    open_dates = [slot for slot in rows if reservations.get(slot["issue_id"], {}).get("state")
                  not in ACTIVE_STATES | {"cancelled"}]
    result = []
    for slot in rows:
        hold = reservations.get(slot["issue_id"], {})
        row = {"issue_id": slot["issue_id"], "date": slot["date"], "programme_id": slot["programme_id"],
               "reservation_state": hold.get("state"), "format": (
                   None if not hold else "current" if hold.get("recommendation_contract") else "older")}
        if hold.get("state") == "cancelled":
            row["ordinal"] = None
        elif hold.get("state") in ACTIVE_STATES:
            row["ordinal"] = draft_ordinal(slot["issue_id"], slot["date"], history, reservations,
                                           client_id=slot["client_id"])
            row["language_state"] = _language(row["ordinal"], policy)
        else:
            alone = draft_ordinal(slot["issue_id"], slot["date"], history, reservations, client_id=slot["client_id"])
            together = draft_ordinal(slot["issue_id"], slot["date"], history, reservations,
                                     client_id=slot["client_id"], planned=open_dates)
            row.update(if_next={"ordinal": alone, "language_state": _language(alone, policy)},
                       if_all_open_produced={"ordinal": together, "language_state": _language(together, policy)})
        result.append(row)
    return result


def report_file_fingerprint(root, issue_ids):
    """Exact bytes of every saved report file for these drafts (read-only)."""
    files = {}
    for issue_id in issue_ids:
        folder = safe_path(runtime_path(root, "data", "outputs"), issue_id)
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if path.is_file() and not path.is_symlink():
                files[str(path.relative_to(runtime_root(root)))] = [path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest()]
    return {"files": len(files), "bytes": sum(row[0] for row in files.values()), "fingerprint": digest(files)}


def _programmes(state, client_id):
    """Programme records from this same verified read.

    Release dates are part of each programme's immutable binding. A newer
    staged checkpoint from a live writer must not be joined to an older read
    (load_programme rightly refuses that), and is not needed to project dates.
    """
    rows = [deepcopy(row) for row in programme_records(state, client_id=client_id)]
    for row in rows:
        _binding(row)
    return sorted(rows, key=lambda row: row["programme_id"])


def _inspect(state, issue_ids):
    drafts = []
    for issue_id in issue_ids:
        row = state["issue_reservations"].get(issue_id, {})
        drafts.append({"issue_id": issue_id, "state": row.get("state"), "revision": row.get("revision"),
            "date": row.get("date"), "products": len(row.get("products", [])),
            "selection_hash": row.get("selection_hash"), "refusals": draft_refusals(state, issue_id)})
    programmes = _programmes(state, "default")
    owners = {slot["issue_id"]: record["programme_id"] for record in programmes for slot in record["release_slots"]}
    for row in drafts:
        row["owning_programme_id"] = owners.get(row["issue_id"])
    return drafts, programmes


def _main(argv=None, *, memory=None, root=ROOT, clock=utc_now):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue-id", action="append", required=True, help="One older private draft; repeat for several.")
    parser.add_argument("--reason", default=DEFAULT_REASON, help="Recorded on every cancelled hold.")
    parser.add_argument("--execute", action="store_true", help="Apply the archive. Without it nothing is written.")
    args = parser.parse_args(argv)
    root = Path(root)
    committed = False
    try:
        issue_ids = sorted({identifier(value) for value in args.issue_id})
        if not args.reason.strip() or len(args.reason) > 500:
            raise SafetyError("ISSUE_CANCEL_REASON_INVALID")
        memory = memory or ResearchMemory(runtime_path(root, "data/research/captured"), Journal(runtime_path(root, "data/benchmarks/state/apify")))
        policy, as_of = load_policy(), clock()
        output = {"mode": "execute" if args.execute else "dry_run", "issue_ids": issue_ids, "reason": args.reason,
                  "as_of": as_of, "client_publication_attempted": False}
        if not args.execute:
            state = memory.snapshot()
            drafts, programmes = _inspect(state, issue_ids)
            after = deepcopy(state)
            for row in drafts:
                if not row["refusals"]:
                    after["issue_reservations"][row["issue_id"]]["state"] = "cancelled"
            output.update(drafts=drafts, eligible=all(not row["refusals"] for row in drafts),
                projection_before=projection(state, programmes, as_of=as_of, policy=policy),
                projection_after_if_executed=projection(after, programmes, as_of=as_of, policy=policy),
                report_files=report_file_fingerprint(root, issue_ids), changed=False)
            print(json.dumps(output, indent=2), flush=True)
            return 0 if output["eligible"] else 1
        # Warm the verified read first (a cold replay of the live store takes
        # minutes), as prepare_programme does. The locked read below then only
        # verifies newer events, so the guard is held for seconds and a
        # research turn starting meanwhile is not turned away for minutes.
        memory.snapshot()
        # Same guard as every programme advance: fail fast while research or
        # another writer holds it; never wait for, steal or unlock it.
        with Journal(safe_path(memory.root, "programme_writer")).locked():
            state = memory.snapshot()
            drafts, programmes = _inspect(state, issue_ids)
            output.update(drafts=drafts, projection_before=projection(state, programmes, as_of=as_of, policy=policy))
            files_before = report_file_fingerprint(root, issue_ids)
            if any(row["refusals"] for row in drafts):
                output.update(changed=False, report_files=files_before)
                print(json.dumps(output, indent=2), flush=True)
                return 1
            cancel_unpublished_issues(memory, {row["issue_id"]: row["selection_hash"] for row in drafts},
                                      as_of=as_of, reason=args.reason)
            committed = True
            state = memory.snapshot()
            files_after = report_file_fingerprint(root, issue_ids)
            if files_after != files_before:
                raise SafetyError("ARCHIVE_REPORT_FILES_CHANGED")
            output.update(changed=True, report_files=files_after,
                cancelled={issue_id: state["issue_reservations"][issue_id]["state"] for issue_id in issue_ids},
                projection_after=projection(state, programmes, as_of=as_of, policy=policy))
        print(json.dumps(output, indent=2), flush=True)
        return 0
    except (SafetyError, OSError) as error:
        print(json.dumps({"state": "refused" if not committed else "archived_with_check_failure",
                          "reason": str(error) if isinstance(error, SafetyError) else "ARCHIVE_LOCAL_FILE_FAILED",
                          "changed": committed, "client_publication_attempted": False}), flush=True)
        return 1


def main(argv=None, **kwargs):
    return _main(argv, **kwargs)


if __name__ == "__main__":
    raise SystemExit(main())
