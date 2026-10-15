"""Focused print-layout and immutable edition recovery proofs; no live packets."""

from copy import deepcopy
from html import escape
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar import live_outputs as live
from product_radar.models import SafetyError
from product_radar.storage import atomic_bytes, atomic_json, load_json
from test_live_outputs import fixture
from test_fit_pipeline import older_format_scores


class Tables(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tables = []
        self.stack = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            record = {"classes": dict(attrs).get("class", "").split(), "tags": [], "text": []}
            self.tables.append(record)
            self.stack.append(record)
        if self.stack:
            self.stack[-1]["tags"].append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        if tag == "table":
            self.stack.pop()

    def handle_data(self, text):
        if self.stack:
            self.stack[-1]["text"].append(text)

    def of_class(self, name):
        return [record for record in self.tables if name in record["classes"]]


def current_fit_fixture():
    from test_fit_pipeline import current_fixture, selection
    _, issue, dataset = selection(current_fixture())
    dataset["narratives"] = {language: {"title": "Fixture research", "products": [
        {"concept_id": product["concept_id"], "reason": "Fixture product research.", "points": [],
         "limitation": "Supplier terms need checking.", "update_sentence": "",
         "checklist_phrases": [{"id": row["id"], "text": "Fixture judgment" if language == "en" else "Test ki rai"}
                               for row in product["criteria"]]}
        for product in dataset["products"]]} for language in ("en", "hi-Latn")}
    return issue, dataset


@older_format_scores
class ReportPrintLayoutTests(unittest.TestCase):
    def test_full_bleed_background_keeps_physical_page_content_margins(self):
        issue, dataset = fixture()
        html = live.render_html(issue, dataset)
        self.assertIn('@page{size:A4 portrait;margin:14mm 16mm 15mm 17mm;background:#F9F3EC;}', html)
        self.assertIn('.page{width:auto;height:auto;min-height:0;padding:0;overflow:visible;}', html)
        self.assertNotIn('@media print{\n      @page{size:A4 portrait;margin:0;', html)
        self.assertIn('.live-cover{min-height:268mm;}', html)

    def test_checklist_has_product_identity_repeating_headings_and_all_ten_answers(self):
        issue, dataset = current_fit_fixture()
        before = deepcopy(dataset)
        for language, headings in (("en", ("Checklist point", "Result", "Reason")),
                                   ("hi-Latn", ("Checklist point", "Nateeja", "Wajah"))):
            with self.subTest(language=language):
                html = live.render_html(issue, dataset, language=language)
                tables = Tables(html).of_class("live-fit-table")
                self.assertEqual(len(tables), 10)
                for product, table in zip(dataset["products"], tables):
                    self.assertIn(f'{product["rank"]:02d} · {product["title"]}', table["text"])
                    self.assertEqual(sum(tag == "thead" for tag, _ in table["tags"]), 1)
                    self.assertEqual(sum(tag == "tr" for tag, _ in table["tags"]), 12)
                    self.assertEqual(["".join(table["text"]).count(heading) for heading in headings], [1, 1, 1])
                    self.assertEqual(sum(text in ("Fixture judgment", "Test ki rai") for text in table["text"]), 10)
                self.assertIn('.live-evidence-table thead,.live-market-table thead{display:table-header-group;}', html)
        self.assertEqual(dataset, before)

    def test_market_checks_keep_order_and_three_columns_without_whole_section_avoid(self):
        issue, dataset = fixture()
        product = dataset["products"][0]
        first = product["market_checks"][0]
        product["market_checks"] = [dict(first, source=f"Fixture source {number}", detail=f"Exact saved detail {number}.")
                                    for number in range(7)]
        before = deepcopy(dataset)
        for language in ("en", "hi-Latn"):
            html = live.render_html(issue, dataset, language=language)
            table = Tables(html).of_class("live-market-table")[0]
            self.assertIn("01 · Synthetic product 1", table["text"])
            self.assertEqual(sum(tag == "tr" for tag, _ in table["tags"]), 4)
            self.assertEqual(sum(tag == "td" for tag, _ in table["tags"]), 9)
            for number in range(7):
                self.assertIn(f"Exact saved detail {number}.", table["text"])
                if number:
                    self.assertLess(table["text"].index(f"Fixture source {number-1}"), table["text"].index(f"Fixture source {number}"))
            self.assertIn('.live-checks{display:block;break-inside:auto;page-break-inside:auto;}', html)
            self.assertNotIn('.live-hero,.live-checks,.live-overview li{break-inside:avoid;', html)
            self.assertIn('.live-market-table td{vertical-align:top;width:33.333%;', html)
            self.assertIn('.live-market-table td{width:100%;padding:0 0 5mm;}', html)
        self.assertEqual(dataset, before)

    def test_numbered_price_rows_keep_every_full_quote_condition_and_link(self):
        issue, dataset = fixture()
        product = dataset["products"][0]
        original = product["prices"][0]
        product["prices"] = [dict(original, amount=str(100 + n), conditions="Original quote condition. " * 50 + f"END-{n}")
                             for n in range(61)]
        before = deepcopy(dataset)
        for language in ("en", "hi-Latn"):
            html = live.render_html(issue, dataset, language=language)
            table = Tables(html).of_class("live-price-table")[0]
            self.assertEqual(sum(tag == "tr" for tag, _ in table["tags"]), 63)
            self.assertIn("61", table["text"])
            self.assertIn("01 · Synthetic product 1", table["text"])
            for price in product["prices"]:
                self.assertIn(live.price_display(price, language), table["text"])
                self.assertIn(escape(price["conditions"]), html)
            self.assertIn('href="https://example.org/ad"', html)
            self.assertIn('.live-price-table,.live-price-table tr:last-child,.live-details>div:first-child{break-after:avoid;', html)
        self.assertEqual(dataset, before)

    def test_missing_price_is_not_turned_into_an_empty_offer(self):
        issue, dataset = fixture()
        dataset["products"][0]["prices"] = []
        for language in ("en", "hi-Latn"):
            html = live.render_html(issue, dataset, language=language)
            self.assertEqual(len(Tables(html).of_class("live-price-table")), 9)
            self.assertIn(live.COPY[language]["missing_price"], html)

    def test_example_table_retains_its_full_cells_link_and_product_context(self):
        product = {"rank": 7, "title": "Fixture type <not markup>", "example_products": [{
            "title": "Original listing", "source": "Fixture source", "seller": "Fixture seller", "pack": "Pack of two",
            "price": {"displayed": "₹110"}, "observed_at": "2026-10-08T09:00:00+00:00",
            "url": "https://example.org/original?x=1&y=2"}], "example_coverage": {"omitted_offers": 0}}
        before = deepcopy(product)
        for language in ("en", "hi-Latn"):
            html = live._example_html(product, language)
            table = Tables(html).of_class("live-examples-table")[0]
            self.assertIn("07 · Fixture type <not markup>", table["text"])
            for text in ("Original listing", "Fixture source", "Fixture seller", "Pack of two", "2026-10-08"):
                self.assertIn(text, table["text"])
            self.assertIn("₹110; unit and price basis not verified", table["text"])
            self.assertIn('href="https://example.org/original?x=1&amp;y=2"', html)
            self.assertNotIn("<not markup>", html)
        self.assertEqual(product, before)


@older_format_scores
class ReportPrintVersionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.issue, self.dataset = fixture()
        self.data = self.root / "data/outputs" / self.issue["issue_id"] / "revision-1"
        self.stages = self.data / "internal-stages"

    def old_edition(self, *, language="en"):
        # An exact-byte historical synthetic edition, not a client report.
        with patch.object(live, "HTML_RENDER_VERSION", live.LEGACY_HTML_RENDER_VERSION), \
                patch.object(live, "render_html", return_value="<!doctype html><p>Historical synthetic edition.</p>"):
            return live.write_issue_edition(self.issue, self.dataset, self.root, language)

    def test_versions_are_separate_for_each_fact_contract(self):
        from product_radar.report_source_facts import CONTRACT
        plain = {"schema_version": 1, "products": []}
        routes = {"schema_version": 2, "products": [{"source_route_contract": CONTRACT}]}
        types = {"schema_version": 3, "products": []}
        for dataset, old, new in ((plain, live.LEGACY_HTML_RENDER_VERSION, live.HTML_RENDER_VERSION),
                                  (routes, live.LEGACY_SOURCE_HTML_RENDER_VERSION, live.SOURCE_HTML_RENDER_VERSION),
                                  (types, live.LEGACY_TYPE_HTML_RENDER_VERSION, live.TYPE_HTML_RENDER_VERSION)):
            self.assertEqual(live._supported_render_versions(dataset), (new, old))
            self.assertNotEqual(old, new)
            self.assertIn("print-v2", new)

    def test_new_stage_uses_new_print_version_without_changing_facts_or_workbook_version(self):
        before = deepcopy(self.dataset)
        edition = live.write_issue_edition(self.issue, self.dataset, self.root)
        self.assertEqual(edition["render_version"], live.HTML_RENDER_VERSION)
        self.assertEqual(live._generation_version(self.dataset), "deterministic-workbook-v1")
        self.assertEqual(self.dataset, before)

    def test_completed_old_edition_reuses_original_bytes_without_rerendering(self):
        for language in ("en", "hi-Latn"):
            first = self.old_edition(language=language)
            path = Path(first["artifacts"]["html"]["path"])
            original = path.read_bytes()
            with patch.object(live, "render_html", side_effect=AssertionError("old HTML must not regenerate")):
                replay = live.write_issue_edition(self.issue, self.dataset, self.root, language)
            self.assertEqual(first, replay)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(replay["render_version"], live.LEGACY_HTML_RENDER_VERSION)

    def test_interrupted_old_alias_recovers_original_payload_and_old_version(self):
        def interrupt(path, raw):
            if Path(path).name == "report-en.html":
                raise OSError("synthetic interrupted promotion")
            atomic_bytes(path, raw)
        with patch.object(live, "atomic_bytes", side_effect=interrupt), self.assertRaises(OSError):
            self.old_edition()
        with patch.object(live, "render_html", side_effect=AssertionError("must recover original bytes")):
            receipt = live.write_issue_edition(self.issue, self.dataset, self.root)
        self.assertEqual(receipt["render_version"], live.LEGACY_HTML_RENDER_VERSION)
        self.assertEqual(Path(receipt["artifacts"]["html"]["path"]).read_bytes(),
                         b"<!doctype html><p>Historical synthetic edition.</p>")

    def test_old_version_cannot_hide_changed_narrative_or_other_binding(self):
        first = self.old_edition()
        path = Path(first["artifacts"]["html"]["path"])
        before = path.read_bytes()
        self.dataset["narratives"]["en"]["title"] = "Changed fixture narrative"
        with self.assertRaisesRegex(SafetyError, "STAGE_BINDING_CHANGED"):
            live.write_issue_edition(self.issue, self.dataset, self.root)
        self.assertEqual(path.read_bytes(), before)

    def test_foreign_or_unknown_saved_render_version_is_rejected(self):
        self.old_edition()
        path = self.stages / "edition-en-intent.json"
        original = load_json(path, 128 * 1024)
        for version in (live.LEGACY_SOURCE_HTML_RENDER_VERSION, live.TYPE_HTML_RENDER_VERSION,
                        "unknown-layout", None, True, []):
            with self.subTest(version=version):
                changed = deepcopy(original)
                changed["binding"]["render_version"] = version
                atomic_json(path, changed)
                with self.assertRaisesRegex(SafetyError, "STAGE_BINDING_CHANGED"):
                    live.write_issue_edition(self.issue, self.dataset, self.root)
        atomic_json(path, original)

    def test_old_final_manifest_reuses_original_packet_without_render_or_workbook_changes(self):
        live.write_issue_workbook(self.issue, self.dataset, self.root)
        self.old_edition()
        first = live.write_issue_package(self.issue, self.dataset, self.root)
        original = {name: Path(record["path"]).read_bytes() for name, record in first["artifacts"].items()}
        self.assertEqual(first["internal_stage"]["edition_render_version"], live.LEGACY_HTML_RENDER_VERSION)
        with patch.object(live, "render_html", side_effect=AssertionError("no historical regeneration")), \
                patch.object(live, "_workbook_bytes", side_effect=AssertionError("no historical workbook change")):
            replay = live.write_issue_package(self.issue, self.dataset, self.root)
        self.assertEqual(first, replay)
        for name, raw in original.items():
            self.assertEqual(Path(replay["artifacts"][name]["path"]).read_bytes(), raw)
        live.validate_stage_provenance(self.dataset, "en", first["internal_stage"])

    def test_old_provenance_still_rejects_wrong_family_unknown_version_or_changed_hash(self):
        live.write_issue_workbook(self.issue, self.dataset, self.root)
        self.old_edition()
        manifest = live.write_issue_package(self.issue, self.dataset, self.root)
        for changes in ({"edition_render_version": live.LEGACY_SOURCE_HTML_RENDER_VERSION},
                        {"edition_render_version": "unknown-layout"}, {"narrative_hash": "f" * 64}):
            with self.subTest(changes=changes), self.assertRaisesRegex(SafetyError, "STAGE_PROVENANCE_CHANGED"):
                live.validate_stage_provenance(self.dataset, "en", dict(manifest["internal_stage"], **changes))


if __name__ == "__main__":
    unittest.main()
