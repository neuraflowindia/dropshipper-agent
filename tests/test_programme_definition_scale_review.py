"""Independent large starting-pool check; original records, fictional A/B only."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar.functional_definition_review import functional_definition_readiness
from product_radar.programme import _defer, _match, prepare_programme
from product_radar.research_flow import run_flow
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal
from product_radar.work_turns import bind_work_turn, open_work_turn
from test_functional_source_market import DiscoveryRunner
from test_research_flow import NOW, capture


class ProgrammeDefinitionScaleReviewTests(unittest.TestCase):
    def test_real_156_type_pool_preserves_bounds_and_reaches_later_unblocked_subjects(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
            values = [{"ad_archive_id": str(1000 + n), "page_id": str(2000 + n), "is_active": True,
                "start_date": 1788220800, "snapshot": {"title": f"Blue sink caddy {n:03d}"}}
                for n in range(156)]
            capture(memory, values=values, observed=NOW)
            runner = DiscoveryRunner()
            with patch("product_radar.research_ai.utc_now", return_value=NOW):
                for _ in range(4):
                    run_flow(memory, runner, limit=50, as_of=NOW)
            ids = sorted(memory.snapshot()["concepts"])
            self.assertEqual(len(ids), 156)
            record = prepare_programme(memory, "definition-scale-review", source_run_id="scale-sources",
                concept_ids=ids, as_of=NOW)
            original = memory.snapshot_with_provenance()
            before_calls = len(runner.calls)
            first = functional_definition_readiness(memory, ids, as_of=NOW)
            self.assertEqual(set(first["bindings"]), set(ids))
            self.assertEqual(first["ready_concept_ids"], ids[:5])
            context = {"at": NOW, "runner": None, "match_limit": 5, "retry_seconds": 3600}
            for cid in ids[:-5]:
                _defer(record, "functional_definition", cid, first["bindings"][cid], "AI_RUNNER_NOT_ENABLED", context)
            preserved = deepcopy(record["deferrals"])
            skipped = {cid: first["bindings"][cid] for cid in ids[:-5]}
            later = functional_definition_readiness(memory, ids, as_of=NOW, skip_concept_bindings=skipped)
            self.assertEqual(later["ready_concept_ids"], ids[-5:])
            self.assertEqual(later["bindings"], first["bindings"])
            self.assertEqual(memory.snapshot_with_provenance(), original)
            turn = open_work_turn(memory, record["programme_id"], at=NOW)
            with bind_work_turn(memory, turn["id"]):
                result = _match(memory, record, context)
            definitions = result["functional_definitions"]
            self.assertEqual(definitions["prepared_count"], 5)
            self.assertEqual(set(definitions["failures"]), set(ids[-5:]))
            for ref, value in preserved.items():
                self.assertEqual(record["deferrals"][ref], value)
            self.assertEqual(len(runner.calls), before_calls)
            self.assertFalse(any(row.get("request", {}).get("prompt_version") == "research-b-functional-definition-v1"
                for row in memory.snapshot()["jobs"].values()))


if __name__ == "__main__":
    unittest.main()
