"""One real simulated GET, separate reviewed photo bindings; no external calls."""

from copy import deepcopy
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.models import SafetyError, digest, utc_now
from product_radar.product_images import (SHARED_READ_KIND, SHARED_VERSION, capture_product_image,
    current_public_image, fetch_product_image, inspect_image, _shared_read_id)
from product_radar.research_memory import change
from product_radar.storage import atomic_json, load_json
from product_radar.work_turns import (DEFAULT_POLICY, active_work_turn, bind_work_turn, open_work_turn)
from product_radar.programme import set_programme_service_state
import test_product_images as image_fixtures
import test_public_pages


class SharedImageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = image_fixtures.ImageBindingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory = self.fixture.memory
        self.cid = self.fixture.concept_id
        self.candidate = self.fixture.prepare_photo()
        self.media = self.fixture.media
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        raw = image_fixtures.sample_bytes()
        return {"state": "usable", "reason": "Synthetic original photo.", "checked_at": utc_now(),
                "requested_url": url, "final_url": url, "http_status": 200, "redirects": [],
                "body_complete": True, "dispatch_started": True, "metadata": inspect_image(raw)}, raw

    def capture(self, cid=None, candidate=None, **options):
        return capture_product_image(self.memory, cid or self.cid, candidate or self.candidate,
            media_root=self.media, fetcher=options.pop("fetcher", self.fetch), shared_acquisitions=True, **options)

    def second_product(self):
        state = self.memory.snapshot()
        concept = deepcopy(state["concepts"][self.cid])
        concept["description"]["label"] = "A separate independently assessed photo subject"
        self.memory.commit("second-photo-subject", [change("concepts", "second-photo-subject", concept)])
        return "second-photo-subject"

    def test_two_subjects_share_one_get_but_keep_separate_original_bindings(self):
        first = self.capture()
        second_cid = self.second_product()
        second = self.capture(second_cid)
        self.assertEqual(self.calls, [self.candidate["url"]])
        self.assertEqual(first["acquisition_ref"], second["acquisition_ref"])
        self.assertNotEqual(first["reference"], second["reference"])
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["at"], second["at"])
        self.assertEqual(second["observed_at"], self.candidate["observed_at"])
        for cid in (self.cid, second_cid):
            photo = current_public_image(self.memory.snapshot(), cid, [self.candidate], media_root=self.media)
            self.assertIsNotNone(photo)
            self.assertEqual(photo["owner_id"], cid)

    def test_target_commit_conflict_recovers_exact_response_without_another_get(self):
        original = self.memory.commit
        def conflict(event_id, changes, **options):
            if any(row["value"].get("version") == SHARED_VERSION for row in changes):
                raise SafetyError("RUNNER_ALREADY_ACTIVE")
            return original(event_id, changes, **options)
        with patch.object(self.memory, "commit", side_effect=conflict):
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                self.capture()
        result = self.capture()
        self.assertTrue(result["reused"])
        self.assertEqual(len(self.calls), 1)

    def test_response_commit_interruption_recovers_atomic_body_and_receipt(self):
        original = self.memory.commit
        def conflict(event_id, changes, **options):
            if any(row["value"].get("kind") == SHARED_READ_KIND and row["value"].get("state") == "usable" for row in changes):
                raise SafetyError("RUNNER_ALREADY_ACTIVE")
            return original(event_id, changes, **options)
        with patch.object(self.memory, "commit", side_effect=conflict):
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                self.capture()
        result = self.capture()
        self.assertTrue(result["reused"])
        self.assertEqual(len(self.calls), 1)

    def test_missing_shared_media_recovers_saved_original_bytes_without_get(self):
        result = self.capture()
        Path(result["image"]["path"]).unlink()
        self.assertIsNone(current_public_image(self.memory.snapshot(), self.cid, [self.candidate], media_root=self.media))
        recovered = self.capture()
        self.assertEqual(Path(recovered["image"]["path"]).read_bytes(), image_fixtures.sample_bytes())
        self.assertEqual(len(self.calls), 1)

    def test_broken_response_link_does_not_reserve_or_start_a_read(self):
        read_id = _shared_read_id(self.candidate["url"])
        folder = self.memory.root / "image_captures" / "shared" / read_id
        folder.mkdir(parents=True)
        (folder / "response.json").symlink_to(folder / "missing-response.json")
        with self.assertRaisesRegex(SafetyError, "SYMLINK_PATH_REFUSED"):
            self.capture()
        self.assertEqual(self.calls, [])
        self.assertNotIn(read_id, self.memory.snapshot()["source_checks"])

    def test_linked_media_destination_does_not_reserve_or_start_a_read(self):
        destination = self.media.parent / "linked-media"
        destination.symlink_to(self.media, target_is_directory=True)
        with self.assertRaisesRegex(SafetyError, "SYMLINK_PATH_REFUSED"):
            capture_product_image(self.memory, self.cid, self.candidate,
                media_root=destination, fetcher=self.fetch, shared_acquisitions=True)
        self.assertEqual(self.calls, [])
        self.assertNotIn(_shared_read_id(self.candidate["url"]), self.memory.snapshot()["source_checks"])

    def test_unreturned_original_is_not_retried_for_a_different_product(self):
        def lost(url):
            self.calls.append(url)
            raise OSError("Synthetic interrupted source reply")
        with self.assertRaises(OSError):
            self.capture(fetcher=lost)
        with self.assertRaisesRegex(SafetyError, "PRODUCT_IMAGE_ORIGINAL_RESPONSE_PENDING"):
            self.capture(self.second_product())
        self.assertEqual(len(self.calls), 1)

    def test_legacy_success_adoption_keeps_original_date_and_does_not_get_again(self):
        legacy = capture_product_image(self.memory, self.cid, self.candidate,
                                      media_root=self.media, fetcher=self.fetch)
        shared = self.capture(self.second_product())
        owner = self.memory.snapshot()["source_checks"][shared["acquisition_ref"]]
        self.assertEqual(owner["origin_reference"], legacy["reference"])
        self.assertEqual(shared["at"], legacy["at"])
        self.assertEqual(len(self.calls), 1)

    def test_legacy_failed_unknown_is_not_bypassed_by_new_contract(self):
        def failed(url):
            self.calls.append(url)
            return {"state": "failed", "reason": "PRODUCT_IMAGE_DEADLINE", "checked_at": utc_now(),
                    "requested_url": url, "dispatch_started": None}, None
        capture_product_image(self.memory, self.cid, self.candidate, media_root=self.media, fetcher=failed)
        with self.assertRaisesRegex(SafetyError, "PRODUCT_IMAGE_ORIGINAL_RESPONSE_PENDING"):
            self.capture(self.second_product())
        self.assertEqual(len(self.calls), 1)

    def test_shared_404_is_saved_once_and_other_subject_can_use_its_limitation(self):
        def failed(url):
            self.calls.append(url)
            return {"state": "failed", "reason": "PRODUCT_IMAGE_HTTP_FAILED", "checked_at": utc_now(),
                    "requested_url": url, "dispatch_started": True, "http_status": 404}, None
        first = self.capture(fetcher=failed)
        second = self.capture(self.second_product(), fetcher=failed)
        self.assertEqual(first["receipt"]["http_status"], 404)
        self.assertEqual(second["state"], "failed")
        self.assertEqual(len(self.calls), 1)

    def test_corrupt_atomic_response_or_owner_is_not_refetched(self):
        result = self.capture()
        response_path = next((self.memory.root / "image_captures" / "shared").glob("*/response.json"))
        saved = load_json(response_path)
        atomic_json(response_path, {**saved, "hash": "invalid"})
        with self.assertRaisesRegex(SafetyError, "PRODUCT_IMAGE_CAPTURE_INVALID"):
            self.capture(self.second_product())
        self.assertEqual(len(self.calls), 1)
        state = self.memory.snapshot()
        owner = state["source_checks"][result["acquisition_ref"]]
        changed = {**owner, "source_url": "https://example.com/another.png"}
        self.memory.put("source_checks", result["acquisition_ref"], changed, previous=owner)
        with self.assertRaisesRegex(SafetyError, "PRODUCT_IMAGE_SHARED_RECORD_INVALID"):
            self.capture()
        self.assertIsNone(current_public_image(self.memory.snapshot(), self.cid, [self.candidate], media_root=self.media))
        self.assertEqual(len(self.calls), 1)

    def test_physical_get_and_intent_share_one_photo_slot_saved_apply_is_free(self):
        policy = deepcopy(DEFAULT_POLICY)
        policy["purpose_items"]["photos"] = 1
        turn = open_work_turn(self.memory, "photo-programme", at=utc_now(), policy=policy)
        with bind_work_turn(self.memory, turn["id"]):
            first = self.capture()
            self.capture(self.second_product())
            self.assertEqual(active_work_turn(self.memory)["counters"]["purpose_items"]["photos"], 1)
        events = [load_json(path) for path in (self.memory.root / "events").glob("*.json")]
        intents = [event for event in events if any(
            row["value"].get("kind") == SHARED_READ_KIND and row["value"].get("state") == "execution_unknown"
            for row in event["changes"])]
        self.assertEqual(len(intents), 1)
        self.assertEqual({row["table"] for row in intents[0]["changes"]}, {"batch_runs", "source_checks"})
        self.assertEqual(len(self.calls), 1)

    def test_new_photo_waits_when_turn_photo_slots_are_exhausted(self):
        policy = deepcopy(DEFAULT_POLICY)
        policy["purpose_items"]["photos"] = 1
        turn = open_work_turn(self.memory, "photo-programme", at=utc_now(), policy=policy)
        with bind_work_turn(self.memory, turn["id"]):
            self.capture()
            state = self.memory.snapshot()
            obs_id = self.candidate["observation_id"] + "-other-url"
            obs = deepcopy(state["observations"][self.candidate["observation_id"]])
            obs["facts"]["image_urls"] = ["https://example.com/different.png"]
            self.memory.put("observations", obs_id, obs)
            candidate = {**self.candidate, "observation_id": obs_id, "url": obs["facts"]["image_urls"][0]}
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_PURPOSE_LIMIT"):
                self.capture(candidate=candidate)
        self.assertEqual(len(self.calls), 1)

    def test_owner_pause_prevents_new_get_but_does_not_destroy_saved_photo(self):
        set_programme_service_state(self.memory, enabled=False)
        with self.assertRaisesRegex(SafetyError, "PROGRAMME_OWNER_PAUSED"):
            self.capture()
        self.assertEqual(self.calls, [])
        set_programme_service_state(self.memory, enabled=True)
        first = self.capture()
        set_programme_service_state(self.memory, enabled=False)
        second = self.capture(self.second_product())
        self.assertEqual(first["acquisition_ref"], second["acquisition_ref"])
        self.assertEqual(len(self.calls), 1)

    def test_changed_target_during_get_preserves_shared_result_for_healthy_target(self):
        second = self.second_product()
        def changing_fetch(url):
            result = self.fetch(url)
            old = self.memory.snapshot()["concepts"][self.cid]
            changed = deepcopy(old)
            changed["description"]["label"] += " corrected"
            self.memory.put("concepts", self.cid, changed, previous=old)
            return result
        with self.assertRaisesRegex(SafetyError, "PRODUCT_IMAGE_PRODUCT_CHANGED"):
            self.capture(fetcher=changing_fetch)
        result = self.capture(second)
        self.assertEqual(result["state"], "usable")
        self.assertEqual(len(self.calls), 1)

    def test_reply_saved_before_exception_is_recovered_without_get(self):
        from product_radar.product_images import atomic_json as original
        def interrupted(path, value):
            original(path, value)
            if Path(path).name == "response.json":
                raise OSError("Synthetic lost local-save confirmation")
        with patch("product_radar.product_images.atomic_json", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.capture()
        self.assertTrue(self.capture()["reused"])
        self.assertEqual(len(self.calls), 1)

    def test_occupied_intent_does_not_launch_after_a_lost_commit_reply(self):
        original = self.memory.commit
        def lost_reply(event_id, changes, **options):
            result = original(event_id, changes, **options)
            if any(row["value"].get("kind") == SHARED_READ_KIND and row["value"].get("state") == "execution_unknown" for row in changes):
                raise OSError("Synthetic lost intent-save confirmation")
            return result
        with patch.object(self.memory, "commit", side_effect=lost_reply):
            with self.assertRaises(OSError):
                self.capture()
        with self.assertRaisesRegex(SafetyError, "PRODUCT_IMAGE_ORIGINAL_RESPONSE_PENDING"):
            self.capture()
        self.assertEqual(self.calls, [])

    def test_actual_intent_is_saved_before_fetcher_is_entered(self):
        def inspect_then_fetch(url):
            owners = [row for row in self.memory.snapshot()["source_checks"].values()
                      if row.get("kind") == SHARED_READ_KIND]
            self.assertEqual(len(owners), 1)
            self.assertEqual(owners[0]["state"], "execution_unknown")
            return self.fetch(url)
        self.capture(fetcher=inspect_then_fetch)
        self.assertEqual(len(self.calls), 1)

    def test_builtin_shared_reader_requests_access_proof_without_changing_custom_fetchers(self):
        with patch("product_radar.product_images.fetch_product_image", side_effect=self.fetch) as builtin:
            # The built-in adapter receives the new optional flag. Custom
            # injected adapters retain their original single-argument API.
            builtin.side_effect = lambda url, **options: self.fetch(url)
            self.capture(fetcher=builtin)
        builtin.assert_called_once_with(self.candidate["url"], track_access=True)
        self.assertEqual(len(self.calls), 1)

    def test_changed_url_query_does_not_reuse_a_different_physical_image(self):
        first = self.capture()
        state = self.memory.snapshot()
        obs_id = self.candidate["observation_id"] + "-query"
        obs = deepcopy(state["observations"][self.candidate["observation_id"]])
        obs["facts"]["image_urls"] = [self.candidate["url"] + "?variant=folding"]
        self.memory.put("observations", obs_id, obs)
        second = self.capture(candidate={**self.candidate, "observation_id": obs_id,
                                         "url": obs["facts"]["image_urls"][0]})
        self.assertNotEqual(first["acquisition_ref"], second["acquisition_ref"])
        self.assertEqual(len(self.calls), 2)


class ImageHttpStatusTests(unittest.TestCase):
    def fetch(self, responses, **options):
        transport = test_public_pages.Transport(responses, detach=True)
        return fetch_product_image("https://example.com/photo.png", resolver=test_public_pages.resolver,
                                   connection_factory=transport, **options)

    def test_real_transport_preserves_404_instead_of_losing_it_as_unknown(self):
        transport = test_public_pages.Transport([test_public_pages.Response(b"", status=404)], detach=True)
        receipt, raw = fetch_product_image("https://example.com/missing.png", resolver=test_public_pages.resolver,
                                          connection_factory=transport)
        self.assertEqual(receipt["http_status"], 404)
        self.assertTrue(receipt["dispatch_started"])
        self.assertEqual(receipt["state"], "failed")
        self.assertIsNone(raw)

    def test_tracked_refusal_keeps_only_real_bounded_retry_hint_and_worker_proof(self):
        receipt, raw = self.fetch([test_public_pages.Response(b"", status=429, headers={
            "Retry-After": "7200", "Set-Cookie": "private-cookie", "Authorization": "private-key"})],
            track_access=True)
        self.assertEqual(receipt["http_status"], 429)
        self.assertEqual(receipt["retry_after"], "7200")
        self.assertEqual(receipt["execution_stage"], "response_received")
        self.assertIsInstance(receipt["execution_ref"], str)
        self.assertTrue(receipt["owned_worker_exited"])
        self.assertNotIn("owned_process_exited", receipt)
        self.assertNotIn("private-cookie", str(receipt))
        self.assertNotIn("private-key", str(receipt))
        self.assertIsNone(raw)

    def test_malformed_retry_hints_are_not_saved(self):
        for hint in ("1\r\nprivate-header: bad", "1\x7f", "1" * 257, 7200):
            with self.subTest(hint=repr(hint)[:30]):
                receipt, _ = self.fetch([test_public_pages.Response(b"", status=503,
                    headers={"Retry-After": hint})], track_access=True)
                self.assertEqual(receipt["http_status"], 503)
                self.assertNotIn("retry_after", receipt)

    def test_redirect_hint_is_not_borrowed_by_the_final_host(self):
        receipt, raw = self.fetch([
            test_public_pages.Response(b"", status=302, headers={"Location": "https://other.example/photo.png",
                "Retry-After": "7200"}),
            test_public_pages.Response(b"", status=503)], track_access=True)
        self.assertEqual(receipt["final_url"], "https://other.example/photo.png")
        self.assertEqual(receipt["http_status"], 503)
        self.assertNotIn("retry_after", receipt)
        self.assertEqual(len(receipt["redirects"]), 1)
        self.assertIsNone(raw)

    def test_legacy_transport_receipt_does_not_gain_optional_access_fields(self):
        receipt, _ = self.fetch([test_public_pages.Response(b"", status=429, headers={"Retry-After": "7200"})])
        for field in ("retry_after", "execution_stage", "execution_ref", "owned_worker_exited"):
            self.assertNotIn(field, receipt)
        self.assertEqual(receipt["http_status"], 429)

    def test_tracked_success_keeps_original_bytes_and_closed_worker_proof(self):
        original = image_fixtures.sample_bytes()
        receipt, raw = self.fetch([test_public_pages.Response(original, headers={
            "Content-Type": "image/png", "Content-Length": str(len(original))})], track_access=True)
        self.assertEqual(raw, original)
        self.assertEqual(receipt["state"], "usable")
        self.assertEqual(receipt["execution_stage"], "response_received")
        self.assertTrue(receipt["owned_worker_exited"])

    def test_timed_out_dns_worker_is_not_claimed_closed_or_dispatched(self):
        release, finished = threading.Event(), threading.Event()
        def blocked_resolver(*args, **options):
            try:
                release.wait(2)
                return test_public_pages.resolver(*args, **options)
            finally:
                finished.set()
        try:
            receipt, raw = fetch_product_image("https://example.com/photo.png", timeout=.03,
                resolver=blocked_resolver, connection_factory=test_public_pages.Transport([], detach=True),
                track_access=True)
            self.assertEqual(receipt["reason"], "PRODUCT_IMAGE_DEADLINE")
            self.assertEqual(receipt["execution_stage"], "not_dispatched")
            self.assertFalse(receipt["dispatch_started"])
            self.assertFalse(receipt["owned_worker_exited"])
            self.assertIsNone(raw)
        finally:
            release.set()
            self.assertTrue(finished.wait(1))

    def test_body_deadline_preserves_that_the_original_worker_is_still_running(self):
        release, finished = threading.Event(), threading.Event()
        def blocked_body(_size):
            try:
                release.wait(2)
                return b""
            finally:
                finished.set()
        transport = test_public_pages.Transport([test_public_pages.Response(b"", headers={
            "Content-Type": "image/png"}, reader=blocked_body)], detach=True)
        try:
            receipt, raw = fetch_product_image("https://example.com/photo.png", timeout=.03,
                resolver=test_public_pages.resolver, connection_factory=transport, track_access=True)
            self.assertEqual(receipt["reason"], "PRODUCT_IMAGE_DEADLINE")
            self.assertEqual(receipt["execution_stage"], "response_received")
            self.assertTrue(receipt["dispatch_started"])
            self.assertFalse(receipt["owned_worker_exited"])
            self.assertIsNone(raw)
        finally:
            release.set()
            self.assertTrue(finished.wait(1))


if __name__ == "__main__":
    unittest.main()
