"""Unit proofs for the redesigned report's drawings, theme, wording and display facts.

Synthetic inputs only; no packets are written and no browser is started.
"""

from datetime import date, timedelta
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from product_radar import report_charts as charts
from product_radar import report_copy as copy_text
from product_radar import report_theme as theme
from product_radar import report_visuals as visuals
from product_radar.models import SafetyError

MAXIMA = {"want": 20, "early": 30, "room": 25, "product": 25}
NAVY = theme.COLOUR["navy"]


def navy_strokes(markup):
    return markup.count(f'stroke="{NAVY}"')


class ChartTests(unittest.TestCase):
    def test_indian_digit_grouping(self):
        for value, expected in ((0, "0"), (999, "999"), (1000, "1,000"), (38412, "38,412"), (1240000, "12,40,000"),
                                (123456789, "12,34,56,789")):
            self.assertEqual(charts.fmt_in(value), expected)
        for bad in (-1, 1.5, "12", True):
            with self.assertRaises(ValueError):
                charts.fmt_in(bad)

    def test_drawing_text_is_never_below_eight_points(self):
        self.assertIn('font-size="2.82"', charts.text(0, 0, "x", 8))
        with self.assertRaises(ValueError):
            charts.text(0, 0, "x", 7.5)
        self.assertIn("&lt;b&gt;", charts.text(0, 0, "<b>", 8))

    def test_label_glyphs_and_state_symbols_have_distinct_shapes(self):
        shapes = {label: charts.glyph_label(label) for label in charts.LABELS}
        self.assertEqual(len(set(shapes.values())), 3)
        self.assertIn("<circle", shapes["sweet_spot"])
        self.assertIn("<path", shapes["early_bet"])
        self.assertEqual(shapes["proven_but_crowded"].count("<rect"), 3)
        bodies = {state: charts.glyph_state(state) for state in ("yes", "no", "Unclear", "not_stated", "conflicting", "caution")}
        self.assertEqual(len(set(bodies.values())), 6)
        self.assertIn(theme.COLOUR["maroon"], bodies["caution"])
        self.assertEqual(charts.glyph_state("Meets"), charts.glyph_state("listed"))
        self.assertEqual(charts.glyph_state("not_checked"), charts.glyph_state("unknown"))
        self.assertIn("stroke-dasharray", bodies["not_stated"])
        with self.assertRaises(KeyError):
            charts.glyph_state("maybe")

    def test_icons_fall_back_to_the_photo_outline(self):
        self.assertIn(charts.ICONS["Baby Products"], charts.icon("Baby Products"))
        self.assertIn(charts.ICONS["photo"], charts.icon("Unknown category"))

    def test_score_ring_compartments_follow_each_part_maximum(self):
        spans = charts.ring_spans(MAXIMA)
        self.assertEqual([round(spans[part], 1) for part in charts.PARTS], [68.8, 103.2, 86.0, 86.0])
        empty = charts.score_ring(dict.fromkeys(charts.PARTS, 0), MAXIMA, 0)
        self.assertEqual(navy_strokes(empty), 0)
        self.assertEqual(empty.count('stroke="' + theme.COLOUR["track"] + '"'), 4)
        full = charts.score_ring(dict(MAXIMA), MAXIMA, 100, label="Score 100")
        self.assertEqual(navy_strokes(full), 4)
        self.assertIn('aria-label="Score 100"', full)
        partial = charts.score_ring({"want": 7, "early": 0, "room": 25, "product": 13}, MAXIMA, 45)
        self.assertEqual(navy_strokes(partial), 3)
        self.assertIn(">45</text>", partial)
        for bad in ({"want": 21, "early": 0, "room": 0, "product": 0}, {"want": -1, "early": 0, "room": 0, "product": 0},
                    {"want": 1.5, "early": 0, "room": 0, "product": 0}, {"want": 1, "early": 0, "room": 0}):
            with self.assertRaises(ValueError):
                charts.score_ring(bad, MAXIMA, 1)

    def test_part_meter_and_compartment_bar_fill_by_points(self):
        self.assertEqual(charts.part_meter(0, 20).count("<rect"), 1)
        half = charts.part_meter(10, 20, width=40)
        self.assertIn('width="20.00"', half)
        self.assertIn('width="40.00"', charts.part_meter(20, 20, width=40).split("<rect")[2])
        self.assertIn('width="2.20"', charts.part_meter(1, 100, width=40))  # never thinner than a round cap
        for points, maximum in ((21, 20), (-1, 20), (1, 0)):
            with self.assertRaises(ValueError):
                charts.part_meter(points, maximum)
        bar = charts.compartment_bar({"want": 20, "early": 15, "room": 0, "product": 25}, MAXIMA, width=100)
        self.assertEqual(bar.count(f'fill="{NAVY}"'), 3)
        self.assertIn('width="14.75"', bar)  # half of the 29.5 mm early compartment

    def test_crowding_gauge_needle_and_fills_follow_the_level(self):
        self.assertEqual([charts.needle_angle(level) for level in charts.LEVELS], [210, 270, 330])
        for number, level in enumerate(charts.LEVELS, 1):
            gauge = charts.crowd_gauge(level, low="Kam", high="Zyada")
            self.assertEqual(sum(gauge.count(f'stroke="{theme.CROWD[name]}"') for name in charts.LEVELS), number)
            self.assertIn(">Kam</text>", gauge)
            pips = charts.crowd_pips(level)
            self.assertEqual(pips.count(f'fill="{theme.CROWD_TRACK}"'), 3 - number)
        with self.assertRaises(ValueError):
            charts.crowd_gauge("not_checked")

    def test_fit_pips_and_bars(self):
        pips = charts.fit_pips(["Meets"] * 6 + ["Unclear"] * 2 + ["Does not meet"] * 2)
        self.assertEqual(pips.count(f'fill="{NAVY}"'), 6)
        self.assertEqual(pips.count("<path"), 2)
        with self.assertRaises(ValueError):
            charts.fit_pips([])
        with self.assertRaises(ValueError):
            charts.fit_pips(["Not assessed"])
        stacked = charts.stacked_bar([2, 0, 1], ["#111111", "#222222", "#333333"], width=30)
        self.assertNotIn("#222222", stacked)
        self.assertIn('width="19.50"', stacked)
        for counts in ([0, 0], [1, -1], [1]):
            with self.assertRaises(ValueError):
                charts.stacked_bar(counts, ["#111111", "#222222"])
        self.assertIn('width="9.00"', charts.category_bar(1, 2))

    def test_timeline_places_dates_on_a_twelve_month_axis_and_stacks_close_dots(self):
        end = date(2026, 10, 15)
        layout = charts.timeline_layout([(end, False), (date(2025, 10, 15), False), (end, True)], end, width=76)
        self.assertEqual(layout["begin"], date(2025, 10, 15))
        # Recent dots are placed first, so a stack keeps them at the bottom (always visible).
        self.assertEqual(layout["dots"], [(74.4, 0, True), (1.6, 0, False), (74.4, 1, False)])
        self.assertEqual((layout["earlier"], layout["more"], layout["hidden"]), (0, [], 0))
        self.assertEqual(charts._months_back(date(2024, 2, 29), 12), date(2023, 2, 28))
        self.assertEqual(charts._months_back(date(2026, 1, 31), 2), date(2025, 11, 30))
        timeline = charts.meta_timeline([(date(2026, 3, 1), False), (date(2026, 10, 1), True)], end, label="2 advertisers")
        self.assertEqual(timeline.count(f'r=".9" fill="{theme.COLOUR["maroon"]}"'), 1)
        self.assertEqual(timeline.count(f'r="1.25" fill="{NAVY}"'), 1)
        self.assertEqual(timeline.count(theme.COLOUR["maroon-pale"]), 1)
        for name in ("Jan 2026", ">Apr<", ">Jul<", ">Oct<"):
            self.assertIn(name, timeline)
        self.assertIn(">Jan 2026<", charts.meta_timeline([], end, month_names={1: "Jan"}))
        self.assertNotIn(theme.COLOUR["maroon-pale"], charts.meta_timeline([(end, True)], end, band=False))

    def test_timeline_keeps_its_height_and_never_misdates_old_starts(self):
        end = date(2026, 10, 15)
        # Twenty advertisers on the same week: three rows, the top one a "+n" for the rest.
        crowd = [(end - timedelta(days=n % 5), n < 4) for n in range(20)]
        layout = charts.timeline_layout(crowd, end)
        self.assertEqual(len(layout["dots"]) + layout["hidden"], 20)
        self.assertEqual(layout["more"][0][1], layout["hidden"])
        # Recent starts are placed first, so the dots left visible in a full stack are the ringed ones.
        self.assertEqual([recent for _, _, recent in layout["dots"]], [True] * (charts.TIMELINE_ROWS - 1))
        spread = charts.timeline_layout([(end - timedelta(days=20 * n), False) for n in range(12)], end)
        self.assertEqual((spread["hidden"], max(row for _, row, _ in spread["dots"])), (0, 0))
        drawn = charts.meta_timeline(crowd, end, label="20 advertisers")
        self.assertIn('height="15.00mm"', drawn)
        self.assertIn(f'>+{layout["hidden"]}</text>', drawn)
        # Starts before the 12-month axis are one marker at the left edge, never stacked at a false date.
        old = [(date(2024, 1, 1), False), (date(2025, 1, 1), False), (date(2026, 9, 1), False)]
        layout = charts.timeline_layout(old, end)
        self.assertEqual(layout["earlier"], 2)
        self.assertEqual(len(layout["dots"]), 1)
        self.assertIn(f'fill="{NAVY}"/>', charts.meta_timeline(old, end))
        # A start next to the left edge stacks above the marker instead of hiding it.
        edge = charts.timeline_layout([(date(2024, 1, 1), False), (date(2025, 10, 16), False)], end)
        self.assertEqual(edge["dots"][0][1], 1)
        self.assertIn("<path", charts.earlier_glyph())
        # Two separate full stacks each get their own "+n".
        two = charts.timeline_layout([(end, False)] * 4 + [(date(2026, 3, 1), False)] * 5, end)
        self.assertEqual(sorted(n for _, n in two["more"]), [2, 3])

    def test_sparkline_windows_gaps_and_bounded_points(self):
        points = [{"start": f"2026-{month:02d}-01", "state": "observed", "value": month * 5} for month in range(1, 11)]
        windows = charts.spark_window_indexes(points, [p["start"] for p in points[2:10]])
        self.assertEqual(windows, list(range(2, 10)))
        self.assertIsNone(charts.spark_window_indexes(points, None))
        self.assertIsNone(charts.spark_window_indexes(points, ["2020-01-01"] * 8))
        spark = charts.trends_spark(points, windows, left="Jan 2026", right="Oct 2026", label="Trends")
        self.assertIn(theme.COLOUR["paper-deep"], spark)
        self.assertIn(theme.COLOUR["maroon-pale"], spark)
        # The recent window is hatched, so the two windows differ in greyscale too (the key uses the same swatches).
        self.assertEqual(spark.count('stroke-opacity=".45"'), 1)
        self.assertIn('stroke-opacity=".45"', charts.window_swatch("recent"))
        self.assertNotIn('stroke-opacity', charts.window_swatch("previous"))
        self.assertEqual(charts._hatch(0, 1, 0, 0.0001), "")
        self.assertEqual(spark.count('fill-opacity=".10"'), 1)
        gapped = [dict(p) for p in points]
        gapped[4] = {"start": gapped[4]["start"], "state": "missing", "value": None}
        gapped[6] = {"start": gapped[6]["start"], "state": "bounded_nonzero", "value": None}
        broken = charts.trends_spark(gapped, None)
        self.assertEqual(broken.count('fill-opacity=".10"'), 2)
        self.assertIn(" 12.94", broken)  # the "<1" week is drawn at 0.5, never at an invented value
        lone = [{"start": "a", "state": "observed", "value": 10}, {"start": "b", "state": "missing", "value": None},
                {"start": "c", "state": "observed", "value": 20}]
        self.assertEqual(charts.trends_spark(lone, None).count('r=".45"'), 2)
        with self.assertRaises(ValueError):
            charts.trends_spark(points[:1], None)
        self.assertIn("Chart not saved", charts.empty_spark(message="Chart not saved"))
        self.assertNotIn("<text", charts.empty_spark())


class ThemeTests(unittest.TestCase):
    def test_fonts_are_embedded_as_woff2_data_with_their_licences(self):
        css = theme.font_css()
        self.assertEqual(css.count("@font-face"), 9)
        self.assertEqual(css.count("data:font/woff2;base64,d09GMg"), 9)
        for name in theme.FONT_LICENCES:
            self.assertTrue((theme.FONT_DIR / name).is_file())

    def test_a_damaged_or_missing_font_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            for _, _, _, name in theme.FONT_FACES:
                (folder / (name + ".woff2")).write_bytes(b"not a font")
            theme.font_css.cache_clear()
            try:
                with patch.object(theme, "FONT_DIR", folder), self.assertRaisesRegex(SafetyError, "REPORT_FONT_INVALID"):
                    theme.font_css()
                for _, _, _, name in theme.FONT_FACES:
                    (folder / (name + ".woff2")).write_bytes((theme.FONT_DIR / (name + ".woff2")).read_bytes())
                theme.font_css.cache_clear()
                with patch.object(theme, "FONT_DIR", folder), self.assertRaisesRegex(SafetyError, "REPORT_FONT_LICENCE_MISSING"):
                    theme.font_css()
            finally:
                theme.font_css.cache_clear()

    def test_margin_box_text_cannot_escape_its_css_string(self):
        self.assertEqual(theme.css_string('a"b\\c</style>'), '"a\\"b\\\\c\\3C /style>"')
        self.assertEqual(theme.css_string("line\nbreak"), '"linebreak"')
        with self.assertRaises(SafetyError):
            theme.css_string(None)

    def test_stylesheet_page_boxes_marker_and_type_scale(self):
        plain = theme.stylesheet(footer="NeuraFlow · Brief · 7 October 2026", page_word="Page", of_word="of")
        self.assertIn('@bottom-left{content:"NeuraFlow · Brief · 7 October 2026"', plain)
        self.assertIn('content:"Page " counter(page) " of " counter(pages)', plain)
        self.assertNotIn("@top-right{content:\"", plain)
        self.assertIn("@page cover{margin:0", plain)
        marked = theme.stylesheet(footer="x", page_word="Page", of_word="/", marker="Illustrative sample")
        self.assertIn('@top-right{content:"ILLUSTRATIVE SAMPLE"', marked)
        self.assertEqual(min(theme.TYPE_SCALE.values()), theme.MIN_PT)
        sizes = [float(value) for value in re.findall(r"font-size:([0-9.]+)pt", plain)]
        self.assertTrue(sizes and min(sizes) >= 8)
        self.assertNotIn("<script", plain.lower())


class CopyTests(unittest.TestCase):
    def keys(self, value, prefix=""):
        if isinstance(value, dict):
            return {key for name, child in value.items() for key in self.keys(child, prefix + "/" + str(name))} | {prefix}
        if isinstance(value, (tuple, list)):
            return {prefix + f"[{len(value)}]"} | {key for n, child in enumerate(value) for key in self.keys(child, prefix + f"[{n}]")}
        return {prefix}

    def test_both_editions_have_the_same_wording_slots(self):
        self.assertEqual(self.keys(copy_text.TEXT["en"]), self.keys(copy_text.TEXT["hi-Latn"]))
        for language, words in copy_text.TEXT.items():
            self.assertEqual(set(words["labels"]), {"too_quiet", "proven_but_crowded", "early_bet", "sweet_spot"})
            flat = repr(words)
            self.assertIsNone(re.search(r"[ऀ-ॿ]", flat), language)

    def test_dates(self):
        self.assertEqual(copy_text.long_date("2026-10-07"), "7 October 2026")
        self.assertEqual(copy_text.short_date("2026-10-14T10:00:00+00:00"), "14 Oct")
        self.assertEqual(copy_text.month_year("2025-09-28"), "Sep 2025")
        self.assertIsNone(copy_text.date_window([]))
        self.assertEqual(copy_text.date_window(["2026-10-05"]), "5 October 2026")
        self.assertEqual(copy_text.date_window(["2026-10-12", "2026-10-19"]), "12–19 October 2026")
        self.assertEqual(copy_text.date_window(["2026-09-28", "2026-10-19"]), "28 September – 19 October 2026")
        self.assertEqual(copy_text.date_window(["2025-12-30", "2026-01-02"]), "30 December 2025 – 2 January 2026")


def basis(*, level="low", label="sweet_spot", reason="opportunity_scored", stock=None, listed=None, rising=False,
          usable=True, rising_points=0):
    room = {"low": 25, "medium": 12, "high": 0, "not_checked": 0}[level]
    parts = {"want": {"points": 7, "max": 20, "items": {
                 "facebook_ads": {"points": 2, "facts": {"relevant_ads": 1, "positive": False, "advertisers": 1}},
                 "google_searches": {"points": 0, "facts": {"usable": usable, "nonzero_points": 4 if usable else None}},
                 "platform_lists": {"points": 5, "facts": {"on_list": {}, "listed": listed or {}}},
                 "quick_commerce": {"points": 0, "facts": {"stock_cities": stock or {}}}}},
             "early": {"points": rising_points, "max": 30, "items": {
                 "rising_searches": {"points": rising_points, "facts": {"rising": rising, "previous_mean_upper": None,
                                                                         "recent_mean_lower": None, "breakout_queries": 0}},
                 "new_advertisers": {"points": 0, "facts": {"recent_start_advertisers": None}},
                 "new_arrivals": {"points": 0, "facts": {"new_arrival": {}}}}},
             "room": {"points": room, "max": 25, "items": {"crowding": {"points": room, "facts": {
                 "level": level, "counts": {"relevant_listings": {"amazon": 1, "flipkart": 0}}}}}},
             "product": {"points": 13, "max": 25, "items": {"checklist": {"points": 13, "facts": {"meets": 6}}}}}
    return {"opportunity_policy_id": "opportunity-score-v3", "opportunity_label": label,
            "opportunity_score": sum(part["points"] for part in parts.values()),
            "opportunity_basis": {"settings_version": "opportunity-score-settings-v1", "parts": parts,
                                  "eligible": reason == "opportunity_scored", "reason": reason}}


class VisualFactTests(unittest.TestCase):
    def test_score_view_reads_the_code_owned_parts_and_fails_closed_on_unreportable_records(self):
        self.assertIsNone(visuals.score_view({"title": "Older packet"}))
        view = visuals.score_view(basis())
        self.assertEqual(view["score"], 45)
        self.assertEqual(view["points"], {"want": 7, "early": 0, "room": 25, "product": 13})
        self.assertEqual(view["items"]["want"]["platform_lists"], 5)
        bad = [basis(level="not_checked"), basis(reason="demand_too_quiet"), basis(label="too_quiet"),
               dict(basis(), opportunity_score=99), dict(basis(), opportunity_policy_id="opportunity-score-v1"),
               {"opportunity_policy_id": "opportunity-score-v3"}]
        for product in bad:
            with self.assertRaisesRegex(SafetyError, "LIVE_REPORT_SCORE_INVALID"):
                visuals.score_view(product)

    def test_without_frozen_originals_the_tiles_use_only_the_score_facts(self):
        score = visuals.score_view(basis(stock={"Mumbai": ["obs-1"]}, listed={"deodap": ["item"]}))
        meta = visuals.meta_view(score, None)
        self.assertEqual((meta["advertisers"], meta["relevant"], meta["starts"]), (1, 1, None))
        self.assertEqual(visuals.trends_view(score, None)["state"], "steady")
        self.assertEqual(visuals.trends_view(visuals.score_view(basis(usable=False)), None)["state"], "none")
        with patch.object(visuals, "_settings", return_value={"early": {"rising_searches": {"strong": 12}}}):
            self.assertEqual(visuals.trends_view(visuals.score_view(basis(rising=True, rising_points=8)), None)["state"], "rising")
            self.assertEqual(visuals.trends_view(visuals.score_view(basis(rising=True, rising_points=12)), None)["state"],
                             "rising_strongly")
        platform = visuals.platform_view(score, None)
        self.assertEqual([row["state"] for row in platform["rows"]], ["listed", "unknown", "unknown"])
        qc = visuals.qc_view(score, None, None)
        self.assertEqual((qc["rows"], qc["in_stock"]), (None, ["Mumbai"]))

    def test_mean_text_rounds_to_whole_points(self):
        self.assertEqual(visuals._mean_text(63.25, 63.25), "63")
        self.assertEqual(visuals._mean_text(34.5, 34.5), "35")  # half up, never banker's rounding
        self.assertEqual(visuals._mean_text(20, 20.5), "20–21")
        self.assertEqual(visuals._mean_text(20.25, 20.4), "20")

    # (category, title, expected classes): the reviewed misses and false flags, plus the earlier cases.
    COMPLIANCE_CASES = (
        ("Travel and Outdoor", "Rechargeable neck fan", ["electrical"]),
        ("Home and Decor", "Cool mist humidifier", ["electrical"]),
        ("Home and Kitchen", "Portable induction cooktop", ["electrical"]),
        ("Travel and Outdoor", "Foldable hair dryer", ["electrical"]),
        ("Travel and Outdoor", "Collapsible silicone travel kettle", ["electrical"]),
        ("Home and Decor", "Wooden aroma diffuser", ["electrical"]),
        ("Home and Kitchen", "Digital kitchen scale", ["electrical"]),
        ("Home and Decor", "Natural mosquito repellent patches", ["topical"]),
        ("Travel and Outdoor", "Pocket hand sanitizer spray", ["topical"]),
        ("Travel and Outdoor", "Knee support sleeve", ["medical"]),
        ("Home and Decor", "Glass tea light holder", []),
        ("Home and Decor", "Light blue storage basket", []),
        ("Home and Kitchen", "Silicone dish scrub", []),
        ("Travel and Outdoor", "Leak-proof travel bottles for shampoo", []),
        ("Home and Kitchen", "Airtight snack jar", []),
        ("Home and Kitchen", "Glass candy jar with lid", []),
        ("Home and Kitchen", "Silicone chocolate mould", []),
        ("Home and Kitchen", "Stovetop kettle with child lock", []),
        ("Home and Kitchen", "Electric kettle with child lock", ["electrical"]),
        ("Home and Decor", "Toy organiser for children", ["child"]),
        ("Home and Kitchen", "Kids lunch box", ["child"]),
        ("Travel and Outdoor", "Travel lotion bottle set", []),
        ("Home and Kitchen", "Soap dispenser pump", []),
        ("Home and Kitchen", "Clip-on sink strainer basket", []),
        ("Home and Kitchen", "Magnetic spice jar rack", []),
        ("Home and Decor", "Rechargeable LED picture light", ["electrical"]),
        ("Baby Products", "Stroller organiser bag", ["baby"]),
        ("Home and Kitchen", "Herbal tea sampler", ["ingestible"]),
        ("Travel and Outdoor", "Knee brace support", ["medical"]),
        ("Home and Decor", "Face wash and body scrub kit", ["topical"]),
    )

    def test_compliance_classes_by_fixed_rule(self):
        for category, title, expected in self.COMPLIANCE_CASES:
            with self.subTest(title=title):
                self.assertEqual(visuals.compliance_classes({"category": category, "title": title}), expected)
        trimmer = {"category": "Baby Products", "title": "Baby nail trimmer", "functional_definition": {
            "report_title": "Electric baby nail trimmer", "primary_use": "trims nails", "mechanism": ["battery motor"]}}
        self.assertEqual(visuals.compliance_classes(trimmer), ["baby", "electrical"])
        # The accepted type wording counts too, and a Baby Products type is stated plainly, never "May count as".
        worded = {"category": "Home and Decor", "title": "Wall planter", "functional_definition": {"use_context": ["Nursery for toddlers"]}}
        self.assertEqual(visuals.compliance_classes(worded), ["child"])
        english = visuals.compliance_text(["baby", "electrical"], "en")
        self.assertTrue(english.startswith("Baby or child product"))
        self.assertTrue(english.endswith("No legal approval is given."))
        self.assertTrue(visuals.compliance_text(["child"], "en").startswith("May count as a baby or child product"))
        self.assertIn("legal manzoori", visuals.compliance_text(["medical"], "hi-Latn"))
        self.assertIn("mana ja sakta hai", visuals.compliance_text(["child"], "hi-Latn"))
        self.assertEqual(visuals.COMPLIANCE_RULE, "compliance-classes-v2")

    def checks(self, **changes):
        value = {"version": visuals.QC_CHECKS_VERSION, "as_of": "2026-10-05T10:00:00+00:00",
                 "places": ["Mumbai", "Delhi NCR", "Bengaluru"],
                 "answered": {"blinkit": {"Mumbai": "2026-10-04T10:00:00+00:00"},
                              "instamart": {"Delhi NCR": "2026-10-05T10:00:00+00:00", "Mumbai": "2026-09-29T10:00:00+00:00"}}}
        value.update(changes)
        return value

    def test_quick_commerce_check_record_is_strictly_shaped(self):
        self.assertEqual(visuals.validate_quick_commerce_checks(self.checks())["places"][0], "Mumbai")
        bad = [self.checks(version="v0"), self.checks(places=[]), self.checks(places=["Mumbai", "Mumbai"]),
               self.checks(places=["", "Mumbai"]), self.checks(answered={"bigbasket": {"Mumbai": "2026-10-04T10:00:00+00:00"}}),
               self.checks(answered={"zepto": {}, }), self.checks(answered={"zepto": {"Pune": "2026-10-04T10:00:00+00:00"}}),
               self.checks(answered={"zepto": {"Mumbai": "2026-09-27T10:00:00+00:00"}}),
               self.checks(answered={"zepto": {"Mumbai": "2026-10-06T10:00:00+00:00"}}),
               self.checks(answered={"zepto": {"Mumbai": "2026-10-04T10:00:00+00:00"}, "blinkit": {"Mumbai": "2026-10-04T10:00:00+00:00"}}),
               self.checks(answered={"zepto": {"Mumbai": "2026-10-04T10:00:00+00:00", "Bengaluru": "2026-10-04T10:00:00+00:00"}}),
               dict(self.checks(), extra=True), None]
        for value in bad:
            with self.assertRaisesRegex(SafetyError, "LIVE_QUICK_COMMERCE_CHECKS_INVALID"):
                visuals.validate_quick_commerce_checks(value)

    def test_quick_commerce_checks_read_answered_questions_within_the_score_week(self):
        def task(route, place, receipt, *, target="c1", status="acquired"):
            return {"route": route, "targets": {target: {}}, "status": status, "receipt_refs": [receipt],
                    "request": {"location": {"name": place}}}
        state = {"query_tasks": {"t1": task("blinkit", "Mumbai", "r1"), "t2": task("zepto", "Mumbai", "r2"),
                                 "t3": task("zepto", "Mumbai", "r3"), "t4": task("instamart", "Mumbai", "r4", status="failed"),
                                 "t5": task("blinkit", "Pune", "r1"), "t6": task("zepto", "Delhi NCR", "r5", target="c2"),
                                 "t7": task("amazon", "Mumbai", "r1"), "t8": task("instamart", "Bengaluru", "r6")},
                 "source_checks": {"r1": {"at": "2026-10-04T10:00:00+00:00"}, "r2": {"at": "2026-10-01T10:00:00+00:00"},
                                   "r3": {"at": "2026-10-03T10:00:00+00:00"}, "r4": {"at": "2026-10-04T10:00:00+00:00"},
                                   "r5": {"at": "2026-10-04T10:00:00+00:00"}, "r6": {"at": "2026-09-20T10:00:00+00:00"}}}
        value = visuals.quick_commerce_checks(state, "c1", as_of="2026-10-05T10:00:00+00:00")
        self.assertEqual(value["places"], ["Mumbai", "Delhi NCR", "Bengaluru"])
        self.assertEqual(value["answered"], {"blinkit": {"Mumbai": "2026-10-04T10:00:00+00:00"},
                                             "zepto": {"Mumbai": "2026-10-03T10:00:00+00:00"}})
        visuals.validate_quick_commerce_checks(value)
        self.assertEqual(visuals.quick_commerce_checks({}, "c1", as_of="2026-10-05T10:00:00+00:00")["answered"], {})


if __name__ == "__main__":
    unittest.main()


class PageHelperTests(unittest.TestCase):
    """Page parts fed with small synthetic views (each state the market page can meet)."""
    words = copy_text.TEXT["en"]

    def test_money_hosts_and_fixed_supplier_caveats(self):
        from product_radar import report_pages as pages
        for amount, shown in (("1299", "₹1,299"), ("1299.00", "₹1,299"), ("1299.50", "₹1,299.50"), ("1299.5", "₹1,299.50"),
                              ("149.05", "₹149.05"), ("0.5", "₹0.50"), ("149.125", "₹149.125"), ("124000", "₹1,24,000")):
            self.assertEqual(pages.rupees(amount), shown, amount)
        self.assertEqual(pages.host("https://www.example.org/x"), "example.org")
        self.assertIsNone(pages.host(None))
        self.assertIsNone(pages.host("not a url"))
        self.assertEqual(pages.source_name("meta"), "Facebook Ads Library")
        self.assertEqual(pages.source_name("Fixture source"), "Fixture source")
        reason = "This dated supplier lead still needs matching to this product type."
        self.assertEqual(pages.fixed_reason(reason, "en"), reason)
        self.assertTrue(pages.fixed_reason(reason, "hi-Latn").startswith("Is purane supplier lead"))
        self.assertEqual(pages.fixed_reason("Other words.", "hi-Latn"), "Other words.")
        self.assertEqual(pages.weekday_line("hi-Latn", "2026-10-20"), "Mangalvaar, 20 October 2026")

    def meta(self, **changes):
        view = {"advertisers": 3, "relevant": 4, "active": 2, "recent": 1, "starts": None, "end": None, "undated": 0,
                "observed": date(2026, 10, 15), "unlinked": False, "missing": 0, "band": True}
        view.update(changes)
        return {"meta": view}

    def test_meta_tile_states(self):
        from product_radar import report_pages as pages
        tile = pages._meta_tile(self.meta(), self.words, {})
        self.assertIn("4 relevant ads · 2 still active", tile)
        self.assertIn("1 started within 30 days of being seen", tile)
        self.assertIn("Facebook Ads Library · 15 Oct", tile)
        none = pages._meta_tile(self.meta(advertisers=None, relevant=0, active=None, recent=None, observed=None), self.words, {})
        self.assertIn(">0</span> <span>advertisers", none)
        self.assertIn(self.words["m_none"], none)
        self.assertIn("0 relevant ads", none)
        starts = [(date(2026, 3, 1), False), (date(2026, 10, 1), True)]
        dated = pages._meta_tile(self.meta(starts=starts, end=date(2026, 10, 15), undated=2), self.words, {})
        self.assertIn('class="timeline"', dated)
        self.assertIn("2 relevant ads had no start date", dated)
        self.assertIn("1 relevant ad had no start date", pages._meta_tile(self.meta(starts=starts, end=date(2026, 10, 15),
                                                                                  undated=1), self.words, {}))
        # Advertisers that could not be placed are counted under the drawn timeline (it is drawn, not hidden).
        unlinked = pages._meta_tile(self.meta(starts=starts[:1], end=date(2026, 10, 15), unlinked=True, missing=2), self.words, {})
        self.assertIn("2 advertisers are not on the timeline", unlinked)
        self.assertIn("1 advertiser is not on the timeline", pages._meta_tile(
            self.meta(starts=starts[:1], end=date(2026, 10, 15), unlinked=True, missing=1), self.words, {}))
        one = pages._meta_tile(self.meta(advertisers=1, relevant=1, active=1), self.words, {})
        self.assertIn(">1</span> <span>advertiser<", one)
        self.assertIn("1 relevant ad · 1 still active", one)
        self.assertIn(">1 relevant ad<", pages._meta_tile(self.meta(relevant=1, active=None), self.words, {}))
        self.assertIn("1 relevant ad · 1 abhi active", pages._meta_tile(self.meta(relevant=1, active=1), copy_text.TEXT["hi-Latn"], {}))
        # Old starts: a marker and its note; a full stack: the recent count is stated, since some dots are hidden.
        old = pages._meta_tile(self.meta(starts=[(date(2024, 1, 1), False), (date(2026, 10, 1), True)], end=date(2026, 10, 15)),
                               self.words, {})
        self.assertIn("1 advertiser started before Oct 2025", old)
        full = pages._meta_tile(self.meta(starts=[(date(2026, 10, 10), True)] * 5, end=date(2026, 10, 15), recent=5),
                                self.words, {})
        self.assertIn(">+3</text>", full)
        self.assertIn("5 started within 30 days of being seen", full)
        # No band when the first ads were seen on different days (the recent rule has no single reference date).
        unbanded = pages._meta_tile(self.meta(starts=starts, end=date(2026, 10, 15), band=False), self.words, {})
        self.assertNotIn(theme.COLOUR["maroon-pale"], unbanded)

    def test_trends_tile_states(self):
        from product_radar import report_pages as pages
        points = [{"start": f"2026-0{n}-01", "state": "observed", "value": 10 * n} for n in range(1, 10)]
        drawn = pages._trends_tile({"trends": {"state": "rising_strongly", "points": points, "windows": None,
                                               "previous": "20–22", "recent": "70", "collected": date(2026, 10, 14)}}, self.words)
        self.assertIn("trend-state--up", drawn)
        self.assertIn("Last 4 weeks averaged 70, the 4 before 20–22", drawn)
        self.assertIn("Jan 2026", drawn)
        self.assertIn("Google Trends India · 14 Oct", drawn)
        short = pages._trends_tile({"trends": {"state": "steady", "points": points, "windows": None, "previous": None,
                                               "recent": None, "collected": None}}, self.words)
        self.assertIn(self.words["tr_cap_short"], short)
        self.assertIn("trend-state--flat", short)
        unsaved = pages._trends_tile({"trends": {"state": "rising", "points": None, "windows": None, "previous": None,
                                                 "recent": None, "collected": None}}, self.words)
        self.assertIn(self.words["tr_no_chart"], unsaved)
        none = pages._trends_tile({"trends": {"state": "none", "points": None, "windows": None, "previous": None,
                                              "recent": None, "collected": None}}, self.words)
        self.assertIn(self.words["tr_state"]["none"], none)
        self.assertNotIn(self.words["tr_no_chart"], none)

    def test_platform_and_quick_commerce_tiles(self):
        from product_radar import report_pages as pages
        rows = [{"route": "deodap", "state": "listed", "roles": ["bestseller", "new_arrival"], "price": "149.50"},
                {"route": "baapstore", "state": "delisted", "roles": [], "price": None},
                {"route": "dropdash", "state": "pending", "roles": [], "price": None}]
        tile = pages._platform_tile({"platform": {"rows": rows, "checked": date(2026, 10, 13), "stale": True}}, self.words)
        for text in ("₹149.50", "Bestseller", "New arrival", "No longer listed", "Being checked", "13 Oct", self.words["p_stale"]):
            self.assertIn(text, tile)
        basis = pages._platform_tile({"platform": {"rows": [dict(rows[0], state="unknown")], "checked": None, "stale": False}},
                                     self.words)
        self.assertIn("Not checked", basis)
        matrix = {"places": ["Mumbai", "Delhi NCR"], "rows": {"blinkit": ["in_stock", "not_checked"],
                  "zepto": ["not_in_stock", "not_checked"], "instamart": ["not_checked", "not_checked"]},
                  "in_stock": ["Mumbai"], "checked": ["Mumbai"], "observed": date(2026, 10, 12)}
        qc = pages._qc_tile({"qc": matrix}, self.words)
        self.assertIn("In stock in 1 of 1 city checked", qc)
        two = pages._qc_tile({"qc": dict(matrix, checked=["Mumbai", "Delhi NCR"])}, self.words)
        self.assertIn("In stock in 1 of 2 cities checked", two)
        self.assertEqual(qc.count('<span class="sr">Not checked</span>'), 4)
        self.assertIn("12 Oct", qc)
        unchecked = pages._qc_tile({"qc": dict(matrix, checked=[], in_stock=[], observed=None)}, self.words)
        self.assertIn(self.words["qc_none"], unchecked)
        self.assertIn("In stock in: Mumbai", pages._qc_tile({"qc": {"rows": None, "in_stock": ["Mumbai"]}}, self.words))
        self.assertIn(self.words["qc_basis_none"], pages._qc_tile({"qc": {"rows": None, "in_stock": []}}, self.words))

    def test_examples_and_suppliers_sections(self):
        from product_radar import report_pages as pages
        empty = {"bulk": "Bulk note.", "prices": None,
                 "examples": {"rows": [], "shops": [], "leads": [], "more": 0, "omitted_for_size": True, "retained": 0}}
        self.assertIn(self.words["ex_omitted"], pages._prices_section(empty, self.words))
        self.assertIn("Bulk note.", pages._prices_section(empty, self.words))
        empty["examples"]["omitted_for_size"] = False
        self.assertIn(self.words["ex_none"], pages._prices_section(empty, self.words))
        nothing = pages._prices_section({"bulk": None, "prices": None, "examples": None}, self.words)
        self.assertIn(self.words["price_none"], nothing)
        rows = [("₹110", "Listing", "Shop · seen 8 Oct", None)]
        listed = {"bulk": None, "prices": None, "examples": {"rows": rows, "shops": rows, "leads": rows, "more": 3,
                  "omitted_for_size": False, "retained": 0, "notes": ["Retained note."]}}
        html = pages._prices_section(listed, self.words)
        self.assertIn("3 more example listings are in the workbook.", html)
        self.assertIn("Retained note.", html)
        self.assertIn(self.words["supplier_examples"], html)
        long_figure = pages._price_rows([("displayed quote ₹149–₹189 per pack", "x", "y", None)], self.words)
        self.assertIn('class="price price--text"', long_figure)
        group = {"kind": "offer", "number": 1, "head": "Offer 1", "label": "Pack <4>", "date": "Source date 1 Oct 2026",
                 "items": ["Minimum purchase: 10 packs", "Offer price: ₹95; pack"], "inherited": ["Subscription: Required"],
                 "inherited_head": "Applicable company terms", "pages": ["https://example.org/offer", None]}
        scoped = {"rows": [{"name": "Lead", "url": None, "details": [None], "scoped": {
                      "groups": [group, dict(group, head="Company information", pages=["https://example.org/a", "https://example.org/b"],
                                             inherited=[], items=[])],
                      "notes": ["A supplier lead is not a confirmed offer for this product type."]}}],
                  "notes": [], "empty": "", "columns": pages.TERM_COLUMNS}
        html = pages._suppliers_section({"suppliers": scoped, "rank": 7, "title": "Fixture <type>"}, self.words)
        self.assertIn('<div class="sup-block s-scoped">', html)
        self.assertNotIn('<table class="sup">', html)
        # The repeating head names the product on every page a long lead reaches.
        self.assertIn('<thead><tr><th scope="col">07 · Fixture &lt;type&gt; · Supplier leads</th></tr></thead>', html)
        # Each offer's head links its own saved page; facts are escaped and joined with a dot, never ": ·".
        self.assertIn('<b><a href="https://example.org/offer">Offer 1</a></b>: Pack &lt;4&gt; <span class="soft">· Source date 1 Oct 2026</span>', html)
        self.assertIn("<p>Minimum purchase: 10 packs · Offer price: ₹95; pack</p>", html)
        self.assertIn('Applicable company terms: Subscription: Required', html)
        self.assertIn('Company information <a href="https://example.org/a">1</a> <a href="https://example.org/b">2</a>', html)
        self.assertIn("A supplier lead is not a confirmed offer", html)
        self.assertNotIn(": ·", html)
        grouped = pages._sources_section({"sources": [("Meta", "https://example.org/1", "5 Oct"), ("Meta", "https://example.org/2", "5 Oct"),
                                                      ("Amazon India", "https://example.org/3", "6 Oct"), ("Google Trends India", None, "6 Oct"),
                                                      ("Amazon India", None, "6 Oct")]}, self.words)
        self.assertIn('<span class="nw">Meta</span> <span class="nw">5 Oct:</span> <a href="https://example.org/1">1</a> '
                      '<a href="https://example.org/2">2</a>', grouped)
        self.assertIn('<a class="nw" href="https://example.org/3">Amazon India</a> <span class="nw">6 Oct</span>', grouped)
        # A site used without a saved page keeps its name and date, unlinked (and once per day); names never break inside.
        self.assertIn('<li><span class="nw">Google Trends India</span> <span class="nw">6 Oct</span></li>', grouped)
        self.assertEqual(grouped.count("Amazon India"), 1)
        legacy = {"rows": [{"name": "Lead", "url": "https://example.org/s", "details": ["reviewed 1 Oct"], "scoped": None,
                            "moq": "100", "terms": {"dispatch": "no", "fees": "optional"}, "terms_text": ""}],
                  "notes": ["Note."], "empty": "", "columns": pages.LEGACY_TERM_COLUMNS}
        html = pages._suppliers_section({"suppliers": legacy}, self.words)
        self.assertNotIn("Cash on delivery", html)
        self.assertIn("Optional", html)
        self.assertNotIn("sup-terms", html)
        self.assertIn("Note. Leads to look into", html)  # the notes read as one short paragraph
        self.assertIn(self.words["sources_none"], pages._sources_section({"sources": []}, self.words))

    def test_key_page_demo_score_stays_within_any_part_maxima(self):
        from product_radar import report_pages as pages
        from product_radar.opportunity_score import load_settings
        settings = load_settings()
        ctx = {"words": self.words, "maxima": {"want": 10, "early": 10, "room": 10, "product": 70}, "footer": "x", "marker": None,
               "settings": settings}
        html = pages.key_page(ctx)
        self.assertIn(">50</text>", html)  # half of every maximum, never more than a part allows

    def test_key_page_point_rules_come_from_the_score_settings(self):
        from copy import deepcopy
        from product_radar import report_pages as pages
        from product_radar.opportunity_score import load_settings
        settings = deepcopy(load_settings())
        ctx = {"words": self.words, "maxima": settings["parts"], "footer": "x", "marker": None, "settings": settings}
        html = pages.key_page(ctx)
        self.assertIn("Low crowding earns all 25 points, Medium 12, High 0.", html)
        self.assertIn("5 points earn 10, and each extra point adds 3, up to 25.", html)
        settings["room"].update(low=24, medium=11)
        settings["product"].update(base_meets=6, base_points=9, per_extra_meet=4)
        html = pages.key_page(dict(ctx, settings=settings))
        self.assertIn("Low crowding earns all 24 points, Medium 11, High 0.", html)
        self.assertIn("6 points earn 9, and each extra point adds 4, up to 25.", html)
        # The supplier key explains the subscription column's own reading (caution = a fee is charged).
        self.assertIn(charts.glyph_state("caution", 3.2), html)


class ContextViewTests(unittest.TestCase):
    """Platform and quick-commerce views read the same records and rules as the score."""

    def presence(self, at="2026-10-05T10:00:00+00:00", **states):
        item = {"listed": True, "roles": {"bestseller": {}, "new_arrival": {}}, "price": "149.50"}
        platforms = {route: {"state": state, "items": [item] if state == "relevant" else []} for route, state in states.items()}
        return {"at": at, "platforms_carrying": [r for r, s in states.items() if s == "relevant"], "platforms": platforms}

    def test_platform_rows_follow_the_score_and_the_record(self):
        score = visuals.score_view(basis(listed={"deodap": ["item"]}))
        record = self.presence(deodap="relevant", baapstore="relevant", dropdash="no_relevant_items_in_sample")
        context = {"as_of": "2026-10-05T12:00:00+00:00", "presence": record}
        with patch("product_radar.opportunity_score.presence_input", return_value={"listed": {"deodap": ["item"]}}):
            view = visuals.platform_view(score, context)
        self.assertEqual([row["state"] for row in view["rows"]], ["listed", "delisted", "not_found"])
        self.assertEqual(view["rows"][0]["roles"], ["bestseller", "new_arrival"])
        self.assertEqual(view["rows"][0]["price"], "149.50")  # the saved amount, printed as ₹149.50
        self.assertIsNone(view["rows"][1]["price"])
        with patch("product_radar.opportunity_score.presence_input", return_value=None):
            # A week-old record gave the score no platform points, so nothing is listed.
            stale = visuals.platform_view(visuals.score_view(basis()), context)
            with self.assertRaisesRegex(SafetyError, "LIVE_REPORT_VISUAL_FACTS_CHANGED"):
                visuals.platform_view(score, context)
        self.assertTrue(stale["stale"])
        self.assertEqual({row["state"] for row in stale["rows"]}, {"not_checked"})
        missing = visuals.platform_view(visuals.score_view(basis()), {"as_of": "2026-10-05T12:00:00+00:00", "presence": None})
        self.assertEqual({row["state"] for row in missing["rows"]}, {"not_checked"})
        self.assertFalse(missing["stale"])

    def test_quick_commerce_matrix_reads_only_the_scores_listings(self):
        def observation(source, place, available, list_type="quickcommerce_context"):
            return {"source": source, "observed_at": "2026-10-04T10:00:00+00:00",
                    "facts": {"location": {"name": place}, "available": available, "list_type": list_type}}
        relevant = {"o1": observation("blinkit", "Mumbai", True), "o2": observation("zepto", "Mumbai", False),
                    "o3": observation("instamart", "Pune", True, list_type="other"), "o4": observation("amazon", "Mumbai", True),
                    "o5": observation("zepto", " ", True)}
        checks = {"version": visuals.QC_CHECKS_VERSION, "as_of": "2026-10-05T10:00:00+00:00", "places": ["Mumbai", "Delhi NCR"],
                  "answered": {"instamart": {"Delhi NCR": "2026-10-04T10:00:00+00:00"}}}
        context = {"as_of": "2026-10-05T10:00:00+00:00", "relevant": relevant}
        score = visuals.score_view(basis(stock={"Mumbai": ["o1"]}))
        view = visuals.qc_view(score, context, checks)
        self.assertEqual(view["places"], ["Mumbai", "Delhi NCR"])
        self.assertEqual(view["rows"], {"blinkit": ["in_stock", "not_checked"], "zepto": ["not_in_stock", "not_checked"],
                                        "instamart": ["not_checked", "not_in_stock"]})
        self.assertEqual(view["checked"], ["Mumbai", "Delhi NCR"])
        self.assertEqual(view["observed"], date(2026, 10, 4))
        with self.assertRaisesRegex(SafetyError, "LIVE_REPORT_VISUAL_FACTS_CHANGED"):
            visuals.qc_view(visuals.score_view(basis(stock={})), context, checks)
        with self.assertRaisesRegex(SafetyError, "LIVE_REPORT_VISUAL_FACTS_CHANGED"):
            visuals.qc_view(score, context, dict(checks, as_of="2026-10-06T10:00:00+00:00"))
        self.assertIsNone(visuals.qc_view(score, None, None)["rows"])


class SupplierAndExampleViewTests(unittest.TestCase):
    def test_term_cells_keep_conflicts_and_optional_subscriptions(self):
        from product_radar import report_view
        from product_radar.live_outputs import SUPPLIER_TERMS_DISPLAY_VERSION, LEGACY_SUPPLIER_TERMS_DISPLAY_VERSION
        supplier = {"subscription": "not_stated", "terms": [
            {"field": "cod", "basis": "conflicting", "value": "Conflicting"},
            {"field": "white_label", "basis": "observed", "value": "Yes"},
            {"field": "neutral_packaging", "basis": "inferred", "value": "Yes"},
            {"field": "subscription_requirement", "basis": "observed", "value": "Optional"}]}
        self.assertEqual(report_view._term(supplier, "cod"), "conflicting")
        self.assertEqual(report_view._term(supplier, "white_label"), "yes")
        self.assertEqual(report_view._term(supplier, "neutral_packaging"), "not_stated")  # an estimate is not a stated term
        self.assertEqual(report_view._subscription(supplier, SUPPLIER_TERMS_DISPLAY_VERSION), "optional")
        self.assertEqual(report_view._subscription(supplier, LEGACY_SUPPLIER_TERMS_DISPLAY_VERSION), "not_stated")
        conflicting = dict(supplier, terms=[{"field": "subscription_requirement", "basis": "conflicting", "value": "Conflicting"}])
        self.assertEqual(report_view._subscription(conflicting, SUPPLIER_TERMS_DISPLAY_VERSION), "conflicting")
        self.assertEqual(report_view._subscription(dict(supplier, subscription="subscription_stated"), SUPPLIER_TERMS_DISPLAY_VERSION), "yes")

    def test_example_notes_name_retained_and_size_limited_examples(self):
        from product_radar import report_view
        rows = [{"title": f"Listing {n}", "source": "indiamart" if n > 6 else "amazon", "seller": None, "pack": "Pack of two",
                 "price": {"displayed": "₹1" + str(n)} if n % 2 else None, "observed_at": "2026-10-08T09:00:00+00:00",
                 "url": None} for n in range(8)]
        product = {"concept_id": "c1", "example_products": rows, "example_coverage": {"omitted_offers": 3},
                   "example_selection": {"requested_limit": 20}}
        view = report_view._examples(product, "en")
        self.assertEqual((len(view["rows"]), view["more"], view["retained"]), (6, 2, 3))
        self.assertEqual(view["shops"][0][2], "Amazon India · pack: Pack of two · seen 8 Oct")
        self.assertEqual(view["shops"][0][0], copy_text.TEXT["en"]["no_quote"])
        self.assertEqual(view["notes"], ["3 further retained examples are not shown in this report.",
                                         "Fewer examples are shown to keep the report file small."])
        html = report_view.examples_html(product, "hi-Latn")
        self.assertIn("2 aur example listings workbook mein hain.", html)
        self.assertIn("3 aur rakhe gaye examples is report mein nahi dikhaye gaye.", html)
