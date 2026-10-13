"""Accept one observed source diagnostic without relaxing page integrity."""

import json
from pathlib import Path
import tempfile
import unittest

from test_benchmark_foundation import CONFIG, cli
from product_radar.benchmark import MetaTrial
from product_radar.models import SafetyError, prepared_request
from product_radar.storage import Journal


class MetaEmptyDiagnosticTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.controller = MetaTrial(self.root, Journal(self.root / "finance"))
        self.controller.prepared = prepared_request(cli.prepare(CONFIG, "empty-diagnostic")["request"])
        self.url = self.controller.prepared.input["urls"][0]["url"]
        self.row = {"error": "Ads not found", "errorCode": "ADS_NOT_FOUND", "url": self.url}
        self.metadata = {"method": "GET", "path": "/v2/datasets/fixture/items", "status": 200,
            "query": {"format": "json", "offset": 0, "limit": 100},
            "headers": {"x-apify-pagination-" + key: str(value) for key, value in
                        {"offset": 0, "limit": 100, "count": 0, "total": 0}.items()}}

    def check(self, rows, *, offset=0, total=None):
        self.controller.saved_response = lambda _: json.loads(json.dumps(rows))
        return self.controller.checked_page(self.metadata, "fixture", offset, 100, total)

    def test_exact_request_bound_no_ads_diagnostic_is_retained(self):
        rows, counters = self.check([self.row])
        self.assertEqual(rows, [self.row])
        self.assertEqual(counters["count"], 0)
        self.assertEqual(counters["total"], 0)

    def test_other_query_or_unknown_error_or_extra_fields_fail(self):
        for row in (dict(self.row, url=self.url + "&different=1"),
                    dict(self.row, errorCode="OTHER"), dict(self.row, error="Unknown error"),
                    dict(self.row, guessed_product="No"), {"ad_archive_id": "123"}):
            with self.subTest(row=row), self.assertRaisesRegex(SafetyError, "PAGINATION_INCONSISTENT"):
                self.check([row])

    def test_multiple_diagnostics_or_later_page_are_not_accepted(self):
        for rows, total in (([self.row, self.row], None), ([self.row], 1)):
            with self.subTest(total=total), self.assertRaisesRegex(SafetyError, "PAGINATION_INCONSISTENT"):
                self.check(rows, total=total)
