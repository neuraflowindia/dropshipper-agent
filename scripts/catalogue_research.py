#!/usr/bin/env python3
"""Prepare grounded catalogue material outside the production research writer.

Default actions reuse files and read canonical state only. --capture-list is
the sole external action: one free public list page, an isolated owned browser,
no import, model job, paid Actor, account change, purchase or publication.
Seed inputs remain side files until the existing writer can admit them.
"""

import argparse
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.catalogue import load_catalogue
from product_radar.catalogue_coverage import catalogue_matrix, workload_scenarios
from product_radar.catalogue_expansion import effective_catalogue
from product_radar.catalogue_seed import (prepare_catalogue_branch_inputs, stage_catalogue_inventory,
    stage_marketplace_materials, stage_page_materials)
from product_radar.marketplace_browser import build_marketplace_request, capture_marketplace, replay_marketplace
from product_radar.models import SafetyError, digest, identifier, utc_now
from product_radar.research_memory import ResearchMemory
from product_radar.runtime_storage import runtime_path
from product_radar.storage import Journal, atomic_json, load_json, safe_path


class _CategoryLinks(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base; self.current = None; self.text = []; self.rows = []
        self.stack = []; self.navigation_depth = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag not in self.VOID_TAGS:
            self.stack.append(tag)
            if attributes.get("id") in {"zg-left-col", "zg_browseRoot"} and self.navigation_depth is None:
                self.navigation_depth = len(self.stack)
        if tag == "a" and self.navigation_depth is not None:
            self.current = dict(attrs).get("href"); self.text = []

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, text):
        if self.current is not None: self.text.append(text)

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            link = urlsplit(urljoin(self.base, self.current)); label = " ".join(" ".join(self.text).split())
            match = re.fullmatch(r"/(?:gp/bestsellers|zgbs)/([a-z][a-z0-9-]{0,79}/[0-9]{1,20})(?:/ref=[a-zA-Z0-9_-]+)?/?", link.path)
            level = re.search(r"/ref=zg_bs_nav_[a-z0-9-]+_([0-9]+)(?:_[0-9]+)?/?$", link.path)
            clean = "https://www.amazon.in/gp/bestsellers/" + match[1] if match else None
            if link.scheme == "https" and link.netloc == "www.amazon.in" and match and label and clean != self.base:
                row = {"label": label[:2000], "department": match[1], "url": clean,
                       "navigation_level": int(level[1]) if level else None}
                if row not in self.rows: self.rows.append(row)
            self.current = None; self.text = []
        if tag in self.stack:
            last = len(self.stack) - 1 - self.stack[::-1].index(tag)
            del self.stack[last:]
        if self.navigation_depth is not None and len(self.stack) < self.navigation_depth:
            self.navigation_depth = None; self.current = None; self.text = []


def parse_child_categories(raw, base):
    """Actual source-owned navigation children, not header or paging links."""
    parser = _CategoryLinks(base)
    parser.feed(raw)
    child_level = max((row["navigation_level"] for row in parser.rows if row["navigation_level"] is not None), default=None)
    return [row for row in parser.rows if child_level is None or row["navigation_level"] == child_level]


def list_child_categories(folder):
    """Only links actually present in a verified owned list page, no fetch."""
    folder = safe_path(Path(folder)); receipt = replay_marketplace(folder)
    if receipt["state"] not in {"usable", "partial"} or "rendered.html" not in receipt["artifacts"]:
        raise SafetyError("CATALOGUE_CATEGORY_PAGE_NOT_USABLE")
    raw = (folder / "rendered.html").read_text(encoding="utf-8")
    children = parse_child_categories(raw, receipt["request"]["url"])
    return {"observed_at": receipt["observed_at"], "reference": str(folder / "receipt.json"),
            "receipt_hash": digest(receipt), "child_categories": children, "provider_calls": 0, "model_calls": 0}


def stage_inventory(memory, catalogue, plan, *, source_root=ROOT, limit=10):
    """Prepare independent branch batches; no model or canonical writes.

    The shared library route; the research programme runs the same staging
    automatically for a store that has no seed manifest yet.
    """
    return stage_catalogue_inventory(memory, catalogue, plan, source_root=source_root, limit=limit)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--matrix", action="store_true")
    group.add_argument("--list-links", type=Path)
    group.add_argument("--stage-marketplace", type=Path)
    group.add_argument("--stage-public-receipt", type=Path)
    group.add_argument("--stage-inventory", type=Path)
    group.add_argument("--prepare-branch")
    group.add_argument("--capture-list", choices=("amazon-bestsellers",))
    parser.add_argument("--branch-id")
    parser.add_argument("--example", action="append")
    parser.add_argument("--material-id", action="append")
    parser.add_argument("--department")
    parser.add_argument("--acquisition-id")
    parser.add_argument("--transport", choices=("auto", "native", "playwright"), default="auto")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--new-candidates-per-query", type=int)
    args = parser.parse_args(argv)
    if (args.stage_marketplace or args.stage_public_receipt) and not args.branch_id:
        parser.error("staging needs --branch-id")
    if args.stage_public_receipt and not args.example:
        parser.error("staging public pages needs exact --example excerpts")
    if args.capture_list and (not args.department or not args.acquisition_id):
        parser.error("one public list capture needs --department and --acquisition-id")
    memory = ResearchMemory(runtime_path(ROOT, "data/research/captured"), Journal(runtime_path(ROOT, "data/benchmarks/state/apify")))
    catalogue = load_catalogue()
    try:
        if args.list_links:
            result = list_child_categories(args.list_links)
        elif args.capture_list:
            request = build_marketplace_request(args.capture_list, department=args.department, limit=args.limit)
            result = capture_marketplace(request, identifier(args.acquisition_id),
                data_root=runtime_path(ROOT, "data/research/catalogue-public"), transport=args.transport)
        elif args.stage_marketplace:
            result = stage_marketplace_materials(memory, catalogue, branch_id=args.branch_id,
                folder=args.stage_marketplace, examples=args.example)
        elif args.stage_public_receipt:
            saved = load_json(args.stage_public_receipt, 4 * 1024 * 1024)
            receipt = saved.get("receipt", saved)
            result = stage_page_materials(memory, catalogue, branch_id=args.branch_id, receipt=receipt,
                examples=args.example, receipt_reference=str(args.stage_public_receipt))
        elif args.stage_inventory:
            result = stage_inventory(memory, catalogue, load_json(args.stage_inventory, 4 * 1024 * 1024), limit=args.limit)
        elif args.prepare_branch:
            result = prepare_catalogue_branch_inputs(memory, catalogue, branch_id=args.prepare_branch,
                material_ids=args.material_id, limit=args.limit)
        else:
            state = memory.snapshot()
            with memory.journal.locked(timeout=5):
                attempts = memory.journal.state()[1]
            matrix = catalogue_matrix(effective_catalogue(catalogue, state), state, financial_attempts=attempts)
            result = {"prepared_at": utc_now(), "matrix": matrix,
                "workload": workload_scenarios(matrix, new_candidates_per_query=args.new_candidates_per_query),
                "canonical_writes": 0, "provider_calls": 0, "model_calls": 0}
        if args.output:
            destination = safe_path(args.output)
            atomic_json(destination, result)
        displayed = result
        if args.output and args.matrix:
            displayed = {"prepared_at": result["prepared_at"], "output": str(destination),
                "categories": matrix["categories"], "totals": matrix["totals"],
                "unmapped_product_checks": len(matrix["unmapped"]), "workload": result["workload"],
                "canonical_writes": 0, "provider_calls": 0, "model_calls": 0}
        elif args.output and args.capture_list:
            displayed = {"state": result["state"], "reason": result["reason"], "output": str(destination),
                "observed_at": result["observed_at"], "dispatch_started": result["dispatch_started"],
                "usable_cards": result.get("usable_cards", 0), "canonical_writes": 0, "model_calls": 0,
                "paid_actor_calls": 0}
        elif args.output and args.stage_inventory:
            displayed = {name: result[name] for name in ("inventory_id", "state", "prepared_branches", "material_examples",
                "prepared_examples", "failures", "canonical_writes", "provider_calls", "model_calls")}
            displayed["output"] = str(destination)
        print(json.dumps(displayed, ensure_ascii=False, indent=2), flush=True)
        return 0
    except (SafetyError, OSError) as error:
        print(json.dumps({"state": "waiting", "reason": str(error) if isinstance(error, SafetyError) else "CATALOGUE_SIDE_FILE_UNREADABLE",
                          "canonical_writes": 0}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
