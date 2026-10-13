"""Original photo proof is independent of shared bytes, not of source facts."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar.models import SafetyError, digest
from product_radar.product_images import capture_product_image, inspect_image
from product_radar.report_examples import RECOMMENDATION_CONTRACT, validate_frozen_functional_photo
from product_radar.report_selection import build_report_pool
from test_functional_source_market import functional_pool_fixture, NOW
from test_report_assets import image_bytes


class FunctionalPhotoProofTests(unittest.TestCase):
    def fixture(self, shared=False, renewable=False):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        memory, _ = functional_pool_fixture(root / "memory", with_images=True)
        pool = build_report_pool(memory, as_of=NOW, recommendation_contract=RECOMMENDATION_CONTRACT)
        product = pool["product_facts"][0]
        raw = image_bytes((400, 200))
        def fetch(url):
            return {"state": "usable", "reason": "Synthetic original photo saved.", "requested_url": url,
                    "final_url": url, "http_status": 200, "response_received": True,
                    "owned_worker_exited": True, "execution_stage": "response_received", "redirects": [],
                    "checked_at": NOW, "dispatch_started": True,
                    "body_complete": True, "metadata": inspect_image(raw)}, raw
        media = root / "photos"
        capture_product_image(memory, product["concept_id"], product["image_candidates"][0],
                              media_root=media, fetcher=fetch, shared_acquisitions=shared,
                              renewable_acquisitions=renewable, as_of=NOW if renewable else None)
        with patch("product_radar.product_images.MEDIA_ROOT", media):
            pool = build_report_pool(memory, as_of=NOW, recommendation_contract=RECOMMENDATION_CONTRACT)
        product = deepcopy(pool["product_facts"][0])
        product["source_revisions"] = pool["eligible"][0]["source_revisions"]
        return memory, product

    def test_original_photo_check_and_actual_narrow_match_are_frozen(self):
        for shared in (False, True):
            with self.subTest(shared=shared):
                memory, product = self.fixture(shared)
                self.assertIn("image_provenance", product)
                self.assertEqual(validate_frozen_functional_photo(product), product)
                originals = product["functional_photo_proof"]["records"]["source_checks"]
                self.assertEqual(len(originals), 2 if shared else 1)
                for rid, wrapper in originals.items():
                    self.assertEqual(product["source_revisions"]["source_checks"][rid], wrapper["sha256"])
                Path(product["image_path"]).unlink()
                self.assertEqual(validate_frozen_functional_photo(product), product)
                self.assertEqual(digest(memory.snapshot()["source_checks"][product["image_provenance"]["id"]]),
                                 originals[product["image_provenance"]["id"]]["sha256"])

    def test_original_url_and_relationship_cannot_be_relabelled(self):
        _, product = self.fixture()
        for field, value in (("source_url", "https://images.example.org/not-this-product.png"),
                             ("relationship", "exact_product"), ("owner_id", "another-concept")):
            with self.subTest(field=field):
                changed = deepcopy(product); changed["image_provenance"][field] = value
                with self.assertRaises(SafetyError):
                    validate_frozen_functional_photo(changed)

    def test_shared_original_is_required_and_hash_bound(self):
        _, product = self.fixture(True)
        photo_id = product["image_provenance"]["id"]
        other = next(rid for rid in product["functional_photo_proof"]["records"]["source_checks"] if rid != photo_id)
        changed = deepcopy(product); del changed["functional_photo_proof"]["records"]["source_checks"][other]
        with self.assertRaisesRegex(SafetyError, "PHOTO_PROOF_INVALID"):
            validate_frozen_functional_photo(changed)
        changed = deepcopy(product); changed["source_revisions"]["source_checks"][other] = "0" * 64
        with self.assertRaisesRegex(SafetyError, "PHOTO_PROOF_INVALID"):
            validate_frozen_functional_photo(changed)

    def test_renewable_original_response_and_body_proof_survive_shared_byte_loss(self):
        _, product = self.fixture(True, True)
        from product_radar.frozen_records import read_original_json, freeze_original_json
        originals = product["functional_photo_proof"]["records"]["source_checks"]
        source = read_original_json(originals[product["image_provenance"]["id"]])
        self.assertEqual(source["version"], "public-product-image-v3")
        validate_frozen_functional_photo(product)
        Path(product["image_path"]).unlink()
        validate_frozen_functional_photo(product)
        for change in (lambda r: r["response_proof"].update(body_sha256="0" * 64),
                       lambda r: r["acquisition_binding"].update(generation=3),
                       lambda r: r["response_proof"]["fetch_receipt"].update(requested_url="https://wrong.example.org/image.png")):
            changed = deepcopy(product)
            shared = read_original_json(changed["functional_photo_proof"]["records"]["source_checks"][source["acquisition_ref"]])
            change(shared)
            changed["functional_photo_proof"]["records"]["source_checks"][source["acquisition_ref"]] = freeze_original_json(shared)
            with self.assertRaises(SafetyError):
                validate_frozen_functional_photo(changed)


if __name__ == "__main__":
    unittest.main()
