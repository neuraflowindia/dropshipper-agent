"""Browser CSV fixtures prove mapping/arithmetic, not live browser access."""

from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.providers.trends_browser import MAX_BYTES, parse_trends_csv
from product_radar.trends import compute_trends_signal


def metadata(**changes):
    return {"term": "sink organiser", "geo": "IN", "time_range": "today 12-m", "kind": "search_term",
            "property": "web", "scope_verified": True, "collected_at": "2026-10-06T10:00:00+00:00",
            "source_reference": "fixture-chart.csv", **changes}


def weekly(values=None, delimiter=",", heading="Week", label="sink organiser: (India)"):
    values = [20] * 49 + [30] * 4 + [100] if values is None else values
    start = date(2025, 9, 28)
    lines = ["Category: All categories", "", delimiter.join((heading, label))]
    lines.extend(delimiter.join(((start + timedelta(weeks=n)).isoformat(), str(v))) for n, v in enumerate(values))
    return ("\ufeff" + "\n".join(lines)).encode("utf-8")


def signal(values=None, meta=None):
    return compute_trends_signal(parse_trends_csv(weekly(values), meta or metadata()))


class TrendsCsvTests(unittest.TestCase):
    def test_weekly_positive_and_ongoing_interval_excluded(self):
        result = signal()
        self.assertEqual(result["state"], "Positive")
        self.assertEqual(result["excluded_ongoing_intervals"], 1)
        self.assertEqual(result["recent_mean_bounds"], [30, 30])
        self.assertEqual(result["previous_mean_bounds"], [20, 20])

    def test_stable_valid_series_is_assessable_not_positive(self):
        result = signal([20] * 54)
        self.assertTrue(result["available"])
        self.assertFalse(result["positive"])
        self.assertEqual(result["state"], "Not-positive")

    def test_ratio_and_point_increase_are_both_needed(self):
        self.assertFalse(signal([1] * 49 + [2] * 4 + [50])["positive"])
        self.assertFalse(signal([80] * 49 + [86] * 4 + [50])["positive"])

    def test_bom_metadata_and_semicolon_locale_header(self):
        parsed = parse_trends_csv(weekly(delimiter=";", heading="Woche", label="sink organiser: (Indien)"),
                                  metadata(column_label="sink organiser: (Indien)"))
        self.assertEqual(parsed["state"], "Usable")
        self.assertEqual(len(parsed["series"]), 54)

    def test_monthly_intervals_and_ongoing_month(self):
        data = "Month,sink organiser: (India)\n" + "\n".join(
            f"{year}-{month:02d},{20 if n < 9 else 30}" for n, (year, month) in enumerate(
                [(2025, m) for m in range(9, 13)] + [(2026, m) for m in range(1, 11)]))
        result = compute_trends_signal(parse_trends_csv(data.encode(), metadata()))
        self.assertEqual(result["state"], "Positive")
        self.assertEqual(result["excluded_ongoing_intervals"], 1)

    def test_bounded_value_retains_bounds_not_fake_integer(self):
        parsed = parse_trends_csv(weekly([20] * 49 + ["<1"] * 4 + [100]), metadata())
        self.assertIsNone(parsed["series"][-2]["value"])
        self.assertEqual(parsed["series"][-2]["upper"], 1)
        self.assertTrue(parsed["series"][-2]["upper_exclusive"])
        self.assertEqual(compute_trends_signal(parsed)["state"], "Not-positive")

    def test_bounded_values_can_support_clear_non_boundary_decision(self):
        parsed = parse_trends_csv(weekly([20] * 45 + ["<1", 20, 20, 20] + [30] * 4 + [100]), metadata())
        self.assertEqual(compute_trends_signal(parsed)["state"], "Positive")

    def test_bounded_boundary_decision_is_insufficient(self):
        parsed = parse_trends_csv(weekly([20] * 45 + ["<1", 20, 20, 20] + [20] * 4 + [100]), metadata())
        self.assertEqual(compute_trends_signal(parsed)["state"], "Insufficient")

    def test_all_zero_and_sparse_are_insufficient(self):
        self.assertIsNone(signal([0] * 54)["positive"])
        self.assertIsNone(signal([0] * 49 + [0, 0, 10, 10, 100])["positive"])

    def test_zero_previous_requires_two_recent_nonzero_and_related_support(self):
        parsed = parse_trends_csv(weekly([20] * 45 + [0] * 4 + [0, 0, 10, 10, 100]), metadata())
        self.assertEqual(compute_trends_signal(parsed)["state"], "Insufficient")
        parsed["rising_related_queries"] = [{"query": "sink caddy", "rising": True, "same_product": True,
                                             "reference": "related-fixture.csv"}]
        self.assertTrue(compute_trends_signal(parsed)["positive"])
        parsed["rising_related_queries"][0]["same_product"] = False
        self.assertIsNone(compute_trends_signal(parsed)["positive"])

    def test_same_product_rising_query_alternative(self):
        parsed = parse_trends_csv(weekly([20] * 54), metadata())
        parsed["rising_related_queries"] = [{"query": "sink caddy", "rising": True, "same_product": True,
                                             "reference": "related-fixture.csv"}]
        self.assertTrue(compute_trends_signal(parsed)["positive"])

    def test_unverified_or_wrong_scope_cannot_be_positive(self):
        for change in ({"scope_verified": False}, {"scope_verified": "true"}, {"geo": "US"},
                       {"time_range": "today 13-m"}, {"property": "youtube"}, {"kind": "unknown"}):
            with self.subTest(change=change):
                self.assertEqual(signal(meta=metadata(**change))["state"], "Insufficient")

    def test_wrong_export_country_even_when_receipt_claims_india(self):
        parsed = parse_trends_csv(weekly(label="sink organiser: (United States)"),
                                  metadata(column_label="sink organiser: (United States)"))
        self.assertEqual(parsed["state"], "Failed")

    def test_wrong_term_and_multi_term_exports_fail(self):
        self.assertEqual(parse_trends_csv(weekly(label="napkin holder: (India)"), metadata())["state"], "Failed")
        self.assertEqual(parse_trends_csv(b"Week,sink organiser: (India),napkin holder: (India)\n2026-09-20,20,30", metadata())["state"], "Failed")

    def test_interval_header_mismatch_and_week_range(self):
        parsed = parse_trends_csv(b"Week,sink organiser: (India)\n2026-09,20", metadata())
        self.assertEqual(parsed["state"], "Failed")
        parsed = parse_trends_csv(b"Week,sink organiser: (India)\n2026-09-27 - 2026-10-03,20", metadata())
        self.assertEqual(parsed["state"], "Usable")
        self.assertEqual(parsed["series"][0]["end"], "2026-10-03")

    def test_duplicate_out_of_order_and_gaps_are_detected(self):
        data = weekly().decode("utf-8-sig").splitlines()
        for mutated in (data + [data[-1]], data[:3] + list(reversed(data[3:]))):
            self.assertEqual(parse_trends_csv("\n".join(mutated).encode(), metadata())["state"], "Failed")
        parsed = parse_trends_csv("\n".join(data[:-3] + data[-2:]).encode(), metadata())
        self.assertEqual(compute_trends_signal(parsed)["state"], "Insufficient")

    def test_missing_windows_and_bad_numbers(self):
        self.assertEqual(signal([20] * 49 + ["", 30, 30, 30, 100])["state"], "Insufficient")
        for value in ("NaN", "inf", "101", "-1", "1.5"):
            with self.subTest(value=value):
                self.assertEqual(parse_trends_csv(weekly([value] * 54), metadata())["state"], "Failed")

    def test_html_binary_empty_oversize_and_too_many_rows(self):
        for data in (b"<html>blocked</html>", b"\x00\xff", b"x" * (MAX_BYTES + 1), weekly([1] * 1001)):
            self.assertEqual(parse_trends_csv(data, metadata())["state"], "Failed")
        self.assertEqual(parse_trends_csv(b"Week,sink organiser: (India)\n", metadata())["state"], "Insufficient")

    def test_stale_capture_and_stale_series(self):
        parsed = parse_trends_csv(weekly(), metadata())
        self.assertEqual(compute_trends_signal(parsed, now="2026-10-14T10:00:00+00:00")["state"], "Stale")
        parsed = parse_trends_csv(weekly([20] * 50), metadata())
        self.assertEqual(compute_trends_signal(parsed)["state"], "Stale")

    def test_inadequate_period_coverage_is_not_a_twelve_month_claim(self):
        parsed = parse_trends_csv(weekly([20] * 8), metadata())
        self.assertEqual(compute_trends_signal(parsed)["state"], "Insufficient")

    def test_topic_explicit_display_label(self):
        parsed = parse_trends_csv(weekly(label="Kitchen sink: (India)"),
                                  metadata(term="/m/topic", kind="topic", column_label="Kitchen sink: (India)"))
        self.assertEqual(parsed["state"], "Usable")

    def test_partial_flag_explicitly_excludes_interval(self):
        parsed = parse_trends_csv(weekly(), metadata())
        parsed["series"][-2]["partial"] = True
        result = compute_trends_signal(parsed)
        self.assertEqual(result["state"], "Stale")
        self.assertEqual(result["excluded_ongoing_intervals"], 2)

    def test_tampered_nonfinite_observation_cannot_score_positive(self):
        parsed = parse_trends_csv(weekly(), metadata())
        for bad in (float("inf"), float("nan"), True, 150):
            modified = deepcopy(parsed)
            modified["series"][-2].update(value=bad, lower=bad, upper=bad)
            self.assertEqual(compute_trends_signal(modified)["state"], "Insufficient")

    def test_overlong_series_does_not_become_twelve_month_evidence(self):
        parsed = parse_trends_csv(weekly([20] * 80), metadata())
        # Move capture to the later date so all intervals are completed: the
        # explicit receipt still cannot make a nineteen-month series comparable.
        parsed["metadata"]["collected_at"] = "2027-04-12T10:00:00+00:00"
        self.assertEqual(compute_trends_signal(parsed)["state"], "Insufficient")


if __name__ == "__main__":
    unittest.main()
