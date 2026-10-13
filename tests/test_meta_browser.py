from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from product_radar.meta_browser import (capture_meta_search, meta_direct_source_handlers,
                                       parse_meta_snapshot, prepare_meta_query)
from product_radar.models import SafetyError
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, atomic_json, load_json


class MetaBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.request = prepare_meta_query("sink caddy")
        self.snapshot = {"url": self.request["url"], "title": "Meta Ad Library", "text": "Ad Library",
            "cards": [{"ad_id": "123456", "text": "Active\nLibrary ID: 123456\nStarted running on Oct 1, 2026\nKitchen sink caddy",
                       "links": [{"url": "https://www.facebook.com/profile.php?id=998877", "text": "Supplier"}],
                       "images": [{"url": "https://example.com/caddy.jpg"}]}]}

    def test_visible_cards_have_observed_identity_status_and_date(self):
        result = parse_meta_snapshot(self.snapshot, self.request, 200)
        self.assertEqual(result["state"], "usable")
        card = result["records"][0]
        self.assertEqual(card["ad_id"], "123456")
        self.assertEqual(card["advertiser_id"], "998877")
        self.assertTrue(card["active"])
        self.assertTrue(card["started_at"].startswith("2026-10-01"))

    def test_query_filter_is_not_invented_as_active_status(self):
        snapshot = deepcopy(self.snapshot); snapshot["cards"][0]["text"] = "Library ID: 123456\nSink caddy"
        card = parse_meta_snapshot(snapshot, self.request, 200)["records"][0]
        self.assertIsNone(card["active"])
        self.assertIsNone(card["started_at"])

    def test_visible_day_month_date_and_page_address_are_usable_without_a_numeric_id(self):
        snapshot = deepcopy(self.snapshot)
        snapshot["cards"][0]["text"] = "Active\nLibrary ID: 123456\nStarted running on 5 Sep 2026\nSink caddy"
        snapshot["cards"][0]["links"] = [{"url": "https://www.facebook.com/VisibleAdvertiser/", "text": "Supplier"}]
        card = parse_meta_snapshot(snapshot, self.request, 200)["records"][0]
        self.assertTrue(card["started_at"].startswith("2026-09-05"))
        self.assertIsNone(card["advertiser_id"])
        from product_radar.discovery import compute_market_signals
        observation = {"source": "meta", "source_item": "123456:parent", "semantic_id": "synthetic-ad",
                       "observed_at": "2026-10-08T00:00:00Z", "facts": card}
        signal = compute_market_signals({"synthetic-observation": observation}, {"synthetic-ad": "relevant_family"},
                                       "2026-10-08T00:00:00Z")
        self.assertTrue(signal["families"]["meta"]["available"])
        self.assertEqual(signal["families"]["meta"]["advertisers"], 1)

    def test_foreign_ids_empty_and_blocked_are_different(self):
        for status, text, expected in ((200, "No ads match this search", "empty"),
                                       (429, "Too many requests", "blocked"),
                                       (200, "Something went wrong", "blocked"),
                                       (200, "Ad Library loading", "insufficient")):
            snapshot = dict(self.snapshot, cards=[], text=text)
            self.assertEqual(parse_meta_snapshot(snapshot, self.request, status)["state"], expected)
        snapshot = deepcopy(self.snapshot); snapshot["cards"][0]["ad_id"] = "777"
        self.assertFalse(parse_meta_snapshot(snapshot, self.request, 200)["records"])

    def test_foreign_search_cannot_be_relabelled(self):
        snapshot = dict(self.snapshot, url=prepare_meta_query("other product")["url"])
        self.assertEqual(parse_meta_snapshot(snapshot, self.request, 200)["state"], "blocked")

    def test_saved_replay_never_navigates_again_and_detects_modified_mapping(self):
        calls = []
        def browser(url, expression, folder, **kwargs):
            calls.append(url)
            return {"snapshot": self.snapshot, "navigation_status": 200, "navigation_started": True,
                    "owned_process_exited": True}
        receipt, folder = capture_meta_search(self.root, "meta-offline", "sink caddy", browser=browser)
        replay, _ = capture_meta_search(self.root, "meta-offline", "sink caddy", browser=browser, reconcile=True)
        self.assertEqual(receipt, replay); self.assertEqual(len(calls), 1)
        modified = deepcopy(receipt); modified["records"][0]["active"] = False
        atomic_json(folder / "receipt.json", modified)
        with self.assertRaisesRegex(SafetyError, "META_PUBLIC_MAPPING_CHANGED"):
            capture_meta_search(self.root, "meta-offline", "sink caddy", browser=browser, reconcile=True)
        self.assertEqual(len(calls), 1)

    def test_missing_reconciliation_receipt_does_not_search(self):
        with self.assertRaisesRegex(SafetyError, "SOURCE_ORIGINAL_RECEIPT_PENDING"):
            capture_meta_search(self.root, "no-original", "sink caddy", reconcile=True)

    def test_connected_acquisition_imports_then_waits_for_relevance_and_replays(self):
        memory = ResearchMemory(self.root / "memory", Journal(self.root / "money"), evidence_type="synthetic")
        calls = []
        def browser(url, expression, folder, **kwargs):
            calls.append(url)
            return {"snapshot": self.snapshot, "navigation_status": 200, "navigation_started": True,
                    "owned_process_exited": True}
        handlers = meta_direct_source_handlers(memory, browser=browser)
        context = {"route": "meta-ads", "request": {"query": "sink caddy", "country": "IN"},
                   "acquisition_id": "source-meta-offline", "concept_ids": ["synthetic-product"],
                   "description_hashes": {"synthetic-product": "synthetic-hash"}}
        result = handlers["handler"](context)
        self.assertEqual(result["target_results"][0]["state"], "interpretation_pending")
        self.assertEqual(len(result["observation_ids"]), 1)
        replay = handlers["reconcile"](context)
        self.assertEqual(result, replay); self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
