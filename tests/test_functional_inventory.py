from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
spec = importlib.util.spec_from_file_location("inspect_functional_types", ROOT / "scripts/inspect_functional_types.py")
inspector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inspector)

from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal
from test_research_flow import capture, FakeRunner, NOW, run_flow
from test_catalogue_expansion import fixture


class InventoryTests(unittest.TestCase):
    def test_actual_read_only_entry_does_not_touch_event_index_or_finance_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
            capture(memory)
            run_flow(memory, FakeRunner(), as_of=NOW)
            before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            output = io.StringIO()
            with redirect_stdout(output):
                status = inspector.main([], root=root, memory=memory, catalogue=fixture())
            after = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            result = json.loads(output.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(before, after)
            self.assertEqual(result["active_concepts"], 2)
            self.assertEqual(result["legacy_views"], 2)
            self.assertEqual(result["accepted_definitions"], 0)
            self.assertEqual(result["resolved_source_children"], 3)
            self.assertEqual(result["model_calls"], 0)
            self.assertEqual(result["research_state_hash"], digest(memory.snapshot()))
            self.assertEqual(result["source_provenance"], {name: memory.snapshot_with_provenance()[name]
                for name in ("head", "event_count", "evidence_type")})

    def test_read_failure_is_not_empty_history(self):
        class FailedMemory:
            def snapshot_with_provenance(self):
                raise SafetyError("RESEARCH_EVENTS_INVALID")
        output = io.StringIO()
        with redirect_stdout(output):
            status = inspector.main([], memory=FailedMemory(), catalogue=fixture())
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue())["state"], "inspection_incomplete")


if __name__ == "__main__":
    unittest.main()
