#!/usr/bin/env python3
"""Build one saved internal report draft; never approve, upload or publish."""

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.models import SafetyError, digest, identifier
from product_radar.report_workflow import prepare_review_package
from product_radar.spend_watch_ai import research_codex_runner
from product_radar.research_memory import ResearchMemory
from product_radar.service import load_policy
from product_radar.runtime_storage import runtime_path
from product_radar.storage import Journal, load_json
from render_live_pdf import inspect_pdf, render


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue-id", required=True, help="One exact issue in saved report_drafts.json.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live-model", action="store_true", help="Use the existing signed-in model for concise prose only.")
    mode.add_argument("--presentation-source-revision", type=int,
                      help="Reuse writing from this earlier complete revision; no model calls.")
    args = parser.parse_args(argv)
    try:
        identifier(args.issue_id)
        if not args.live_model and args.presentation_source_revision is None:
            raise SafetyError("EXPLICIT_LIVE_MODEL_REQUIRED")
        if args.presentation_source_revision is not None and args.presentation_source_revision < 1:
            raise SafetyError("REPORT_PRESENTATION_REVISION_INVALID")
        memory = ResearchMemory(runtime_path(ROOT, "data/research/captured"), Journal(runtime_path(ROOT, "data/benchmarks/state/apify")))
        drafts = load_json(memory.root / "report_drafts.json", 8 * 1024 * 1024)
        pool = load_json(memory.root / "report_pool.json", 8 * 1024 * 1024)
        if drafts.get("pool_hash") != digest(pool) or pool.get("evidence_type") != "captured":
            raise SafetyError("SAVED_REPORT_POOL_CHANGED")
        issues = [issue for issue in drafts["allocation"]["issues"] if issue["issue_id"] == args.issue_id]
        if len(issues) != 1:
            raise SafetyError("SAVED_REPORT_ISSUE_REQUIRED")
        issue = issues[0]
        wanted = {product["concept_id"] for product in issue["products"]}
        facts = [row for row in pool["product_facts"] if row["concept_id"] in wanted]
        policy = load_policy()
        choice = policy["ongoing_pdf_language"]
        can_write = issue.get("ready_for_review") is True and (issue["projected_ordinal"] <= 2 or choice is not None)
        runner = research_codex_runner(load_json(ROOT / "config/ai/codex_luna.json"), root=ROOT) if can_write and args.live_model else None
        result = prepare_review_package(memory, issue, facts, root=ROOT, runner=runner,
                                        render_pdf=render, inspect_pdf=inspect_pdf, selected_language=choice,
                                        presentation_source_revision=args.presentation_source_revision)
        print("Issue: " + args.issue_id + "; state: " + result["state"])
        if result.get("manifest_path"):
            print("Review package: " + result["manifest_path"])
        if result.get("shortfall"):
            print("More qualifying products needed: " + str(result["shortfall"]))
        if result.get("failures"):
            print("Saved writing problems remain; inspect package-status.json before retrying.")
        print("No collection, reservation, approval, upload or client publication occurred.")
        return 0
    except (SafetyError, OSError, KeyError, TypeError) as error:
        print(str(error) if isinstance(error, SafetyError) else "REPORT_LOCAL_OPERATION_FAILED")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
