"""Report-facing photo continuation; synthetic local originals, no network."""

from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from product_radar.operations import operation_run, operation_writer, read_operation_status
from product_radar.product_images import current_public_image
import product_radar.programme as programme
import test_photo_operation_progress as photo_fixtures


class ProgrammeReportPhotoQualityTests(unittest.TestCase):
    setUp = photo_fixtures.PhotoOperationProgressTests.setUp
    stage = photo_fixtures.PhotoOperationProgressTests.stage
    rows = photo_fixtures.PhotoOperationProgressTests.rows

    def small_fetch(self, url):
        with patch("test_product_images.sample_bytes", return_value=photo_fixtures.report_image_bytes(60, 60)):
            return self.fixture.fetch(url)

    def alternatives(self, count):
        return [self.fixture.alternate(f"https://images.example.org/alternative-{number}.png")
                for number in range(count)]

    def readiness(self, *, detailed=False):
        with patch("product_radar.report_selection.build_report_pool", return_value=self.pool):
            return programme._photo_turn_ready(self.memory, self.memory.snapshot(), self.record,
                self.fixture.at, media_root=self.fixture.media, detailed=detailed)

    def test_new_small_download_keeps_original_success_and_uses_next_photo(self):
        fallback = self.alternatives(1)[0]
        self.pool["facts_by_concept"][self.cid]["image_candidates"].append(fallback)
        def fetch(url):
            return self.small_fetch(url) if url == self.candidate["url"] else self.fixture.fetch(url)
        with operation_run(self.root) as recorder, operation_writer():
            result = self.stage(photo_fetcher=fetch)
            self.assertEqual(result["outcomes"][0]["state"], "usable")
            events = [row for row in self.rows(recorder) if row.get("phase_state") == "photo_result"]
            self.assertEqual([row["state"] for row in events], ["omitted", "usable"])
            self.assertEqual([row["photo_applied"] for row in events], [False, True])
            self.assertEqual(events[0]["next_action"], "continue_to_next_photo_candidate")
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 1)
        state = self.memory.snapshot()
        first = state["source_checks"][result["outcomes"][0]["references"][0]]
        self.assertEqual(first["state"], "usable")
        self.assertEqual(first["image"]["width"], 60)
        self.assertIsNotNone(current_public_image(state, self.cid, [self.candidate], media_root=self.fixture.media))
        self.assertIsNone(current_public_image(state, self.cid, [self.candidate], media_root=self.fixture.media,
                                              require_report_quality=True))
        self.assertEqual(len(self.fixture.calls), 2)
        self.assertEqual(self.stage()["outcomes"], [])
        self.assertEqual(len(self.fixture.calls), 2)

    def test_three_saved_small_photos_do_not_hide_fourth_or_trust_old_provenance(self):
        candidates = [self.candidate] + self.alternatives(3)
        for candidate in candidates[:3]:
            self.fixture.capture(candidate=candidate, fetcher=self.small_fetch)
        self.pool["facts_by_concept"][self.cid].update(image_candidates=candidates,
            image_provenance={"id": "old-small-photo-not-a-new-quality-proof"})
        self.assertTrue(self.readiness())
        result = self.stage()
        self.assertEqual(result["outcomes"][0]["state"], "usable")
        self.assertEqual(self.fixture.calls, [row["url"] for row in candidates])
        photo = current_public_image(self.memory.snapshot(), self.cid, candidates, media_root=self.fixture.media,
                                     require_report_quality=True)
        self.assertEqual(photo["source_url"], candidates[3]["url"])

    def test_three_terminal_failures_do_not_hide_fourth_actionable_photo(self):
        candidates = [self.candidate] + self.alternatives(3)
        for candidate in candidates[:3]:
            self.fixture.capture(candidate=candidate, fetcher=self.fixture.failed(404))
        self.pool["facts_by_concept"][self.cid]["image_candidates"] = candidates
        result = self.stage()
        self.assertEqual(result["outcomes"][0]["state"], "usable")
        self.assertEqual(self.fixture.calls, [row["url"] for row in candidates])

    def test_only_small_photo_settles_without_retry_progress_or_another_request(self):
        with operation_run(self.root), operation_writer():
            result = self.stage(photo_fetcher=self.small_fetch)
            self.assertEqual(result["outcomes"][0]["state"], "omitted")
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 0)
        self.assertEqual(self.record["deferrals"], {})
        self.assertFalse(self.readiness())
        self.assertFalse(self.readiness(detailed=True)["ready"])
        before = deepcopy(self.memory.snapshot()["source_checks"])
        self.assertEqual(self.stage()["outcomes"], [])
        self.assertEqual(self.fixture.calls, [self.candidate["url"]])
        self.assertEqual(self.memory.snapshot()["source_checks"], before)

    def test_good_missing_bytes_remain_recoverable_after_a_small_candidate(self):
        fallback = self.alternatives(1)[0]
        small = self.fixture.capture(fetcher=self.small_fetch)
        good = self.fixture.capture(candidate=fallback)
        Path(good["image"]["path"]).unlink()
        self.pool["facts_by_concept"][self.cid]["image_candidates"].append(fallback)
        original_small = deepcopy(self.memory.snapshot()["source_checks"][small["reference"]])
        self.assertTrue(self.readiness(detailed=True)["ready"])
        self.assertEqual(self.stage()["outcomes"][0]["state"], "usable")
        self.assertTrue(Path(good["image"]["path"]).is_file())
        self.assertEqual(len(self.fixture.calls), 2)
        self.assertEqual(self.memory.snapshot()["source_checks"][small["reference"]], original_small)

    def test_legacy_terminal_failures_do_not_hide_fourth_without_active_turn(self):
        from product_radar.product_images import capture_product_image
        candidates = [self.candidate] + self.alternatives(3)
        for candidate in candidates[:3]:
            capture_product_image(self.memory, self.cid, candidate, media_root=self.fixture.media,
                                  fetcher=self.fixture.failed(404))
        self.pool["facts_by_concept"][self.cid]["image_candidates"] = candidates
        with patch("product_radar.report_selection.build_report_pool", return_value=self.pool):
            result = programme._photos(self.memory, self.record, self.context)
        self.assertEqual(result["outcomes"][0]["state"], "usable")
        self.assertEqual(self.fixture.calls, [row["url"] for row in candidates])

    def missing_legacy_originals_flow(self, *, use_turn):
        from product_radar.product_images import capture_product_image
        candidates = [self.candidate] + self.alternatives(3)
        originals = []
        for number, candidate in enumerate(candidates[:3]):
            def fetch(url, number=number):
                with patch("test_product_images.sample_bytes", return_value=
                           photo_fixtures.report_image_bytes(256 + number, 192)):
                    return self.fixture.fetch(url)
            saved = capture_product_image(self.memory, self.cid, candidate,
                media_root=self.fixture.media, fetcher=fetch)
            originals.append(deepcopy(self.memory.snapshot()["source_checks"][saved["reference"]]))
            Path(saved["image"]["path"]).unlink()
        self.pool["facts_by_concept"][self.cid]["image_candidates"] = candidates
        self.assertTrue(self.readiness(detailed=True)["ready"])
        if use_turn:
            result = self.stage()
        else:
            with patch("product_radar.report_selection.build_report_pool", return_value=self.pool):
                result = programme._photos(self.memory, self.record, self.context)
        self.assertEqual(result["outcomes"][0]["state"], "usable")
        self.assertEqual(self.fixture.calls, [row["url"] for row in candidates])
        state = self.memory.snapshot()
        self.assertEqual([state["source_checks"][row["reference"]] for row in originals], originals)
        self.assertEqual(self.record["deferrals"], {})
        self.assertFalse(self.readiness())
        with patch("product_radar.report_selection.build_report_pool", return_value=self.pool):
            self.assertEqual(programme._photos(self.memory, self.record, self.context)["outcomes"], [])
        self.assertEqual(len(self.fixture.calls), 4)

    def test_missing_legacy_originals_do_not_hide_fourth_with_active_turn(self):
        self.missing_legacy_originals_flow(use_turn=True)

    def test_missing_legacy_originals_do_not_hide_fourth_without_active_turn(self):
        self.missing_legacy_originals_flow(use_turn=False)

    def corrupted_legacy_originals_flow(self, *, use_turn):
        from product_radar.product_images import capture_product_image, product_image_readiness
        from product_radar.storage import atomic_bytes
        from product_radar.work_turns import active_work_turn
        candidates = [self.candidate] + self.alternatives(3)
        originals, corrupted_files = [], {}
        for number, candidate in enumerate(candidates[:3]):
            # Keep these originals distinct from the healthy fourth image,
            # which uses the fixture's normal 256 x 192 bytes.
            with patch("test_product_images.sample_bytes", return_value=
                       photo_fixtures.report_image_bytes(257 + number, 192)):
                saved = capture_product_image(self.memory, self.cid, candidate,
                    media_root=self.fixture.media, fetcher=self.fixture.fetch)
            originals.append(deepcopy(self.memory.snapshot()["source_checks"][saved["reference"]]))
            path = Path(saved["image"]["path"])
            raw = path.read_bytes()
            damaged = bytes([raw[0] ^ 1]) + raw[1:]
            atomic_bytes(path, damaged)
            self.assertEqual(path.stat().st_size, saved["image"]["bytes"])
            corrupted_files[path] = damaged
        self.pool["facts_by_concept"][self.cid]["image_candidates"] = candidates
        before = self.memory.snapshot()
        # The cheap scheduling hint intentionally does not decode every saved
        # file. A later exact local adoption failure must not take the place
        # of one of the three actual alternate attempts.
        hints = [product_image_readiness(self.memory, self.cid, candidate,
            as_of=self.fixture.at, state=before, require_report_quality=True) for candidate in candidates]
        self.assertEqual([row["ready"] for row in hints], [True] * 4)
        self.assertEqual([row["local_saved"] for row in hints], [True, True, True, False])
        seen_counters = []
        def observe(row):
            if row.get("phase_state") == "photo_result" and use_turn:
                seen_counters.append(deepcopy(active_work_turn(self.memory)["counters"]))
        with operation_run(self.root) as recorder, operation_writer():
            if use_turn:
                result = self.stage(on_progress=observe)
            else:
                with patch("product_radar.report_selection.build_report_pool", return_value=self.pool):
                    result = programme._photos(self.memory, self.record, self.context)
            self.assertEqual(result["outcomes"][0]["state"], "usable")
            events = [row for row in self.rows(recorder) if row.get("phase_state") == "photo_result"]
            self.assertEqual([row["reason"] for row in events[:3]], ["PRODUCT_IMAGE_SAVED_BYTES_CHANGED"] * 3)
            self.assertEqual([row["photo_applied"] for row in events], [False, False, False, True])
            self.assertEqual(sum(row.get("useful_result") is True for row in self.rows(recorder)), 1)
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 1)
        self.assertEqual(self.fixture.calls, [row["url"] for row in candidates])
        if use_turn:
            self.assertEqual([row["purpose_items"]["photos"] for row in seen_counters], [0, 0, 0, 1])
            self.assertTrue(all(row["source_callbacks"] == 0 and row["model_executions"] == 0
                                for row in seen_counters))
        else:
            self.assertEqual(self.memory.snapshot()["batch_runs"], before["batch_runs"])
        state = self.memory.snapshot()
        self.assertEqual([state["source_checks"][row["reference"]] for row in originals], originals)
        for path, raw in corrupted_files.items():
            self.assertEqual(path.read_bytes(), raw)
        selected = current_public_image(state, self.cid, candidates, media_root=self.fixture.media,
                                        require_report_quality=True)
        self.assertEqual(selected["source_url"], candidates[3]["url"])
        self.assertEqual(self.record["deferrals"], {})
        self.assertFalse(self.readiness())
        with patch("product_radar.report_selection.build_report_pool", return_value=self.pool):
            self.assertEqual(programme._photos(self.memory, self.record, self.context)["outcomes"], [])
        self.assertEqual(len(self.fixture.calls), 4)

    def test_corrupted_legacy_originals_do_not_hide_fourth_with_active_turn(self):
        self.corrupted_legacy_originals_flow(use_turn=True)

    def test_corrupted_legacy_originals_do_not_hide_fourth_without_active_turn(self):
        self.corrupted_legacy_originals_flow(use_turn=False)

    def test_three_unsaved_http_failures_still_cap_attempts_before_fourth(self):
        from product_radar.work_turns import active_work_turn
        candidates = [self.candidate] + self.alternatives(3)
        self.pool["facts_by_concept"][self.cid]["image_candidates"] = candidates
        counters = []
        def observe(row):
            if row.get("phase_state") == "photo_result":
                counters.append(deepcopy(active_work_turn(self.memory)["counters"]))
        with operation_run(self.root) as recorder, operation_writer():
            result = self.stage(photo_fetcher=self.fixture.failed(404), on_progress=observe)
            self.assertEqual(result["outcomes"][0]["state"], "failed")
            self.assertEqual(len(result["outcomes"][0]["references"]), 3)
            events = [row for row in self.rows(recorder) if row.get("phase_state") == "photo_result"]
            self.assertEqual(len(events), 3)
            self.assertTrue(all(row["photo_applied"] is False for row in events))
            self.assertFalse(any(row.get("useful_result") is True for row in self.rows(recorder)))
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 0)
        self.assertEqual(self.fixture.calls, [row["url"] for row in candidates[:3]])
        self.assertNotIn(candidates[3]["url"], self.fixture.calls)
        self.assertEqual([row["purpose_items"]["photos"] for row in counters], [1, 2, 3])
        self.assertTrue(all(row["source_callbacks"] == 0 and row["model_executions"] == 0 for row in counters))


if __name__ == "__main__":
    unittest.main()
