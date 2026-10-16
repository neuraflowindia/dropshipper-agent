"""Actual F4 -> selected type -> G caveat -> HTML/workbook commercial scope."""

from pathlib import Path
import tempfile
import unittest

from product_radar.live_outputs import render_html, supplier_display, supplier_workbook_rows, _workbook_bytes
from product_radar.delivery_package import _workbook
from product_radar.report_narration import generate_report_narratives, make_narrative_input
from product_radar.supplier_scoped_display import scoped_supplier_lines
from test_functional_report_packet import functional_packet_fixture
from test_functional_source_market import functional_pool_fixture
from test_report_scoped_suppliers import scoped_supplier_fixture, NOW
from test_fit_pipeline import WritingFixture


class ScopedSupplierOutputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="scoped-output-"); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        memory, cids = functional_pool_fixture(self.root / "inputs", count=10)
        self.cid = cids[0]; scoped_supplier_fixture(memory, self.cid)
        self.memory, _, self.issue, self.dataset = functional_packet_fixture(self.root, memory=memory)
        self.product = next(row for row in self.dataset["products"] if row["concept_id"] == self.cid)

    def test_actual_offer_prices_moq_units_dates_and_company_terms_remain_visible(self):
        supplier, = self.product["suppliers"]
        for language in ("en", "hi-Latn"):
            text = supplier_display(supplier, language)
            for literal in ("10 pieces", "100 packs", "INR 100", "INR 250", "pack of 3", NOW[:10]):
                self.assertIn(literal, text)
            self.assertNotIn("INR 83", text)
            self.assertEqual(len(scoped_supplier_lines(supplier, language)), len(text.splitlines()))
        row, = supplier_workbook_rows(self.product)
        self.assertEqual(row[3], "See each offer's dispatch terms")
        self.assertEqual(row[5], "See each offer's minimum purchase")
        self.assertIn("100 packs", row[7])
        self.assertIn("10 pieces", row[7])
        self.assertEqual(self.product["prices"], [])

    def test_both_language_writing_uses_fixed_offer_caveat_and_exact_workbook_rows(self):
        runner = WritingFixture()
        written = generate_report_narratives(self.memory, self.issue, self.dataset, runner)
        self.assertEqual(written["failures"], {}, written)
        for language in ("en", "hi-Latn"):
            html = render_html(self.issue, written["dataset"], root=self.root, language=language)
            self.assertIn('<td class="s-scoped" colspan="', html)
            self.assertIn("100 packs", html)
            self.assertIn("10 pieces", html)
            self.assertIn("/offers/a", html)
            self.assertIn("/offers/b", html)
        payload = make_narrative_input(self.dataset, "en")
        item = next(row for row in payload["items"] if row["id"] == self.cid)
        self.assertIn("each offer's purchase and dispatch terms", item["required_limitation"])
        self.assertIn("not confirmed for every offer", item["required_limitation"])
        self.assertIn("bulk quote", item["required_limitation"])
        raw = _workbook_bytes(self.issue, written["dataset"], root=self.root)
        _workbook(raw, written["dataset"])


if __name__ == "__main__":
    unittest.main()
