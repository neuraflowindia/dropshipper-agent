"""Whole-edition proofs for the redesigned report (synthetic fixtures; no browser)."""

from copy import deepcopy
import re
import unittest
from unittest.mock import patch

from product_radar import live_outputs as live
from product_radar import report_view
from product_radar.models import SafetyError
from product_radar.report_charts import PT
from product_radar.report_copy import TEXT
from test_fit_pipeline import older_format_scores
from test_live_outputs import fixture
from test_report_print_layout import current_fit_fixture

LANGUAGES = ("en", "hi-Latn")


def sheets(html, name):
    return re.findall(r'<section class="sheet ' + name + r'\b[^>]*>', html)


def svg_text_sizes(html):
    return [float(value) for value in re.findall(r'<text [^>]*font-size="([0-9.]+)"', html)]


@older_format_scores
class RedesignedEditionTests(unittest.TestCase):
    def test_every_edition_is_self_contained_and_scriptless(self):
        for build in (fixture, current_fit_fixture):
            issue, dataset = build()
            for language in LANGUAGES:
                with self.subTest(fixture=build.__name__, language=language):
                    html = live.render_html(issue, dataset, language=language)
                    self.assertTrue(html.startswith('<!doctype html><html lang="' + language + '">'))
                    self.assertIsNone(re.search(r"<(?:script|iframe|object|embed)\b", html, re.I))
                    self.assertEqual(re.findall(r'\bsrc="(?!data:)', html), [])
                    self.assertEqual(re.findall(r'url\((?!data:)', html), [])
                    self.assertEqual(re.findall(r'href="(?!https://|#)', html), [])
                    self.assertEqual(html.count("@font-face"), 9)

    def test_page_structure_two_sheets_per_product_and_working_anchors(self):
        issue, dataset = current_fit_fixture()
        html = live.render_html(issue, dataset)
        products = dataset["products"]
        self.assertEqual(len(sheets(html, "cover")), 1)
        self.assertEqual(len(sheets(html, "glance")), 1)
        self.assertEqual(len(sheets(html, "key")), 1)
        self.assertEqual(len(sheets(html, "closing")), 1)
        self.assertEqual(len(sheets(html, "product-a")), len(products))
        self.assertEqual(len(sheets(html, "product-b")), len(products))
        order = re.findall(r'<section class="sheet (product-[ab])" (?:id="[^"]+" )?data-concept-id="([^"]+)"', html)
        self.assertEqual(order, [(kind, p["concept_id"]) for p in products for kind in ("product-a", "product-b")])
        for product in products:
            self.assertIn(f'id="product-{product["concept_id"]}"', html)
            self.assertGreaterEqual(html.count(f'href="#product-{product["concept_id"]}"'), 1)
        self.assertIn(".sheet{break-after:page;page-break-after:always}", html)

    def test_scored_products_show_the_code_owned_score_in_both_languages(self):
        issue, dataset = current_fit_fixture()
        editions = {language: live.render_html(issue, dataset, language=language) for language in LANGUAGES}
        for language, html in editions.items():
            words = TEXT[language]
            self.assertEqual(html.count('class="dial"'), len(dataset["products"]) + 1)  # one per product + the key page
            self.assertEqual(html.count('class="partbar"'), 4 * len(dataset["products"]))
            self.assertEqual(html.count('class="gauge"'), len(dataset["products"]) + 1)
            self.assertIn(words["adds_up"], html)
            self.assertIn(words["labels"]["sweet_spot"], html)
        numbers = {language: re.findall(r'<span class="scorecell">.*?<b>(\d+)</b>', html) for language, html in editions.items()}
        self.assertEqual(numbers["en"], numbers["hi-Latn"])
        self.assertEqual(numbers["en"], [str(p["opportunity_score"]) for p in dataset["products"]])
        parts = {language: re.findall(r'class="part-val"><b>(\d+)</b>/(\d+)', html) for language, html in editions.items()}
        self.assertEqual(parts["en"], parts["hi-Latn"])
        self.assertIn("Poora karta hai", editions["hi-Latn"])
        self.assertIn("Small in size", editions["hi-Latn"])  # criterion names stay English (shared contract)

    def test_unscored_packets_render_honestly_without_a_score(self):
        issue, dataset = fixture()
        for language in LANGUAGES:
            html = live.render_html(issue, dataset, language=language)
            words = TEXT[language]
            self.assertIn(words["not_scored"], html)
            self.assertNotIn('class="dial"', html.split('id="key"')[0])
            self.assertIn("Synthetic ad evidence for a fixture only.", html)  # the saved market check stays visible
            self.assertIn(words["count_dek_unscored"].split("{n}")[1].split(".")[0].strip(), html)

    def test_compliance_flag_is_code_owned_and_counted(self):
        issue, dataset = fixture()
        baby = [p for p in dataset["products"] if p["category"] == "Baby Products"]
        self.assertTrue(baby)
        for language in LANGUAGES:
            html = live.render_html(issue, dataset, language=language)
            words = TEXT[language]
            self.assertEqual(html.count('<div class="comply">'), len(baby))
            self.assertEqual(html.count(words["compliance"]["baby"]), len(baby))
            self.assertIn(f'<b>{len(baby)}</b><span>{words["kpi"]["compliance"]}</span>', html)

    def test_synthetic_marker_replaces_the_old_notice_line(self):
        issue, dataset = fixture()
        html = live.render_html(issue, dataset)
        self.assertIn('@top-right{content:"ILLUSTRATIVE SAMPLE · NOT RESEARCH"', html)
        self.assertIn(TEXT["en"]["synthetic_note"], html)
        captured = deepcopy(dataset)
        captured["evidence_type"] = "captured"
        html = live.render_html(issue, captured)
        self.assertNotIn("@top-right{content:\"", html)
        self.assertNotIn(TEXT["en"]["synthetic_note"], html)

    def test_nothing_is_set_below_eight_points(self):
        issue, dataset = current_fit_fixture()
        for language in LANGUAGES:
            html = live.render_html(issue, dataset, language=language)
            sizes = [float(value) for value in re.findall(r"font-size:([0-9.]+)pt", html)]
            self.assertGreaterEqual(min(sizes), 8)
            self.assertGreaterEqual(min(svg_text_sizes(html)), round(8 * PT, 2))
            tokens = dict(re.findall(r"--fs-([a-z]+):([0-9.]+)pt", html))
            self.assertGreaterEqual(min(float(value) for value in tokens.values()), 8)

    def test_longer_issues_use_the_dense_glance_and_keep_every_row(self):
        issue, dataset = fixture(count=15)
        html = live.render_html(issue, dataset)
        self.assertIn('class="sheet glance glance--dense"', html)
        glance = html.split('id="glance"')[1].split('id="key"')[0]
        self.assertEqual(glance.count('<td class="c0">'), 15)
        issue, dataset = fixture()
        self.assertFalse('class="sheet glance glance--dense"' in live.render_html(issue, dataset))

    def test_an_unreportable_score_fails_closed(self):
        issue, dataset = current_fit_fixture()
        broken = deepcopy(dataset)
        basis = broken["products"][0]["opportunity_basis"]
        basis["parts"]["room"]["items"]["crowding"]["facts"]["level"] = "not_checked"
        with self.assertRaisesRegex(SafetyError, "LIVE_REPORT_SCORE_INVALID"):
            report_view.render_document(issue, broken, broken["narratives"]["en"], "en", logo=None, images=[None] * 10)

    def test_render_refuses_markup_that_could_fetch_or_run_anything(self):
        issue, dataset = fixture()
        for injected in ('<script>x</script>', '<img src="https://example.org/x.png">', 'url(https://example.org/f.woff2)',
                         '<a href="javascript:alert(1)">x</a>'):
            with self.subTest(injected=injected), \
                    patch("product_radar.report_view.render_document", return_value="<html>" + injected + "</html>"), \
                    self.assertRaisesRegex(SafetyError, "LIVE_RENDER_HTML_INVALID"):
                live.render_html(issue, dataset)

    def test_writing_budgets_are_enforced_per_language(self):
        issue, dataset = fixture()
        for language, limit in (("en", 320), ("hi-Latn", 360)):
            for size, ok in ((limit, True), (limit + 1, False)):
                changed = deepcopy(dataset)
                row = changed["narratives"][language]["products"][0]
                row["reason"] = "{{title}} " + "a" * (size - len("{{title}} "))
                with self.subTest(language=language, size=size):
                    if ok:
                        live.validate_dataset(issue, changed)
                    else:
                        with self.assertRaisesRegex(SafetyError, "LIVE_NARRATIVE_TOO_LONG"):
                            live.validate_dataset(issue, changed)
        changed = deepcopy(dataset)
        changed["narratives"]["en"]["products"][0]["points"] = ["b" * 91]
        with self.assertRaisesRegex(SafetyError, "LIVE_NARRATIVE_TOO_LONG"):
            live.validate_dataset(issue, changed)
        changed = deepcopy(dataset)
        changed["narratives"]["hi-Latn"]["products"][0]["points"] = ["b" * 105]
        live.validate_dataset(issue, changed)


if __name__ == "__main__":
    unittest.main()
