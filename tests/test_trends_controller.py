"""Direct Trends route proofs use synthetic originals; never live calls."""

from copy import deepcopy
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal
from product_radar.trends_capture import explore_url
from product_radar.trends_controller import (
    EXPORT_JS, READY_JS, build_trends_request, capture_direct_trends,
    create_trends_handlers, replay_direct_trends,
)

NOW = "2026-10-06T10:00:00+00:00"
SIGNED_IN_AT = "2026-10-06T09:00:00+00:00"


def sign_in(root, at=SIGNED_IN_AT):
    """The owner opens the sign-in window, signs in and closes it (local records only)."""
    from product_radar.signed_in_browser import note_sign_in_window
    note_sign_in_window(root, True, opened=True, at=at)
    note_sign_in_window(root, False, at=at)


def snapshot(term="chair cover", **changes):
    return {"url": explore_url(term), "title": "Synthetic chart",
            "text": "\n".join([term, "Search term", "India", "Past 12 months", "All categories", "Web Search", "Interest over time"]),
            "rows": [["x", "y1"], ["Sep 13, 2026", "12"], ["Sep 20, 2026", "<1"], ["Sep 27, 2026", "20"]],
            "download_found": True, "download_clicked": True, "time_widget_present": True,
            "no_data_text": None, **changes}


def original(snap=None, raw=None, **changes):
    return {"state": "captured", "reason": "Synthetic original transport", "navigation_status": 200,
            "navigation_started": True, "navigation_started_at": NOW, "completed_at": NOW,
            "owned_process_exited": True, "snapshot": snapshot() if snap is None else snap,
            "csv_bytes": b"Week,chair cover: (India)\n2026-09-13,12\n2026-09-20,<1\n2026-09-27,20\n" if raw is None else raw,
            **changes}


class TrendsControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "money"), evidence_type="synthetic")
        self.memory.put("concepts", "chair", {"description": {"name": "chair cover"}})
        self.signed_root = self.root / "signed-in"; sign_in(self.signed_root)

    def capture(self, name="fixture", actual=None, request=None):
        with patch("product_radar.trends_controller._wrapper_utc_now", return_value=NOW):
            return capture_direct_trends(request or build_trends_request("chair cover"), name, data_root=self.root / "captures",
                snapshot_reader=lambda request: actual if actual is not None else original(), transport="signed-in")

    def context(self, name="fixture"):
        return {"route": "google-trends", "request": build_trends_request("chair cover"), "acquisition_id": name,
                "concept_ids": ["chair"], "description_hashes": {"chair": digest(self.memory.snapshot()["concepts"]["chair"]["description"])}}

    def test_verified_original_export_and_chart_pairs_replay_without_navigation(self):
        receipt = self.capture()
        self.assertEqual(receipt["state"], "usable")
        self.assertEqual(receipt, replay_direct_trends(receipt["folder"]))
        self.assertEqual((Path(receipt["folder"]) / "multiTimeline.csv").read_bytes(), original()["csv_bytes"])
        with patch("product_radar.native_public_browser.native_snapshot", side_effect=AssertionError("No new browser")):
            self.assertEqual(self.capture(), receipt)
        self.assertEqual(receipt["apify_requests"], 0); self.assertEqual(receipt["model_calls"], 0)

    def test_unknown_scope_missing_chart_and_bad_completed_pair_are_not_no_data(self):
        variants = {"country": snapshot(text=snapshot()["text"].replace("India", "Canada")),
                    "empty": snapshot(rows=[], download_found=False),
                    "mismatch": snapshot(rows=[["x", "y1"], ["Sep 13, 2026", "99"], ["Sep 20, 2026", "<1"], ["Sep 27, 2026", "20"]])}
        for name, snap in variants.items():
            with self.subTest(name=name):
                receipt = self.capture(name, original(snap))
                self.assertEqual(receipt["state"], "insufficient")
                self.assertEqual(replay_direct_trends(receipt["folder"]), receipt)

    def test_readable_time_widget_no_data_is_a_completed_check_not_zero_series(self):
        snap = snapshot(rows=[], download_found=False, download_clicked=False,
                        no_data_text="Your search doesn't have enough data to show here")
        receipt = self.capture("no-data", original(snap, csv_bytes=None))
        self.assertEqual(receipt["state"], "no_data")
        self.assertNotIn("multiTimeline.csv", receipt["artifacts"])
        self.assertEqual(replay_direct_trends(receipt["folder"]), receipt)

    def test_no_data_requires_scope_and_time_widget_not_just_a_phrase(self):
        for name, change in (("wrong-widget", {"time_widget_present": False}), ("wrong-scope", {"text": "No data available"}),
                             ("vague", {"no_data_text": "Maybe there is not much data"})):
            snap = snapshot(**{"rows": [], "download_found": False, "no_data_text": "No data available", **change})
            self.assertEqual(self.capture(name, original(snap, csv_bytes=None))["state"], "insufficient")

    def test_rendered_and_http_blocks_stay_distinct_from_no_data_or_failure(self):
        for name, actual in (("429", original(navigation_status=429)),
                             ("challenge", original(snapshot(text="Unusual traffic CAPTCHA")))):
            receipt = self.capture(name, actual)
            self.assertEqual(receipt["state"], "blocked")
            self.assertEqual(replay_direct_trends(receipt["folder"]), receipt)
        receipt = self.capture("startup", original(state="unavailable", snapshot=None, csv_bytes=None,
                               navigation_started=False, navigation_started_at=None))
        self.assertEqual(receipt["state"], "unavailable"); self.assertFalse(receipt["dispatch_started"])

    def test_wrong_url_scope_html_csv_and_multi_query_are_rejected(self):
        for name, actual in (("url", original(snapshot(url=explore_url("table mat")))),
                             ("html", original(raw=b"<html>Denied</html>")),
                             ("two", original(raw=b"Week,chair cover: (India),table mat: (India)\n2026-09-13,12,20"))):
            self.assertNotIn(self.capture(name, actual)["state"], ("usable", "no_data"))

    def test_google_default_url_rewrite_is_accepted_only_with_visible_full_scope(self):
        url = "https://trends.google.com/trends/explore?geo=IN&q=chair%20cover&hl=en"
        self.assertEqual(self.capture("default-url", original(snapshot(url=url)))["state"], "usable")
        altered = snapshot(url=url, text=snapshot()["text"].replace("Past 12 months", "Past 5 years"))
        self.assertEqual(self.capture("not-default-scope", original(altered))["state"], "insufficient")

    def test_changed_original_and_request_rebinding_fail_replay_without_relaunch(self):
        receipt = self.capture()
        with self.assertRaisesRegex(SafetyError, "ACQUISITION_CONFLICT"):
            self.capture(request=build_trends_request("table mat"))
        (Path(receipt["folder"]) / "multiTimeline.csv").write_bytes(b"changed")
        with self.assertRaisesRegex(SafetyError, "HASH_MISMATCH"):
            replay_direct_trends(receipt["folder"])

    def test_controller_imports_current_family_once_and_reconcile_is_saved_only(self):
        calls = []
        def capture(request, acquisition_id, **options):
            calls.append(acquisition_id)
            return self.capture(acquisition_id, request=request)
        handlers = create_trends_handlers(self.memory, data_root=self.root / "captures", capture=capture,
                                          signed_in_root=self.signed_root)
        context = self.context(); dispatch = []; context["record_dispatch"] = lambda: dispatch.append(True)
        result = handlers["google-trends"]["handler"](context)
        self.assertEqual(result["target_results"][0]["state"], "completed_relevant")
        self.assertEqual(len(result["evidence_refs"]), 1)
        self.assertEqual(len(self.memory.snapshot()["trends_evidence"]), 1)
        self.assertFalse(self.memory.journal.path.exists())
        again = handlers["google-trends"]["reconcile"](context)
        self.assertEqual(result, again); self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.memory.snapshot()["trends_evidence"]), 1)

    def test_controller_no_data_completes_without_fake_evidence(self):
        snap = snapshot(rows=[], download_found=False, no_data_text="No data available")
        receipt = self.capture("no-data", original(snap, csv_bytes=None))
        handlers = create_trends_handlers(self.memory, data_root=self.root / "captures")
        result = handlers["google-trends"]["reconcile"](self.context("no-data"))
        self.assertEqual(result["target_results"][0]["state"], "completed_no_results")
        self.assertEqual(result["evidence_refs"], [])
        self.assertEqual(self.memory.snapshot()["trends_evidence"], {})

    def test_synthetic_capture_cannot_be_imported_as_real_or_attached_to_changed_product(self):
        self.capture()
        captured = ResearchMemory(self.root / "captured", self.memory.journal)
        captured.put("concepts", "chair", {"description": {"name": "chair cover"}})
        handlers = create_trends_handlers(captured, data_root=self.root / "captures")
        with self.assertRaisesRegex(SafetyError, "NAMESPACE_INVALID"):
            handlers["google-trends"]["reconcile"](self.context())
        handlers = create_trends_handlers(self.memory, data_root=self.root / "captures")
        context = self.context(); previous = self.memory.snapshot()["concepts"]["chair"]
        self.memory.put("concepts", "chair", {"description": {"name": "changed"}}, previous=previous)
        with self.assertRaisesRegex(SafetyError, "PRODUCT_CHANGED"):
            handlers["google-trends"]["reconcile"](context)

    def test_native_ready_and_export_only_target_public_time_chart(self):
        self.assertIn("Interest by subregion|Related topics|Related queries", READY_JS)
        self.assertIn("chart.button.click()", EXPORT_JS)
        self.assertIn("chart&&scoped&&!blocked", EXPORT_JS)
        self.assertNotIn("fetch(", EXPORT_JS)

    def test_trends_import_validates_product_binding_inside_atomic_commit(self):
        from product_radar.research_operations import import_trends_evidence
        from product_radar.providers.trends_browser import parse_trends_csv
        desc_hash = self.context()["description_hashes"]["chair"]
        actual = original()
        metadata = {"term": "chair cover", "geo": "IN", "time_range": "today 12-m", "kind": "search_term",
                    "property": "web", "scope_verified": True, "collected_at": NOW, "source_reference": explore_url("chair cover")}
        original_commit = self.memory.commit
        def changed_during_commit(event_id, changes, *, validate=None):
            previous = self.memory.snapshot()["concepts"]["chair"]
            from product_radar.research_memory import change
            original_commit("changed-before-trends-import", [change("concepts", "chair", {"description": {"name": "table mat"}}, previous)])
            return original_commit(event_id, changes, validate=validate)
        with patch.object(self.memory, "commit", side_effect=changed_during_commit), self.assertRaisesRegex(SafetyError, "PRODUCT_CHANGED"):
            import_trends_evidence(self.memory, concept_id="chair", acquisition_id="race", csv_bytes=actual["csv_bytes"],
                metadata=metadata, expected_description_hash=desc_hash)
        self.assertEqual(self.memory.snapshot()["trends_evidence"], {})


if __name__ == "__main__": unittest.main()
