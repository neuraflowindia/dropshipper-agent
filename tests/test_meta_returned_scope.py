"""Meta returned-search scope: requested values exact, only known defaults added.

Returned addresses are the sanitised saved Ad Library addresses in
tests/fixtures/meta. The installed Meta leaf runs with a synthetic browser.
"""

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from product_radar.meta_browser import META_RETURNED_DEFAULTS, meta_returned_scope_matches, prepare_meta_query
from product_radar.source_coverage import prepare_source_tasks
import test_shared_meta_discovery as shared_meta

SAVED = json.loads((Path(__file__).resolve().parent / "fixtures" / "meta" / "meta-ad-library-returned-urls-2026-10.json")
                   .read_text(encoding="utf-8"))
QUERY = SAVED["query"]
RETURNED = SAVED["captures"][0]["returned"]


class MetaReturnedScopeRuleTests(unittest.TestCase):
    def test_saved_meta_returned_addresses_are_the_requested_search(self):
        for capture in SAVED["captures"]:
            with self.subTest(observed_at=capture["observed_at"]):
                self.assertEqual(prepare_meta_query(QUERY)["url"], capture["requested"])
                self.assertTrue(meta_returned_scope_matches(capture["requested"], capture["returned"]))
        self.assertTrue(meta_returned_scope_matches(SAVED["captures"][0]["requested"], SAVED["captures"][0]["requested"]))
        self.assertEqual(set(META_RETURNED_DEFAULTS),
                         {"is_targeted_country", "media_type", "sort_data[mode]", "sort_data[direction]"})

    def test_any_other_change_to_the_returned_search_is_not_the_requested_scope(self):
        requested = SAVED["captures"][0]["requested"]
        changed = {
            "unexpected extra parameter": RETURNED + "&view_all_page_id=123",
            "unexpected blank parameter": RETURNED + "&page_ids=",
            "different value for a known default": RETURNED.replace("media_type=all", "media_type=image"),
            "repeated known default": RETURNED + "&media_type=image",
            "different sort default": RETURNED.replace("direction]=desc", "direction]=asc"),
            "missing requested parameter": RETURNED.replace("&search_type=keyword_unordered", ""),
            "changed requested parameter": RETURNED.replace("active_status=all", "active_status=active"),
            "changed search phrase": RETURNED.replace("Food%20wrap%20paper", "Food%20wrap%20foil"),
            "changed country": RETURNED.replace("country=IN", "country=US"),
            "different host": RETURNED.replace("www.facebook.com", "facebook.com"),
            "different path": RETURNED.replace("/ads/library/", "/ads/library/report/"),
            "not a URL": None}
        for label, returned in changed.items():
            with self.subTest(label):
                self.assertFalse(meta_returned_scope_matches(requested, returned))


class MetaReturnedScopeLeafTests(unittest.TestCase):
    setUp = shared_meta.SharedMetaTests.setUp
    handlers = shared_meta.SharedMetaTests.handlers
    checks = shared_meta.SharedMetaTests.checks
    owners = shared_meta.SharedMetaTests.owners
    returned = RETURNED

    def browser(self, url, expression, folder, **options):
        result = shared_meta.SharedMetaTests.browser(self, url, expression, folder, **options)
        # What Meta actually serves: the requested search plus its own defaults.
        self.assertEqual(url, prepare_meta_query(QUERY)["url"])
        result["snapshot"]["url"] = self.returned
        return result

    def prepare(self, run="checks"):
        self.run = run
        prepare_source_tasks(self.memory, [{"route": "meta-ads", "request": {"query": QUERY, "country": "IN"},
            "concept_ids": ["sink"]}], run_id=run, as_of=self.now)

    def task(self):
        return next(row for row in self.memory.snapshot()["query_tasks"].values()
                    if row.get("kind") == "all_source_task_v1" and row.get("run_id") == self.run)

    def envelope(self):
        return self.memory.snapshot()["source_checks"][self.task()["receipt_refs"][-1]]["envelope"]

    def response(self):
        owner = next(iter(self.owners().values()))
        return owner, self.memory.snapshot()["source_checks"][owner["active_response_id"]]

    def test_meta_defaults_keep_a_closed_usable_read_complete(self):
        handlers = self.handlers(); self.prepare()
        self.assertEqual(self.checks(handlers)["counts"], {"acquired": 1})
        _, response = self.response()
        self.assertTrue(response["complete"])
        self.assertTrue(response["coverage"]["scope_complete"])
        self.assertEqual(self.envelope()["state"], "usable")

    def test_meta_defaults_keep_an_explicit_empty_search_a_completed_no_result(self):
        handlers = self.handlers(); self.prepare(); self.mode = "empty"
        self.assertEqual(self.checks(handlers)["counts"], {"completed_no_results": 1})
        owner, response = self.response()
        self.assertTrue(response["complete"])
        self.assertEqual(owner["state"], "completed_empty")
        self.assertEqual(self.memory.snapshot()["observations"], {})

    def test_an_unexpected_returned_change_stays_incomplete(self):
        for mode, counts, state, reason in (("usable", {"acquired": 1}, "partial", None),
                                            ("empty", {"insufficient": 1}, "insufficient", "META_EMPTY_RESPONSE_COVERAGE_INCOMPLETE")):
            with self.subTest(mode=mode):
                self.setUp(); self.returned = RETURNED + "&view_all_page_id=123"
                handlers = self.handlers(); self.prepare(); self.mode = mode
                self.assertEqual(self.checks(handlers)["counts"], counts)
                _, response = self.response()
                self.assertFalse(response["complete"])
                self.assertEqual(self.envelope()["state"], state)
                if reason:
                    self.assertEqual(self.envelope()["reason"], reason)

    def test_saved_incomplete_response_is_reassessed_from_its_unchanged_original(self):
        handlers = self.handlers(); self.prepare("earlier-assessment")
        # The earlier rule (exact query equality) judged this same read incomplete.
        with patch("product_radar.meta_browser.meta_returned_scope_matches", return_value=False):
            self.assertEqual(self.checks(handlers)["counts"], {"acquired": 1})
        owner, earlier = self.response()
        self.assertFalse(earlier["complete"])
        self.assertEqual(self.envelope()["state"], "partial")
        folder = self.root / "research" / "direct_meta" / next(iter(self.owners()))
        originals = {name: (folder / name).read_bytes() for name in ("receipt.json", "snapshot.json", "transport.json")}
        self.prepare("later-question")
        result = self.checks(handlers)
        self.assertEqual((result["handler_calls"], result["counts"]), (0, {"acquired": 1}))
        self.assertEqual(len(self.calls), 1)
        owner, current = self.response()
        self.assertEqual(len(owner["response_ids"]), 2)
        self.assertEqual(self.memory.snapshot()["source_checks"][owner["response_ids"][0]], earlier)
        self.assertTrue(current["complete"])
        self.assertEqual(current["observed_at"], earlier["observed_at"])
        self.assertEqual(self.envelope()["state"], "usable")
        self.assertEqual({name: (folder / name).read_bytes() for name in originals}, originals)


if __name__ == "__main__":
    unittest.main()
