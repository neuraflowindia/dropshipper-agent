"""Synthetic CSV/relevance fixtures; no live capture, provider or model calls."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from product_radar.discovery import compute_market_signals
from product_radar.models import SafetyError, digest
from product_radar.research_flow import candidate_summary
from product_radar.research_memory import ResearchMemory, change
from product_radar.research_operations import import_trends_evidence
from product_radar.report_selection import build_report_pool, selection_scope
from product_radar.storage import Journal
from product_radar.trends import compute_trends_signal
from product_radar.trends_related import (chart_with_related_support, import_related_queries_evidence,
                                         parse_related_queries_csv)
from test_report_selection import NOW, fixture
from test_trends_browser import metadata, weekly

AT = "2026-10-06T10:00:00+00:00"
LATER = "2026-10-06T10:01:00+00:00"
TERM = "sink organiser"
REFERENCE = "https://trends.google.com/trends/explore?geo=IN&q=sink+organiser#related-queries"


def receipt(**changes):
    return {**metadata(), "collected_at": LATER, "source_reference": REFERENCE,
            "widget": "related_queries", "displayed_section": "Rising", **changes}


def exported(value="Breakout", query="sink caddy", section="RISING", delimiter=","):
    return ("\ufeffCategory: All categories\n\n" + TERM + ": (India)\n\nTOP\n"
            + delimiter.join(("generic kitchen storage", "100")) + "\n\n"
            + section + "\n" + delimiter.join((query, value)) + "\n").encode()


def review(query="sink caddy", relationship="same_product", **changes):
    return {"query": query, "relationship": relationship,
            "reason": "Synthetic explicit review: this is the same sink sponge-storage use.",
            "reference": REFERENCE, **changes}


class RelatedCsvTests(unittest.TestCase):
    def actual_export(self):
        return (Path(__file__).parent / "fixtures/benchmarks/related_queries_mattress.csv").read_bytes()

    def actual_receipt(self, **changes):
        return {**receipt(), "term": "waterproof mattress protector", "collected_at": "2026-10-06T21:30:00+00:00", **changes}

    def test_actual_dated_public_export_keeps_both_dates_and_growth(self):
        parsed = parse_related_queries_csv(self.actual_export(), self.actual_receipt())
        self.assertEqual(parsed["state"], "Usable")
        self.assertEqual(parsed["metadata"]["export_date_range"], {"start": "2025-10-06", "end": "2026-10-06"})
        self.assertEqual([row["growth_percent"] for row in parsed["queries"]], [70, 50])
        self.assertEqual(len(parsed["top_queries"]), 5)
        self.assertTrue(all(not row["rising"] for row in parsed["top_queries"]))
        self.assertTrue(all("same_product" not in row for row in parsed["queries"]))

    def test_dated_export_wrong_country_or_term_fails(self):
        for old, new in ((b", India)", b", United States)"),
                         (b"waterproof mattress protector:", b"mattress topper:")):
            self.assertEqual(parse_related_queries_csv(self.actual_export().replace(old, new), self.actual_receipt())["state"], "Failed")

    def test_dated_export_wrong_period_or_inverted_bounds_fails(self):
        for replacement in (b"7/6/26 - 10/6/26", b"10/6/24 - 10/6/26", b"10/6/26 - 10/6/25"):
            raw = self.actual_export().replace(b"10/6/25 - 10/6/26", replacement)
            self.assertEqual(parse_related_queries_csv(raw, self.actual_receipt())["state"], "Failed")

    def test_dated_export_stale_or_future_range_cannot_use_current_receipt(self):
        for replacement in (b"10/3/25 - 10/3/26", b"10/9/25 - 10/9/26"):
            raw = self.actual_export().replace(b"10/6/25 - 10/6/26", replacement)
            self.assertEqual(parse_related_queries_csv(raw, self.actual_receipt())["state"], "Failed")

    def test_dated_export_timezone_day_boundary_is_allowed(self):
        for replacement in (b"10/5/25 - 10/5/26", b"10/7/25 - 10/7/26"):
            raw = self.actual_export().replace(b"10/6/25 - 10/6/26", replacement)
            self.assertEqual(parse_related_queries_csv(raw, self.actual_receipt())["state"], "Usable")

    def test_dated_export_invalid_dates_or_missing_one_bound_fails(self):
        for replacement in (b"13/6/25 - 10/6/26", b"10/6/25 - 10/32/26", b"10/6/26"):
            raw = self.actual_export().replace(b"10/6/25 - 10/6/26", replacement)
            self.assertEqual(parse_related_queries_csv(raw, self.actual_receipt())["state"], "Failed")

    def test_dated_export_four_digit_years_are_unambiguous(self):
        raw = self.actual_export().replace(b"10/6/25 - 10/6/26", b"10/6/2025 - 10/6/2026")
        self.assertEqual(parse_related_queries_csv(raw, self.actual_receipt())["state"], "Usable")

    def test_dated_export_receipt_still_requires_verified_twelve_month_web_scope(self):
        for changes in ({"time_range": "today 3-m"}, {"geo": "US"}, {"property": "youtube"}, {"scope_verified": False}):
            self.assertEqual(parse_related_queries_csv(self.actual_export(), self.actual_receipt(**changes))["state"], "Insufficient")

    def test_breakout_remains_a_label_not_an_invented_percentage(self):
        parsed = parse_related_queries_csv(exported(), receipt())
        self.assertEqual(parsed["state"], "Usable")
        self.assertTrue(parsed["queries"][0]["rising"])
        self.assertEqual(parsed["queries"][0]["growth_kind"], "breakout")
        self.assertIsNone(parsed["queries"][0]["growth_percent"])
        self.assertNotIn("same_product", parsed["queries"][0])

    def test_positive_percentage_and_semicolon_export(self):
        parsed = parse_related_queries_csv(exported("+250%", delimiter=";"), receipt())
        self.assertEqual(parsed["queries"][0]["growth_percent"], 250)
        quoted = exported().replace(b"sink caddy,Breakout", b'sink caddy,"+1,200%"')
        self.assertEqual(parse_related_queries_csv(quoted, receipt())["queries"][0]["growth_percent"], 1200)

    def test_top_popularity_never_becomes_growth(self):
        raw = (TERM + ": (India)\nTOP\nsink caddy,100\n").encode()
        result = parse_related_queries_csv(raw, receipt())
        self.assertEqual(result["state"], "Insufficient")
        self.assertEqual(result["queries"], [])
        self.assertFalse(result["top_queries"][0]["rising"])

    def test_missing_and_zero_growth_do_not_count(self):
        for value in ("0%", "", "-", "N/A"):
            with self.subTest(value=value):
                result = parse_related_queries_csv(exported(value), receipt())
                self.assertEqual(result["state"], "Insufficient")
                self.assertFalse(result["queries"][0]["rising"])

    def test_unlabelled_number_cannot_be_confused_with_percentage(self):
        for value in ("100", "NaN%", "inf%", "-50%", "1000000001%"):
            self.assertEqual(parse_related_queries_csv(exported(value), receipt())["state"], "Failed")

    def test_scope_widget_and_rising_display_are_required(self):
        for changes in ({"geo": "US"}, {"time_range": "today 3-m"}, {"property": "youtube"},
                        {"kind": "topic"}, {"scope_verified": False}, {"widget": "related_topics"},
                        {"displayed_section": "Top"}):
            with self.subTest(changes=changes):
                self.assertEqual(parse_related_queries_csv(exported(), receipt(**changes))["state"], "Insufficient")

    def test_export_scope_not_requested_url_controls_acceptance(self):
        for old, new in ((b"(India)", b"(United States)"), (b"sink organiser: ", b"home storage: ")):
            self.assertEqual(parse_related_queries_csv(exported().replace(old, new), receipt())["state"], "Failed")
        self.assertEqual(parse_related_queries_csv(exported().replace(b"organiser: (India)", b"organiser (India)"), receipt())["state"], "Usable")

    def test_html_malformed_duplicate_and_oversize_downloads_fail(self):
        from product_radar.providers.trends_browser import MAX_BYTES
        for raw in (b"<!doctype html>blocked", exported() + b"sink caddy,50%\n", exported() + b"a,b,c\n",
                    b"x" * (MAX_BYTES + 1), b"\xff\x00"):
            self.assertEqual(parse_related_queries_csv(raw, receipt())["state"], "Failed")

    def test_no_receipt_or_unusable_capture_time_fails(self):
        self.assertEqual(parse_related_queries_csv(exported(), {})["state"], "Failed")
        self.assertEqual(parse_related_queries_csv(exported(), receipt(collected_at="yesterday"))["state"], "Failed")


class RelatedIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
        state = fixture().state
        self.memory.commit("fixture", [change(table, i, row) for table, rows in state.items() for i, row in rows.items()])
        self.base = import_trends_evidence(self.memory, concept_id="c000", acquisition_id="chart",
            csv_bytes=weekly([20] * 45 + [0] * 4 + [0, 0, 10, 10, 100]), metadata=metadata())
        self.description_hash = self.base["description_hash"]

    def attach(self, *, raw=None, meta=None, reviews=None, acquisition_id="related", **changes):
        return import_related_queries_evidence(self.memory, base_evidence_id=self.base["id"],
            acquisition_id=acquisition_id, csv_bytes=exported() if raw is None else raw,
            metadata=receipt() if meta is None else meta, reviews=[review()] if reviews is None else reviews,
            description_hash=self.description_hash, reviewer="synthetic operator", **changes)

    def signal(self, *, at=LATER):
        state = self.memory.snapshot()
        rows = [dict(id=i, **row) for i, row in state["trends_evidence"].items()]
        return compute_market_signals({}, {}, at, rows)["families"]["trends"]

    def test_explicit_review_supports_existing_zero_baseline_rule(self):
        self.assertEqual(self.signal()["state"], "Insufficient")
        support = self.attach()
        signal = self.signal()
        self.assertEqual(signal["state"], "Positive")
        self.assertIn(REFERENCE, signal["used_ids"])
        self.assertEqual(signal["recent_mean_bounds"], [5, 5])
        state = self.memory.snapshot()
        self.assertEqual(state["trends_evidence"][self.base["id"]], {k: v for k, v in self.base.items() if k != "id"})
        self.assertEqual(support["parsed"]["metadata"]["collected_at"], LATER)
        derived = chart_with_related_support(self.base, [support], LATER)
        self.assertEqual(derived["metadata"]["collected_at"], AT)
        self.assertEqual(derived["series"], self.base["parsed"]["series"])

    def test_unreviewed_unrelated_and_unclear_queries_cannot_supply_support(self):
        for n, rows in enumerate(([], [review(relationship="not_same_product")], [review(relationship="unclear")])):
            self.attach(reviews=rows, acquisition_id="case" + str(n))
            self.assertEqual(self.signal()["state"], "Insufficient")

    def test_newest_unusable_capture_does_not_cherry_pick_older_positive(self):
        self.attach()
        self.assertEqual(self.signal()["state"], "Positive")
        self.attach(raw=exported("0%"), meta=receipt(collected_at="2026-10-06T10:02:00+00:00"), acquisition_id="later")
        self.assertEqual(self.signal(at="2026-10-06T10:03:00+00:00")["state"], "Insufficient")

    def test_failed_download_does_not_erase_still_fresh_valid_support(self):
        self.attach()
        failed = self.attach(raw=exported("100"), reviews=[],
            meta=receipt(collected_at="2026-10-06T10:02:00+00:00"), acquisition_id="failed")
        self.assertEqual(failed["parsed"]["state"], "Failed")
        self.assertEqual(self.signal(at="2026-10-06T10:03:00+00:00")["state"], "Positive")

    def test_top_only_is_recorded_without_growth_or_review(self):
        raw = (TERM + ": (India)\nTOP\nsink caddy,100\n").encode()
        support = self.attach(raw=raw, reviews=[])
        self.assertEqual(support["parsed"]["state"], "Insufficient")
        self.assertEqual(self.signal()["state"], "Insufficient")

    def test_zero_series_still_needs_two_nonzero_recent_periods(self):
        other = import_trends_evidence(self.memory, concept_id="c000", acquisition_id="one-period",
            csv_bytes=weekly([20] * 45 + [0] * 4 + [0, 0, 0, 10, 100]), metadata=metadata())
        self.base = other
        support = self.attach()
        derived = chart_with_related_support(other, [support], LATER)
        self.assertTrue(derived["rising_related_queries"][0]["same_product"])
        self.assertEqual(compute_trends_signal(derived)["state"], "Insufficient")

    def test_wrong_scope_does_not_write(self):
        before = self.memory.snapshot()
        with self.assertRaisesRegex(SafetyError, "SCOPE_MISMATCH"):
            self.attach(meta=receipt(term="other"))
        self.assertEqual(self.memory.snapshot(), before)

    def test_stale_support_is_retained_for_audit_without_positive_contribution(self):
        support = self.attach(meta=receipt(collected_at="2026-09-20T10:01:00+00:00"))
        self.assertEqual(support["parsed"]["metadata"]["collected_at"], "2026-09-20T10:01:00+00:00")
        self.assertIn(support["id"], self.memory.snapshot()["trends_evidence"])
        self.assertEqual(self.signal()["state"], "Insufficient")

    def test_legitimate_overnight_support_keeps_both_original_dates(self):
        support = self.attach(meta=receipt(collected_at="2026-10-07T00:01:00+00:00"))
        self.assertEqual(self.signal(at="2026-10-07T00:02:00+00:00")["state"], "Positive")
        derived = chart_with_related_support(self.base, [support], "2026-10-07T00:02:00+00:00")
        self.assertEqual(derived["metadata"]["collected_at"], AT)
        self.assertEqual(support["parsed"]["metadata"]["collected_at"], "2026-10-07T00:01:00+00:00")

    def test_reviews_require_actual_exact_rows_reason_and_reference(self):
        for rows in ([review(query="invented")], [review(reason="")], [review(reference="different")],
                     [review(relationship="probably")], [review(), review()], [review(query=[])], [{}]):
            with self.subTest(rows=rows):
                with self.assertRaisesRegex(SafetyError, "REVIEWS_INVALID"):
                    self.attach(reviews=rows)

    def test_replay_is_identical_and_does_not_rebind_changed_product(self):
        original = self.attach()
        before = self.memory.snapshot()
        self.assertEqual(self.attach(), original)
        self.assertEqual(self.memory.snapshot(), before)
        old = before["concepts"]["c000"]
        changed = deepcopy(old)
        changed["description"]["function"] = "A changed product meaning"
        self.memory.put("concepts", "c000", changed, previous=old)
        self.assertEqual(self.attach(), original)
        self.assertEqual(selection_scope(self.memory.snapshot(), "c000")["trends_evidence"], {})
        with self.assertRaisesRegex(SafetyError, "PRODUCT_CHANGED"):
            self.attach(acquisition_id="not-replay")

    def test_changed_reviews_or_bytes_conflict_without_overwriting(self):
        self.attach()
        for changes in ({"raw": exported("50%")}, {"reviews": [review(relationship="not_same_product")]}):
            with self.assertRaisesRegex(SafetyError, "CAPTURE_CONFLICT"):
                self.attach(**changes)

    def test_newer_base_chart_is_not_rescued_by_older_support(self):
        self.attach()
        import_trends_evidence(self.memory, concept_id="c000", acquisition_id="new-chart",
            csv_bytes=weekly([20] * 54), metadata=metadata(collected_at="2026-10-06T10:05:00+00:00"))
        self.assertEqual(self.signal(at="2026-10-06T10:06:00+00:00")["state"], "Not-positive")

    def test_support_cannot_reset_chart_expiry_or_enter_before_capture(self):
        self.attach()
        self.assertEqual(self.signal(at=AT)["state"], "Insufficient")
        self.assertEqual(self.signal(at="2026-10-14T10:00:00+00:00")["state"], "Stale")

    def test_separately_older_support_expires_without_resetting_chart(self):
        self.attach(meta=receipt(collected_at="2026-09-30T10:01:00+00:00"))
        self.assertEqual(self.signal()["state"], "Positive")
        self.assertEqual(self.signal(at="2026-10-08T10:00:00+00:00")["state"], "Insufficient")

    def test_report_facts_and_reservation_scope_include_support_and_chart(self):
        before = selection_scope(self.memory.snapshot(), "c000")
        support = self.attach()
        state = self.memory.snapshot()
        scope = selection_scope(state, "c000")
        self.assertNotEqual(scope, before)
        self.assertEqual(set(scope["trends_evidence"]), {self.base["id"], support["id"]})
        pool = build_report_pool(self.memory, as_of=LATER)
        fact = pool["facts_by_concept"]["c000"]
        self.assertIn(support["id"], fact["trends_evidence_ids"])
        self.assertIn(self.base["id"], fact["trends_evidence_ids"])
        self.assertEqual(next(p for p in candidate_summary(self.memory, as_of=LATER)["products"]
                              if p["id"] == "c000")["market"]["families"]["trends"]["state"], "Positive")


if __name__ == "__main__":
    unittest.main()
