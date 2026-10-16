"""The redesigned report printed by the production engine (native Windows Chrome).

Builds the synthetic rich fixture issue through the real writers, renders both
language editions exactly as ``scripts/render_live_pdf.py`` does, and checks
the printed result: every product starts on a new page and takes two pages,
every page after the cover carries its footer, nothing is set below 8 pt and
only the embedded report fonts are used. A stress edition of each language
(the title at its 100-character limit, every writing slot at its budget, the
updated type also compliance-flagged) is printed too: the cover stays one page
with its notice band whole, and every product's page one still fits one page.
Skipped where Windows Chrome is not reachable (for example outside WSL).
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
        from product_radar import live_outputs
        from product_radar.report_view import render_document
        from report_redesign_fixture import stress_edition
        cls.documents, cls.stress = {}, {}
        logo = live_outputs._owned_asset(cls.dataset, "brand-logo", root=root / "fixture")
        images = [live_outputs._owned_asset(cls.dataset, p["concept_id"], root=root / "fixture") for p in cls.dataset["products"]]
        for language in ("en", "hi-Latn"):
            html = root / f"report-{language}.html"
            html.write_text(render_html(cls.issue, cls.dataset, language=language, root=root / "fixture"), encoding="utf-8")
            result = render_live_pdf.render(html, root / f"report-{language}.pdf", timeout=120)
            cls.documents[language] = fitz.open(result["path"])
            cls.addClassCleanup(cls.documents[language].close)
            stressed, narrative = stress_edition(cls.dataset, language)
            html = root / f"stress-{language}.html"
            html.write_text(render_document(cls.issue, stressed, narrative, language, logo=logo, images=images), encoding="utf-8")
            result = render_live_pdf.render(html, root / f"stress-{language}.pdf", timeout=120)
            cls.stress[language] = (fitz.open(result["path"]), narrative, stressed)
            cls.addClassCleanup(cls.stress[language][0].close)

    def test_each_product_starts_on_a_new_page_and_takes_two_pages_unless_its_leads_are_long(self):
        """Plan rule: two pages per product; a third only when the data is genuinely long (here,
        supplier leads whose terms differ by offer). Page one never runs over; page two starts fresh."""
        kicker = {"en": "MARKET AND SOURCING", "hi-Latn": "BAZAAR AUR SOURCING"}
        long_leads = [sum(bool(s.get("scoped_supplier_contract")) for s in product["suppliers"]) >= 3
                      for product in self.dataset["products"]]
        self.assertTrue(any(long_leads) and not all(long_leads))
        editions = [(language, "fixture", document) for language, document in self.documents.items()]
        editions += [(language, "at the writing budgets", document) for language, (document, _, _) in self.stress.items()]
        for language, edition, document in editions:
            with self.subTest(language=language, edition=edition):
                spans = product_spans(document)
                markets = [number for number, page in enumerate(document) if kicker[language] in page.get_text()]
                self.assertEqual(len(spans), len(self.dataset["products"]))
                self.assertEqual(spans[0][0], 3)  # cover, at a glance, how to read
                self.assertEqual(len(markets), len(spans))
                for (start, pages), market, long in zip(spans, markets, long_leads):
                    self.assertEqual(market, start + 1)  # page one fits on one page; page two starts fresh
                    if long:  # three offer-level leads: a whole lead may move to a third page
                        self.assertIn(pages, (2, 3))
                    else:
                        self.assertEqual(pages, 2)
                self.assertEqual(len(document), 4 + sum(pages for _, pages in spans))

    def test_stress_editions_really_are_at_the_limits(self):
        from product_radar.report_narration import WRITING_LIMITS, budget_length
        for language, (_, narrative, stressed) in self.stress.items():
            limits = WRITING_LIMITS[language]
            self.assertEqual(len(narrative["title"]), 100)
            for product, row in zip(stressed["products"], narrative["products"]):
                self.assertGreaterEqual(budget_length(row["reason"], product["title"]), limits["reason"] - 12)
                self.assertLessEqual(budget_length(row["reason"], product["title"]), limits["reason"])
                self.assertEqual(len(row["points"]), 3)
                self.assertTrue(all(limits["point"] - 12 <= len(point) <= limits["point"] for point in row["points"]))
            updated = next(p for p in stressed["products"] if p["status"] == "Updated")
            from product_radar.report_visuals import compliance_classes
            self.assertTrue(compliance_classes(updated))  # the hardest page one: flagged and updated

    def test_a_maximum_length_title_keeps_the_whole_cover_on_one_page(self):
        glance = {"en": "This issue at a glance", "hi-Latn": "Ek nazar mein"}
        notice = {"en": "not a promise of sales", "hi-Latn": "bikri ka vaada nahi"}
        for language, (document, narrative, _) in self.stress.items():
            with self.subTest(language=language):
                cover = document[0]
                self.assertIn(glance[language], document[1].get_text())  # the cover is exactly one page
                band = [drawing["rect"] for drawing in cover.get_drawings() if drawing.get("fill")
                        and abs(drawing["rect"].width - cover.rect.width) < 2 and 10 < drawing["rect"].height < 200]
                self.assertEqual(len(band), 1)
                band = band[0]
                self.assertLessEqual(band.y1, cover.rect.height + 1)  # the navy band is whole
                blocks = cover.get_text("blocks")
                inside = " ".join(block[4] for block in blocks if block[1] >= band.y0)
                self.assertIn(notice[language], inside.replace("\n", " "))
                above = [block for block in blocks if block[1] < band.y0]
                self.assertLess(max(block[3] for block in above), band.y0 - 2)  # nothing runs into the band
                words = cover.get_text().replace("\n", " ")
                self.assertIn(narrative["title"].split(":")[0], words)

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
