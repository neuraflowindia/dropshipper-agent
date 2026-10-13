"""Real Pillow transforms and revision-owned local bytes; no external calls."""

from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from random import Random
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from PIL import Image
from product_radar.models import SafetyError, digest
from product_radar.product_images import inspect_image
from product_radar.report_assets import (MAX_DISPLAY_BYTES, TRANSFORM, freeze_report_assets,
    prepare_report_asset_plan, resolve_report_asset, validate_report_asset_receipt, _derivative)
from product_radar.storage import atomic_bytes, atomic_json


def image_bytes(size=(2000, 1000), mode="RGB", orientation=None):
    image = Image.new(mode, size, (130, 50, 20, 128) if mode == "RGBA" else (130, 50, 20))
    output = BytesIO()
    exif = Image.Exif()
    if orientation is not None:
        exif[274] = orientation
        exif[315] = "Private creator metadata"
    image.save(output, format="PNG", exif=exif)
    return output.getvalue()


class ReportAssetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.media = self.root / "source-media"
        self.stage = self.root / "display-staging"
        self.raw = image_bytes()
        self.path = self.media / "original.png"
        atomic_bytes(self.path, self.raw)
        self.products = [self.product("type-one")]

    def product(self, cid):
        return {"concept_id": cid, "image_provenance": {"id": "photo-" + cid, "owner_id": cid,
            "path": str(self.path), "sha256": sha256(self.raw).hexdigest(), "source_kind": "public_product_image",
            "source_ref": "observation-" + cid, "source_url": "https://example.com/public-photo.png",
            "observed_at": "2026-10-09T09:00:00Z", "relationship": "exact_product"}}

    def plan(self, products=None, **options):
        return prepare_report_asset_plan(self.products if products is None else products,
            staging_root=self.stage, media_root=self.media, **options)

    def freeze(self, bundle, revision=1):
        return freeze_report_assets(bundle, root=self.root, issue_id="issue-one", revision=revision)

    def resolve(self, receipt, index=0, revision=1):
        return resolve_report_asset(receipt, receipt["plan"]["entries"][index]["asset_id"],
            root=self.root, issue_id="issue-one", revision=revision)

    def test_full_frame_bounded_copy_does_not_edit_source(self):
        bundle = self.plan()
        row = bundle["plan"]["entries"][0]
        self.assertEqual(row["state"], "ready")
        self.assertEqual(row["display"]["width"], 1600)
        self.assertEqual(row["display"]["height"], 800)
        self.assertLessEqual(row["display"]["bytes"], MAX_DISPLAY_BYTES)
        result = self.resolve(self.freeze(bundle))
        self.assertEqual(len(result["data_url"].encode()), bundle["plan"]["embedded_bytes"])
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(result["display_asset"]["source_hash"], sha256(self.raw).hexdigest())
        self.assertNotEqual(result["display_asset"]["output_sha256"], result["display_asset"]["source_hash"])

    def test_plan_identity_has_no_revision_or_paths(self):
        first = self.plan()
        other = self.media / "moved.png"
        atomic_bytes(other, self.raw)
        self.products[0]["image_provenance"]["path"] = str(other)
        second = self.plan()
        self.assertEqual(first["plan_hash"], second["plan_hash"])
        one, two = self.freeze(first), self.freeze(second, 2)
        self.assertEqual(one["plan_hash"], two["plan_hash"])
        self.assertNotEqual(digest(one), digest(two))

    def test_no_upscale_and_private_metadata_removed(self):
        original = image_bytes((40, 20), orientation=6)
        saved, raw = _derivative(original)
        self.assertEqual((saved["display"]["width"], saved["display"]["height"]), (20, 40))
        with Image.open(BytesIO(raw)) as image:
            self.assertEqual(dict(image.getexif()), {})
        self.assertFalse(saved["transform"]["upscaled"])

    def test_transparency_survives_webp_and_png_fallback(self):
        raw = image_bytes((100, 50), "RGBA")
        for fallback in (False, True):
            with self.subTest(png_fallback=fallback):
                with patch("PIL.features.check", return_value=not fallback):
                    saved, derivative = _derivative(raw)
                self.assertEqual(saved["display"]["format"], "PNG" if fallback else "WEBP")
                with Image.open(BytesIO(derivative)) as image:
                    self.assertLess(image.convert("RGBA").getextrema()[3][0], 255)

    def test_exact_replay_does_not_use_current_encoder_or_source(self):
        first = self.plan()
        self.path.unlink()
        with patch("product_radar.report_assets._derivative", side_effect=AssertionError("Must reuse saved bytes")):
            self.assertEqual(self.plan(), first)

    def test_owned_copy_survives_shared_photo_and_staging_removal(self):
        bundle = self.plan()
        receipt = self.freeze(bundle)
        self.path.unlink()
        stage_file = next(self.stage.rglob("*.jpg"))
        stage_file.unlink()
        again = self.freeze(bundle)
        self.assertEqual(receipt, again)
        self.assertTrue(self.resolve(again)["bytes"])

    def test_both_editions_resolve_the_same_owned_bytes(self):
        receipt = self.freeze(self.plan())
        english, hinglish = self.resolve(receipt), self.resolve(receipt)
        self.assertEqual(english, hinglish)

    def test_receipt_validator_is_pure_even_when_owned_directory_is_lost(self):
        receipt = self.freeze(self.plan())
        with patch("product_radar.report_assets.load_json", side_effect=AssertionError("No filesystem reads")), \
                patch("product_radar.report_assets.safe_path", side_effect=AssertionError("No filesystem probes")):
            validated = validate_report_asset_receipt(receipt, issue_id="issue-one", revision=1)
        self.assertEqual(validated, receipt)
        validated["plan"]["entries"].clear()
        self.assertTrue(receipt["plan"]["entries"])

    def test_receipt_validator_rejects_foreign_and_boolean_owner_revisions(self):
        receipt = self.freeze(self.plan())
        for issue_id, revision in (("foreign", 1), ("issue-one", True), ("issue-one", 2)):
            with self.subTest(issue_id=issue_id, revision=revision):
                with self.assertRaises(SafetyError):
                    validate_report_asset_receipt(receipt, issue_id=issue_id, revision=revision)

    def test_receipt_validator_names_malformed_nested_values(self):
        receipt = self.freeze(self.plan())
        for field, value in (("format", ["JPEG"]), ("width", None)):
            with self.subTest(field=field):
                changed = deepcopy(receipt)
                changed["plan"]["entries"][0]["display"][field] = value
                changed["plan_hash"] = digest(changed["plan"])
                changed["record_hash"] = digest({k: v for k, v in changed.items() if k != "record_hash"})
                with self.assertRaises(SafetyError):
                    validate_report_asset_receipt(changed, issue_id="issue-one", revision=1)

    def test_every_embedded_occurrence_counts_even_for_shared_hash(self):
        one = self.plan()
        size = one["plan"]["embedded_bytes"]
        two = self.plan([self.product("one"), self.product("two")], encoded_budget=size)
        self.assertEqual([row["state"] for row in two["plan"]["entries"]], ["ready", "omitted"])
        self.assertEqual(two["plan"]["embedded_bytes"], size)
        self.assertEqual(two["plan"]["entries"][1]["failure_code"], "REPORT_ASSET_ENCODED_LIMIT")

    def test_twenty_photos_stay_inside_aggregate_budget(self):
        bundle = self.plan([self.product("type-" + str(n)) for n in range(20)])
        self.assertEqual(len(bundle["plan"]["entries"]), 20)
        self.assertTrue(all(row["state"] == "ready" for row in bundle["plan"]["entries"]))
        self.assertLessEqual(bundle["plan"]["embedded_bytes"], 8 * 1024 * 1024)
        self.freeze(bundle)

    def test_twenty_near_five_megabyte_originals_do_not_create_an_oversized_packet(self):
        # Deterministic high-detail pixels exercise real encoding, not a mocked
        # byte count or twenty easily compressed solid-colour photographs.
        photo = Image.frombytes("RGB", (1600, 1050), Random(120).randbytes(1600 * 1050 * 3))
        output = BytesIO()
        photo.save(output, format="PNG", compress_level=0)
        self.raw = output.getvalue()
        self.assertGreater(len(self.raw), 4_800_000)
        self.assertLessEqual(len(self.raw), 5 * 1024 * 1024)
        atomic_bytes(self.path, self.raw)
        bundle = self.plan([self.product("type-" + str(n)) for n in range(20)])
        entries = bundle["plan"]["entries"]
        self.assertTrue(all(row["state"] == "ready" for row in entries))
        self.assertTrue(all(row["display"]["bytes"] <= MAX_DISPLAY_BYTES for row in entries))
        receipt = self.freeze(bundle)
        actual = sum(len(self.resolve(receipt, n)["data_url"].encode("ascii")) for n in range(20))
        self.assertEqual(actual, bundle["plan"]["embedded_bytes"])
        self.assertLessEqual(actual, 8 * 1024 * 1024)
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_budget_includes_the_logo_and_each_product_occurrence(self):
        logo = self.root / "brand.png"
        atomic_bytes(logo, image_bytes((100, 30), "RGBA"))
        unrestricted = self.plan(logo_path=logo)
        logo_only = prepare_report_asset_plan([], staging_root=self.stage,
            media_root=self.media, logo_path=logo)
        exact = unrestricted["plan"]["embedded_bytes"]
        limited = self.plan(logo_path=logo, encoded_budget=exact - 1)
        self.assertEqual([row["state"] for row in limited["plan"]["entries"]], ["ready", "omitted"])
        self.assertEqual(limited["plan"]["embedded_bytes"], logo_only["plan"]["embedded_bytes"])
        self.freeze(limited)

    def test_one_unusable_photo_does_not_block_healthy_products(self):
        bad = self.product("bad")
        bad["image_provenance"]["sha256"] = "f" * 64
        bundle = self.plan([bad, self.product("healthy")])
        self.assertEqual([row["state"] for row in bundle["plan"]["entries"]], ["omitted", "ready"])
        self.assertEqual(bundle["plan"]["entries"][0]["failure_code"], "REPORT_ASSET_SOURCE_CHANGED")
        self.freeze(bundle)

    def test_zero_budget_is_an_explicit_no_photo_plan(self):
        bundle = self.plan(encoded_budget=0)
        self.assertEqual(bundle["plan"]["embedded_bytes"], 0)
        receipt = self.freeze(bundle)
        with self.assertRaisesRegex(SafetyError, "REPORT_ASSET_NOT_DECLARED"):
            self.resolve(receipt)

    def test_brand_logo_is_separate_and_owned_after_source_loss(self):
        logo = self.root / "brand.png"
        atomic_bytes(logo, image_bytes((100, 30), "RGBA"))
        bundle = self.plan(logo_path=logo)
        self.assertEqual(bundle["plan"]["entries"][0]["source"]["kind"], "brand_logo")
        receipt = self.freeze(bundle)
        logo.unlink()
        self.assertTrue(self.resolve(receipt)["bytes"])

    def test_missing_or_linked_optional_source_keeps_text_working(self):
        self.path.unlink()
        self.path.symlink_to(self.media / "missing.png")
        bundle = self.plan(logo_path=self.path)
        self.assertTrue(all(row["state"] == "omitted" for row in bundle["plan"]["entries"]))
        self.freeze(bundle)

    def test_partial_copy_recovers_without_new_transform(self):
        bundle = self.plan()
        from product_radar import report_assets
        original = report_assets.atomic_json
        def interrupted(path, value):
            if Path(path).name == "receipt.json":
                raise OSError("Synthetic receipt interruption")
            return original(path, value)
        with patch.object(report_assets, "atomic_json", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.freeze(bundle)
        with patch.object(report_assets, "_derivative", side_effect=AssertionError("No transform during freeze")):
            receipt = self.freeze(bundle)
        self.assertTrue(self.resolve(receipt)["bytes"])

    def test_missing_owned_copy_recovers_exact_staged_bytes(self):
        bundle = self.plan()
        receipt = self.freeze(bundle)
        asset = next((self.root / "media").rglob("*.jpg"))
        expected = asset.read_bytes()
        asset.unlink()
        self.assertEqual(self.freeze(bundle), receipt)
        self.assertEqual(self.resolve(receipt)["bytes"], expected)

    def test_corrupt_owned_copy_recovers_only_the_exact_frozen_bytes(self):
        bundle = self.plan()
        receipt = self.freeze(bundle)
        asset = next((self.root / "media").rglob("*.jpg"))
        expected = asset.read_bytes()
        atomic_bytes(asset, image_bytes((30, 20)))
        with self.assertRaisesRegex(SafetyError, "REPORT_ASSET_OWNED_BYTES_CHANGED"):
            self.resolve(receipt)
        self.assertEqual(self.freeze(bundle), receipt)
        self.assertEqual(asset.read_bytes(), expected)
        self.assertEqual(self.resolve(receipt)["bytes"], expected)

    def test_without_exact_recovery_the_damaged_copy_is_not_overwritten(self):
        bundle = self.plan()
        self.freeze(bundle)
        asset = next((self.root / "media").rglob("*.jpg"))
        wrong = image_bytes((30, 20))
        atomic_bytes(asset, wrong)
        next(self.stage.rglob("*.jpg")).unlink()
        with self.assertRaisesRegex(SafetyError, "REPORT_ASSET_EXACT_RECOVERY_MISSING"):
            self.freeze(bundle)
        self.assertEqual(asset.read_bytes(), wrong)

    def test_display_size_reduction_is_three_attempts_not_an_unbounded_loop(self):
        original = Image.Image.save
        calls = []
        def tracked(image, *args, **options):
            calls.append(image.size)
            return original(image, *args, **options)
        with patch("product_radar.report_assets.MAX_DISPLAY_BYTES", 1), patch.object(Image.Image, "save", tracked):
            saved, raw = _derivative(self.raw)
        self.assertEqual(calls, [(1600, 800), (1200, 600), (960, 480)])
        self.assertEqual(saved["state"], "omitted")
        self.assertIsNone(raw)

    def test_same_revision_cannot_accept_a_different_plan(self):
        self.freeze(self.plan())
        with self.assertRaisesRegex(SafetyError, "REPORT_ASSET_FROZEN_INPUT_CHANGED"):
            self.freeze(self.plan(encoded_budget=0))

    def test_foreign_revision_and_undeclared_asset_refused(self):
        receipt = self.freeze(self.plan())
        with self.assertRaisesRegex(SafetyError, "REPORT_ASSET_RECEIPT_INVALID"):
            self.resolve(receipt, revision=2)
        with self.assertRaisesRegex(SafetyError, "REPORT_ASSET_NOT_DECLARED"):
            resolve_report_asset(receipt, "not-a-declared-asset", root=self.root, issue_id="issue-one", revision=1)

    def test_tampered_plan_hash_and_transform_refused(self):
        bundle = self.plan()
        bad = deepcopy(bundle)
        bad["plan_hash"] = "f" * 64
        with self.assertRaisesRegex(SafetyError, "REPORT_ASSET_PLAN_CHANGED"):
            self.freeze(bad)
        bad = deepcopy(bundle)
        bad["plan"]["entries"][0]["transform"]["full_frame"] = False
        bad["plan_hash"] = digest(bad["plan"])
        with self.assertRaisesRegex(SafetyError, "REPORT_DISPLAY_TRANSFORM_INVALID"):
            self.freeze(bad)

    def test_cached_derivative_tamper_is_omitted_before_freeze(self):
        self.plan()
        staged = next(self.stage.rglob("*.jpg"))
        atomic_bytes(staged, image_bytes((50, 50)))
        bundle = self.plan()
        self.assertEqual(bundle["plan"]["entries"][0]["state"], "omitted")
        self.assertEqual(bundle["plan"]["entries"][0]["failure_code"], "REPORT_DISPLAY_BYTES_CHANGED")


if __name__ == "__main__":
    unittest.main()
