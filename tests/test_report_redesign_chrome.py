"""The redesigned report printed by the production engine (native Windows Chrome).

Builds the synthetic rich fixture issue through the real writers, renders both
language editions exactly as ``scripts/render_live_pdf.py`` does, and checks
the printed result: every product starts on a new page and takes two pages,
every page after the cover carries its footer, nothing is set below 8 pt and
only the embedded report fonts are used. Skipped where Windows Chrome is not
reachable (for example outside WSL).
"""

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import render_live_pdf  # noqa: E402

CHROME = next((path for path in render_live_pdf.CHROME_CANDIDATES if path.is_file()), None)


def product_spans(document):
    """[(first page, pages)] per product, from the glance page's links to each product page."""
    starts = []
    for link in document[1].get_links():
        if link.get("kind") in (1, 4) and link.get("page", -1) >= 0 and link["page"] not in starts:  # internal links
            starts.append(link["page"])
    closing = len(document) - 1
    return [(start, (starts[n + 1] if n + 1 < len(starts) else closing) - start) for n, start in enumerate(starts)]


@unittest.skipUnless(CHROME and render_live_pdf.POWERSHELL.is_file(), "native Windows Chrome is not reachable")
class ChromePrintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import fitz
        from product_radar.live_outputs import render_html
        from report_redesign_fixture import rich_issue
        cls.folder = tempfile.TemporaryDirectory(prefix="report-print-")
        cls.addClassCleanup(cls.folder.cleanup)
        root = Path(cls.folder.name)
        _, cls.issue, cls.dataset = rich_issue(root / "fixture")
        cls.documents = {}
        for language in ("en", "hi-Latn"):
            html = root / f"report-{language}.html"
            html.write_text(render_html(cls.issue, cls.dataset, language=language, root=root / "fixture"), encoding="utf-8")
            result = render_live_pdf.render(html, root / f"report-{language}.pdf", timeout=120)
            cls.documents[language] = fitz.open(result["path"])
            cls.addClassCleanup(cls.documents[language].close)

    def test_each_product_starts_on_a_new_page_and_takes_two_pages_unless_its_leads_are_long(self):
        """Plan rule: two pages per product; a third only when the data is genuinely long (here,
        supplier leads whose terms differ by offer). Page one never runs over; page two starts fresh."""
        kicker = {"en": "MARKET AND SOURCING", "hi-Latn": "BAZAAR AUR SOURCING"}
        long_leads = [any(s.get("scoped_supplier_contract") for s in product["suppliers"]) for product in self.dataset["products"]]
        self.assertTrue(any(long_leads) and not all(long_leads))
        for language, document in self.documents.items():
            with self.subTest(language=language):
                spans = product_spans(document)
                markets = [number for number, page in enumerate(document) if kicker[language] in page.get_text()]
                self.assertEqual(len(spans), len(self.dataset["products"]))
                self.assertEqual(spans[0][0], 3)  # cover, at a glance, how to read
                self.assertEqual(len(markets), len(spans))
                for (start, pages), market, long in zip(spans, markets, long_leads):
                    self.assertEqual(market, start + 1)  # page one fits on one page; page two starts fresh
                    if long:
                        self.assertIn(pages, (2, 3))
                    else:
                        self.assertEqual(pages, 2)
                self.assertEqual(len(document), 4 + sum(pages for _, pages in spans))

    def test_every_page_after_the_cover_has_its_footer_and_page_number(self):
        for language, document in self.documents.items():
            word = "of" if language == "en" else "/"
            for number, page in enumerate(document, 1):
                text = page.get_text()
                self.assertTrue(text.strip())
                if number > 1:
                    self.assertIn(f"Page {number} {word} {len(document)}", text, (language, number))
                    self.assertIn("NeuraFlow · Product Opportunity Brief", text)

    def test_printed_text_is_never_below_eight_points_and_uses_only_the_report_fonts(self):
        for language, document in self.documents.items():
            sizes, fonts = [], set()
            for page in document:
                for block in page.get_text("dict")["blocks"]:
                    for line in block.get("lines", []):
                        for span in line["spans"]:
                            if span["text"].strip():
                                sizes.append(span["size"])
                                fonts.add(span["font"].split("+")[-1])
            with self.subTest(language=language):
                self.assertGreaterEqual(min(sizes), 7.95)
                self.assertTrue(fonts)
                self.assertTrue(all(name.startswith("NF") for name in fonts), fonts)


if __name__ == "__main__":
    unittest.main()
