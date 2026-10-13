"""Legacy discovery must not mistake a shorter saved read for exact coverage."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.catalogue_discovery import _recent_capture
from product_radar.marketplace_browser import build_marketplace_request

NOW = "2026-10-09T12:00:00+00:00"


class DiscoveryRequestReuseTests(unittest.TestCase):
    def request(self, **options):
        return build_marketplace_request("amazon-search", term="sink caddy", **options)

    def state(self, request, **changes):
        capture = {"reference": {"route": "amazon-search", "request": deepcopy(request)},
                   "mapping_status": "usable", "observed_at": NOW, **changes}
        return {"captures": {"original": capture}}

    def test_same_complete_request_reuses_original_without_mutating_it(self):
        request = self.request()
        state = self.state(request)
        before = deepcopy(state)
        self.assertEqual(_recent_capture(state, "amazon-search", request, NOW, 7), state["captures"]["original"])
        self.assertEqual(state, before)

    def test_changed_limit_country_or_filter_does_not_reuse_same_words(self):
        request = self.request()
        alternatives = [self.request(limit=10), dict(request, country="US"),
                        dict(request, filters={"rating": "4+"}),
                        dict(request, date_scope={"from": "2026-10-01", "to": "2026-10-09"})]
        for captured in alternatives:
            with self.subTest(captured=captured):
                self.assertIsNone(_recent_capture(self.state(captured), "amazon-search", request, NOW, 7))

    def test_sparse_legacy_request_cannot_prove_full_current_bounds(self):
        state = self.state({"query": "sink caddy", "country": "IN", "limit": 30})
        self.assertIsNone(_recent_capture(state, "amazon-search", self.request(), NOW, 7))
        state["captures"]["original"]["reference"]["request"]["unsupported_old_field"] = "retained"
        self.assertIsNone(_recent_capture(state, "amazon-search", self.request(), NOW, 7))

    def test_future_stale_or_failed_capture_cannot_claim_fresh_exact_reuse(self):
        request = self.request()
        for changes in ({"observed_at": "2026-10-10T12:00:00+00:00"},
                        {"observed_at": "2026-09-01T12:00:00+00:00"},
                        {"mapping_status": "blocked"}):
            with self.subTest(changes=changes):
                self.assertIsNone(_recent_capture(self.state(request, **changes), "amazon-search", request, NOW, 7))


if __name__ == "__main__":
    unittest.main()
