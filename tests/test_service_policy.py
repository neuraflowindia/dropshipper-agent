"""Current service rules and append-only spending migration. Offline only."""

from copy import deepcopy
from decimal import Decimal
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.models import SafetyError, digest
from product_radar.service import calendar_plan, load_policy, pdf_languages, validate_issue, policy_for_fit
from product_radar.storage import Journal, PERIOD_CAP, PROTECTED_BUFFER, PENNY_TOLERANCE
from test_benchmark_foundation import binding, request


def products(count=10):
    return [{"concept_id": "concept-" + str(i), "category": "Baby Products",
             "identity_reviewed": True, "status": "New",
             "kpi_states": ["Meets", "Meets", "Not assessed"] + ["Meets"]*5 + ["Unclear"]*5}
            for i in range(count)]


class ServicePolicyTests(unittest.TestCase):
    def test_version_two_allows_only_reviewed_owner_settings(self):
        from product_radar.service import _validate_policy
        policy = dict(policy_for_fit("fit-13-v1"), publication_time="08:30", ongoing_pdf_language="hi-Latn")
        self.assertEqual(_validate_policy(policy), policy)
        self.assertEqual(pdf_languages(3, policy=policy), ("hi-Latn",))
        for name, value in (("publication_time", "24:00"), ("publication_time", True),
                            ("ongoing_pdf_language", "hi"), ("minimum_products", 5), ("schema_version", True)):
            changed = dict(policy, **{name: value})
            with self.subTest(name=name), self.assertRaises(SafetyError):
                _validate_policy(changed)
    def test_config_matches_code_and_owner_confirmed_release_time(self):
        policy = load_policy()
        self.assertEqual(policy["schema_version"], 3)
        self.assertEqual(policy["fit_policy_id"], "fit-10-v1")
        self.assertEqual(policy["minimum_kpi_matches"], 5)
        self.assertEqual(policy["good_fit_kpi_matches"], 6)
        self.assertEqual(policy["publication_time"], "09:00")
        self.assertEqual(policy["timezone"], "Asia/Kolkata")
        self.assertEqual(policy["publication_weekdays"], [1, 3, 5])
        self.assertIsNone(policy["ongoing_pdf_language"])
        self.assertEqual(policy["categories"][1], "Baby Products")
        self.assertEqual(policy["apify_penny_tolerance_usd"], "0.05")
        self.assertEqual(Decimal(policy["apify_period_limit_usd"]), PERIOD_CAP)
        self.assertEqual(Decimal(policy["apify_protected_buffer_usd"]), PROTECTED_BUFFER)
        self.assertEqual(Decimal(policy["apify_penny_tolerance_usd"]), PENNY_TOLERANCE)

    def test_passed_policy_cannot_bypass_current_rules(self):
        policy = load_policy(); policy["publication_weekdays"][0] = True
        for bad in ({}, policy):
            with self.assertRaises(SafetyError):
                calendar_plan("2026-10-03", "2026-11-02", "3", policy=bad)
            with self.assertRaises(SafetyError): validate_issue(products(), policy=bad)
            with self.assertRaises(SafetyError): pdf_languages(1, policy=bad)

    def test_four_week_and_partial_fifth_week_calendar(self):
        for end, count in (("2026-10-31", 12), ("2026-11-03", 13)):
            plan = calendar_plan("2026-10-03", end, "3.75")
            self.assertEqual(len(plan["issues"]), count)
            self.assertEqual(plan["minimum_entries"], count*10)
            self.assertEqual(plan["minimum_new"], count*8)
            self.assertEqual(sum(Decimal(i["allocation_usd"]) for i in plan["issues"])
                             + Decimal(plan["unallocated_usd"]), Decimal("3.75"))
            self.assertTrue(plan["issues"][0]["collection_before_window"])
            self.assertFalse(plan["ready_for_scheduling"])
            self.assertEqual(plan["publication_time"], "09:00")
        mondays = {i["collection_monday"] for i in plan["issues"]}
        self.assertEqual(len(mondays), 5)

    def test_calendar_empty_zero_invalid_and_overallocation(self):
        self.assertEqual(calendar_plan("2026-10-04", "2026-10-05", "1")["unallocated_usd"], "1")
        self.assertEqual(calendar_plan("2026-10-03", "2026-10-04", "0")["issues"][0]["allocation_usd"], "0E-8")
        for start, end, amount in (("bad", "2026-10-05", "1"), ("2026-10-05", "2026-10-04", "1"),
                                  ("2026-10-03", "2026-10-05", "4.51"), (None, "2026-10-05", "1")):
            with self.subTest(start=start, amount=amount), self.assertRaises(SafetyError):
                calendar_plan(start, end, amount)

    def test_minimum_maximum_and_all_new(self):
        self.assertEqual(validate_issue(products()), {"products": 10, "new": 10, "updates": 0})
        self.assertEqual(validate_issue(products(20))["products"], 20)
        for count in (0, 9, 21):
            with self.assertRaisesRegex(SafetyError, "COUNT_REVIEW"):
                validate_issue(products(count))

    def test_update_rounding_and_independent_reason_required(self):
        for size, allowed in ((10, 2), (14, 2), (15, 3), (19, 3), (20, 4)):
            rows = products(size)
            published = {p["concept_id"] for p in rows[:allowed]}
            for p in rows[:allowed]:
                p.update(status="Updated", material_update_reviewed=True, update_reason="New supplier terms checked.")
            self.assertEqual(validate_issue(rows, published)["updates"], allowed)
            rows[allowed].update(status="Updated", material_update_reviewed=True, update_reason="Changed evidence.")
            with self.assertRaisesRegex(SafetyError, "TOO_MANY_UPDATES"):
                validate_issue(rows, published | {rows[allowed]["concept_id"]})
        rows = products()
        rows[0]["status"] = "Updated"
        with self.assertRaisesRegex(SafetyError, "UPDATE_EVIDENCE"):
            validate_issue(rows, {"concept-0"})

    def test_duplicates_history_and_queue(self):
        with self.assertRaisesRegex(SafetyError, "NOVELTY_MISMATCH"):
            validate_issue(products(), {"concept-0"})
        with self.assertRaisesRegex(SafetyError, "DUPLICATE_OR_QUEUED"):
            validate_issue(products(), queued_ids={"concept-0"})
        rows = products(); rows[1]["concept_id"] = rows[0]["concept_id"]
        with self.assertRaisesRegex(SafetyError, "DUPLICATE_OR_QUEUED"):
            validate_issue(rows)

    def test_categories_identity_and_seven_real_judgments(self):
        for field, value in (("category", "Pets"), ("identity_reviewed", False),
                             ("kpi_states", ["Meets"]*13), ("kpi_states", ["Not assessed"]*13)):
            rows = products(); rows[0][field] = value
            with self.subTest(field=field), self.assertRaises(SafetyError): validate_issue(rows)
        rows = products(); rows[0]["kpi_states"][0] = "Unclear"
        with self.assertRaisesRegex(SafetyError, "KPI_REQUIREMENTS"):
            validate_issue(rows)

    def test_bilingual_is_two_issues_not_two_weeks_or_render_attempts(self):
        for number in (1, 2): self.assertEqual(pdf_languages(number), ("en", "hi-Latn"))
        self.assertEqual(pdf_languages(3, "hi-Latn"), ("hi-Latn",))
        with self.assertRaisesRegex(SafetyError, "LANGUAGE_DECISION"):
            pdf_languages(3)
        for number in (0, True, "1"):
            with self.assertRaises(SafetyError): pdf_languages(number)


class PeriodPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="radar-policy-test-", dir=os.environ.get("RADAR_TEST_STORAGE_ROOT"))
        self.addCleanup(self.temp.cleanup)
        self.journal = Journal(Path(self.temp.name))
        self.account = binding()

    def legacy_budget(self):
        ns = self.account.namespace
        self.journal.append("budget-" + digest(ns)[:20], "budget",
                            {"namespace": ns, "allowance": "5", "production_provision": "3.85", "emergency": "0.75"})

    def test_explicit_migration_preserves_bytes_and_is_idempotent(self):
        with self.journal.locked():
            self.legacy_budget()
            original = self.journal.path.read_bytes()
            with self.assertRaisesRegex(SafetyError, "MIGRATION_REQUIRED"):
                self.journal.spendable(self.account.namespace)
            self.journal.apply_policy(self.account.namespace, "3.75", "owner-policy")
            after = self.journal.path.read_bytes()
            self.assertTrue(after.startswith(original))
            self.journal.apply_policy(self.account.namespace, "3.75", "owner-policy")
            self.assertEqual(self.journal.path.read_bytes(), after)
            self.assertEqual(self.journal.spendable(self.account.namespace), Decimal("0.80"))
            self.assertEqual(self.journal.state()[0][self.account.namespace]["allowance"], "5")

    def test_known_charge_and_unknown_read_hold_survive_policy_change(self):
        with self.journal.locked():
            self.journal.setup_budget(self.account.namespace, "5")
            self.journal.reserve(request(), self.account, read_reserve="0.02")
            self.journal.append("submitting", "submitting", {"attempt": "meta-one"})
            self.journal.append("receipt", "receipt", {"attempt": "meta-one", "run_id": "fictional-run", "dataset_id": "fictional-data"})
            self.journal.append("terminal", "terminal", {"attempt": "meta-one", "status": "SUCCEEDED"})
            self.journal.append("charge", "charge", {"attempt": "meta-one", "usd": "0.01", "evidence": "synthetic-charge"})
            before = deepcopy(self.journal.state()[1])
            self.journal.apply_policy(self.account.namespace, "3.75", "owner-policy")
            self.assertEqual(self.journal.state()[1], before)
            self.assertEqual(self.journal.spendable(self.account.namespace), Decimal("0.77"))

    def test_interrupted_initial_policy_can_resume_without_new_balance(self):
        with self.journal.locked():
            with patch.object(self.journal, "apply_policy", side_effect=SafetyError("SYNTHETIC_INTERRUPTION")):
                with self.assertRaisesRegex(SafetyError, "SYNTHETIC_INTERRUPTION"):
                    self.journal.setup_budget(self.account.namespace, "5")
            original = self.journal.path.read_bytes()
            with self.assertRaisesRegex(SafetyError, "MIGRATION_REQUIRED"):
                self.journal.spendable(self.account.namespace)
            self.journal.setup_budget(self.account.namespace, "5")
            self.assertTrue(self.journal.path.read_bytes().startswith(original))
            self.assertEqual(len(self.journal.events()), 2)
            self.assertEqual(self.journal.spendable(self.account.namespace), Decimal("4.55"))

    def test_target_tolerance_and_external_usage_not_free_reset(self):
        with self.journal.locked():
            self.journal.setup_budget(self.account.namespace, "5")
            self.assertEqual(self.journal.spendable(self.account.namespace), Decimal("4.55"))
            self.assertEqual(self.journal.spendable(self.account.namespace, "0.50"), Decimal("0.05"))
            self.assertEqual(self.journal.spendable(self.account.namespace, "0.45"), Decimal("0"))
            self.assertEqual(self.journal.spendable(self.account.namespace, "100"), Decimal("4.55"))
            self.assertLess(self.journal.spendable(self.account.namespace, "0.44"), 0)


if __name__ == "__main__":
    unittest.main()
