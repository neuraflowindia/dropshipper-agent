"""Offline original-photo acquisition and report binding checks."""

from copy import deepcopy
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import test_public_pages
import test_fit_review
from product_radar.models import SafetyError, utc_now
from product_radar.product_images import (capture_product_image, current_public_image,
                                         fetch_product_image, inspect_image)
from product_radar.research_memory import change


def sample_bytes():
    stream = BytesIO()
    Image.new("RGB", (6, 4), (12, 24, 36)).save(stream, format="PNG")
    return stream.getvalue()


class ImageTransportTests(unittest.TestCase):
    def fetch(self, responses, **kwargs):
        transport = test_public_pages.Transport(responses, detach=True)
        result = fetch_product_image("https://example.com/product.png", resolver=test_public_pages.resolver,
                                     connection_factory=transport, **kwargs)
        return result, transport

    def test_original_bytes_and_http_socket_lifecycle(self):
        raw = sample_bytes()
        (receipt, actual), transport = self.fetch([test_public_pages.Response(raw, headers={
            "Content-Type": "image/png", "Content-Length": str(len(raw))})])
        self.assertEqual(receipt["state"], "usable")
        self.assertEqual(actual, raw)
        self.assertEqual(receipt["metadata"], inspect_image(raw))
        self.assertTrue(all(connection.closed for connection in transport.connections))

    def test_private_addresses_are_not_requested(self):
        receipt, raw = fetch_product_image("https://127.0.0.1/photo.png")
        self.assertEqual(receipt["state"], "failed")
        self.assertFalse(receipt["dispatch_started"])
        self.assertIsNone(raw)

    def test_unrecognised_html_or_incomplete_photo_is_not_saved(self):
        raw = sample_bytes()
        for headers in ({"Content-Type": "text/html"},
                        {"Content-Type": "image/png", "Content-Length": str(len(raw) + 1)}):
            (receipt, actual), _ = self.fetch([test_public_pages.Response(raw, headers=headers)])
            self.assertEqual(receipt["state"], "failed")
            self.assertIsNone(actual)

    def test_invalid_image_bytes_and_body_limit_are_not_usable(self):
        for body in (b"Not a product picture", b""):
            with self.assertRaises(SafetyError):
                inspect_image(body)
        (receipt, actual), _ = self.fetch([test_public_pages.Response(sample_bytes(), headers={
            "Content-Type": "image/png", "Content-Length": "99999999"})])
        self.assertEqual(receipt["reason"], "PRODUCT_IMAGE_SIZE_LIMIT")
        self.assertIsNone(actual)

    def test_redirect_target_is_validated_again(self):
        (receipt, actual), _ = self.fetch([test_public_pages.Response(b"", status=302, headers={
            "Location": "http://169.254.169.254/latest/meta-data/"})])
        self.assertEqual(receipt["state"], "failed")
        self.assertIsNone(actual)


class ImageBindingTests(unittest.TestCase):
    setUp = test_fit_review.FitReviewTests.setUp

    def prepare_photo(self):
        state = self.memory.snapshot()
        observation_id, observation = next(iter(state["observations"].items()))
        # Immutable observations are normally imported with this original URL;
        # manufacture a separate acquisition rather than rewriting a source.
        observation_id += "-photo"
        observation = deepcopy(observation)
        observation["facts"]["image_urls"] = ["https://example.com/original.png"]
        self.memory.commit("synthetic-photo-observation", [change("observations", observation_id, observation)])
        candidate = {"observation_id": observation_id, "url": observation["facts"]["image_urls"][0],
                     "observed_at": observation["observed_at"], "relationship": "relevant_family", "source": "meta"}
        self.media = Path(self.temp.name) / "photos"
        return candidate

    def usable_fetch(self, url):
        self.calls.append(url)
        raw = sample_bytes()
        return {"state": "usable", "reason": "Synthetic original photo.", "checked_at": utc_now(),
                "metadata": inspect_image(raw), "requested_url": url}, raw

    def test_capture_replay_and_current_provenance(self):
        self.calls = []; candidate = self.prepare_photo()
        saved = capture_product_image(self.memory, self.concept_id, candidate,
                                      media_root=self.media, fetcher=self.usable_fetch)
        replay = capture_product_image(self.memory, self.concept_id, candidate,
                                       media_root=self.media, fetcher=self.usable_fetch)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(saved["reused"]); self.assertTrue(replay["reused"])
        photo = current_public_image(self.memory.snapshot(), self.concept_id, [candidate], media_root=self.media)
        self.assertEqual(photo["source_url"], candidate["url"])
        self.assertEqual(photo["relationship"], "relevant_family")
        self.assertEqual(Path(photo["path"]).read_bytes(), sample_bytes())

    def test_missing_or_corrupted_photo_does_not_block_text_report(self):
        self.calls = []; candidate = self.prepare_photo()
        saved = capture_product_image(self.memory, self.concept_id, candidate,
                                      media_root=self.media, fetcher=self.usable_fetch)
        from product_radar.storage import atomic_bytes
        atomic_bytes(Path(saved["image"]["path"]), b"changed")
        self.assertIsNone(current_public_image(self.memory.snapshot(), self.concept_id, [candidate], media_root=self.media))
        self.assertEqual(len(self.calls), 1)
        with self.assertRaisesRegex(SafetyError, "PRODUCT_IMAGE_SAVED_BYTES_CHANGED"):
            capture_product_image(self.memory, self.concept_id, candidate, media_root=self.media, fetcher=self.usable_fetch)
        self.assertEqual(len(self.calls), 1)

    def test_successful_photo_is_recovered_after_local_save_conflict_without_refetch(self):
        self.calls = []; candidate = self.prepare_photo()
        with patch.object(self.memory, "commit", side_effect=SafetyError("RUNNER_ALREADY_ACTIVE")):
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                capture_product_image(self.memory, self.concept_id, candidate, media_root=self.media, fetcher=self.usable_fetch)
        saved = capture_product_image(self.memory, self.concept_id, candidate, media_root=self.media, fetcher=self.usable_fetch)
        self.assertEqual(saved["state"], "usable")
        self.assertTrue(saved["reused"])
        self.assertEqual(len(self.calls), 1)
        self.assertIsNotNone(current_public_image(self.memory.snapshot(), self.concept_id, [candidate], media_root=self.media))

    def test_failed_photo_receipt_is_recovered_after_local_conflict_without_repeat(self):
        candidate = self.prepare_photo(); calls = []
        def unavailable(url):
            calls.append(url)
            return {"state": "failed", "reason": "PRODUCT_IMAGE_HTTP_FAILED", "checked_at": utc_now()}, None
        with patch.object(self.memory, "commit", side_effect=SafetyError("RUNNER_ALREADY_ACTIVE")):
            with self.assertRaises(SafetyError):
                capture_product_image(self.memory, self.concept_id, candidate, media_root=self.media, fetcher=unavailable)
        saved = capture_product_image(self.memory, self.concept_id, candidate, media_root=self.media, fetcher=unavailable)
        self.assertEqual(saved["state"], "failed")
        self.assertEqual(calls, [candidate["url"]])

    def test_corrupt_staged_receipt_cannot_trigger_a_replacement_request(self):
        self.calls = []; candidate = self.prepare_photo()
        with patch.object(self.memory, "commit", side_effect=SafetyError("RUNNER_ALREADY_ACTIVE")):
            with self.assertRaises(SafetyError):
                capture_product_image(self.memory, self.concept_id, candidate, media_root=self.media, fetcher=self.usable_fetch)
        from product_radar.storage import atomic_json
        staged = next((self.memory.root / "image_captures").glob("product-image-*/capture.json"))
        atomic_json(staged, {"value": {}, "hash": "invalid"})
        with self.assertRaisesRegex(SafetyError, "PRODUCT_IMAGE_CAPTURE_INVALID"):
            capture_product_image(self.memory, self.concept_id, candidate, media_root=self.media, fetcher=self.usable_fetch)
        self.assertEqual(len(self.calls), 1)

    def test_failed_photo_is_saved_and_not_repeated_indefinitely(self):
        candidate = self.prepare_photo(); calls = []
        def unavailable(url):
            calls.append(url)
            return {"state": "failed", "reason": "PRODUCT_IMAGE_HTTP_FAILED", "checked_at": utc_now()}, None
        capture_product_image(self.memory, self.concept_id, candidate, media_root=self.media, fetcher=unavailable)
        capture_product_image(self.memory, self.concept_id, candidate, media_root=self.media, fetcher=unavailable)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(current_public_image(self.memory.snapshot(), self.concept_id, [candidate], media_root=self.media))

    def test_unobserved_photo_and_changed_product_do_not_get_attached(self):
        self.calls = []; candidate = self.prepare_photo()
        with self.assertRaisesRegex(SafetyError, "PRODUCT_IMAGE_URL_NOT_IN_SOURCE"):
            capture_product_image(self.memory, self.concept_id, dict(candidate, url="https://example.com/invented.png"),
                                  media_root=self.media, fetcher=self.usable_fetch)
        capture_product_image(self.memory, self.concept_id, candidate, media_root=self.media, fetcher=self.usable_fetch)
        state = self.memory.snapshot(); concept = deepcopy(state["concepts"][self.concept_id])
        concept["description"]["label"] += " changed"
        self.memory.commit("synthetic-photo-product-change", [change("concepts", self.concept_id, concept, state["concepts"][self.concept_id])])
        self.assertIsNone(current_public_image(self.memory.snapshot(), self.concept_id, [candidate], media_root=self.media))


if __name__ == "__main__":
    unittest.main()
