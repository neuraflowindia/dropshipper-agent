#!/usr/bin/env python3
"""Inspect the existing identities before any functional-type migration.

This command only reads verified research events and the accepted catalogue.
It does not write overlays, rebuild an index, call a model, search, or publish.
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.catalogue import load_catalogue
from product_radar.catalogue_expansion import effective_catalogue
from product_radar.functional_types import functional_type_inventory
from product_radar.fit_policy import FIT_POLICY_ID, policy_for
from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory
from product_radar.research_records import iter_catalogue_scopes
from product_radar.runtime_storage import runtime_path
from product_radar.storage import Journal


def migration_inventory(state, catalogue):
    inventory = functional_type_inventory(state)
    active_ids = {row["concept_id"] for row in inventory["concepts"]}
    effective = effective_catalogue(catalogue, state)
    families = {row["id"] for row in effective["families"] if row["disposition"] == "usable"}
    unresolved_associations = [child_id for child_id, row in state["associations"].items()
                              if row.get("status") != "resolved" or row.get("concept_id") is None]
    missing_concepts = [child_id for child_id, row in state["associations"].items()
                        if row.get("concept_id") is not None and row["concept_id"] not in state["concepts"]]
    scope_rows = list(iter_catalogue_scopes(state))
    categories = Counter(state["concepts"][cid]["description"].get("category", "Not recorded") for cid in active_ids)
    return {**inventory, "inspection_contract": "functional-type-migration-inventory-v1",
            "research_state_hash": digest(state), "effective_catalogue_hash": digest(effective),
            "fit_policy_hash": digest(policy_for(FIT_POLICY_ID)), "fit_policy_id": FIT_POLICY_ID,
            "accepted_catalogue_directions": len(families),
            "active_concepts_by_category": dict(sorted(categories.items())),
            "resolved_source_children": sum(row.get("status") == "resolved" and row.get("concept_id") in active_ids
                                            for row in state["associations"].values()),
            "unresolved_source_children": sorted(unresolved_associations),
            "missing_concept_references": sorted(missing_concepts),
            "recorded_scope_overlays": len(scope_rows),
            "scope_overlays_missing_family": [record_id for record_id, row in scope_rows if row.get("family_id") not in families],
            "note": "Catalogue directions are places to research. Concepts are types already found. Neither count is a count of searches performed or a promise of future reports. Legacy views are not newly accepted definitions. Scope-overlay counts do not validate semantic equivalence.",
            "research_writes": 0, "provider_calls": 0, "model_calls": 0}


def main(argv=None, *, root=ROOT, memory=None, catalogue=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research-root", type=Path)
    args = parser.parse_args(argv)
    root = Path(root)
    memory = memory or ResearchMemory(args.research_root or runtime_path(root, "data/research/captured"),
                                      Journal(runtime_path(root, "data/benchmarks/state/apify")))
    try:
        snapshot = memory.snapshot_with_provenance()
        result = migration_inventory(snapshot["state"], catalogue or load_catalogue())
        result["source_provenance"] = {name: snapshot[name] for name in ("head", "event_count", "evidence_type")}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (SafetyError, OSError) as error:
        print(json.dumps({"state": "inspection_incomplete",
                          "reason": str(error) if isinstance(error, SafetyError) else "RESEARCH_FILES_UNREADABLE",
                          "research_writes": 0, "provider_calls": 0, "model_calls": 0}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
