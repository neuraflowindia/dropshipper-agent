"""Report-only image quality; original download and recovery proof stays raw."""

from copy import deepcopy
from io import BytesIO
from pathlib import Path
import unittest
from unittest.mock import patch

from PIL import Image

import product_radar.product_images as images
from product_radar.models import SafetyError, digest
from product_radar.storage import atomic_bytes
from product_radar.work_turns import DEFAULT_POLICY, bind_work_turn, open_work_turn
import test_renewable_product_images as fixtures


class ReportPhotoQualityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RenewableImageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory = self.fixture.memory
        self.cid = self.fixture.cid
        self.candidate = self.fixture.candidate
        self.media = self.fixture.media

    def fetch(self, size):
        def read(url):
            receipt, _ = self.fixture.fetch(url)
            buffer = BytesIO()
            Image.new("RGB", size, (12, 24, 36)).save(buffer, format="PNG")
            raw = buffer.getvalue()
            return dict(receipt, metadata=images.inspect_image(raw)), raw
        return read

    def selected(self, candidates=None, **options):
        return images.current_public_image(self.memory.snapshot(), self.cid,
            candidates or [self.candidate], media_root=self.media, **options)

    def ready(self, candidate=None, **options):
        return images.product_image_readiness(self.memory, self.cid,
            candidate or self.candidate, as_of=self.fixture.at, **options)

    def test_report_quality_is_opt_in_and_does_not_rewrite_small_original(self):
        saved = self.fixture.capture()
        before = self.memory.snapshot()
        raw = Path(saved["image"]["path"]).read_bytes()
        self.assertEqual(saved["state"], "usable")
        self.assertIsNotNone(self.selected())
        self.assertIsNone(self.selected(require_report_quality=True))
        self.assertFalse(images.candidate_report_quality(before, self.cid, self.candidate))
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(Path(saved["image"]["path"]).read_bytes(), raw)
        self.assertEqual(len(self.fixture.calls), 1)

    def test_quality_selection_continues_past_small_first_candidate(self):
        self.fixture.capture()
        alternate = self.fixture.alternate("https://other.example.org/creative.png")
        saved = self.fixture.capture(candidate=alternate, fetcher=self.fetch((256, 128)))
        self.assertIsNotNone(self.selected([self.candidate, alternate]))
        selected = self.selected([self.candidate, alternate], require_report_quality=True)
        self.assertEqual(selected["id"], saved["reference"])
        self.assertTrue(images.candidate_report_quality(self.memory.snapshot(), self.cid, alternate))

    def test_known_small_missing_media_is_not_restore_work_for_report(self):
        saved = self.fixture.capture()
        Path(saved["image"]["path"]).unlink()
        self.assertTrue(self.ready()["ready"])
        quality = self.ready(require_report_quality=True)
        self.assertFalse(quality["ready"])
        self.assertFalse(quality["local_saved"])
        self.assertIsNone(quality["next_retry_at"])
        self.assertEqual(quality["reason"], "PRODUCT_IMAGE_REPORT_PHOTO_UNUSABLE")
        self.assertFalse(images.candidate_report_quality(self.memory.snapshot(), self.cid, self.candidate))

    def test_short_edge_threshold_and_strict_dimensions(self):
        self.assertEqual(images.MIN_REPORT_IMAGE_SHORT_EDGE, 128)
        for size, expected in (((128, 128), True), ((900, 127), False), ((128, 900), True)):
            self.assertEqual(images.report_image_quality(*size), expected)
        for size in ((0, 128), (True, 128), (128.0, 128), (-1, 128), (128, None)):
            with self.subTest(size=size), self.assertRaisesRegex(SafetyError, "PRODUCT_IMAGE_REPORT_DIMENSIONS_INVALID"):
                images.report_image_quality(*size)

    def test_quality_flags_are_real_booleans(self):
        for flag in (None, 0, 1, "true"):
            with self.subTest(flag=flag):
                with self.assertRaisesRegex(SafetyError, "PRODUCT_IMAGE_REPORT_QUALITY_OPTION_INVALID"):
                    self.selected(require_report_quality=flag)
                with self.assertRaisesRegex(SafetyError, "PRODUCT_IMAGE_REPORT_QUALITY_OPTION_INVALID"):
                    self.ready(require_report_quality=flag)

    def test_unknown_candidate_does_not_become_rejection_or_reserve_work(self):
        before = self.memory.snapshot()
        self.assertIsNone(images.candidate_report_quality(before, self.cid, self.candidate))
        self.assertTrue(self.ready(state=before, require_report_quality=True)["ready"])
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(self.fixture.calls, [])

    def test_small_original_is_known_before_missing_consumer_application(self):
        saved = self.fixture.capture()
        state = self.memory.snapshot()
        del state["source_checks"][saved["reference"]]
        self.assertFalse(images.candidate_report_quality(state, self.cid, self.candidate))
        self.assertTrue(self.ready(state=state)["ready"])
        self.assertFalse(self.ready(state=state, require_report_quality=True)["ready"])

    def test_small_complete_response_is_known_before_original_summary_save(self):
        saved = self.fixture.capture()
        state = self.memory.snapshot()
        del state["source_checks"][saved["reference"]]
        del state["source_checks"][saved["acquisition_ref"]]
        self.assertFalse(images.candidate_report_quality(state, self.cid, self.candidate))
        self.assertFalse(self.ready(state=state, require_report_quality=True)["ready"])

    def test_adequate_missing_media_restores_same_original_without_get(self):
        saved = self.fixture.capture(fetcher=self.fetch((218, 218)))
        Path(saved["image"]["path"]).unlink()
        state = self.memory.snapshot()
        self.assertTrue(images.candidate_report_quality(state, self.cid, self.candidate))
        self.assertTrue(self.ready(state=state, require_report_quality=True)["ready"])
        self.assertIsNone(self.selected(require_report_quality=True))
        recovered = self.fixture.capture()
        self.assertTrue(recovered["reused"])
        self.assertEqual(recovered["reference"], saved["reference"])
        self.assertIsNotNone(self.selected(require_report_quality=True))
        self.assertFalse(self.ready(require_report_quality=True)["ready"])
        self.assertEqual(len(self.fixture.calls), 1)

    def test_frozen_metadata_only_default_stays_raw_after_local_bytes_loss(self):
        saved = self.fixture.capture()
        Path(saved["image"]["path"]).unlink()
        self.assertEqual(self.selected(verify_bytes=False)["id"], saved["reference"])
        self.assertIsNone(self.selected(verify_bytes=False, require_report_quality=True))

    def test_legacy_and_shared_saved_small_originals_remain_raw_and_unchanged(self):
        for shared in (False, True):
            candidate = self.fixture.alternate(f"https://legacy.example.org/{shared}.png")
            saved = images.capture_product_image(self.memory, self.cid, candidate,
                media_root=self.media, fetcher=self.fixture.fetch, shared_acquisitions=shared)
            before = self.memory.snapshot()
            self.assertIsNotNone(self.selected([candidate]))
            self.assertIsNone(self.selected([candidate], require_report_quality=True))
            self.assertFalse(images.candidate_report_quality(before, self.cid, candidate))
            Path(saved["image"]["path"]).unlink(missing_ok=True)
            self.assertFalse(self.ready(candidate, require_report_quality=True)["ready"])
            self.assertEqual(self.memory.snapshot(), before)

    def test_adequate_shared_original_missing_media_keeps_local_recovery(self):
        saved = images.capture_product_image(self.memory, self.cid, self.candidate,
            media_root=self.media, fetcher=self.fetch((160, 128)), shared_acquisitions=True)
        Path(saved["image"]["path"]).unlink()
        self.assertTrue(self.ready(require_report_quality=True)["local_saved"])
        recovered = images.capture_product_image(self.memory, self.cid, self.candidate,
            media_root=self.media, fetcher=self.fixture.fetch, shared_acquisitions=True)
        self.assertTrue(recovered["reused"])
        self.assertIsNotNone(self.selected(require_report_quality=True))
        self.assertEqual(len(self.fixture.calls), 1)

    def test_legacy_missing_sole_original_parks_without_weakening_raw_or_frozen_proof(self):
        saved = images.capture_product_image(self.memory, self.cid, self.candidate,
            media_root=self.media, fetcher=self.fetch((128, 256)))
        path = Path(saved["image"]["path"])
        raw = path.read_bytes()
        before = self.memory.snapshot()
        path.unlink()
        self.assertTrue(images.candidate_report_quality(before, self.cid, self.candidate))
        self.assertTrue(self.ready()["ready"])  # Preserve the default/raw hint.
        parked = self.ready(state=before, require_report_quality=True)
        self.assertFalse(parked["ready"])
        self.assertFalse(parked["local_saved"])
        self.assertIsNone(parked["next_retry_at"])
        self.assertEqual(parked["reason"], "PRODUCT_IMAGE_SAVED_BYTES_CHANGED")
        self.assertEqual(self.selected(verify_bytes=False)["id"], saved["reference"])
        with self.assertRaisesRegex(SafetyError, "PRODUCT_IMAGE_SAVED_BYTES_CHANGED"):
            images.capture_product_image(self.memory, self.cid, self.candidate,
                media_root=self.media, fetcher=self.fixture.fetch)
        self.assertEqual(self.fixture.calls, [self.candidate["url"]])
        self.assertEqual(self.memory.snapshot(), before)
        atomic_bytes(path, raw)  # A real local restoration can wake this hint.
        restored = self.ready(state=before, require_report_quality=True)
        self.assertTrue(restored["ready"])
        self.assertTrue(restored["local_saved"])
        self.assertNotEqual(restored["signal"], parked["signal"])
        self.assertIsNotNone(self.selected(require_report_quality=True))
        self.assertEqual(self.memory.snapshot(), before)

    def test_three_missing_legacy_originals_do_not_hide_fourth_actionable_candidate(self):
        candidates = [self.candidate] + [self.fixture.alternate(f"https://fourth.example.org/{index}.png")
            for index in range(3)]
        for index, candidate in enumerate(candidates[:3]):
            saved = images.capture_product_image(self.memory, self.cid, candidate,
                media_root=self.media, fetcher=self.fetch((128 + index, 256)))
            Path(saved["image"]["path"]).unlink()
        before = self.memory.snapshot()
        ready = [candidate for candidate in candidates
            if self.ready(candidate, state=before, require_report_quality=True)["ready"]][:3]
        self.assertEqual(ready, [candidates[3]])
        self.assertEqual(self.memory.snapshot(), before)
        saved = self.fixture.capture(candidate=ready[0], fetcher=self.fetch((128, 256)))
        self.assertEqual(saved["state"], "usable")
        self.assertEqual(self.fixture.calls, [candidate["url"] for candidate in candidates])
        self.assertEqual(self.selected(candidates, require_report_quality=True)["id"], saved["reference"])

    def test_failed_original_is_unknown_quality_but_not_report_retry_work(self):
        for shared, renewable in ((False, False), (True, False), (True, True)):
            candidate = self.fixture.alternate(f"https://failed.example.org/{shared}-{renewable}.png")
            images.capture_product_image(self.memory, self.cid, candidate, media_root=self.media,
                fetcher=self.fixture.failed(404), shared_acquisitions=shared,
                renewable_acquisitions=renewable, as_of=self.fixture.at)
            state = self.memory.snapshot()
            self.assertIsNone(images.candidate_report_quality(state, self.cid, candidate))
            self.assertFalse(self.ready(candidate, state=state, require_report_quality=True)["ready"])

    def test_uncertain_original_stays_unknown_and_does_not_wake(self):
        self.fixture.capture(fetcher=self.fixture.failed(None, reason="PRODUCT_IMAGE_DEADLINE", closed=False))
        state = self.memory.snapshot()
        self.assertIsNone(images.candidate_report_quality(state, self.cid, self.candidate))
        hint = self.ready(state=state, require_report_quality=True)
        self.assertFalse(hint["ready"])
        self.assertIsNone(hint["next_retry_at"])
        self.assertEqual(len(self.fixture.calls), 1)

    def test_reserved_unstarted_original_retains_normal_admission_readiness(self):
        policy = deepcopy(DEFAULT_POLICY)
        policy["purpose_items"]["photos"] = 1
        turn = open_work_turn(self.memory, "synthetic-report-photo", at=self.fixture.at, policy=policy)
        with bind_work_turn(self.memory, turn["id"]):
            self.fixture.capture(candidate=self.fixture.alternate("https://other.example.org/first.png"))
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_PURPOSE_LIMIT"):
                self.fixture.capture()
        state = self.memory.snapshot()
        self.assertIsNone(images.candidate_report_quality(state, self.cid, self.candidate))
        self.assertTrue(self.ready(state=state, require_report_quality=True)["ready"])
        self.assertEqual(len(self.fixture.calls), 1)

    def test_invalid_original_metadata_is_not_silently_treated_as_small(self):
        saved = self.fixture.capture()
        for field in ("response", "read"):
            with self.subTest(field=field):
                state = self.memory.snapshot()
                if field == "response":
                    owner = state["batch_runs"][saved["physical_acquisition_id"]]
                    state["source_checks"][owner["active_response_id"]]["body_sha256"] = "a" * 64
                else:
                    original = state["source_checks"][saved["acquisition_ref"]]
                    original["image"]["width"] = 400
                    original["record_hash"] = digest({k: v for k, v in original.items() if k != "record_hash"})
                with self.assertRaises(SafetyError):
                    images.candidate_report_quality(state, self.cid, self.candidate)
                with self.assertRaises(SafetyError):
                    self.ready(state=state, require_report_quality=True)
        self.assertEqual(len(self.fixture.calls), 1)

    def test_invalid_candidate_is_not_a_quality_rejection(self):
        with self.assertRaisesRegex(SafetyError, "PRODUCT_IMAGE_URL_NOT_IN_SOURCE"):
            images.candidate_report_quality(self.memory.snapshot(), self.cid,
                {**self.candidate, "url": "https://other.example.org/not-observed.png"})

    def test_quality_hints_use_supplied_state_without_snapshot_or_file_mutation(self):
        self.fixture.capture()
        state = self.memory.snapshot()
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("No cold replay")):
            self.assertFalse(self.ready(state=state, require_report_quality=True)["ready"])
            self.assertFalse(images.candidate_report_quality(state, self.cid, self.candidate))
        self.assertEqual(self.memory.snapshot(), state)


if __name__ == "__main__":
    unittest.main()
