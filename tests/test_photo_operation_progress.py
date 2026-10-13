"""Actual offline photo controllers and advisory progress, never provider calls."""

from contextlib import redirect_stderr
from copy import deepcopy
from datetime import timedelta
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from PIL import Image

import product_radar.programme as programme
from product_radar.models import SafetyError, timestamp
from product_radar.operations import (operation_run, operation_writer, read_operation_status,
                                      record_event, record_progress)
from product_radar.product_images import current_public_image, fetch_product_image
from product_radar.work_turns import (DEFAULT_POLICY, active_work_turn, bind_work_turn,
                                     close_work_turn, open_work_turn)
import test_renewable_product_images as fixtures
import test_public_pages


def report_image_bytes(width=256, height=192):
    stream = io.BytesIO()
    Image.new("RGB", (width, height), (12, 24, 36)).save(stream, format="PNG")
    return stream.getvalue()


class PhotoOperationProgressTests(unittest.TestCase):
    def setUp(self):
        # These tests exercise report-photo progress, not the raw decoder's
        # intentionally tiny transport fixture. Keep that fixture unchanged.
        self.report_bytes = self.enterContext(patch(
            "test_product_images.sample_bytes", side_effect=report_image_bytes))
        self.fixture = fixtures.RenewableImageTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.memory, self.cid, self.candidate = self.fixture.memory, self.fixture.cid, self.fixture.candidate
        self.root = self.fixture.media.parent
        self.record = {"programme_id": "programme-photo-progress", "concept_ids": [self.cid], "client_id": "default",
                       "source_run_id": "source-photo", "deferrals": {}, "follow_on_reviews": {}}
        self.context = {"stage": "photos", "at": self.fixture.at, "fixed_as_of": self.fixture.at,
                        "photo_limit": 3, "retry_seconds": 300, "media_root": self.fixture.media,
                        "photo_fetcher": self.fixture.fetch}
        self.pool = {"facts_by_concept": {self.cid: {"image_candidates": [self.candidate]}}}

    def stage(self, *, policy=None, before=None, **options):
        context = dict(self.context, **options)
        turn = open_work_turn(self.memory, self.record["programme_id"], at=context["at"], policy=policy)
        try:
            with bind_work_turn(self.memory, turn["id"]), \
                 patch("product_radar.report_selection.build_report_pool", return_value=self.pool):
                if before is not None:
                    before()
                return programme._photos(self.memory, self.record, context)
        finally:
            close_work_turn(self.memory, turn["id"], at=context["at"], reason="Offline photo progress test complete")

    def rows(self, recorder):
        return [json.loads(line) for line in (recorder.folder / (recorder.run_id + ".jsonl")).read_text().splitlines()]

    def test_real_success_is_visible_before_stage_finish_and_is_not_counted_twice(self):
        observations = []
        def observe(row):
            if row.get("phase_state") == "photo_result":
                observations.append(read_operation_status(self.root))
        with operation_run(self.root) as recorder, operation_writer():
            programme._observe(self.record, self.context, phase_state="started")
            result = self.stage(on_progress=observe)
            self.assertEqual(result["outcomes"][0]["state"], "usable")
            self.assertEqual(len(self.fixture.calls), 1)
            self.assertEqual(len(observations), 1)
            current = observations[0]
            self.assertEqual(current["counts"]["completed_results"], 1)
            self.assertEqual(current["phase"], "photos")
            self.assertIsNone(current["phase_deadline_seconds"])
            self.assertEqual(current["last_event"]["concept_id"], self.cid)
            self.assertEqual(current["last_event"]["next_action"], "continue_to_next_photo_product")
            programme._observe(self.record, self.context, phase_state="finished", result=deepcopy(result))
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 1)
        self.assertEqual(sum(row.get("useful_result") is True for row in self.rows(recorder)), 1)

    def test_missing_media_restoration_counts_once_but_unchanged_next_pass_does_not(self):
        saved = self.fixture.capture()
        Path(saved["image"]["path"]).unlink()
        with operation_run(self.root), operation_writer():
            programme._observe(self.record, self.context, phase_state="started")
            restored = self.stage()
            self.assertEqual(restored["outcomes"][0]["state"], "usable")
            self.assertEqual(len(self.fixture.calls), 1)
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 1)
            record_progress({"stage": "photos", "phase_state": "finished", "result": restored})
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 1)
            programme._observe(self.record, self.context, phase_state="started")
            replayed = self.stage()
            self.assertEqual(replayed["outcomes"], [])
            record_progress({"stage": "photos", "phase_state": "finished", "result": replayed})
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 1)
            self.assertEqual(len(self.fixture.calls), 1)

    def test_http_failure_uses_next_candidate_then_continues_to_next_product(self):
        fallback = self.fixture.alternate("https://healthy.example.org/alternative.png")
        second = self.fixture.second_product()
        second_candidate = self.fixture.alternate("https://another.example.net/product.png")
        self.record["concept_ids"].append(second)
        self.pool["facts_by_concept"][self.cid]["image_candidates"].append(fallback)
        self.pool["facts_by_concept"][second] = {"image_candidates": [second_candidate]}
        def fetch(url):
            return (self.fixture.failed(404)(url) if url == self.candidate["url"]
                    else self.fixture.fetch(url))
        seen = []
        def observe(row):
            if row.get("phase_state") == "photo_result":
                seen.append(deepcopy(read_operation_status(self.root)))
        with operation_run(self.root) as recorder, operation_writer():
            programme._observe(self.record, self.context, phase_state="started")
            result = self.stage(photo_fetcher=fetch, on_progress=observe)
            self.assertEqual([row["state"] for row in result["outcomes"]], ["usable", "usable"])
            self.assertEqual(len(self.fixture.calls), 3)
            self.assertEqual([row["counts"]["completed_results"] for row in seen], [0, 1, 2])
            self.assertEqual(seen[0]["last_event"]["reason"], "PRODUCT_IMAGE_HTTP_FAILED")
            self.assertEqual(seen[0]["last_event"]["next_action"], "continue_to_next_photo_candidate")
            self.assertEqual(seen[-1]["last_event"]["next_action"], "continue_to_next_photo_product")
            self.assertEqual(seen[-1]["counts"]["failures"], 1)
            programme._observe(self.record, self.context, phase_state="finished", result=result)
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 2)
        events = self.rows(recorder)
        self.assertEqual(sum(row.get("phase_state") == "photo_candidate" for row in events), 3)
        self.assertEqual(sum(row.get("phase_state") == "photo_result" for row in events), 3)
        self.assertNotIn("https://", json.dumps(events))
        self.assertNotIn("alternative.png", json.dumps(events))

    def test_local_exception_is_logged_without_private_text_and_fallback_succeeds(self):
        fallback = self.fixture.alternate("https://healthy.example.org/alternative.png")
        self.pool["facts_by_concept"][self.cid]["image_candidates"].append(fallback)
        def fetch(url):
            if url == self.candidate["url"]:
                self.fixture.calls.append(url)
                raise OSError("private-cookie-and-private-url")
            return self.fixture.fetch(url)
        with operation_run(self.root) as recorder, operation_writer():
            result = self.stage(photo_fetcher=fetch)
            self.assertEqual(result["outcomes"][0]["state"], "usable")
            current = read_operation_status(self.root)
            self.assertEqual(current["counts"]["completed_results"], 1)
            self.assertEqual(current["counts"]["failures"], 1)
            self.assertEqual(len(self.fixture.calls), 2)
        failures = [row for row in self.rows(recorder) if row.get("error_type")]
        self.assertEqual(failures[0]["reason"], "PRODUCT_IMAGE_LOCAL_SAVE_FAILED")
        self.assertEqual(failures[0]["next_action"], "continue_to_next_photo_candidate")
        self.assertNotIn("private-cookie", json.dumps(self.rows(recorder)))

    def test_all_failed_candidates_record_no_useful_progress_and_keep_normal_deferral(self):
        with operation_run(self.root) as recorder, operation_writer():
            result = self.stage(photo_fetcher=self.fixture.failed(404))
            programme._observe(self.record, self.context, phase_state="finished", result=result)
            current = read_operation_status(self.root)
            self.assertEqual(current["counts"]["completed_results"], 0)
            self.assertIsNone(current["last_useful_result_at"])
            self.assertEqual(current["counts"]["failures"], 1)
        self.assertEqual(self.record["deferrals"]["photos:" + self.cid]["reason"], "PRODUCT_IMAGE_HTTP_FAILED")
        results = [row for row in self.rows(recorder) if row.get("phase_state") == "photo_result"]
        self.assertEqual(results[0]["next_action"], "continue_to_next_photo_product")
        self.assertFalse(results[0]["photo_applied"])

    def test_existing_photos_and_terminal_originals_are_not_repeated_or_credited(self):
        self.fixture.capture(fetcher=self.fixture.failed(404))
        with operation_run(self.root) as recorder, operation_writer():
            result = self.stage()
            programme._observe(self.record, self.context, phase_state="finished", result=result)
            self.assertEqual(result["outcomes"], [])
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 0)
        self.assertEqual(len(self.fixture.calls), 1)
        self.assertFalse(any(row.get("phase_state") == "photo_candidate" for row in self.rows(recorder)))

    def test_saved_original_missing_consumer_applies_with_spent_photo_grant_without_get(self):
        original_commit = self.memory.commit
        def refuse_consumer(event_id, changes, **options):
            if any(row["value"].get("version") == fixtures.RENEWABLE_VERSION for row in changes):
                raise SafetyError("RUNNER_ALREADY_ACTIVE")
            return original_commit(event_id, changes, **options)
        with patch.object(self.memory, "commit", side_effect=refuse_consumer):
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                self.fixture.capture()
        self.assertEqual(len(self.fixture.calls), 1)
        policy = deepcopy(DEFAULT_POLICY); policy["purpose_items"]["photos"] = 1
        other = self.fixture.alternate("https://healthy.example.org/used-photo-slot.png")
        counters = []
        def observe(row):
            if row.get("phase_state") == "photo_result":
                counters.append(active_work_turn(self.memory)["counters"]["purpose_items"]["photos"])
        with operation_run(self.root), operation_writer():
            result = self.stage(policy=policy, before=lambda: self.fixture.capture(candidate=other), on_progress=observe)
            self.assertEqual(result["outcomes"][0]["state"], "usable")
            self.assertEqual(len(self.fixture.calls), 2)  # original + actual allowance-consuming other photo
            self.assertEqual(counters, [1])
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 1)
            replayed = self.stage(policy=policy)
            self.assertEqual(replayed["outcomes"], [])
            self.assertEqual(len(self.fixture.calls), 2)
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 1)
        self.assertIsNotNone(current_public_image(self.memory.snapshot(), self.cid, [self.candidate],
                                                media_root=self.fixture.media))

    def test_parent_photo_clock_and_existing_deadline_do_not_restart_on_item_updates(self):
        clock = [self.fixture.at]
        seen = []
        def fetch(url):
            clock[0] = (timestamp(self.fixture.at) + timedelta(seconds=401)).isoformat()
            return self.fixture.fetch(url)
        def observe(row):
            if row.get("phase_state") in {"photo_candidate", "photo_result"}:
                seen.append(read_operation_status(self.root, as_of=clock[0]))
        with patch("product_radar.operations.utc_now", side_effect=lambda: clock[0]), \
             operation_run(self.root) as recorder, operation_writer():
            programme._observe(self.record, self.context, phase_state="started", deadline_seconds=500)
            result = self.stage(photo_fetcher=fetch, on_progress=observe)
            current = read_operation_status(self.root, as_of=clock[0])
            self.assertEqual(current["seconds_in_current_phase"], 401)
            self.assertEqual(current["seconds_since_activity"], 0)
            self.assertEqual(current["seconds_since_useful_result"], 0)
            self.assertIsNone(current["attention"])
            self.assertTrue(all(row["phase"] == "photos" and row["phase_started_at"] == self.fixture.at
                                and row["phase_deadline_seconds"] == 500 for row in seen))
            self.assertEqual(result["outcomes"][0]["state"], "usable")
        item_events = [row for row in self.rows(recorder) if row.get("phase_state") in {"photo_candidate", "photo_result"}]
        self.assertTrue(all(row["event"] == "programme_progress" and "deadline_seconds" not in row for row in item_events))
        self.assertIn("photo_candidate", item_events[-1]["timings"])

    def test_logger_status_and_callback_failures_do_not_stop_real_photo_capture(self):
        notice = io.StringIO()
        def callback(_row):
            raise RuntimeError("private-cookie-from-callback")
        with redirect_stderr(notice), operation_run(self.root) as recorder, operation_writer():
            with patch.object(recorder.logger, "info", side_effect=OSError("private-log-path")), \
                 patch("product_radar.operations.atomic_json", side_effect=OSError("private-status-path")):
                result = self.stage(on_progress=callback)
            record_event("after_photo_diagnostic_failure", stage="photos")
            self.assertEqual(result["outcomes"][0]["state"], "usable")
            self.assertEqual(len(self.fixture.calls), 1)
            current = read_operation_status(self.root)
            self.assertTrue(current["recording_failed"])
            self.assertEqual(current["counts"]["completed_results"], 1)
        self.assertEqual(notice.getvalue().count("Research activity could not be saved."), 1)
        self.assertNotIn("private-", notice.getvalue())

    def test_real_spent_photo_admission_is_deferred_not_a_false_failed_website(self):
        policy = deepcopy(DEFAULT_POLICY); policy["purpose_items"]["photos"] = 1
        other = self.fixture.alternate("https://healthy.example.org/used-photo-slot.png")
        with operation_run(self.root) as recorder, operation_writer():
            result = self.stage(policy=policy, before=lambda: self.fixture.capture(candidate=other))
            self.assertEqual(result["outcomes"][0]["state"], "waiting")
            self.assertEqual(result["outcomes"][0]["reason"], "WORK_TURN_PURPOSE_LIMIT")
            self.assertEqual(len(self.fixture.calls), 1)
            current = read_operation_status(self.root)
            self.assertEqual(current["counts"]["completed_results"], 0)
            self.assertEqual(current["counts"]["failures"], 0)
            self.assertEqual(current["counts"]["deferred"], 1)
        results = [row for row in self.rows(recorder) if row.get("phase_state") == "photo_result"]
        self.assertEqual(results[0]["reason"], "WORK_TURN_PURPOSE_LIMIT")
        self.assertNotIn("error_type", results[0])
        self.assertEqual(self.record["deferrals"], {})

    def test_actual_redirect_guard_receipt_is_waiting_without_changing_original_failure(self):
        blocked = self.fixture.alternate("https://blocked.example.org/known.png")
        original = self.fixture.capture(candidate=blocked, fetcher=self.fixture.failed(429, retry_after="900"))
        destination = "https://blocked.example.org/another.png"
        transport = test_public_pages.Transport([
            test_public_pages.Response(b"", status=302, headers={"Location": destination})], detach=True)
        with operation_run(self.root) as recorder, operation_writer():
            with patch("product_radar.product_images.socket.getaddrinfo", side_effect=test_public_pages.resolver), \
                 patch("product_radar.product_images.public_pages._PinnedConnection", side_effect=transport), \
                 patch("product_radar.product_images.utc_now", return_value=self.fixture.at):
                result = self.stage(photo_fetcher=fetch_product_image)
            outcome = result["outcomes"][0]
            self.assertEqual(outcome["state"], "failed")
            self.assertEqual(outcome["reason"], "SOURCE_ACCESS_WAIT")
            capture = self.memory.snapshot()["source_checks"][outcome["references"][0]]
            self.assertEqual(capture["state"], "failed")
            self.assertIsNone(capture["receipt"]["http_status"])
            self.assertEqual(capture["receipt"]["access_wait"]["acquisition_id"], original["physical_acquisition_id"])
            self.assertEqual(len(transport.requests), 1)
            self.assertNotEqual(transport.requests[0]["host"], "blocked.example.org")
            current = read_operation_status(self.root)
            self.assertEqual(current["counts"]["completed_results"], 0)
            self.assertEqual(current["counts"]["failures"], 0)
            self.assertEqual(current["counts"]["deferred"], 1)
            self.assertIsNone(current["last_useful_result_at"])
        results = [row for row in self.rows(recorder) if row.get("phase_state") == "photo_result"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["state"], "waiting")
        self.assertEqual(results[0]["reason"], "SOURCE_ACCESS_WAIT")
        self.assertNotIn("error_type", results[0])
        self.assertEqual(results[0]["next_action"], "continue_to_next_photo_product")
        self.assertEqual(self.record["deferrals"]["photos:" + self.cid]["reason"], "SOURCE_ACCESS_WAIT")

    def test_integrity_failure_is_recorded_and_still_raises_instead_of_trying_fallback(self):
        self.pool["facts_by_concept"][self.cid]["image_candidates"].append(
            self.fixture.alternate("https://healthy.example.org/alternative.png"))
        with operation_run(self.root) as recorder, operation_writer():
            with patch("product_radar.product_images.capture_product_image",
                       side_effect=SafetyError("RESEARCH_EVENTS_INVALID")) as capture:
                with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
                    self.stage()
            self.assertEqual(capture.call_count, 1)
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 0)
        results = [row for row in self.rows(recorder) if row.get("phase_state") == "photo_result"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["concept_id"], self.cid)
        self.assertEqual(results[0]["reason"], "RESEARCH_EVENTS_INVALID")
        self.assertEqual(results[0]["next_action"], "stop_for_integrity_review")

    def test_photo_context_allowlist_rejects_private_urls_text_and_false_flags(self):
        with operation_run(self.root) as recorder, operation_writer():
            record_progress({"stage": "photos", "phase_state": "photo_candidate",
                "concept_id": "https://private.example.org/product", "reason": "private-cookie-reason",
                "photo_applied": "true", "candidate": {"url": "https://private.example.org/image"},
                "context": {"cookie": "private-cookie-context"}, "counts": {"candidate": 1, "candidates": 3}})
        row = self.rows(recorder)[-2]  # result precedes writer release
        self.assertNotIn("concept_id", row)
        self.assertNotIn("photo_applied", row)
        self.assertEqual(row["counts"], {"candidate": 1, "candidates": 3})
        self.assertNotIn("private.example", json.dumps(self.rows(recorder)))
        self.assertNotIn("private-cookie", json.dumps(self.rows(recorder)))

    def test_only_verified_photo_result_flag_credits_useful_work(self):
        with operation_run(self.root), operation_writer():
            for row in ({"stage": "photos", "phase_state": "photo_candidate", "state": "usable", "photo_applied": True},
                        {"stage": "photos", "phase_state": "photo_result", "state": "usable"},
                        {"stage": "photos", "phase_state": "photo_result", "state": "failed", "photo_applied": True},
                        {"stage": "fit", "phase_state": "photo_result", "state": "usable", "photo_applied": True}):
                record_progress(row)
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 0)
            record_progress({"stage": "photos", "phase_state": "photo_result", "state": "usable", "photo_applied": True})
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 1)


if __name__ == "__main__":
    unittest.main()
