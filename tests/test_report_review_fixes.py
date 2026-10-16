"""Behaviour proofs for the independent review of the redesigned report (16 October 2026).

Each test pins a reviewed defect so it cannot come back: the at-a-glance order
sentence and crowded divider are only printed when the rows show them, money
always prints paise, a legacy lead keeps its own caveat beside scoped leads,
scoped offers read as short dated paragraphs, every site used is listed, the
subscription column reads fees as a caution, crowding counts are whole numbers,
the HTML guard reads markup rather than text, the writing budgets count the
product name, and the workbook's data bars follow the score settings.
Synthetic inputs only; no browser.
"""

from copy import deepcopy
from datetime import date
import unittest

from product_radar import report_charts as charts
from product_radar import report_pages as pages
from product_radar import report_view
from product_radar.models import SafetyError
from product_radar.report_copy import TEXT
from product_radar.report_guard import css_fetches, fetches_or_runs

EN, HI = TEXT["en"], TEXT["hi-Latn"]
MAXIMA = {"want": 20, "early": 30, "room": 25, "product": 25}


def view(rank, status, level, score, *, early=10, product=13, compliance=()):
    return {"rank": rank, "status": status, "anchor": f"product-c{rank}", "title": f"Type {rank}", "category": "Home and Decor",
            "compliance": list(compliance), "fit": {"matches": 8, "total": 10},
            "score": {"level": level, "score": score, "label": "sweet_spot" if level != "high" else "proven_but_crowded",
                      "points": {"want": 5, "early": early, "room": 0, "product": product}, "maxima": dict(MAXIMA)}}


def glance_ctx(views):
    from product_radar.opportunity_score import load_settings
    settings = load_settings()
    new = sum(row["status"] == "New" for row in views)
    return {"words": EN, "views": views, "counts": {"products": len(views), "new": new, "updates": len(views) - new},
            "date_long": "7 October 2026", "footer": "x", "marker": None, "maxima": settings["parts"], "settings": settings}


class GlanceOrderTests(unittest.TestCase):
    """Finding 1: the order sentence and the divider never claim an order the rows do not show."""

    def test_the_order_sentence_is_chosen_from_the_actual_rows(self):
        sorted_rows = [view(1, "New", "low", 80), view(2, "New", "medium", 70), view(3, "Updated", "high", 75)]
        self.assertEqual(pages.order_rule(sorted_rows), "sorted")
        # Score order holds whatever the statuses are, so "ranked by score" is then the true sentence.
        self.assertEqual(pages.order_rule([view(1, "New", "low", 80), view(2, "Updated", "low", 70), view(3, "New", "low", 60)]),
                         "sorted")
        # New first, then updates; each group in score order (the allocator's own pattern).
        status = [view(1, "New", "low", 80), view(2, "New", "high", 40), view(3, "Updated", "low", 90)]
        self.assertEqual(pages.order_rule(status), "status")
        # A Low/Medium extra after a High row, or an update before a new type: no ordering claim at all.
        for rows in ([view(1, "New", "high", 80), view(2, "New", "low", 50)],
                     [view(1, "Updated", "low", 80), view(2, "New", "low", 90)],
                     [view(1, "New", "low", 60), view(2, "Updated", "low", 70), view(3, "New", "low", 80)]):
            self.assertEqual(pages.order_rule(rows), "issue")
        self.assertEqual(pages.order_rule([dict(view(1, "New", "low", 80), score=None)]), "issue")
        self.assertEqual(pages.order_rule([]), "issue")
        # Ties are broken the score's way: early points, then product points.
        self.assertEqual(pages.order_rule([view(1, "New", "low", 60, early=20), view(2, "New", "low", 60, early=10)]), "sorted")
        self.assertEqual(pages.order_rule([view(1, "New", "low", 60, early=10), view(2, "New", "low", 60, early=20),
                                           view(3, "Updated", "low", 10)]), "issue")

    def test_the_crowded_divider_only_heads_a_run_of_high_rows_to_the_end(self):
        self.assertEqual(pages.crowded_from([view(1, "New", "low", 80), view(2, "New", "high", 50), view(3, "Updated", "high", 40)]), 1)
        self.assertIsNone(pages.crowded_from([view(1, "New", "high", 80), view(2, "New", "low", 50)]))
        self.assertIsNone(pages.crowded_from([view(1, "New", "low", 80)]))
        mixed = [view(1, "New", "low", 80), view(2, "New", "high", 50), view(3, "New", "medium", 70)]
        html = pages.glance(glance_ctx(mixed))
        self.assertNotIn('<tr class="group">', html)
        self.assertNotIn("ranked by opportunity score", html)
        self.assertIn("3 product types, in the order chosen for this issue.", html)
        last = [view(1, "New", "low", 80), view(2, "New", "medium", 60), view(3, "New", "high", 50)]
        html = pages.glance(glance_ctx(last))
        self.assertEqual(html.count('<tr class="group">'), 1)
        self.assertLess(html.index('<tr class="group">'), html.index(">03</td>"))
        self.assertGreater(html.index('<tr class="group">'), html.index(">02</td>"))
        self.assertIn("ranked by opportunity score, with types crowded on Amazon and Flipkart after the less crowded ones.", html)
        self.assertIn("Crowded types are only added while fewer than 10 less crowded types are ready.", html)
        self.assertIn("Score bar, left to right: Want (20) · Early (30) · Room (25) · Product (25).", html)

    def test_dense_and_unscored_glance_and_cover_make_no_ranking_claim(self):
        rows = [view(n, "New", "low", 90 - n) for n in range(1, 14)]
        html = pages.glance(glance_ctx(rows))
        self.assertIn(EN["glance_dek_short"]["sorted"], html)
        self.assertNotIn("Crowded types are only added", html)  # the dense dek stays one line
        unscored = [dict(view(1, "New", "low", 80), score=None)]
        self.assertIn("1 product types, in the order chosen for this issue.", pages.glance(glance_ctx(unscored)))
        self.assertNotIn("ranked", EN["count_dek"] + HI["count_dek"])

    def test_the_method_page_states_the_real_shortlist_rule(self):
        shortlist = dict(EN["steps"])["Shortlist"]
        self.assertIn("new types first, then updates", shortlist)
        self.assertNotIn("crowded ones last", shortlist)
        self.assertNotIn("listed last", EN["key_crowd_text"])


class CoverSizeTests(unittest.TestCase):
    """Finding 2: the cover title steps down in size; the plates (not hidden overflow) give way."""

    def test_title_size_steps_with_length(self):
        self.assertEqual(pages.display_size("x" * 48), "")
        self.assertEqual(pages.display_size("x" * 49), " h-display--long")
        self.assertEqual(pages.display_size("x" * 75), " h-display--long")
        self.assertEqual(pages.display_size("x" * 76), " h-display--xlong")
        from product_radar.report_theme import stylesheet
        css = stylesheet(footer="x", page_word="Page", of_word="of")
        self.assertIn(".cover{page:cover;height:297mm}", css)
        self.assertNotIn("overflow:hidden", css.split(".cover{page:cover")[1].split("}")[0])
        self.assertIn(".mosaic .plate{flex:0 1 50mm;height:50mm;min-height:30mm}", css)
        self.assertIn("--fs-display-xlong:21pt", css)


class MoneyAndCountTests(unittest.TestCase):
    """Findings 6 and the crowding counts: paise always print, counts are whole numbers."""

    def crowd_view(self, counts):
        return {"crowd_line": "Crowding", "score": {"level": "low", "points": {"room": 25}, "maxima": {"room": 25},
                                                    "facts": {"crowding": {"counts": counts}}}}

    def test_crowding_counts_are_whole_numbers_grouped_or_fail_closed(self):
        tile = pages._crowd_tile(self.crowd_view({"relevant_listings": {"amazon": 1200, "flipkart": 7}, "listings_with_brand": 3,
                                                  "brands_or_sellers": 1500, "brand_not_shown": 1, "max_ratings": 124000,
                                                  "bestseller_hits": 2}), EN)
        for text in ("Amazon 1,200 · Flipkart 7", ">1,500 <", "1,24,000", ">2</dd>"):
            self.assertIn(text, tile)
        for bad in ({"relevant_listings": {"amazon": "<b>9</b>"}}, {"relevant_listings": {"amazon": -1}},
                    {"bestseller_hits": True}, {"max_ratings": 1.5}, {"relevant_listings": ["amazon"]}, {"brands_or_sellers": "9",
                                                                                                        "listings_with_brand": 1}):
            with self.subTest(counts=bad), self.assertRaisesRegex(SafetyError, "LIVE_REPORT_SCORE_INVALID"):
                pages._crowd_tile(self.crowd_view(bad), EN)
        empty = pages._crowd_tile(self.crowd_view(None), EN)
        self.assertIn("Amazon 0 · Flipkart 0", empty)
        self.assertIn("not shown", empty)

    def test_platform_prices_keep_their_paise(self):
        tile = pages._platform_tile({"platform": {"rows": [{"route": "deodap", "state": "listed", "roles": [], "price": "1299.50"}],
                                                  "checked": None, "stale": False}}, EN)
        self.assertIn("₹1,299.50", tile)


class SubscriptionColumnTests(unittest.TestCase):
    """Minor finding: a charged subscription is the caution state, none stated is the good state."""

    def test_fee_symbols_read_the_other_way_round(self):
        charged, free = pages._term_cell("yes", EN, "fees"), pages._term_cell("no", EN, "fees")
        self.assertIn(charts.glyph_state("caution", 3.2), charged)
        self.assertIn(">Yes<", charged)
        self.assertIn(charts.glyph_state("yes", 3.2), free)
        self.assertIn(">No<", free)
        # Every other term keeps yes = tick, no = crossed ring.
        self.assertIn(charts.glyph_state("yes", 3.2), pages._term_cell("yes", EN, "cod"))
        self.assertIn(charts.glyph_state("no", 3.2), pages._term_cell("no", EN, "cod"))
        self.assertIn(charts.glyph_state("Unclear", 3.2), pages._term_cell("optional", EN, "fees"))


def scoped_supplier():
    company = {"scope_label": "Direct dispatch", "observed_at": "2026-10-05T10:00:00+00:00", "current": True, "fees": [],
               "source_pages": [{"url": "https://supplier.example.org/about"}], "application_id": "app-company",
               "terms": [{"field": "subscription_requirement", "value": "Required", "basis": "observed", "qualified": True,
                          "current": True, "observed_at": "2026-10-05T10:00:00+00:00"},
                         {"field": "cod", "value": "Not stated", "basis": "not_stated", "qualified": False, "current": False,
                          "observed_at": None},
                         {"field": "neutral_packaging", "value": "Yes", "basis": "conflicting", "qualified": True, "current": True,
                          "observed_at": "2026-10-05T10:00:00+00:00"}]}
    inherited = dict(company["terms"][0], application_id="app-company")
    offer = {"scope_label": "Kit, pack of 4", "observed_at": "2026-10-05T10:00:00+00:00", "current": True, "application_id": "app-offer",
             "source_pages": [{"url": "https://supplier.example.org/offer"}],
             "terms": [{"field": "purchase_moq", "value": "20 packs", "basis": "observed", "qualified": True, "current": True,
                        "observed_at": "2026-10-05T10:00:00+00:00"},
                       {"field": "direct_customer_dispatch", "value": "Yes", "basis": "inferred", "qualified": False, "current": False,
                        "observed_at": "2026-09-01T10:00:00+00:00"}],
             "effective_terms": [inherited, {"field": "purchase_moq", "value": "20 packs", "application_id": "app-offer"}],
             "prices": [{"currency": "INR", "amount": "1340.5", "unit": "pack of 4", "quantity_condition": "minimum 20 packs",
                         "qualifier": "", "qualified": True, "current": True, "observed_at": "2026-10-05T10:00:00+00:00"}],
             "fees": [{"currency": "USD", "amount": "5", "unit": "month", "qualified": False, "current": True,
                       "observed_at": "2026-10-05T10:00:00+00:00"}]}
    from product_radar.supplier_scoped_display import CONTRACT
    return {"scoped_supplier_contract": CONTRACT, "company_workflows": [company], "product_offers": [offer]}


class ScopedSupplierTests(unittest.TestCase):
    """Finding 7 and the scoped-paragraph minor findings: dates once, ₹, no ': ·', Hinglish caveat."""

    def test_groups_keep_every_fact_and_state_each_date_once(self):
        from product_radar.supplier_scoped_display import scoped_supplier_groups, scoped_supplier_lines
        supplier = scoped_supplier()
        view = scoped_supplier_groups(supplier, "en", day=lambda value: "5 Oct 2026" if value.startswith("2026-10-05") else "1 Sep 2026",
                                      money=report_view._report_money)
        company, offer = view["groups"]
        self.assertEqual((company["head"], company["label"], company["date"]), ("Company information", "Direct dispatch", "Source date 5 Oct 2026"))
        # A not-stated term is not printed; a conflicting one keeps its flag.
        self.assertEqual(company["items"], ["Subscription: Required", "Plain packaging: Yes (statements conflict)"])
        self.assertEqual(company["pages"], ["https://supplier.example.org/about"])
        self.assertEqual(offer["items"][0], "Minimum purchase: 20 packs")
        # A term from another date (and estimated, unconfirmed, older) keeps its own date and flags.
        self.assertEqual(offer["items"][1], "Dispatch to the customer: Yes (estimated); not confirmed for this offer; "
                                            "Source date 1 Sep 2026; older information; check again")
        self.assertEqual(offer["items"][2], "Offer price: ₹1,340.50; pack of 4; minimum 20 packs")
        self.assertEqual(offer["items"][3], "Fee: USD 5; month; needs confirmation")
        self.assertEqual(offer["inherited"], ["Subscription: Required"])
        self.assertEqual(view["notes"], [])
        # The workbook's lines are unchanged: ISO dates, "INR 1340.5", one line per term.
        lines = scoped_supplier_lines(supplier, "en")
        self.assertIn("Offer price: INR 1340.5; pack of 4; minimum 20 packs; Source date 2026-10-05", lines)
        self.assertIn("Applicable company terms:", lines)
        hindi = scoped_supplier_groups(supplier, "hi-Latn")
        self.assertEqual(hindi["groups"][0]["head"], "Company ki jankari")
        self.assertEqual(hindi["groups"][1]["inherited_head"], "Is offer par lagu company ki shartein")
        no_offers = dict(supplier, product_offers=[])
        self.assertEqual(scoped_supplier_groups(no_offers, "en")["notes"],
                         ["A supplier lead is not a confirmed offer for this product type."])
        offer_without_price = dict(supplier, product_offers=[dict(supplier["product_offers"][0], prices=[], fees=[])])
        self.assertIn("Offer price not stated.", scoped_supplier_groups(offer_without_price, "en")["groups"][1]["items"])
        with self.assertRaisesRegex(SafetyError, "LIVE_SCOPED_SUPPLIER_DISPLAY_INVALID"):
            scoped_supplier_groups({}, "en")
        with self.assertRaisesRegex(SafetyError, "LIVE_SCOPED_SUPPLIER_FIELD_INVALID"):
            scoped_supplier_groups(dict(supplier, company_workflows=[dict(supplier["company_workflows"][0], terms=[
                {"field": "unknown_field", "value": "x", "basis": "observed"}])]), "en")


def product_with(suppliers, limitation):
    from product_radar.live_outputs import SUPPLIER_TERMS_DISPLAY_VERSION
    from product_radar.report_source_facts import CONTRACTS
    return {"suppliers": suppliers, "supplier_limitation": limitation, "source_route_contract": sorted(CONTRACTS)[0],
            "supplier_terms_display_version": SUPPLIER_TERMS_DISPLAY_VERSION, "supplier_search_status": "complete"}


class SupplierCaveatTests(unittest.TestCase):
    """Finding 3: a lead's own caveat is hidden only when the same note is printed under the leads."""

    def legacy(self):
        from product_radar.report_selection import LEGACY_LEAD_LIMITATION
        return {"name": "Old lead", "url": None, "reason": LEGACY_LEAD_LIMITATION, "type_scope_limitation": LEGACY_LEAD_LIMITATION,
                "reviewed_at": "2026-10-01T10:00:00+00:00", "single_piece_dispatch": "not_stated", "subscription": "not_stated",
                "min_order": None, "terms": []}

    def test_legacy_caveat_stays_beside_scoped_leads(self):
        from product_radar.report_selection import LEGACY_LEADS_LIMITATION, LEGACY_LEAD_LIMITATION
        scoped = dict(scoped_supplier(), name="Scoped lead", url=None, reason="Company information and each offer's own terms are shown separately.")
        mixed = report_view._suppliers(product_with([scoped, self.legacy()], scoped["reason"]), "en")
        legacy_row = mixed["rows"][1]
        self.assertIn(LEGACY_LEAD_LIMITATION, legacy_row["details"])
        self.assertNotIn(scoped["reason"], mixed["rows"][0]["details"])  # its sentence is the printed note
        self.assertEqual(mixed["notes"], [scoped["reason"]])
        only_legacy = report_view._suppliers(product_with([self.legacy()], LEGACY_LEADS_LIMITATION), "en")
        self.assertNotIn(LEGACY_LEAD_LIMITATION, only_legacy["rows"][0]["details"])  # the plural note says it once
        self.assertEqual(only_legacy["notes"], [LEGACY_LEADS_LIMITATION])
        hindi = report_view._suppliers(product_with([scoped, self.legacy()], scoped["reason"]), "hi-Latn")
        self.assertEqual(hindi["notes"], ["Company ki jankari aur har offer ki apni shartein alag dikhayi gayi hain."])
        self.assertIn("Is purane supplier lead ka is product type se mel abhi baaki hai.", hindi["rows"][1]["details"])
        self.assertEqual(hindi["rows"][0]["scoped"]["groups"][1]["items"][2], "Offer ki keemat: ₹1,340.50; pack of 4; minimum 20 packs")
        self.assertIn("Source ki tareekh 5 Oct 2026", hindi["rows"][0]["scoped"]["groups"][0]["date"])


class SourceListTests(unittest.TestCase):
    """Finding 8: every site used is listed, linked or not, with client wording for raw labels."""

    def test_sources_include_unlinked_sites_and_the_market_checks(self):
        product = {"source_refs": [
            {"source": "meta", "url": "https://www.facebook.com/ads/library/?id=1", "observed_at": "2026-10-05T10:00:00+00:00"},
            {"source": "google-trends", "url": None, "observed_at": "2026-10-05T10:00:00+00:00"},
            {"source": "amazon", "url": None, "observed_at": "2026-10-05T10:00:00+00:00"},
            {"source": "amazon", "url": None, "observed_at": "2026-10-05T11:00:00+00:00"},
            {"source": "Supplier page (scoped review)", "url": "https://supplier.example.org/a", "observed_at": "2026-10-05T10:00:00+00:00"}]}
        view = {"platform": {"checked": date(2026, 10, 5), "rows": [{"route": "deodap", "state": "listed"},
                                                                   {"route": "baapstore", "state": "not_checked"},
                                                                   {"route": "dropdash", "state": "not_found"}]},
                "qc": {"rows": {"blinkit": ["in_stock"]}, "app_dates": {"blinkit": date(2026, 10, 4), "zepto": date(2026, 10, 3)}}}
        sources = report_view._sources(product, view, "en")
        self.assertEqual(sources, [("Facebook Ads Library", "https://www.facebook.com/ads/library/?id=1", "5 Oct"),
                                   ("Google Trends India", None, "5 Oct"), ("Amazon India", None, "5 Oct"),
                                   ("Supplier pages", "https://supplier.example.org/a", "5 Oct"), ("DeoDap", None, "5 Oct"),
                                   ("Dropdash", None, "5 Oct"), ("Blinkit", None, "4 Oct"), ("Zepto", None, "3 Oct")])
        self.assertEqual(report_view._sources(product, {}, "hi-Latn")[3][0], "Supplier ke pages")
        self.assertEqual(report_view.site_name("indiamart", "en"), "IndiaMART")


class GuardTests(unittest.TestCase):
    """Minor finding: the HTML guard reads tags, attributes and style sheets, not raw text."""

    def test_markup_that_could_run_or_fetch_is_refused(self):
        for markup in ('<img srcset="https://example.org/a.png 2x">', '<link rel="stylesheet" href="https://example.org/a.css">',
                       '<style>@import "https://example.org/a.css";</style>', '<style>p{background:url(https://example.org/a.png)}</style>',
                       '<p style="background:url(//example.org/a.png)">x</p>', '<svg><image href="https://example.org/a.png"/></svg>',
                       '<svg><use xlink:href="https://example.org/a.svg#x"></use></svg>', '<body onload="x()">', '<a href="javascript:x()">x</a>',
                       '<img src="https://example.org/a.png">', '<meta http-equiv="refresh" content="0;url=https://example.org">',
                       '<object data="x"></object>', '<video poster="https://example.org/p.png"></video>',
                       '<style>p{background:image-set("https://example.org/a.png" 1x)}</style>', '<style>p{b:\\75rl(x)}</style>',
                       '<img src="&#104;ttps://example.org/a.png">'):
            with self.subTest(markup=markup):
                self.assertTrue(fetches_or_runs("<html><body>" + markup + "</body></html>"))

    def test_escaped_text_and_embedded_data_are_not_refused(self):
        page = ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                '<style>@font-face{src:url(data:font/woff2;base64,AAAA) format("woff2")}@page{@bottom-left{content:"url(x) @import"}}'
                '/* url(https://example.org) */</style></head><body><p>See url(https://example.org) and href=&quot;x&quot;</p>'
                '<img src="data:image/png;base64,AAAA" alt="x"><a href="https://example.org/page">page</a><a href="#product-1">1</a>'
                '<svg xmlns="http://www.w3.org/2000/svg"><use href="#icon"/></svg></body></html>')
        self.assertFalse(fetches_or_runs(page))
        self.assertFalse(css_fetches('a{content:"\\"url(x)"}'))
        self.assertTrue(css_fetches("a{b:url( 'https://x' )}"))


class WritingBudgetTests(unittest.TestCase):
    """Minor findings: the budgets count {{title}} as the product name; the prompt version was bumped
    with the old text kept for saved jobs."""

    def item(self, title="Rechargeable LED motion sensor cabinet light"):
        return {"id": "c1", "kind": "product", "issue_id": "issue", "language": "en",
                "facts": [{"id": "title", "text": title}, {"id": "market-label", "text": "x"}],
                "required_limitation": "", "update_fact": ""}

    def value(self, reason):
        from product_radar.research_ai import ACTIVE_CRITERION_IDS
        return {"id": "c1", "issue_id": "issue", "language": "en", "title": "", "reason": reason, "points": [],
                "limitation": "", "update_sentence": "", "references": ["market-label"],
                "checklist_phrases": [{"id": number, "text": "Supplied judgment."} for number in ACTIVE_CRITERION_IDS]}

    def test_the_title_counts_as_the_name_it_prints(self):
        from product_radar.report_narration import budget_length, validate_narration_result
        from product_radar.research_ai import FUNCTIONAL_NARRATION_PROMPT_VERSION, LEGACY_FUNCTIONAL_NARRATION_PROMPT_VERSION
        title = self.item()["facts"][0]["text"]
        self.assertEqual(budget_length("{{title}} works.", title), len(title) + 7)
        fits = "{{title}} " + "a" * (320 - len(title) - 1)
        validate_narration_result(self.item(), self.value(fits), declared_version=FUNCTIONAL_NARRATION_PROMPT_VERSION)
        with self.assertRaisesRegex(SafetyError, "AI_REPORT_TEXT_TOO_LONG_reason"):
            validate_narration_result(self.item(), self.value(fits + "a"), declared_version=FUNCTIONAL_NARRATION_PROMPT_VERSION)
        # Genuine saved v4 writing keeps the limits it was written under (450, counted as written).
        validate_narration_result(self.item(), self.value(fits + "a" * 100), declared_version=LEGACY_FUNCTIONAL_NARRATION_PROMPT_VERSION)

    def test_prompt_versions_name_the_exact_instructions(self):
        from product_radar.report_examples import RECOMMENDATION_CONTRACT
        from product_radar.report_narration import WRITING_LIMITS_TEXT
        from product_radar.research_ai import (ACTIVE_CRITERION_IDS, FIT_NARRATION_PROMPT_VERSION, FIT_POLICY_ID,
                                               FUNCTIONAL_NARRATION_PROMPT_VERSION, g_contract)
        functional = {"recommendation_contract": RECOMMENDATION_CONTRACT, "writing_contract": "writing-input-v1",
                      "fit_policy_id": FIT_POLICY_ID, "criterion_ids": list(ACTIVE_CRITERION_IDS)}
        current, old = g_contract(functional), g_contract(functional, "research-g-v4")
        self.assertEqual((current["version"], old["version"]), (FUNCTIONAL_NARRATION_PROMPT_VERSION, "research-g-v4"))
        self.assertEqual(FUNCTIONAL_NARRATION_PROMPT_VERSION, "research-g-v6")
        self.assertTrue(current["instructions"].endswith(WRITING_LIMITS_TEXT))
        self.assertEqual(current["instructions"], old["instructions"] + WRITING_LIMITS_TEXT)
        self.assertTrue(old["instructions"].endswith("No browsing, collection, tools or new assessment."))
        fit = {"fit_policy_id": FIT_POLICY_ID, "criterion_ids": list(ACTIVE_CRITERION_IDS)}
        self.assertEqual(g_contract(fit)["version"], FIT_NARRATION_PROMPT_VERSION)
        self.assertEqual(g_contract(fit)["instructions"], g_contract(fit, "research-g-v3")["instructions"] + WRITING_LIMITS_TEXT)
        for wrong in ("research-g-v5", "research-g-v2"):
            with self.assertRaisesRegex(SafetyError, "AI_G_CONTRACT_INVALID"):
                g_contract(functional, wrong)
        with self.assertRaisesRegex(SafetyError, "AI_G_CONTRACT_INVALID"):
            g_contract(fit, "research-g-v6")
        self.assertIn("counts as the full product name", WRITING_LIMITS_TEXT)


class WorkbookMaximaTests(unittest.TestCase):
    def test_data_bar_maxima_follow_the_score_settings(self):
        from product_radar.opportunity_score import load_settings
        from product_radar.report_workbook_style import score_maxima
        self.assertEqual(score_maxima(), {"Score": 100, "Want /20": 20, "Early /30": 30, "Room /25": 25, "Product /25": 25})
        settings = deepcopy(load_settings())
        settings["parts"].update(want=25, early=25)
        self.assertEqual(score_maxima(settings), {"Score": 100, "Want /25": 25, "Early /25": 25, "Room /25": 25, "Product /25": 25})


class SubPointTests(unittest.TestCase):
    def test_each_sub_point_label_stays_with_its_number(self):
        score = {"items": {"want": {"facebook_ads": 6, "google_searches": 5}}, "level": "low"}
        html = pages._part_items({"score": score, "fit": {"states": ["Meets", "Unclear"]}}, "want", EN)
        self.assertEqual(html, '<span class="nw">Facebook ads&nbsp;<b>6</b></span>&nbsp;· <span class="nw">Google searches&nbsp;<b>5</b></span>')
        self.assertIn('<span class="nw">Unclear&nbsp;<b>1</b></span>',
                      pages._part_items({"score": score, "fit": {"states": ["Meets", "Unclear"]}}, "product", EN))
        self.assertTrue(pages._part_items({"score": score, "fit": {"states": []}}, "room", EN).startswith('<span class="nw">'))


if __name__ == "__main__":
    unittest.main()
