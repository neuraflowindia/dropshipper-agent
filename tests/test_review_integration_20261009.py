"""Independent saved-contract regressions; fictional local records only.

No collector, external model, provider, cloud system or production file is used.
Only genuine existing constructors and their exact synthetic fixtures are used.
"""

from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from product_radar.functional_types import effective_functional_definition
from product_radar.models import SafetyError, digest
import test_functional_type_review as functional_fixtures
import test_research_scope as scope_fixtures


class SavedContractIntegrationReviewTests(unittest.TestCase):
    def functional_fixture(self):
        fixture = functional_fixtures.FunctionalTypeReviewTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def scope_fixture(self):
        fixture = scope_fixtures.ScopeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_older_definition_revision_cannot_change_under_its_original_id(self):
        fixture = self.functional_fixture()
        first = fixture.accept(example_decisions=fixture.decisions)
        fixture.accept({**fixture.definition, "report_title": "A tidier sink"},
                       reviewed_at=functional_fixtures.LATER)
        old = fixture.memory.snapshot()["source_checks"][first["definition_id"]]
        changed = deepcopy(old)
        # This is still the old meaning, so the latest predecessor-meaning hash
        # does not expose the changed historical title. The immutable old ID
        # must nevertheless authenticate that exact originally saved review.
        changed["definition"]["report_title"] = "A different historical title"
        changed["presentation_hash"] = digest({"report_title": changed["definition"]["report_title"]})
        changed["record_hash"] = digest({key: value for key, value in changed.items() if key != "record_hash"})
        fixture.memory.put("source_checks", first["definition_id"], changed, previous=old)
        with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_DEFINITION_.*(?:BINDING|HASH|RECORD)"):
            effective_functional_definition(fixture.memory.snapshot(), fixture.cid)

    def test_corrupt_scope_lookup_key_cannot_hide_latest_correction_during_commit(self):
        fixture = self.scope_fixture()
        fixture.evidence()
        correction = fixture.evidence(support="not_positive", as_of=scope_fixtures.LATER,
                                      reason="The original saved result is not encouraging.")
        old = fixture.memory.snapshot()["source_checks"][correction["id"]]
        changed = {**old, "binding_key": "damaged-lookup-key"}
        fixture.memory.put("source_checks", correction["id"], changed, previous=old)
        before = fixture.memory.snapshot()
        with self.assertRaisesRegex(SafetyError, "RESEARCH_SCOPE_.*"):
            fixture.evidence(support="positive", as_of=scope_fixtures.LATER,
                             reason="A third explicit review must see its true predecessor.")
        self.assertEqual(fixture.memory.snapshot(), before)

    def test_corrupt_definition_subject_does_not_silently_restore_legacy_meaning(self):
        for rehash in (False, True):
            with self.subTest(self_consistent_hash=rehash):
                fixture = self.functional_fixture()
                accepted = fixture.accept(example_decisions=fixture.decisions)
                old = fixture.memory.snapshot()["source_checks"][accepted["definition_id"]]
                changed = {**old, "concept_id": "different-concept"}
                if rehash:
                    changed["record_hash"] = digest({key: value for key, value in changed.items() if key != "record_hash"})
                fixture.memory.put("source_checks", accepted["definition_id"], changed, previous=old)
                with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_DEFINITION_.*"):
                    effective_functional_definition(fixture.memory.snapshot(), fixture.cid)

    def test_corrupt_scope_subject_does_not_resurrect_earlier_positive_review(self):
        from product_radar.research_scope import _binding_key, validated_scoped_evidence
        for rehash in (False, True):
            with self.subTest(self_consistent_hash=rehash):
                fixture = self.scope_fixture()
                fixture.evidence()
                corrected = fixture.evidence(support="not_positive", as_of=scope_fixtures.LATER,
                                             reason="A real saved correction to the initial positive review.")
                old = fixture.memory.snapshot()["source_checks"][corrected["id"]]
                damaged = deepcopy(old)
                damaged["subject"]["concept_id"] = "different-concept"
                if rehash:
                    damaged["concept_id"] = "different-concept"
                    damaged["binding_key"] = _binding_key(damaged)
                    damaged["record_hash"] = digest({key: value for key, value in damaged.items() if key != "record_hash"})
                fixture.memory.put("source_checks", corrected["id"], damaged, previous=old)
                with self.assertRaisesRegex(SafetyError, "RESEARCH_SCOPE_.*"):
                    validated_scoped_evidence(fixture.memory.snapshot(), fixture.cid)


if __name__ == "__main__":
    unittest.main()
