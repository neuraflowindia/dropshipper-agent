"""A bounded receipt check recovers lagging counters, never recollects ads."""

import unittest

import test_meta_live_trial as helpers


class LagTests(unittest.TestCase):
    setUp = helpers.TrialTests.setUp
    controller = helpers.TrialTests.controller
    start = helpers.TrialTests.start
    good_rows = helpers.TrialTests.good_rows
    save_facts = helpers.TrialTests.save_facts
    bind = helpers.TrialTests.bind
    sleep = helpers.TrialTests.sleep

    def lag(self, once=True):
        original = self.provider.open
        lagging = True
        def open_request(request, timeout):
            nonlocal lagging
            response = original(request, timeout)
            if "/v2/datasets/" in request.full_url and lagging:
                response.headers["x-apify-pagination-count"] = "2"
                response.headers["x-apify-pagination-total"] = "2"
                if once:
                    lagging = False
            return response
        self.provider.open = open_request

    def test_nonzero_counter_lag_reuses_same_body_and_one_launch(self):
        self.good_rows()
        self.lag()
        with self.journal.locked():
            self.assertEqual(self.start()["code"], "DATASET_PAGINATION_INCONSISTENT")
            self.assertEqual(self.controller().run(self.attempt, resume_only=True)["captured_count"], 5)
            self.controller().run(self.attempt, resume_only=True)
        self.assertEqual(sum(c[0] == "POST" for c in self.provider.calls), 1)
        self.assertEqual(sum("/v2/datasets/" in c[1] for c in self.provider.calls), 2)

    def test_persistent_lag_stays_blocked_without_endless_reads(self):
        self.good_rows()
        self.lag(once=False)
        with self.journal.locked():
            self.start()
            for _ in range(3):
                self.assertEqual(self.controller().run(self.attempt, resume_only=True)["code"], "DATASET_PAGINATION_INCONSISTENT")
        self.assertEqual(sum("/v2/datasets/" in c[1] for c in self.provider.calls), 2)
        self.assertEqual(sum(c[0] == "POST" for c in self.provider.calls), 1)

    def test_changed_body_remains_conflict(self):
        self.good_rows()
        self.lag()
        with self.journal.locked():
            self.start()
            self.provider.rows[0]["snapshot"]["body"]["text"] = "Changed source meaning"
            self.assertEqual(self.controller().run(self.attempt, resume_only=True)["code"], "SAVED_PAGE_CONFLICT")
        self.assertEqual(sum(c[0] == "POST" for c in self.provider.calls), 1)
