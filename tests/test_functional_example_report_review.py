"""New saved example judgments reach frozen fit/report readers, never providers."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_functional_source_market as fixtures
from test_functional_example_review import Runner
from product_radar.functional_example_review import review_functional_examples
from product_radar.functional_types import accept_functional_definition, effective_functional_definition
from product_radar.frozen_records import freeze_original_json, read_original_json
from product_radar.models import SafetyError, digest
from product_radar.report_examples import RECOMMENDATION_CONTRACT, validate_functional_examples, freeze_functional_examples
from product_radar.report_selection import build_report_pool, validate_frozen_functional_fit


class FunctionalExampleReportReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        original = fixtures.accept_functional_definition
        def accept_and_review(memory, cid, definition, **options):
            options["example_decisions"] = {}
            accepted = original(memory, cid, definition, **options)
            review = review_functional_examples(memory, [cid], Runner(), as_of=options["reviewed_at"])
            self.assertFalse(review["failures"], review)
            return accepted
        with patch.object(fixtures, "accept_functional_definition", accept_and_review), patch(
                "product_radar.research_ai.utc_now", return_value=fixtures.NOW):
            self.memory, ids = fixtures.functional_pool_fixture(Path(temporary.name))
        pool = build_report_pool(self.memory, as_of=fixtures.NOW, recommendation_contract=RECOMMENDATION_CONTRACT)
        self.assertEqual(len(pool["eligible"]), 1, pool["decisions"])
        self.product = pool["product_facts"][0]

    def test_exact_original_review_is_shared_by_example_and_fit_without_redefining_type(self):
        product = self.product
        validate_functional_examples(product, concept_id=product["concept_id"])
        validate_frozen_functional_fit(product)
        self.assertEqual(len(product["example_review_proofs"]), 1)
        refs = product["functional_fit_reference_proofs"]["records"]["source_checks"]
        for entries in product["functional_fit_proof"]["examples"].values():
            for entry in entries:
                rid = entry["example_review_id"]
                original = read_original_json(refs[rid])
                self.assertEqual(entry["example_review_hash"], original["record_hash"])
                self.assertEqual(refs[rid], product["example_review_proofs"][rid])
        self.assertEqual(read_original_json(product["functional_definition_proof"]["record"])
                         if product["functional_definition_proof"]["record"].get("contract") else
                         product["functional_definition_proof"]["record"],
                         self.memory.snapshot()["source_checks"][product["functional_definition"]["definition_id"]])

    def test_missing_or_resealed_review_cannot_supply_frozen_fit_features(self):
        for mode in ("missing", "different", "wrong_original"):
            product = deepcopy(self.product)
            entry = next(iter(product["functional_fit_proof"]["examples"].values()))[0]
            rid = entry["example_review_id"]
            records = product["functional_fit_reference_proofs"]["records"]["source_checks"]
            if mode == "missing":
                del records[rid]
            else:
                original = read_original_json(records[rid])
                if mode == "different": original["result"]["decisions"][0]["decision"] = "different"
                else: original["job_proof"]["original_job"]["request"]["model"] = "unapproved-model"
                original["record_hash"] = digest({k: v for k, v in original.items() if k != "record_hash"})
                records[rid] = freeze_original_json(original)
            with self.subTest(mode=mode), self.assertRaises(SafetyError):
                validate_frozen_functional_fit(product)

    def test_old_fit_review_cannot_override_a_current_explicit_type_judgment(self):
        product = deepcopy(self.product)
        state = self.memory.snapshot(); cid = product["concept_id"]
        current = effective_functional_definition(state, cid)
        definition = state["source_checks"][current["definition_id"]]
        entry = next(iter(product["functional_fit_proof"]["examples"].values()))[0]
        accept_functional_definition(self.memory, cid, definition["definition"],
            basis_refs=sorted(definition["basis_refs"]), reviewer="synthetic_operator",
            reviewed_at=fixtures.NOW, expected_meaning_hash=current["meaning_hash"],
            example_decisions={entry["child_id"]: {"decision": "different", "reason": "Explicit saved correction."}})
        selected = freeze_functional_examples(self.memory.snapshot(), cid, as_of=fixtures.NOW)
        self.assertEqual(selected["example_products"], [])
        product.update(selected)
        with self.assertRaisesRegex(SafetyError, "REPORT_FUNCTIONAL_FIT_PROOF_INVALID"):
            validate_frozen_functional_fit(product)


if __name__ == "__main__": unittest.main()
