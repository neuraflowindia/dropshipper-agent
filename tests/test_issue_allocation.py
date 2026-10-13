"""Issue rules and genuine local journal transactions; no client publication."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.issue_allocation import (allocate_issues, allocation_snapshot_hash, cancel_issue, draft_ordinal,
                                          mark_issue_uploaded, record_publication_receipt, reserve_issue)
from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory
from product_radar.service import CATEGORIES, validate_issue
from product_radar.storage import Journal

DATES = ["2026-10-13", "2026-10-15", "2026-10-17"]
AS_OF = "2026-10-14T10:00:00+00:00"


def products(count, offset=0):
    return [{"concept_id": f"concept{offset + n:04d}", "category": CATEGORIES[n % 4], "identity_reviewed": True,
             "kpi_states": ["Meets", "Meets", "Not assessed"] + ["Meets"] * 5 + ["Unclear"] * 5,
             "evidence_refs": [f"obs{offset + n}"], "status": "New"} for n in range(count)]


def history(values, issue_id="oldissue"):
    return [{"concept_id": value["concept_id"], "status": "published", "issue_id": issue_id, "client_id": "default", "ordinal": 1} for value in values]


def receipt(issue, *, remote_state="active", scheduled="2026-10-13T04:00:00+00:00", deactivated=None):
    return {"issue_id": issue["issue_id"], "revision": issue["revision"], "selection_hash": issue["selection_hash"],
            "receipt_id": "receipt1", "verified": True, "authority": "release_index", "remote_state": remote_state,
            "package_hash": "a" * 64, "server_activated_at": "2026-10-12T10:00:00+00:00", "scheduled_at": scheduled,
            "server_observed_at": AS_OF, "server_deactivated_at": deactivated}


class AllocationTests(unittest.TestCase):
    def test_30_new_with_twenty_own_holds_keep_three_complete_minimums(self):
        values = products(30)
        for value in values:
            value["category"] = CATEGORIES[0]
        holds = [{"issue_id": "issue-" + DATES[n], "date": DATES[n], "state": "reserved",
                  "client_id": "default", "products": values[1 + n * 10:11 + n * 10]}
                 for n in range(2)]
        before = deepcopy((values, holds))
        unrestricted = allocate_issues(values, DATES, [], [])
        held = allocate_issues(values, DATES, [], holds)
        self.assertEqual([i["counts"]["products"] for i in unrestricted["issues"]], [10, 10, 10])
        self.assertEqual([i["counts"]["products"] for i in held["issues"]], [10, 10, 10])
        self.assertTrue(all(i["shortfall"] == 0 and i["ready_for_review"] for i in held["issues"]))
        for n in range(2):
            self.assertEqual({p["concept_id"] for p in held["issues"][n]["products"]},
                             {p["concept_id"] for p in holds[n]["products"]})
        self.assertEqual((values, holds), before)

    def test_25_new_with_nineteen_own_holds_leave_five_for_third_issue(self):
        values = products(25)
        for value in values:
            value["category"] = CATEGORIES[0]
        holds = [{"issue_id": "issue-" + DATES[0], "date": DATES[0], "state": "reserved",
                  "client_id": "default", "products": values[1:11]},
                 {"issue_id": "issue-" + DATES[1], "date": DATES[1], "state": "reserved",
                  "client_id": "default", "products": values[11:20]}]
        result = allocate_issues(values, DATES, [], holds)
        self.assertEqual([i["counts"]["products"] for i in result["issues"]], [10, 10, 5])
        self.assertEqual([i["shortfall"] for i in result["issues"]], [0, 0, 5])
        self.assertEqual({p["concept_id"] for p in result["issues"][0]["products"]},
                         {p["concept_id"] for p in holds[0]["products"]})
        self.assertEqual(len({p["concept_id"] for i in result["issues"] for p in i["products"]}), 25)

    def test_own_hold_order_retains_priority_within_held_and_shared_choices(self):
        from product_radar.fit_policy import ACTIVE_CRITERION_IDS, FIT_POLICY_ID
        from product_radar.opportunity_policy import OPPORTUNITY_POLICY_ID
        values = products(12)
        priorities = [1] * 10 + [4, 3]
        for value, priority in zip(values, priorities):
            matches = 5 if priority in (2, 4) else 6
            available = 2 if priority in (1, 2) else 1
            value.update(category=CATEGORIES[0], fit_policy_id=FIT_POLICY_ID,
                         criterion_ids=list(ACTIVE_CRITERION_IDS), kpi_total=10,
                         kpi_states=["Meets"] * matches + ["Unclear"] * (10 - matches),
                         kpi_matches=matches, fit_tier="good" if matches >= 6 else "decent",
                         opportunity_policy_id=OPPORTUNITY_POLICY_ID, opportunity_priority=priority,
                         opportunity_basis={"available_families": available, "positive_families": 1})
        holds = [{"issue_id": "issue-" + DATES[0], "state": "reserved",
                  "client_id": "default", "date": DATES[0], "products": values[10:]}]
        baseline = allocate_issues(values, [DATES[0]], [], [])["issues"][0]
        held = allocate_issues(values, [DATES[0]], [], holds)["issues"][0]
        self.assertEqual([p["opportunity_priority"] for p in baseline["products"]], [1] * 10 + [3, 4])
        self.assertEqual([p["opportunity_priority"] for p in held["products"]], [3, 4] + [1] * 10)
        self.assertEqual(held["counts"], {"products": 12, "new": 12, "updates": 0})

    def test_own_holds_never_force_stale_or_ineligible_members(self):
        values = products(14)
        for value in values:
            value["category"] = CATEGORIES[0]
        values[1]["usable_until"] = "2026-10-12T23:59:59+00:00"
        values[2]["identity_reviewed"] = False
        values[3]["eligible"] = False
        holds = [{"issue_id": "issue-" + DATES[0], "state": "reserved",
                  "client_id": "default", "date": DATES[0], "products": values[1:11]}]
        before = deepcopy(holds)
        issue = allocate_issues(values, [DATES[0]], [], holds)["issues"][0]
        ids = [p["concept_id"] for p in issue["products"]]
        self.assertEqual(ids[:7], [p["concept_id"] for p in values[4:11]])
        self.assertTrue(set(ids).isdisjoint(p["concept_id"] for p in values[1:4]))
        self.assertEqual(issue["counts"]["products"], 11)
        self.assertEqual(holds, before)

    def test_other_client_and_cancelled_own_holds_have_no_preference(self):
        values = products(20)
        for value in values:
            value["category"] = CATEGORIES[0]
        baseline = allocate_issues(values, [DATES[0]], [], [])
        for client, state in (("another-client", "published"), ("default", "cancelled")):
            with self.subTest(client=client, state=state):
                hold = {"issue_id": "issue-" + DATES[0], "date": DATES[0], "state": state,
                        "client_id": client, "products": values[10:]}
                issue = allocate_issues(values, [DATES[0]], [], [hold])["issues"][0]
                self.assertEqual(issue["products"], baseline["issues"][0]["products"])

    def test_other_issue_uploaded_and_published_holds_remain_protected(self):
        values = products(30)
        for value in values:
            value["category"] = CATEGORIES[0]
        for state in ("uploaded", "published"):
            with self.subTest(state=state):
                holds = [{"issue_id": "issue-" + DATES[0], "date": DATES[0], "state": "reserved",
                          "client_id": "default", "products": values[1:11]},
                         {"issue_id": "protected-issue", "date": DATES[1], "state": state,
                          "client_id": "default", "products": values[11:21]}]
                before = deepcopy(holds)
                issue = allocate_issues(values, [DATES[0]], [], holds)["issues"][0]
                ids = [p["concept_id"] for p in issue["products"]]
                self.assertEqual(ids[:10], [p["concept_id"] for p in values[1:11]])
                self.assertTrue(set(ids).isdisjoint(p["concept_id"] for p in values[11:21]))
                self.assertEqual(holds, before)

    def test_owned_updates_keep_new_targets_and_twenty_percent_cap(self):
        new, updated = products(24), products(6, 100)
        for value in new + updated:
            value["category"] = CATEGORIES[0]
        for value in updated:
            value.update(material_update_reviewed=True, update_reason="Relevant verified supplier terms changed.")
        holds = [{"issue_id": "issue-" + DATES[n], "date": DATES[n], "state": "reserved",
                  "client_id": "default", "products": new[n * 8:(n + 1) * 8] + updated[n * 2:(n + 1) * 2]}
                 for n in range(2)]
        result = allocate_issues(new + updated, DATES, history(updated), holds)
        self.assertEqual([i["counts"] for i in result["issues"]],
                         [{"products": 10, "new": 8, "updates": 2}] * 3)
        for n in range(2):
            self.assertEqual({p["concept_id"] for p in result["issues"][n]["products"]},
                             {p["concept_id"] for p in holds[n]["products"]})

    def test_own_hold_does_not_turn_unsupported_published_product_into_update(self):
        values = products(20)
        for value in values:
            value["category"] = CATEGORIES[0]
        holds = [{"issue_id": "issue-" + DATES[0], "date": DATES[0], "state": "reserved",
                  "client_id": "default", "products": values[10:]}]
        result = allocate_issues(values, [DATES[0]], history(values[10:]), holds)
        issue = result["issues"][0]
        self.assertEqual([p["concept_id"] for p in issue["products"]],
                         [p["concept_id"] for p in values[:10]])
        self.assertEqual(issue["counts"], {"products": 10, "new": 10, "updates": 0})

    def test_35_new_fill_all_minimums_before_fair_extras(self):
        result = allocate_issues(products(35), DATES, [], [])
        self.assertEqual([len(value["products"]) for value in result["issues"]], [12, 12, 11])
        self.assertEqual(len({p["concept_id"] for i in result["issues"] for p in i["products"]}), 35)
        self.assertTrue(all(i["counts"]["updates"] == 0 and i["ready_for_review"] for i in result["issues"]))
        self.assertTrue(all(i["client_ready"] is False for i in result["issues"]))

    def test_25_new_prepare_earlier_complete_issues_and_expose_later_shortfall(self):
        result = allocate_issues(products(25), DATES, [], [])
        self.assertEqual([len(value["products"]) for value in result["issues"]], [10, 10, 5])
        self.assertEqual([i["ready_for_review"] for i in result["issues"]], [True, True, False])
        self.assertEqual(result["issues"][2]["shortfall"], 5)

    def test_24_new_six_supported_updates_fill_three_valid_issues(self):
        new, updated = products(24), products(6, 100)
        for product in updated:
            product.update(material_update_reviewed=True, update_reason="Verified single-piece supplier dispatch added.")
        past = history(updated)
        result = allocate_issues(new + updated, DATES, past, [])
        self.assertEqual([i["counts"] for i in result["issues"]], [{"products": 10, "new": 8, "updates": 2}] * 3)

    def test_no_supported_update_means_no_assumed_update_slots(self):
        new, old = products(24), products(6, 100)
        result = allocate_issues(new + old, DATES, history(old), [])
        self.assertEqual([i["counts"]["products"] for i in result["issues"]], [10, 10, 4])
        self.assertTrue(all(i["counts"]["updates"] == 0 for i in result["issues"]))

    def test_update_rounding_for_10_12_15(self):
        for total, allowed in ((10, 2), (12, 2), (15, 3)):
            new = products(total - allowed)
            updated = products(allowed, 100)
            for value in updated:
                value.update(material_update_reviewed=True, update_reason="Relevant verified market evidence changed.")
            result = allocate_issues(new + updated, [DATES[0]], history(updated), [])
            issue = result["issues"][0]
            self.assertEqual(issue["counts"], {"products": total, "new": total - allowed, "updates": allowed})
            validate_issue(issue["products"], [value["concept_id"] for value in updated])

    def test_category_balance_is_a_preference_not_a_quota(self):
        mixed = allocate_issues(products(10), [DATES[0]], [], [])["issues"][0]
        self.assertTrue(all(sum(p["category"] == category for p in mixed["products"]) >= 2 for category in CATEGORIES))
        only_one = products(10)
        for value in only_one:
            value["category"] = CATEGORIES[0]
        self.assertTrue(allocate_issues(only_one, [DATES[0]], [], [])["issues"][0]["ready_for_review"])

    def test_pending_and_conflicted_identities_are_excluded_with_reasons(self):
        values = products(12)
        values[0]["identity_reviewed"] = False
        values[1]["identity_conflicted"] = True
        result = allocate_issues(values, [DATES[0]], [], [])
        self.assertEqual(len(result["issues"][0]["products"]), 10)
        self.assertTrue(all("Identity" in value["reason"] for value in result["decisions"][:2]))

    def test_duplicate_canonical_concepts_are_not_counted_twice(self):
        values = products(10)
        result = allocate_issues(values + [deepcopy(values[0])], [DATES[0]], [], [])
        self.assertEqual(result["issues"][0]["counts"]["products"], 10)
        self.assertTrue(any(value["decision"] == "excluded" for value in result["decisions"]))

    def test_other_issue_holds_remain_but_own_revision_holds_can_be_replaced(self):
        values = products(20)
        holds = [{"issue_id": "issue-2026-10-13", "state": "reserved", "client_id": "default", "date": DATES[0], "products": values[:10]},
                 {"issue_id": "other", "state": "uploaded", "client_id": "default", "date": DATES[1], "products": values[10:]}]
        issue = allocate_issues(values, [DATES[0]], [], holds)["issues"][0]
        self.assertEqual({p["concept_id"] for p in issue["products"]}, {p["concept_id"] for p in values[:10]})

    def test_old_dates_and_revisions_do_not_increment_language_ordinal(self):
        old = history(products(10))
        old += [dict(old[0], revision=2)]
        queue = [{"issue_id": "second", "date": DATES[0], "state": "reserved", "client_id": "default"},
                 {"issue_id": "third", "date": DATES[1], "state": "reserved", "client_id": "default"}]
        self.assertEqual(draft_ordinal("third", DATES[1], old, queue), 3)
        queue[0]["state"] = "cancelled"
        self.assertEqual(draft_ordinal("third", DATES[1], old, queue), 2)

    def test_initial_three_issues_have_one_two_three_ordinals_and_third_language_pending(self):
        issues = allocate_issues(products(30), DATES, [], [])["issues"]
        self.assertEqual([i["projected_ordinal"] for i in issues], [1, 2, 3])
        self.assertEqual([i["language_state"] for i in issues], ["bilingual_trial", "bilingual_trial", "language_pending"])

    def test_unfilled_date_does_not_take_a_bilingual_position_from_a_produced_set(self):
        from unittest.mock import patch
        import product_radar.issue_allocation as allocation
        fresh = allocation._fresh_at_release
        # The first date cannot be filled (for example, its release passed
        # before the supporting evidence existed). It is not a produced set.
        with patch.object(allocation, "_fresh_at_release",
                          side_effect=lambda product, issue, **options: issue["date"] != DATES[0] and fresh(product, issue, **options)):
            issues = allocate_issues(products(20), DATES, [], [])["issues"]
        self.assertEqual([issue["ready_for_review"] for issue in issues], [False, True, True])
        self.assertEqual([issue["projected_ordinal"] for issue in issues[1:]], [1, 2])
        self.assertEqual([issue["language_state"] for issue in issues[1:]], ["bilingual_trial", "bilingual_trial"])

    def test_archiving_older_private_holds_makes_next_two_produced_sets_bilingual(self):
        older = [{"issue_id": "issue-default-" + day, "date": day, "state": "reserved", "client_id": "default",
                  "products": products(10, offset=100 + n * 10)} for n, day in enumerate(("2026-10-06", "2026-10-08", "2026-10-10"))]
        held = allocate_issues(products(20), DATES, [], older)["issues"]
        self.assertEqual([issue["projected_ordinal"] for issue in held[:2]], [4, 5])
        self.assertEqual({issue["language_state"] for issue in held[:2]}, {"language_pending"})
        archived = [dict(row, state="cancelled") for row in older]
        released = allocate_issues(products(20), DATES, [], archived)["issues"]
        self.assertEqual([issue["projected_ordinal"] for issue in released], [1, 2, 3])
        self.assertEqual([issue["language_state"] for issue in released[:2]], ["bilingual_trial", "bilingual_trial"])
        self.assertFalse(released[2]["ready_for_review"])

    def held(self, issue_id, day, position, state="reserved"):
        return {"issue_id": issue_id, "date": day, "state": state, "client_id": "default",
                "projected_ordinal": position, "products": []}

    def test_held_set_keeps_its_saved_position_when_an_earlier_date_fills_later(self):
        holds = [self.held("later-held", DATES[2], 1)]
        new = allocate_issues(products(10), [DATES[1]], [], holds)["issues"][0]
        self.assertEqual((new["projected_ordinal"], new["language_state"]), (2, "bilingual_trial"))
        self.assertEqual(draft_ordinal("later-held", DATES[2], [], holds + [dict(self.held(new["issue_id"], DATES[1], 2))]), 1)

    def test_back_fill_after_two_held_or_uploaded_sets_is_never_a_third_bilingual_set(self):
        holds = [self.held("first-held", DATES[1], 1), self.held("second-uploaded", DATES[2], 2, "uploaded")]
        late = allocate_issues(products(10), [DATES[0]], [], holds)["issues"][0]
        self.assertEqual((late["projected_ordinal"], late["language_state"]), (3, "language_pending"))
        for issue_id, day, position in (("first-held", DATES[1], 1), ("second-uploaded", DATES[2], 2)):
            self.assertEqual(draft_ordinal(issue_id, day, [], holds, planned=[late]), position)

    def test_saved_positions_survive_revision_cancellation_and_publication(self):
        holds = [self.held("issue-" + DATES[1], DATES[1], 1), self.held("earlier", DATES[0], 2)]
        revised = allocate_issues(products(10), [{"issue_id": "issue-" + DATES[1], "date": DATES[1], "revision": 2}],
                                  [], holds)["issues"][0]
        self.assertEqual(revised["projected_ordinal"], 1)
        cancelled = [dict(holds[0], state="cancelled"), holds[1]]
        self.assertEqual(draft_ordinal("earlier", DATES[0], [], cancelled), 2)
        self.assertEqual(draft_ordinal("next", DATES[2], [], cancelled), 3)
        published = [dict(row, issue_id="shown", ordinal=2) for row in history(products(1))]
        self.assertEqual(draft_ordinal("next", DATES[2], published, []), 3)
        self.assertEqual(draft_ordinal("next", DATES[2], published, [self.held("held", DATES[1], 5)]), 6)

    def test_correcting_same_published_issue_preserves_new_flags_but_cannot_seed_another_issue(self):
        values = products(10)
        old = history(values, issue_id="issue-2026-10-13")
        correction = allocate_issues(values, [{"issue_id": "issue-2026-10-13", "date": DATES[0], "revision": 2}], old, [])
        self.assertEqual(correction["issues"][0]["counts"], {"products": 10, "new": 10, "updates": 0})
        new_issue = allocate_issues(values, [DATES[1]], old, [])
        self.assertEqual(new_issue["issues"][0]["counts"]["products"], 0)

    def test_frozen_hashes_are_stable_for_shuffled_inputs_and_sensitive_to_facts(self):
        values = products(10)
        first = allocate_issues(values, [DATES[0]], [], [])
        self.assertEqual(first, allocate_issues(list(reversed(values)), [DATES[0]], [], []))
        values[0]["evidence_refs"] = ["changed-evidence"]
        changed = allocate_issues(values, [DATES[0]], [], [])
        self.assertNotEqual(first["issues"][0]["evidence_fingerprint"], changed["issues"][0]["evidence_fingerprint"])
        self.assertNotEqual(first["issues"][0]["selection_hash"], changed["issues"][0]["selection_hash"])


class ReservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
        self.issue = allocate_issues(products(10), [DATES[0]], [], [])["issues"][0]

    def reserve(self, issue=None, **options):
        issue = self.issue if issue is None else issue
        return reserve_issue(self.memory, issue, expected_input_hash=issue["input_hash"],
                             expected_snapshot_hash=allocation_snapshot_hash(self.memory.snapshot()), as_of=AS_OF, **options)

    def test_reservation_is_durable_idempotent_and_does_not_create_publication(self):
        value = self.reserve()
        self.assertEqual(value["state"], "reserved")
        self.assertEqual(self.reserve(), value)
        self.assertEqual(self.memory.snapshot()["published_history"], {})
        reopened = ResearchMemory(self.memory.root, self.memory.journal, evidence_type="synthetic")
        self.assertEqual(reopened.snapshot()["issue_reservations"][self.issue["issue_id"]], value)

    def test_short_issue_cannot_be_reserved_for_release(self):
        short = allocate_issues(products(5), [DATES[0]], [], [])["issues"][0]
        with self.assertRaisesRegex(SafetyError, "COUNT"):
            self.reserve(short)
        self.assertEqual(self.memory.snapshot()["issue_reservations"], {})

    def test_stale_snapshot_or_changed_selection_is_rejected_without_partial_writes(self):
        before = allocation_snapshot_hash(self.memory.snapshot())
        self.reserve()
        with self.assertRaisesRegex(SafetyError, "INPUT_CHANGED"):
            reserve_issue(self.memory, self.issue, expected_input_hash=self.issue["input_hash"], expected_snapshot_hash=before)
        changed = deepcopy(self.issue)
        changed["products"][0]["evidence_refs"] = ["newfacts"]
        changed["revision"] = 2
        with self.assertRaisesRegex(SafetyError, "EVIDENCE_CHANGED"):
            self.reserve(changed)
        self.assertEqual(self.memory.snapshot()["issue_reservations"][self.issue["issue_id"]]["revision"], 1)

    def test_replacing_draft_is_atomic_and_retains_other_holds(self):
        self.reserve()
        dates = [{"issue_id": self.issue["issue_id"], "date": DATES[0], "revision": 2}]
        state = self.memory.snapshot()
        replacement = allocate_issues(products(10, 100), dates, state["published_history"], state["issue_reservations"])["issues"][0]
        self.reserve(replacement)
        saved = self.memory.snapshot()["issue_reservations"][self.issue["issue_id"]]
        self.assertEqual(saved["revision"], 2)
        self.assertTrue(all(p["concept_id"].startswith("concept01") for p in saved["products"]))

    def test_upload_does_not_mark_future_release_as_published(self):
        self.reserve()
        future = receipt(self.issue, scheduled="2026-10-17T04:00:00+00:00")
        mark_issue_uploaded(self.memory, self.issue["issue_id"], future, as_of=AS_OF)
        result = record_publication_receipt(self.memory, self.issue["issue_id"], future, as_of=AS_OF)
        self.assertFalse(result["published"])
        self.assertEqual(self.memory.snapshot()["published_history"], {})

    def test_verified_available_receipt_reconciles_once_and_replay_preserves_history(self):
        self.reserve()
        active = receipt(self.issue)
        first = record_publication_receipt(self.memory, self.issue["issue_id"], active, as_of=AS_OF)
        self.assertTrue(first["published"])
        self.assertEqual(len(self.memory.snapshot()["published_history"]), 10)
        record_publication_receipt(self.memory, self.issue["issue_id"], active, as_of=AS_OF)
        self.assertEqual(len(self.memory.snapshot()["published_history"]), 10)

    def test_unverified_download_or_future_receipt_is_not_publication(self):
        self.reserve()
        for changes in ({"verified": False}, {"authority": "download"}, {"server_observed_at": "2026-11-01T10:00:00+00:00"}):
            with self.assertRaises(SafetyError):
                record_publication_receipt(self.memory, self.issue["issue_id"], dict(receipt(self.issue), **changes), as_of=AS_OF)
        self.assertEqual(self.memory.snapshot()["published_history"], {})

    def test_uploaded_replacement_requires_confirmed_remote_withdrawal(self):
        self.reserve()
        future = receipt(self.issue, scheduled="2026-10-17T04:00:00+00:00")
        mark_issue_uploaded(self.memory, self.issue["issue_id"], future, as_of=AS_OF)
        state = self.memory.snapshot()
        replacement = allocate_issues(products(10, 100), [{"issue_id": self.issue["issue_id"], "date": DATES[0], "revision": 2}],
                                      state["published_history"], state["issue_reservations"])["issues"][0]
        with self.assertRaisesRegex(SafetyError, "REMOTE_CONFIRMATION"):
            self.reserve(replacement)
        self.assertEqual(self.memory.snapshot()["issue_reservations"][self.issue["issue_id"]]["revision"], 1)
        withdrawn = dict(future, remote_state="withdrawn", server_deactivated_at="2026-10-14T09:00:00+00:00")
        self.reserve(replacement, remote_confirmation=withdrawn)
        self.assertEqual(self.memory.snapshot()["issue_reservations"][self.issue["issue_id"]]["revision"], 2)

    def test_cancel_uploaded_issue_needs_receipt_and_records_any_past_availability(self):
        self.reserve()
        mark_issue_uploaded(self.memory, self.issue["issue_id"], receipt(self.issue), as_of=AS_OF)
        with self.assertRaisesRegex(SafetyError, "REMOTE_CONFIRMATION"):
            cancel_issue(self.memory, self.issue["issue_id"], as_of=AS_OF)
        withdrawn = receipt(self.issue, remote_state="withdrawn", deactivated="2026-10-14T09:00:00+00:00")
        cancel_issue(self.memory, self.issue["issue_id"], as_of=AS_OF, remote_confirmation=withdrawn)
        state = self.memory.snapshot()
        self.assertEqual(state["issue_reservations"][self.issue["issue_id"]]["state"], "cancelled")
        self.assertEqual(len(state["published_history"]), 10, "Cancellation cannot make a previously available product new again")

    def test_cancelled_hold_is_never_revived_by_a_late_upload_receipt(self):
        self.reserve()
        cancel_issue(self.memory, self.issue["issue_id"], as_of=AS_OF)
        before = self.memory.snapshot_with_provenance()
        with self.assertRaisesRegex(SafetyError, "ISSUE_RESERVATION_CANCELLED"):
            mark_issue_uploaded(self.memory, self.issue["issue_id"], receipt(self.issue), as_of=AS_OF)
        self.assertEqual(self.memory.snapshot_with_provenance(), before)

    def test_cancel_never_uploaded_draft_releases_holds_without_adding_history(self):
        self.reserve()
        cancel_issue(self.memory, self.issue["issue_id"], as_of=AS_OF)
        self.assertEqual(self.memory.snapshot()["published_history"], {})
        reallocated = allocate_issues(products(10), [DATES[1]], [], self.memory.snapshot()["issue_reservations"])
        self.assertEqual(reallocated["issues"][0]["counts"]["products"], 10)

    def test_source_revision_check_detects_actual_fact_change_before_reservation(self):
        state = self.memory.snapshot()
        fact = {"description": "Synthetic fact"}
        self.memory.put("concepts", "concept0000", fact)
        values = products(10)
        values[0]["source_revisions"] = {"concepts": {"concept0000": digest(fact)}}
        issue = allocate_issues(values, [DATES[0]], [], [])["issues"][0]
        self.memory.put("concepts", "concept0000", {"description": "Changed synthetic fact"}, previous=fact)
        with self.assertRaisesRegex(SafetyError, "SOURCE_FACTS_CHANGED"):
            self.reserve(issue)
        self.assertEqual(self.memory.snapshot()["issue_reservations"], {})

    def test_malformed_receipt_and_wrong_frozen_counts_do_not_change_records(self):
        altered = dict(self.issue, counts={"products": 99, "new": 99, "updates": 0})
        with self.assertRaisesRegex(SafetyError, "COUNTS_CHANGED"):
            self.reserve(altered)
        self.reserve()
        bad = receipt(self.issue)
        del bad["server_observed_at"]
        with self.assertRaises(SafetyError):
            record_publication_receipt(self.memory, self.issue["issue_id"], bad, as_of=AS_OF)
        self.assertEqual(self.memory.snapshot()["published_history"], {})


if __name__ == "__main__":
    unittest.main()
