"""Bounded visible creative selection and exact historical Meta parser replay."""

from copy import deepcopy
from hashlib import sha256
import json
import subprocess
import unittest
from unittest.mock import patch

import product_radar.meta_browser as meta
from product_radar.models import SafetyError, canonical, digest, utc_now
from product_radar.storage import atomic_bytes, atomic_json, load_json
import test_meta_browser as fixtures


class MetaCreativeImageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MetaBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.request = self.fixture.request
        self.snapshot = deepcopy(self.fixture.snapshot)

    def image(self, url, size=(600, 600), display=(300, 300), **options):
        return {"url": url, "alt": "", "natural_width": size[0], "natural_height": size[1],
            "display_width": display[0], "display_height": display[1], "visible": True,
            "link_url": None, **options}

    def presentation(self, **options):
        result = meta.parse_meta_snapshot(self.snapshot, self.request, 200, **options)
        self.assertEqual(result["state"], "usable")
        return result["records"][0]["presentation"]

    def test_first_avatar_is_not_selected_in_place_of_visible_creative(self):
        self.snapshot["cards"][0]["images"] = [
            self.image("https://example.com/avatar.jpg", size=(60, 60), display=(40, 40),
                link_url="https://www.facebook.com/profile.php?id=998877"),
            self.image("https://example.com/product.jpg")]
        self.assertEqual(self.presentation()["original_image_url"], "https://example.com/product.jpg")

    def test_metadata_free_new_snapshot_does_not_guess_first_image_role(self):
        self.assertNotIn("original_image_url", self.presentation())

    def test_malformed_optional_image_list_does_not_erase_valid_ad_card(self):
        for images in (None, {}, 3, "not a list"):
            with self.subTest(images=images):
                self.snapshot["cards"][0]["images"] = images
                self.assertNotIn("original_image_url", self.presentation())

    def test_large_advertiser_linked_avatar_is_not_creative(self):
        self.snapshot["cards"][0]["images"] = [
            self.image("https://example.com/avatar.jpg", size=(1200, 1200), display=(400, 400),
                link_url="https://facebook.com/profile.php?id=998877"),
            self.image("https://example.com/product.jpg", display=(200, 200))]
        self.assertEqual(self.presentation()["original_image_url"], "https://example.com/product.jpg")

    def test_hidden_small_unsafe_or_invalid_metadata_does_not_hide_next_creative(self):
        self.snapshot["cards"][0]["images"] = [
            self.image("https://example.com/hidden.jpg", visible=False),
            self.image("https://example.com/small.jpg", size=(100, 100)),
            self.image("https://example.com/avatar.jpg", display=(60, 60)),
            self.image("https://127.0.0.1/not-public.jpg"),
            self.image("https://user:secret@example.com/not-public.jpg"),
            self.image("https://example.com/invalid.jpg", natural_width=True),
            self.image("https://example.com/product.jpg")]
        self.assertEqual(self.presentation()["original_image_url"], "https://example.com/product.jpg")

    def test_creative_ranking_is_visible_area_then_original_area_then_order(self):
        images = [self.image("https://example.com/small-view.jpg", size=(1200, 1200), display=(200, 200)),
            self.image("https://example.com/larger-view.jpg", size=(600, 600), display=(300, 300)),
            self.image("https://example.com/larger-original.jpg", size=(900, 900), display=(300, 300)),
            self.image("https://example.com/equal-later.jpg", size=(900, 900), display=(300, 300))]
        self.snapshot["cards"][0]["images"] = images
        self.assertEqual(self.presentation()["original_image_url"], images[2]["url"])

    def test_only_first_twelve_snapshot_images_are_examined(self):
        self.snapshot["cards"][0]["images"] = [self.image("https://example.com/small.jpg", size=(60, 60))] * 12
        self.snapshot["cards"][0]["images"].append(self.image("https://example.com/ignored.jpg"))
        self.assertNotIn("original_image_url", self.presentation())

    def test_new_capture_and_replay_use_new_metadata_without_another_navigation(self):
        self.snapshot["cards"][0]["images"] = [self.image("https://example.com/product.jpg")]
        calls = []
        def browser(url, expression, folder, **options):
            calls.append(url)
            return {"snapshot": self.snapshot, "navigation_status": 200, "navigation_started": True,
                "owned_process_exited": True}
        saved, folder = meta.capture_meta_search(self.fixture.root, "new-creative", "sink caddy", browser=browser)
        self.assertEqual(saved["parser_version"], meta.PARSER_VERSION)
        self.assertNotEqual(meta.PARSER_VERSION, "visible-cards-dates-v2")
        self.assertEqual(saved["records"][0]["presentation"]["original_image_url"], "https://example.com/product.jpg")
        before = {name: (folder / name).read_bytes() for name in ("snapshot.json", "receipt.json", "intent.json")}
        replay, _ = meta.capture_meta_search(self.fixture.root, "new-creative", "sink caddy", browser=browser, reconcile=True)
        self.assertEqual(saved, replay)
        self.assertEqual(calls, [self.request["url"]])
        self.assertEqual(before, {name: (folder / name).read_bytes() for name in before})

    def save_old(self, parser):
        acquisition_id = "historical-meta-v2" if parser else "historical-meta-v1"
        folder = self.fixture.root / acquisition_id
        folder.mkdir()
        snapshot = deepcopy(self.snapshot)
        snapshot["cards"][0]["text"] = "Active\nLibrary ID: 123456\nStarted running on 5 Sep 2026\nSink caddy"
        snapshot["cards"][0]["images"] = [
            self.image("https://example.com/old-avatar.jpg", size=(60, 60), display=(40, 40)),
            self.image("https://example.com/creative.jpg")]
        parsed = meta.parse_meta_snapshot(snapshot, self.request, 200, legacy_dates=parser is None, legacy_images=True)
        raw = canonical(snapshot)
        receipt = {"version": meta.VERSION, "acquisition_id": acquisition_id, "request": self.request,
            "observed_at": utc_now(), **parsed, "raw_hash": sha256(raw).hexdigest(), "mapping_hash": digest(parsed),
            "dispatch_started": True, "navigation_status": 200, "owned_process_exited": True,
            "apify_usd": "0", "model_calls": 0}
        if parser is not None:
            receipt["parser_version"] = parser
        atomic_bytes(folder / "snapshot.json", raw)
        atomic_json(folder / "receipt.json", receipt)
        return acquisition_id, folder, receipt

    def test_saved_v2_keeps_exact_first_image_and_normal_date_mapping(self):
        acquisition_id, folder, receipt = self.save_old("visible-cards-dates-v2")
        before = {name: (folder / name).read_bytes() for name in ("snapshot.json", "receipt.json")}
        def forbidden(*args, **options):
            self.fail("Historical replay must not navigate")
        replay, _ = meta.capture_meta_search(self.fixture.root, acquisition_id, "sink caddy", browser=forbidden, reconcile=True)
        self.assertEqual(replay, receipt)
        self.assertEqual(replay["records"][0]["presentation"]["original_image_url"], "https://example.com/old-avatar.jpg")
        self.assertTrue(replay["records"][0]["started_at"].startswith("2026-09-05"))
        self.assertEqual(before, {name: (folder / name).read_bytes() for name in before})

    def test_saved_unversioned_parser_keeps_original_dates_and_first_image(self):
        acquisition_id, folder, receipt = self.save_old(None)
        before = (folder / "receipt.json").read_bytes()
        replay, _ = meta.capture_meta_search(self.fixture.root, acquisition_id, "sink caddy", reconcile=True)
        self.assertEqual(replay, receipt)
        self.assertIsNone(replay["records"][0]["started_at"])
        self.assertEqual(replay["records"][0]["presentation"]["original_image_url"], "https://example.com/old-avatar.jpg")
        self.assertEqual((folder / "receipt.json").read_bytes(), before)

    def test_unknown_or_malformed_saved_parser_is_refused_without_reparse(self):
        acquisition_id, folder, receipt = self.save_old("visible-cards-dates-v2")
        for parser in ("unknown-future-parser", [], {"v": 3}):
            atomic_json(folder / "receipt.json", {**receipt, "parser_version": parser})
            with self.subTest(parser=parser), self.assertRaisesRegex(SafetyError, "META_PUBLIC_PARSER_VERSION_INVALID"):
                meta.capture_meta_search(self.fixture.root, acquisition_id, "sink caddy", reconcile=True)

    def test_original_snapshot_dimension_tampering_is_refused_not_reparsed(self):
        self.snapshot["cards"][0]["images"] = [self.image("https://example.com/product.jpg")]
        def browser(*args, **options):
            return {"snapshot": self.snapshot, "navigation_status": 200, "navigation_started": True,
                "owned_process_exited": True}
        _, folder = meta.capture_meta_search(self.fixture.root, "image-raw-hash", "sink caddy", browser=browser)
        snapshot = load_json(folder / "snapshot.json")
        snapshot["cards"][0]["images"][0]["natural_width"] = 999
        atomic_json(folder / "snapshot.json", snapshot)
        with self.assertRaisesRegex(SafetyError, "META_PUBLIC_CAPTURE_CHANGED"):
            meta.capture_meta_search(self.fixture.root, "image-raw-hash", "sink caddy", reconcile=True)

    def test_existing_v2_deadline_reparse_keeps_original_parser_and_bytes(self):
        folder = self.fixture.root / "old-deadline"
        folder.mkdir()
        original = {"artifacts": {"outer_deadline.json": "a" * 64}}
        atomic_json(folder / "receipt.json", original)
        actual = {"snapshot": self.snapshot, "completed_at": utc_now(), "navigation_status": 200}
        parsed = meta.parse_meta_snapshot(self.snapshot, self.request, 200, legacy_images=True)
        raw = canonical(self.snapshot)
        saved = {"version": meta.VERSION, "parser_version": "visible-cards-dates-v2", "acquisition_id": "old-deadline",
            "request": self.request, "observed_at": actual["completed_at"], **parsed,
            "raw_hash": sha256(raw).hexdigest(), "mapping_hash": digest(parsed), "dispatch_started": True,
            "navigation_status": 200, "owned_process_exited": True, "apify_usd": "0", "model_calls": 0,
            "recovery": {"version": "source-deadline-reparse-v1",
                "original_receipt_sha256": sha256((folder / "receipt.json").read_bytes()).hexdigest(),
                "original_artifacts_hash": digest(original["artifacts"])},
            "receipt_file": "deadline_reparse.json", "snapshot_file": "deadline_snapshot.json"}
        atomic_bytes(folder / "deadline_snapshot.json", raw)
        atomic_json(folder / "deadline_reparse.json", saved)
        before = {name: (folder / name).read_bytes() for name in ("receipt.json", "deadline_reparse.json", "deadline_snapshot.json")}
        with patch.object(meta, "saved_completed_transport", return_value=actual):
            replay = meta._reconcile_meta_deadline(folder, original, "old-deadline", self.request)
        self.assertEqual(replay, saved)
        self.assertEqual(before, {name: (folder / name).read_bytes() for name in before})

    def test_extractor_records_bounded_explicit_dimensions_visibility_and_placement(self):
        # Execute only the extraction expression against a tiny local DOM stub.
        # No browser process, real page, network or new dependency is involved.
        script = r"""
const picture=(width,height,displayWidth,displayHeight,visible,link)=>({
 currentSrc:'https://example.com/photo.png',src:'unused',alt:'x'.repeat(700),naturalWidth:width,naturalHeight:height,
 getBoundingClientRect:()=>({width:displayWidth,height:displayHeight}),getClientRects:()=>visible?[{}]:[],
 closest:()=>link?{href:link}:null});
const photos=[picture(60,60,40,40,true,'https://www.facebook.com/profile.php?id=998877'),
 picture(600,400,300.4,200.6,true,null),picture(900,900,300,300,false,null)];
const card={innerText:'Active\nLibrary ID: 123456\nProduct',parentElement:null,
 querySelectorAll:(selector)=>selector==='img[src]'?photos:[]};
const own={innerText:'Library ID: 123456',parentElement:card};
global.document={body:{innerText:card.innerText},title:'Test',querySelectorAll:()=>[own]};
global.location={href:'https://www.facebook.com/ads/library/?country=IN&q=test'};
global.getComputedStyle=()=>({display:'block',visibility:'visible',opacity:'1'});
process.stdout.write(eval(EXPRESSION));
""".replace("EXPRESSION", json.dumps(meta.EXTRACT))
        result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10, check=True)
        photos = json.loads(result.stdout)["cards"][0]["images"]
        self.assertEqual((photos[0]["natural_width"], photos[0]["natural_height"]), (60, 60))
        self.assertEqual((photos[1]["display_width"], photos[1]["display_height"]), (300, 201))
        self.assertTrue(photos[0]["visible"])
        self.assertFalse(photos[2]["visible"])
        self.assertEqual(photos[0]["link_url"], "https://www.facebook.com/profile.php?id=998877")
        self.assertIsNone(photos[1]["link_url"])
        self.assertEqual(len(photos[0]["alt"]), 500)


if __name__ == "__main__":
    unittest.main()
