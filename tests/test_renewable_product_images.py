"""Real image-owner/controller joins with synthetic bounded HTTP results only."""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import unittest
from unittest.mock import patch

from product_radar.models import SafetyError, digest, timestamp, utc_now
from product_radar.product_images import (RENEWABLE_VERSION, capture_product_image, current_public_image,
    fetch_product_image, inspect_image, product_image_readiness, _image_request,
    _renewable_response_path, _renewable_read_record)
from product_radar.research_memory import change
from product_radar.research_questions import physical_request_fingerprint, prepare_research_questions
from product_radar.source_acquisitions import ACQUISITION_KIND
from product_radar.source_coverage import ROUTES, acquisition_access_scope, source_access_wait
from product_radar.storage import atomic_json
from product_radar.work_turns import (DEFAULT_POLICY, active_work_turn, bind_work_turn,
    close_work_turn, open_work_turn, reserve_stage_items)
import test_product_images as image_fixtures
import test_shared_product_images as shared_fixtures
import test_public_pages


class RenewableImageTests(unittest.TestCase):
    def setUp(self):
        shared_fixtures.SharedImageTests.setUp(self)
        self.at = utc_now()
        self.fetch_at = self.at

    def fetch(self, url):
        self.calls.append(url)
        raw = image_fixtures.sample_bytes()
        return {"state": "usable", "reason": "Synthetic saved original photo.", "checked_at": self.fetch_at,
            "requested_url": url, "final_url": url, "http_status": 200, "redirects": [],
            "body_complete": True, "dispatch_started": True, "metadata": inspect_image(raw),
            "response_received": True, "owned_worker_exited": True, "execution_stage": "response_received"}, raw

    def capture(self, cid=None, candidate=None, *, at=None, fetcher=None):
        return capture_product_image(self.memory, cid or self.cid, candidate or self.candidate,
            media_root=self.media, fetcher=fetcher or self.fetch, shared_acquisitions=True,
            renewable_acquisitions=True, as_of=at or self.at)

    def second_product(self):
        return shared_fixtures.SharedImageTests.second_product(self)

    def alternate(self, url):
        state = self.memory.snapshot()
        oid = "synthetic-alternate-image-" + digest(url)[:12]
        original = deepcopy(state["observations"][self.candidate["observation_id"]])
        original["facts"]["image_urls"] = [url]
        self.memory.put("observations", oid, original)
        return {**self.candidate, "observation_id": oid, "url": url}

    def owners(self):
        return [(rid, row) for rid, row in self.memory.snapshot()["batch_runs"].items()
                if row.get("kind") == ACQUISITION_KIND]

    def later(self, seconds):
        return (timestamp(self.at) + timedelta(seconds=seconds)).isoformat(timespec="seconds")

    def failed(self, status, *, reason="PRODUCT_IMAGE_HTTP_FAILED", closed=True, retry_after=None):
        def read(url):
            self.calls.append(url)
            receipt = {"state": "failed", "reason": reason, "checked_at": self.fetch_at,
                "requested_url": url, "final_url": url, "http_status": status, "redirects": [],
                "body_complete": False, "dispatch_started": True, "metadata": None,
                "response_received": status is not None, "owned_worker_exited": closed}
            if retry_after is not None:
                receipt["retry_after"] = retry_after
            return receipt, None
        return read

    def test_two_consumers_share_exact_global_origin_and_one_atomic_photo_slot(self):
        policy = deepcopy(DEFAULT_POLICY); policy["purpose_items"]["photos"] = 1
        turn = open_work_turn(self.memory, "photo-programme", at=self.at, policy=policy)
        with bind_work_turn(self.memory, turn["id"]):
            first = self.capture()
            second = self.capture(self.second_product())
            self.assertEqual(active_work_turn(self.memory)["counters"]["purpose_items"]["photos"], 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.owners()), 1)
        self.assertEqual(first["physical_acquisition_id"], second["physical_acquisition_id"])
        self.assertNotEqual(first["reference"], second["reference"])
        self.assertEqual(first["version"], RENEWABLE_VERSION)
        for cid in (self.cid, second["concept_id"]):
            self.assertIsNotNone(current_public_image(self.memory.snapshot(), cid, [self.candidate], media_root=self.media))
        owner = self.owners()[0][1]
        self.assertEqual(owner["binding"]["route"], "public_image_http")
        self.assertEqual(owner["binding"]["physical_request_fingerprint"],
            physical_request_fingerprint("public_image_http", _image_request(self.candidate["url"])))

    def test_source_response_save_interruption_replays_atomic_original_without_get(self):
        original_commit = self.memory.commit
        def conflict(event_id, changes, **kwargs):
            if any(row["value"].get("stage_type") == "response" for row in changes):
                raise SafetyError("RUNNER_ALREADY_ACTIVE")
            return original_commit(event_id, changes, **kwargs)
        with patch.object(self.memory, "commit", side_effect=conflict):
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                self.capture()
        result = self.capture()
        self.assertEqual(result["state"], "usable")
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(result["reused"])

    def test_missing_media_and_consumer_save_failure_recover_exact_original(self):
        original_commit = self.memory.commit
        def conflict(event_id, changes, **kwargs):
            if any(row["value"].get("version") == RENEWABLE_VERSION for row in changes):
                raise SafetyError("RUNNER_ALREADY_ACTIVE")
            return original_commit(event_id, changes, **kwargs)
        with patch.object(self.memory, "commit", side_effect=conflict):
            with self.assertRaises(SafetyError): self.capture()
        result = self.capture()
        Path(result["image"]["path"]).unlink()
        recovered = self.capture()
        self.assertEqual(Path(recovered["image"]["path"]).read_bytes(), image_fixtures.sample_bytes())
        self.assertEqual(len(self.calls), 1)

    def test_unreturned_original_is_never_replaced_by_another_consumer(self):
        def lost(url):
            self.calls.append(url)
            raise OSError("Synthetic source reply lost after intent")
        with self.assertRaises(OSError): self.capture(fetcher=lost)
        with self.assertRaisesRegex(SafetyError, "PRODUCT_IMAGE_ORIGINAL_RESPONSE_PENDING"):
            self.capture(self.second_product(), at=self.later(86400))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.owners()), 1)

    def test_terminal404_and_invalid_bytes_park_without_more_generations(self):
        for status, reason in ((404, "PRODUCT_IMAGE_HTTP_FAILED"), (None, "PRODUCT_IMAGE_NOT_DECODABLE")):
            with self.subTest(reason=reason):
                candidate = self.alternate("https://example.com/" + str(status) + ".png")
                result = self.capture(candidate=candidate, fetcher=self.failed(status, reason=reason))
                self.assertEqual(result["failure_classification"], "terminal")
                for days in (1, 30, 365):
                    same = self.capture(candidate=candidate, at=self.later(days * 86400))
                    self.assertEqual(same["physical_acquisition_id"], result["physical_acquisition_id"])
        self.assertEqual(len(self.calls), 2)

    def test_actual429_waits_across_urls_other_host_runs_and_one_due_generation_recovers(self):
        first = self.capture(fetcher=self.failed(429, retry_after="900"))
        self.assertEqual(first["next_retry_at"], self.later(900))
        same = self.capture(self.second_product(), at=self.later(899))
        self.assertEqual(same["physical_acquisition_id"], first["physical_acquisition_id"])
        with self.assertRaisesRegex(SafetyError, "SOURCE_ACCESS_WAIT"):
            self.capture(candidate=self.alternate("https://example.com/another-photo.png"), at=self.later(899))
        self.fetch_at = self.later(899)
        self.capture(candidate=self.alternate("https://healthy.example.org/another-photo.png"), at=self.fetch_at)
        self.assertEqual(len(self.calls), 2)
        self.fetch_at = self.later(900)
        recovered = self.capture(at=self.fetch_at)
        self.assertEqual(recovered["generation"], 2)
        self.assertNotEqual(recovered["physical_acquisition_id"], first["physical_acquisition_id"])
        self.assertEqual(self.capture(self.second_product(), at=self.fetch_at)["reference"],
                         self.capture(self.second_product(), at=self.fetch_at)["reference"])
        self.assertEqual(len(self.calls), 3)
        state = self.memory.snapshot()
        owner = state["batch_runs"][recovered["physical_acquisition_id"]]
        self.assertEqual(owner["binding"]["previous_acquisition_id"], first["physical_acquisition_id"])
        self.assertIsNone(source_access_wait(state, acquisition_access_scope(owner), as_of=self.fetch_at))
        self.assertEqual(state["source_checks"][first["reference"]]["at"], self.at)

    def test_due_probe_reply_lost_remains_pending_and_does_not_launch_again(self):
        first = self.capture(fetcher=self.failed(503, retry_after="300"))
        def lost(url):
            self.calls.append(url)
            raise OSError("Synthetic due probe lost")
        with self.assertRaises(OSError): self.capture(at=self.later(300), fetcher=lost)
        with self.assertRaisesRegex(SafetyError, "PRODUCT_IMAGE_ORIGINAL_RESPONSE_PENDING"):
            self.capture(at=self.later(600))
        with self.assertRaisesRegex(SafetyError, "SOURCE_ACCESS_PROBE_PENDING"):
            self.capture(candidate=self.alternate("https://example.com/other.png"), at=self.later(600))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.owners()), 2)
        self.assertEqual(first["generation"], 1)

    def test_unclosed_http_worker_cannot_create_recovery_generation(self):
        first = self.capture(fetcher=self.failed(429, closed=False, retry_after="1"))
        self.assertEqual(first["failure_classification"], "original_pending")
        self.capture(at=self.later(86400))
        with self.assertRaisesRegex(SafetyError, "SOURCE_ACCESS_ORIGINAL_RECOVERY_REQUIRED"):
            self.capture(candidate=self.alternate("https://example.com/other.png"), at=self.later(86400))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.owners()), 1)

    def test_transient_transport_failure_has_bounded_linked_generations(self):
        failed = self.failed(None, reason="PRODUCT_IMAGE_TRANSPORT_FAILED")
        first = self.capture(fetcher=failed)
        for generation in (2, 3):
            self.fetch_at = self.later((generation - 1) * 300)
            result = self.capture(at=self.fetch_at, fetcher=failed)
            self.assertEqual(result["generation"], generation)
        self.capture(at=self.later(900), fetcher=failed)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(self.owners()), 3)
        self.assertEqual(first["generation"], 1)

    def test_photo_budget_failure_leaves_reserved_owner_unstarted_then_current_turn_admits(self):
        policy = deepcopy(DEFAULT_POLICY); policy["purpose_items"]["photos"] = 1
        turn = open_work_turn(self.memory, "photo-programme", at=self.at, policy=policy)
        with bind_work_turn(self.memory, turn["id"]):
            self.capture(candidate=self.alternate("https://healthy.example.org/first.png"))
            with self.assertRaisesRegex(SafetyError, "WORK_TURN_PURPOSE_LIMIT"):
                self.capture()
        original = next((rid, row) for rid, row in self.owners() if row["binding"]["request"]["url"] == self.candidate["url"])
        self.assertEqual(original[1]["state"], "reserved")
        self.assertEqual(original[1]["stage_ids"], [])
        close_work_turn(self.memory, turn["id"], at=self.at, reason="Synthetic bounded turn complete")
        next_turn = open_work_turn(self.memory, "photo-programme", at=self.at, policy=policy)
        with bind_work_turn(self.memory, next_turn["id"]):
            result = self.capture()
            self.assertEqual(active_work_turn(self.memory)["counters"]["purpose_items"]["photos"], 1)
        self.assertEqual(result["physical_acquisition_id"], original[0])
        self.assertEqual(len(self.calls), 2)

    def test_shared_validator_failure_keeps_photo_counter_and_joined_intent_unchanged(self):
        turn = open_work_turn(self.memory, "photo-programme", at=self.at)
        before = self.memory.snapshot()
        def refusal(state): raise SafetyError("SYNTHETIC_ADMISSION_REFUSED")
        with bind_work_turn(self.memory, turn["id"]):
            with self.assertRaisesRegex(SafetyError, "SYNTHETIC_ADMISSION_REFUSED"):
                reserve_stage_items(self.memory, "photos", "synthetic-intent", 1, at=self.at,
                    changes=[change("source_checks", "synthetic-intent", {"kind": "synthetic"})], validate=refusal)
            self.assertEqual(self.memory.snapshot(), before)
            one = reserve_stage_items(self.memory, "photos", "synthetic-intent", 1, at=self.at,
                changes=[change("source_checks", "synthetic-intent", {"kind": "synthetic"})], validate=lambda _: None)
            two = reserve_stage_items(self.memory, "photos", "synthetic-intent", 1, at=self.at,
                changes=[change("source_checks", "synthetic-intent", {"kind": "synthetic"})], validate=lambda _: None)
            self.assertEqual(one, two)
            self.assertEqual(active_work_turn(self.memory)["counters"]["purpose_items"]["photos"], 1)

    def test_supporting_physical_read_is_not_a_market_route_or_question(self):
        from product_radar.functional_types import effective_functional_definition
        self.assertNotIn("public_image_http", ROUTES)
        physical_request_fingerprint("public_image_http", _image_request(self.candidate["url"]))
        with self.assertRaisesRegex(SafetyError, "RESEARCH_QUESTION_ROUTE_INVALID"):
            prepare_research_questions(self.memory, [{"subject": {"kind": "concept", "concept_id": self.cid,
                "definition_hash": effective_functional_definition(self.memory.snapshot(), self.cid)["meaning_hash"]},
                "route": "public_image_http", "purpose": "validation",
                "request": _image_request(self.candidate["url"])}], as_of=self.at)

    def test_legacy_v2_success_adopts_actual_saved_response_without_new_get(self):
        old = capture_product_image(self.memory, self.cid, self.candidate, media_root=self.media,
                                    fetcher=self.fetch, shared_acquisitions=True)
        renewable = self.capture(self.second_product())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(renewable["at"], old["at"])
        summary = self.memory.snapshot()["source_checks"][renewable["acquisition_ref"]]
        self.assertEqual(summary["origin_reference"], old["acquisition_ref"])
        _renewable_read_record(summary, renewable["acquisition_ref"], self.candidate["url"])

    def test_actual_http_reader_receipt_drives_shared_wait_without_fake_process_exit(self):
        transport = test_public_pages.Transport([test_public_pages.Response(b"", status=429,
            headers={"Retry-After": "900"})], detach=True)
        def actual_read(url):
            return fetch_product_image(url, resolver=test_public_pages.resolver,
                connection_factory=transport, track_access=True)
        first = capture_product_image(self.memory, self.cid, self.candidate, media_root=self.media,
            fetcher=actual_read, shared_acquisitions=True, renewable_acquisitions=True)
        owner = self.memory.snapshot()["batch_runs"][first["physical_acquisition_id"]]
        wait = source_access_wait(self.memory.snapshot(), acquisition_access_scope(owner), as_of=first["at"])
        self.assertEqual(wait["phase"], "waiting")
        summary = self.memory.snapshot()["source_checks"][first["acquisition_ref"]]
        access = summary["response_proof"]["access"]
        self.assertTrue(access["owned_worker_exited"])
        self.assertNotIn("owned_process_exited", access)
        self.assertEqual(first["receipt"]["execution_stage"], "response_received")
        with self.assertRaisesRegex(SafetyError, "SOURCE_ACCESS_WAIT"):
            self.capture(candidate=self.alternate("https://example.com/second.png"), at=first["at"])
        self.assertEqual(len(transport.requests), 1)

    def test_late_original_file_wakes_local_replay_while_host_waits_with_zero_photo_slots(self):
        import base64
        def lost(url):
            self.calls.append(url)
            raise OSError("Synthetic missing saved reply")
        policy = deepcopy(DEFAULT_POLICY); policy["purpose_items"]["photos"] = 1
        turn = open_work_turn(self.memory, "photo-programme", at=self.at, policy=policy)
        with bind_work_turn(self.memory, turn["id"]):
            with self.assertRaises(OSError): self.capture(fetcher=lost)
            before = product_image_readiness(self.memory, self.cid, self.candidate, as_of=self.at)
            self.assertFalse(before["ready"])
            acquisition_id, owner = self.owners()[0]
        # A separate original can establish a real host refusal while our
        # earlier original is still awaiting its saved reply.
        self.capture(candidate=self.alternate("https://example.com/other-original.png"),
                     fetcher=self.failed(503, retry_after="900"))
        with bind_work_turn(self.memory, turn["id"]):
            raw = image_fixtures.sample_bytes()
            receipt = {"state": "usable", "reason": "Synthetic original late response.", "checked_at": self.at,
                "requested_url": self.candidate["url"], "final_url": self.candidate["url"], "http_status": 200,
                "redirects": [], "body_complete": True, "dispatch_started": True, "metadata": inspect_image(raw),
                "response_received": True, "owned_worker_exited": True}
            response = {"kind": "public-product-image-response-v1", "reference": acquisition_id,
                "request": _image_request(self.candidate["url"]), "origin_reference": acquisition_id,
                "original": None, "dispatch_intent_id": owner["active_dispatch_intent_id"],
                "receipt": receipt, "body_base64": base64.b64encode(raw).decode("ascii")}
            atomic_json(_renewable_response_path(self.memory, acquisition_id), {"value": response, "hash": digest(response)})
            ready = product_image_readiness(self.memory, self.cid, self.candidate, as_of=self.at)
            self.assertTrue(ready["ready"]); self.assertTrue(ready["local_saved"])
            with patch("product_radar.source_acquisitions.mark_source_dispatching",
                       side_effect=AssertionError("No launch during local replay")):
                result = self.capture()
            self.assertEqual(result["state"], "usable")
            self.assertEqual(active_work_turn(self.memory)["counters"]["purpose_items"]["photos"], 1)
        self.assertEqual(len(self.calls), 2)

    def test_redirect_refusal_belongs_to_actual_final_host_not_initial_host(self):
        target = "https://blocked.example.org/photo.png"
        transport = test_public_pages.Transport([
            test_public_pages.Response(b"", status=302, headers={"Location": target}),
            test_public_pages.Response(b"", status=429, headers={"Retry-After": "900"})], detach=True)
        with patch("product_radar.product_images.socket.getaddrinfo", side_effect=test_public_pages.resolver), \
             patch("product_radar.product_images.public_pages._PinnedConnection", side_effect=transport):
            first = capture_product_image(self.memory, self.cid, self.candidate, media_root=self.media,
                shared_acquisitions=True, renewable_acquisitions=True)
        state = self.memory.snapshot(); owner = state["batch_runs"][first["physical_acquisition_id"]]
        wait = source_access_wait(state, acquisition_access_scope(owner, state=state), as_of=first["at"])
        self.assertEqual(wait["scope"]["host"], "blocked.example.org")
        self.assertEqual(len(transport.requests), 2)
        from product_radar.source_coverage import source_access_scope
        self.assertIsNone(source_access_wait(state, source_access_scope(self.candidate["url"],
            transport="public-http", access_identity="owned-public-http"), as_of=first["at"]))

    def test_known_final_host_wait_stops_redirect_before_navigation_then_due_probe_recovers(self):
        blocked = self.alternate("https://blocked.example.org/known.png")
        first = self.capture(candidate=blocked, fetcher=self.failed(429, retry_after="900"))
        target = "https://blocked.example.org/another.png"
        transport = test_public_pages.Transport([
            test_public_pages.Response(b"", status=302, headers={"Location": target})], detach=True)
        with patch("product_radar.product_images.socket.getaddrinfo", side_effect=test_public_pages.resolver), \
             patch("product_radar.product_images.public_pages._PinnedConnection", side_effect=transport):
            guarded = capture_product_image(self.memory, self.cid, self.candidate, media_root=self.media,
                shared_acquisitions=True, renewable_acquisitions=True)
        self.assertEqual(len(transport.requests), 1)
        state = self.memory.snapshot()
        wait = source_access_wait(state, acquisition_access_scope(
            state["batch_runs"][first["physical_acquisition_id"]], state=state), as_of=first["at"])
        self.assertEqual(guarded["receipt"]["access_wait"]["failure_id"], wait["failure_id"])
        self.assertIsNone(guarded["receipt"]["http_status"])
        self.assertEqual(guarded["failure_classification"], "retryable")
        self.assertEqual(guarded["next_retry_at"], first["next_retry_at"])
        raw = image_fixtures.sample_bytes()
        recovery = test_public_pages.Transport([
            test_public_pages.Response(b"", status=302, headers={"Location": target}),
            test_public_pages.Response(raw, headers={"Content-Type": "image/png", "Content-Length": str(len(raw))})], detach=True)
        with patch("product_radar.product_images.socket.getaddrinfo", side_effect=test_public_pages.resolver), \
             patch("product_radar.product_images.public_pages._PinnedConnection", side_effect=recovery), \
             patch("product_radar.product_images.utc_now", return_value=first["next_retry_at"]):
            result = capture_product_image(self.memory, self.cid, self.candidate, media_root=self.media,
                shared_acquisitions=True, renewable_acquisitions=True, as_of=first["next_retry_at"])
        self.assertEqual(result["state"], "usable")
        self.assertEqual(result["generation"], 2)
        self.assertEqual(len(recovery.requests), 2)

    def test_readiness_parks_terminal_and_uncertain_and_wakes_due_or_missing_media(self):
        candidate = self.alternate("https://terminal.example.org/photo.png")
        self.capture(candidate=candidate, fetcher=self.failed(404))
        hint = product_image_readiness(self.memory, self.cid, candidate, as_of=self.later(86400))
        self.assertFalse(hint["ready"]); self.assertIsNone(hint["next_retry_at"])
        transient = self.alternate("https://transient.example.org/photo.png")
        self.capture(candidate=transient, fetcher=self.failed(503, retry_after="600"))
        hint = product_image_readiness(self.memory, self.cid, transient, as_of=self.later(599))
        self.assertFalse(hint["ready"]); self.assertEqual(hint["next_retry_at"], self.later(600))
        self.assertTrue(product_image_readiness(self.memory, self.cid, transient, as_of=self.later(600))["ready"])
        usable = self.capture()
        self.assertFalse(product_image_readiness(self.memory, self.cid, self.candidate, as_of=self.at)["ready"])
        Path(usable["image"]["path"]).unlink()
        self.assertTrue(product_image_readiness(self.memory, self.cid, self.candidate, as_of=self.at)["local_saved"])

    def test_a_saved_photo_reply_lost_in_a_power_cut_is_read_once_more(self):
        # R15: the photo lane's own readiness and read use the set-aside path, like the other routes.
        from product_radar.saved_read_sweep import sweep_damaged_reads
        self.capture()
        (aid, owner), = self.owners()
        path = _renewable_response_path(self.memory, aid)
        path.write_bytes(b"\x00" * path.stat().st_size)  # a cut-short write left only zero bytes
        self.assertEqual(sweep_damaged_reads(self.memory, as_of=self.later(60))["set_aside"], 1)
        hint = product_image_readiness(self.memory, self.cid, self.candidate, as_of=self.later(120))
        self.assertTrue(hint["ready"])
        self.fetch_at = self.later(180)
        self.capture(at=self.later(180))
        self.assertEqual(len(self.calls), 2)  # the one fresh read
        owners = dict(self.owners())
        self.assertEqual(owners[aid]["state"], "set_aside")
        (fresh,) = [row for rid, row in owners.items() if rid != aid]
        self.assertEqual((fresh["binding"]["generation"], fresh["binding"]["damage_recovery"]["acquisition_id"]), (2, aid))
        self.assertIsNotNone(current_public_image(self.memory.snapshot(), self.cid, [self.candidate], media_root=self.media))
        # A replacement lost again is never read a third time.
        fresh_id = next(rid for rid in owners if rid != aid)
        fresh_path = _renewable_response_path(self.memory, fresh_id)
        fresh_path.write_bytes(b"")
        sweep_damaged_reads(self.memory, as_of=self.later(240))
        hint = product_image_readiness(self.memory, self.cid, self.candidate, as_of=self.later(300))
        self.assertEqual((hint["ready"], hint["reason"]), (False, "SOURCE_ACQUISITION_DAMAGE_REPEATED"))

    def test_due_probe_changed_destination_closes_only_owned_probe_not_other_host_refusal(self):
        target = "https://blocked.example.org/photo.png"
        transport = test_public_pages.Transport([
            test_public_pages.Response(b"", status=302, headers={"Location": target}),
            test_public_pages.Response(b"", status=429, headers={"Retry-After": "900"})], detach=True)
        with patch("product_radar.product_images.socket.getaddrinfo", side_effect=test_public_pages.resolver), \
             patch("product_radar.product_images.public_pages._PinnedConnection", side_effect=transport):
            first = capture_product_image(self.memory, self.cid, self.candidate, media_root=self.media,
                shared_acquisitions=True, renewable_acquisitions=True)
        self.fetch_at = first["next_retry_at"]
        # The next original now serves its image directly instead of redirecting.
        # This closes our probe but is not evidence that the old target recovered.
        recovered = self.capture(at=self.fetch_at)
        state = self.memory.snapshot()
        original = state["batch_runs"][first["physical_acquisition_id"]]
        wait = source_access_wait(state, acquisition_access_scope(original, state=state), as_of=self.fetch_at)
        self.assertEqual(recovered["generation"], 2)
        self.assertEqual(wait["phase"], "due")
        self.assertIsNone(wait["probe"])
        head = state["batch_runs"][wait["scope_id"]]
        self.assertEqual(head["probe_closed_by"]["acquisition_id"], recovered["physical_acquisition_id"])
        self.capture(candidate=self.alternate("https://healthy.example.org/photo.png"), at=self.fetch_at)
        self.assertEqual(len(self.calls), 2)

    def test_unknown_legacy_v2_is_not_bypassed_by_v3(self):
        def lost(url):
            self.calls.append(url)
            raise OSError("Synthetic legacy original not returned")
        with self.assertRaises(OSError):
            capture_product_image(self.memory, self.cid, self.candidate, media_root=self.media,
                fetcher=lost, shared_acquisitions=True)
        with self.assertRaisesRegex(SafetyError, "PRODUCT_IMAGE_ORIGINAL_RESPONSE_PENDING"):
            self.capture(self.second_product(), at=self.later(86400))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.owners(), [])

    def test_reusable_owner_or_response_proof_tampering_cannot_authorize_or_display_photo(self):
        first = self.capture()
        state = self.memory.snapshot()
        summary = state["source_checks"][first["acquisition_ref"]]
        changed = deepcopy(summary)
        changed["receipt"]["metadata"]["sha256"] = "a" * 64
        changed["record_hash"] = digest({k: v for k, v in changed.items() if k != "record_hash"})
        with self.assertRaisesRegex(SafetyError, "PRODUCT_IMAGE_RENEWABLE_RECORD_INVALID"):
            _renewable_read_record(changed, first["acquisition_ref"], self.candidate["url"])
        acquisition_id = first["physical_acquisition_id"]
        owner = state["batch_runs"][acquisition_id]
        damaged = deepcopy(owner)
        damaged["binding"]["physical_request_fingerprint"] = "a" * 64
        self.memory.put("batch_runs", acquisition_id, damaged, previous=owner)
        with self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_RECORD_INVALID"):
            self.capture(self.second_product())
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
