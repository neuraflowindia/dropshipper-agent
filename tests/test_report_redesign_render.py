"""Whole-edition proofs for the redesigned report (synthetic fixtures; no browser)."""

from copy import deepcopy
from datetime import timedelta
from html import unescape
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
            self.assertTrue(words["count_dek"].format(n=10, new=10, upd=0) in html)
            self.assertIn(words["glance_dek"]["issue"].format(n=10), html)  # no ranking claim without a score

    def test_saved_market_check_details_are_escaped(self):
        issue, dataset = fixture()
        dataset["products"][0]["market_checks"][0]["detail"] = 'Detail <b onclick="x()">bold</b> & more'
        for language in LANGUAGES:
            html = live.render_html(issue, dataset, language=language)
            self.assertIn("Detail &lt;b onclick=&quot;x()&quot;&gt;bold&lt;/b&gt; &amp; more", html)
            self.assertNotIn("<b onclick", html)

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
        for injected in ('<script>x</script>', '<img src="https://example.org/x.png">', '<style>a{b:url(https://example.org/f.woff2)}</style>',
                         '<a href="javascript:alert(1)">x</a>', '<img srcset="https://example.org/x.png 2x">',
                         '<link rel="stylesheet" href="https://example.org/x.css">', '<style>@import "https://example.org/x.css";</style>',
                         '<svg><image href="https://example.org/x.png"/></svg>', '<p onmouseover="x()">x</p>'):
            with self.subTest(injected=injected), \
                    patch("product_radar.report_view.render_document", return_value="<html>" + injected + "</html>"), \
                    self.assertRaisesRegex(SafetyError, "LIVE_RENDER_HTML_INVALID"):
                live.render_html(issue, dataset)

    def test_escaped_words_that_look_like_markup_are_not_refused(self):
        """A title or reason may say "url(" or "href=": escaped text is never mistaken for a fetch."""
        issue, dataset = fixture()
        product = dataset["products"][0]
        product["market_checks"][0]["detail"] = 'Shows url(https://example.org) and href="x" as plain words.'
        html = live.render_html(issue, dataset)
        self.assertIn("Shows url(https://example.org) and href=&quot;x&quot; as plain words.", html)

    def test_writing_budgets_are_enforced_per_language(self):
        issue, dataset = fixture()
        title = dataset["products"][0]["title"]
        for language, limit in (("en", 320), ("hi-Latn", 360)):
            # {{title}} counts as the product name it prints.
            for size, ok in ((limit, True), (limit + 1, False)):
                changed = deepcopy(dataset)
                row = changed["narratives"][language]["products"][0]
                row["reason"] = "{{title}} " + "a" * (size - len(title) - 1)
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
        changed["narratives"]["hi-Latn"]["products"][0]["points"] = ["{{title}} " + "b" * (105 - len(title))]
        with self.assertRaisesRegex(SafetyError, "LIVE_NARRATIVE_TOO_LONG"):
            live.validate_dataset(issue, changed)

    def test_limitation_and_update_sentence_limits_hold_in_packet_validation(self):
        issue, dataset = fixture()
        title = dataset["products"][0]["title"]
        changed = deepcopy(dataset)
        changed["narratives"]["en"]["products"][0]["limitation"] = "c" * 1000
        live.validate_dataset(issue, changed)
        changed["narratives"]["en"]["products"][0]["limitation"] = "c" * 1001
        with self.assertRaisesRegex(SafetyError, "LIVE_NARRATIVE_TOO_LONG"):
            live.validate_dataset(issue, changed)
        # The update sentence: checked with {{title}} expanded (a New product may not carry one at all).
        changed = deepcopy(dataset)
        changed["narratives"]["en"]["products"][0]["update_sentence"] = "x"
        with self.assertRaisesRegex(SafetyError, "LIVE_NARRATIVE_UPDATE_NOT_ADMITTED"):
            live.validate_dataset(issue, changed)
        from product_radar.report_narration import WRITING_LIMITS, budget_length
        self.assertEqual(budget_length("{{title}} rose.", title), len(title) + 6)
        self.assertEqual((WRITING_LIMITS["en"]["update_sentence"], WRITING_LIMITS["hi-Latn"]["update_sentence"]), (130, 150))

    def test_checklist_phrase_limits_hold_in_packet_validation(self):
        issue, dataset = current_fit_fixture()
        title = dataset["products"][0]["title"]
        for language, limit in (("en", 90), ("hi-Latn", 105)):
            for size, ok in ((limit, True), (limit + 1, False)):
                changed = deepcopy(dataset)
                changed["narratives"][language]["products"][0]["checklist_phrases"][0]["text"] = "{{title}} " + "d" * (size - len(title) - 1)
                with self.subTest(language=language, size=size):
                    if ok:
                        live.validate_dataset(issue, changed)
                    else:
                        with self.assertRaisesRegex(SafetyError, "LIVE_CHECKLIST_PHRASES_INVALID"):
                            live.validate_dataset(issue, changed)


if __name__ == "__main__":
    unittest.main()


class RichFixtureEditionTests(unittest.TestCase):
    """The rich synthetic issue (real writers; see tests/report_redesign_fixture.py) in both editions."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        from pathlib import Path
        from report_redesign_fixture import rich_issue
        cls.folder = tempfile.TemporaryDirectory(prefix="report-rich-")
        cls.addClassCleanup(cls.folder.cleanup)
        cls.root = Path(cls.folder.name) / "fixture"
        _, cls.issue, cls.dataset = rich_issue(cls.root)
        cls.html = {language: live.render_html(cls.issue, cls.dataset, language=language, root=cls.root)
                    for language in LANGUAGES}
        cls.views = {p["concept_id"]: report_view.product_view(p, prose, cls.dataset, "en", None)
                     for p, prose in zip(cls.dataset["products"], cls.dataset["narratives"]["en"]["products"])}

    def by_title(self, words):
        return next(view for view in self.views.values() if words in view["title"])

    def test_charts_draw_the_frozen_originals_the_score_read(self):
        from product_radar.report_visuals import score_view
        for product in self.dataset["products"]:
            view = self.views[product["concept_id"]]
            score = score_view(product)
            facts = score["facts"]
            with self.subTest(title=product["title"]):
                # Meta: the timeline has one dot per counted advertiser and the same recent rule.
                if view["meta"]["starts"] is not None:
                    starts = view["meta"]["starts"]
                    self.assertEqual(sum(recent for _, recent in starts) if starts else None,
                                     facts["new_advertisers"]["recent_start_advertisers"])
                # Trends: a usable chart is drawn with the score's compared windows.
                if facts["google_searches"]["usable"]:
                    self.assertGreaterEqual(len(view["trends"]["points"]), 50)
                    self.assertEqual(len(view["trends"]["windows"]), 8)
                else:
                    self.assertEqual(view["trends"]["state"], "none")
                # Platforms: listed exactly where the score counted a listing.
                listed = {row["route"] for row in view["platform"]["rows"] if row["state"] == "listed"}
                self.assertEqual(listed, set(facts["platform_lists"]["listed"]))
                # Quick commerce: in-stock places are the score's stock cities.
                self.assertEqual(view["qc"]["in_stock"], sorted(facts["quick_commerce"]["stock_cities"]))

    def test_states_cover_the_plan(self):
        labels = {view["score"]["label"] for view in self.views.values()}
        self.assertEqual(labels, {"sweet_spot", "early_bet", "proven_but_crowded"})
        self.assertTrue(any(view["image"] is None for view in self.views.values()))
        self.assertTrue(any(view["compliance"] for view in self.views.values()))
        self.assertTrue(any(view["status"] == "Updated" and view["prose"]["update"] for view in self.views.values()))
        trimmer = self.by_title("nail trimmer")
        self.assertEqual(trimmer["compliance"], ["baby", "electrical"])
        self.assertTrue(any(recent for _, recent in trimmer["meta"]["starts"]))
        self.assertTrue(any(row["roles"] for row in trimmer["platform"]["rows"]))
        feeding = self.by_title("feeding set")
        self.assertEqual(feeding["qc"]["rows"]["blinkit"][feeding["qc"]["places"].index("Mumbai")], "in_stock")
        self.assertEqual(feeding["qc"]["rows"]["instamart"][feeding["qc"]["places"].index("Mumbai")], "not_in_stock")
        self.assertIn("not_checked", feeding["qc"]["rows"]["blinkit"])

    @staticmethod
    def sheet_numbers(html):
        """Every figure printed on each sheet (text and drawing labels; never markup or style),
        as a sorted list per sheet: word order may differ between languages, figures may not."""
        body = html.split("</style>", 1)[1]
        numbers = []
        for sheet in re.split(r'(?=<section class="sheet )', body)[1:]:
            text = re.sub(r"<[^>]+>", " ", sheet)
            numbers.append(sorted(re.findall(r"\d+(?:[.,]\d+)*", unescape(text))))
        return numbers

    def test_both_editions_show_the_same_figures(self):
        english, hinglish = self.sheet_numbers(self.html["en"]), self.sheet_numbers(self.html["hi-Latn"])
        self.assertEqual(len(english), 3 + 2 * 10 + 1)
        for number, (en, hi) in enumerate(zip(english, hinglish)):
            with self.subTest(sheet=number):
                self.assertTrue(en)
                self.assertEqual(en, hi)
        for pattern in (r'<span class="scorecell">.*?<b>(\d+)</b>', r'class="part-val"><b>(\d+)</b>/(\d+)',
                        r'<span class="p-price">(₹[0-9,.]+)</span>', r'aria-label="(\d+) [a-z]+"'):
            with self.subTest(pattern=pattern):
                found = re.findall(pattern, self.html["en"])
                self.assertTrue(found)
                self.assertEqual(found, re.findall(pattern, self.html["hi-Latn"]))

    def test_a_changed_figure_in_one_language_is_caught(self):
        changed = self.html["hi-Latn"].replace('<span class="stat">7</span>', '<span class="stat">8</span>', 1)
        self.assertNotEqual(changed, self.html["hi-Latn"])
        self.assertNotEqual(self.sheet_numbers(self.html["en"]), self.sheet_numbers(changed))

    def context(self, words):
        from product_radar.report_visuals import market_context, score_view
        product = next(p for p in self.dataset["products"] if words in p["title"])
        return product, score_view(product), market_context(product)

    def test_meta_counts_and_recent_starts_are_cross_checked_with_the_score(self):
        from product_radar.report_visuals import meta_view
        _, score, context = self.context("nail trimmer")
        self.assertEqual(meta_view(score, context)["recent"], score["facts"]["new_advertisers"]["recent_start_advertisers"])
        for part, fact, change in (("facebook_ads", "relevant_ads", 1), ("new_advertisers", "recent_start_advertisers", 1)):
            changed = deepcopy(score)
            changed["facts"][part][fact] += change
            with self.subTest(fact=fact), self.assertRaisesRegex(SafetyError, "LIVE_REPORT_VISUAL_FACTS_CHANGED"):
                meta_view(changed, context)
        changed = deepcopy(context)
        changed["market"]["sources"]["meta"]["used_ids"] = changed["market"]["sources"]["meta"]["used_ids"][1:]
        with self.assertRaisesRegex(SafetyError, "LIVE_REPORT_VISUAL_FACTS_CHANGED"):
            meta_view(score, changed)

    def test_the_trends_chart_must_be_the_one_the_score_used(self):
        from product_radar.report_visuals import trends_view
        _, score, context = self.context("plant spike")
        view = trends_view(score, context)
        # Only completed weeks the score compares are drawn: the line ends where the recent window ends.
        self.assertEqual(view["windows"][-1], len(view["points"]) - 1)
        self.assertTrue(all(len(value) <= 3 for value in (view["recent"], view["previous"])))  # whole points
        for changed in (dict(context, charts=[]), dict(context, charts=[dict(row, id="other") for row in context["charts"]])):
            with self.assertRaisesRegex(SafetyError, "LIVE_REPORT_VISUAL_FACTS_CHANGED"):
                trends_view(score, changed)

    def test_quick_commerce_checks_must_be_dated_with_the_market_proof(self):
        from product_radar.issue_allocation import selection_fingerprint
        from product_radar.models import timestamp
        index = next(n for n, p in enumerate(self.dataset["products"]) if "feeding set" in p["title"])
        issue, dataset = deepcopy(self.issue), deepcopy(self.dataset)
        later = (timestamp(dataset["products"][index]["quick_commerce_checks"]["as_of"]) + timedelta(seconds=1)).isoformat()
        for record in (issue["products"][index], dataset["products"][index]):
            record["quick_commerce_checks"]["as_of"] = later
        issue["selection_hash"] = dataset["selection_hash"] = selection_fingerprint(issue)
        with self.assertRaisesRegex(SafetyError, "LIVE_QUICK_COMMERCE_CHECKS_INVALID"):
            live.validate_dataset(issue, dataset, root=self.root)

    def test_the_order_sentence_and_divider_match_this_issues_rows(self):
        glance = self.html["en"].split('id="glance"')[1].split('id="key"')[0]
        # New types first (each by score, crowded last), then the one update: the "status" sentence.
        self.assertIn(TEXT["en"]["glance_dek"]["status"].format(n=10), glance)
        levels = [p["opportunity_basis"]["parts"]["room"]["items"]["crowding"]["facts"]["level"] for p in self.dataset["products"]]
        first_high = levels.index("high")
        self.assertTrue(all(level == "high" for level in levels[first_high:]))
        self.assertLess(glance.index('<tr class="group">'), glance.index(f'<td class="c0">{first_high + 1:02d}</td>'))
        self.assertIn(TEXT["hi-Latn"]["glance_dek"]["status"].format(n=10), self.html["hi-Latn"])

    def test_every_site_used_is_listed_with_or_without_a_link(self):
        closing = self.html["en"].split('id="method"')[1]
        for site in ("Google Trends India", "Facebook Ads Library", "Amazon India", "Flipkart", "DeoDap", "BaapStore", "Dropdash",
                     "Blinkit", "Zepto", "Supplier pages"):
            self.assertIn(f"<li>{site}</li>", closing)
        self.assertNotIn("Supplier page (scoped review)", self.html["en"])
        self.assertNotIn("Supplier page (scoped review)", self.html["hi-Latn"])
        self.assertIn("<li>Supplier ke pages</li>", self.html["hi-Latn"].split('id="method"')[1])
        spikes = self.by_title("plant spike")
        self.assertIn(("Google Trends India", None, "5 Oct"), spikes["sources"])
        self.assertIn(("DeoDap", None, "5 Oct"), spikes["sources"])
        for language, html in self.html.items():
            self.assertEqual(len(sheets(html, "product-a")), 10)
            self.assertEqual(html.count('class="timeline"'), sum(view["meta"]["starts"] is not None and view["meta"]["end"] is not None
                                                                  for view in self.views.values()))
            self.assertTrue(TEXT[language]["changed"] in html)
