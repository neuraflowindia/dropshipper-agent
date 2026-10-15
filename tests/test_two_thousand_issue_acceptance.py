"""Actual local allocation/publication producers over a fictional long service.

This supplies eligible fictional types, not real market research, provider
permission, a claim of discovery yield, rendered PDFs or client publication.
There are no source/model/cloud calls. Every saved exposure below is emitted by
record_publication_receipt after reserve_issue, never a handcrafted archive row.
"""

from copy import deepcopy
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from product_radar.accounts import bind_registered_account, context_for_origin
from product_radar.fit_policy import ACTIVE_CRITERION_IDS, FIT_POLICY_ID
from product_radar.issue_allocation import (allocate_issues, allocation_snapshot_hash,
    draft_ordinal, record_publication_receipt, reserve_issue, selection_fingerprint, _fingerprint)
from product_radar.models import digest, timestamp
from product_radar.discovery import MARKET_SITES
from product_radar.opportunity_score import evaluate_opportunity, opportunity_fields
from product_radar.programme import release_slots
from product_radar.research_memory import ResearchMemory, change, key
from product_radar.service import CATEGORIES, calendar_plan, load_policy
from product_radar.storage import Journal, atomic_json, load_json


def next_month(day):
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def calendar_slots(count):
    """Use actual monthly policy and actual weekly 09:00 India release slots."""
    if not 0 < count <= 2000:
        raise ValueError("The fictional acceptance envelope is 1..2,000 issues.")
    policy = load_policy()
    current = date(2026, 10, 1)
    output, windows = [], []
    while len(output) < count:
        stop = next_month(current)
        plan = calendar_plan(current.isoformat(), stop.isoformat(), "0", policy)
        windows.append(plan)
        for row in plan["issues"]:
            day = date.fromisoformat(row["date"])
            monday = day - timedelta(days=day.weekday())
            slot = next(item for item in release_slots(monday.isoformat(), policy=policy) if item["date"] == row["date"])
            output.append(slot)
            if len(output) == count:
                break
        current = stop
    return output, windows


def fictional_types(offset, count):
    output = []
    for number in range(offset, offset + count):
        cid = f"scale-type-{number:05d}"
        evidence = [f"scale-meta-{number:05d}", f"scale-amazon-{number:05d}"]
        fit = {"fit_policy_id": FIT_POLICY_ID, "kpi_matches": 6, "kpi_total": 10}
        sources = {source: {"available": source in ("meta", "amazon"),
            "positive": source == "meta", "used_ids": [evidence[0 if source == "meta" else 1]] if source in ("meta", "amazon") else []}
            for source in MARKET_SITES}
        sources["meta"]["depth"] = {"advertisers": 3, "recent_start_advertisers": 0}
        # Meta positive (6 want points) and a Low crowding answer: an eligible scored type.
        priority = evaluate_opportunity(fit, {"sources": sources}, presence=None, crowding={
            "level": "low", "rule": "few_listings", "thresholds_version": "crowding-thresholds-v1",
            "counts": None, "evidence_ids": [evidence[1]]})
        output.append({"concept_id": cid, "category": CATEGORIES[number % len(CATEGORIES)],
            "identity_reviewed": True, "fit_policy_id": FIT_POLICY_ID,
            "criterion_ids": list(ACTIVE_CRITERION_IDS), "kpi_total": 10,
            "kpi_states": ["Meets"] * 6 + ["Unclear"] * 4, "kpi_matches": 6, "fit_tier": "good",
            "evidence_refs": evidence, "status": "New", **opportunity_fields(priority)})
    return output


def _eligible_changes(fresh):
    return [change("concepts", product["concept_id"], {
        "status": "provisional", "description": {"label": product["concept_id"],
            "category": product["category"], "function": "Fictional use " + product["concept_id"],
            "mechanism": "Fictional manual mechanism", "audience": "Adults", "features": [],
            "basis": "observed", "reason": "Explicitly supplied synthetic eligible inventory.",
            "brand": "generic_or_small"}}) for product in fresh]


def _publication_receipt(issue, ordinal):
    scheduled = timestamp(issue["scheduled_at"])
    observed = scheduled + timedelta(days=8 if (ordinal - 1) % 97 == 0 else 1)
    return {"issue_id": issue["issue_id"], "revision": issue["revision"],
        "selection_hash": issue["selection_hash"], "receipt_id": "synthetic-" + issue["issue_id"],
        "verified": True, "authority": "release_index", "remote_state": "active",
        "package_hash": "a" * 64, "server_activated_at": (scheduled - timedelta(minutes=1)).isoformat(),
        "scheduled_at": issue["scheduled_at"], "server_observed_at": observed.isoformat(),
        "server_deactivated_at": None}


def _resume_prefix(memory, slots):
    """Validate the original synthetic chain; never manufacture missing history.

    A crash may leave an eligible batch, or the next genuine reservation, but
    publication events are atomic. There can therefore be one continuous
    published prefix followed by at most one unfinished reservation. The
    validated prefix is retained in place and all later writes still go
    through reserve_issue and record_publication_receipt.
    """
    proof = memory.snapshot_with_provenance()
    state = proof["state"]
    if any(rows for table, rows in state.items()
           if table not in ("concepts", "issue_reservations", "published_history")):
        raise AssertionError("The explicit acceptance root contains unrelated research.")
    reservations, history = state["issue_reservations"], state["published_history"]
    published = {row["issue_id"] for row in history.values()}
    completed = len(published)
    if completed > len(slots) or published != {row["issue_id"] for row in slots[:completed]}:
        raise AssertionError("The saved synthetic publications are not one calendar prefix.")
    pending = {iid for iid, row in reservations.items() if row.get("state") != "published"}
    expected_pending = {slots[completed]["issue_id"]} if completed < len(slots) else set()
    if pending and pending != expected_pending:
        raise AssertionError("The saved synthetic reservation is not the next calendar issue.")
    if set(reservations) != published | pending:
        raise AssertionError("The saved synthetic issue set is incomplete or unrelated.")
    concepts = state["concepts"]
    committed = len(concepts)
    prior_batch = completed // 3 * 3
    lower = completed * 10 if completed == len(slots) else prior_batch * 10
    upper = min(len(slots), prior_batch + 3) * 10
    if completed == len(slots):
        upper = lower
    # A complete prior batch may have no following eligible event yet. A
    # partial batch (including a reserved next issue) must retain all its types.
    if committed not in {lower, upper} or ((completed % 3 or pending) and committed != upper):
        raise AssertionError("The synthetic eligible batch is partial or extends beyond its owner.")
    supplied = fictional_types(0, committed)
    expected_concepts = {row["id"]: row["value"] for row in _eligible_changes(supplied)}
    if concepts != expected_concepts:
        raise AssertionError("The supplied fictional types changed or contain unrelated rows.")
    by_id = {row["concept_id"]: row for row in supplied}
    expected_events = {"synthetic-eligible-" + str(start // 10): digest(_eligible_changes(supplied[start:start + 30]))
                       for start in range(0, committed, 30)}
    appeared, all_reserved, expected_history, old_examples = set(), set(), {}, []
    for index, slot in enumerate(slots[:completed + bool(pending)]):
        issue = reservations[slot["issue_id"]]
        products = issue.get("products", [])
        ids = {row.get("concept_id") for row in products}
        batch_start = index // 3 * 30
        batch_stop = min(len(slots) * 10, batch_start + 30)
        if (any(issue.get(field) != value for field, value in slot.items())
                or issue.get("projected_ordinal") != index + 1
                or issue.get("language_state") != ("bilingual_trial" if index < 2 else "language_pending")
                or issue.get("counts") != {"products": 10, "new": 10, "updates": 0}
                or len(products) != 10 or len(ids) != 10 or ids & all_reserved
                or not ids <= {f"scale-type-{number:05d}" for number in range(batch_start, batch_stop)}
                or any(product != by_id.get(product.get("concept_id")) for product in products)
                or issue.get("selection_hash") != selection_fingerprint(issue)
                or issue.get("evidence_fingerprint") != _fingerprint(products)):
            raise AssertionError("A saved synthetic issue changed or reused a fictional New type.")
        all_reserved.update(ids)
        original_reservation = deepcopy(issue)
        original_reservation["state"] = "reserved"
        original_reservation.pop("activation_receipt", None)
        expected_events[key("reserve-issue", [issue["issue_id"], issue["revision"], issue["selection_hash"]])] = digest([
            change("issue_reservations", issue["issue_id"], original_reservation)])
        if index >= completed:
            if issue.get("state") != "reserved" or "activation_receipt" in issue:
                raise AssertionError("The unfinished synthetic issue is not a clean original reservation.")
            continue
        receipt = _publication_receipt(issue, index + 1)
        if issue.get("state") != "published" or issue.get("activation_receipt") != receipt:
            raise AssertionError("An original synthetic publication receipt changed.")
        publication_changes = []
        for product in products:
            rid = key("published", [issue["client_id"], issue["issue_id"], issue["revision"], product["concept_id"]])
            expected_history[rid] = {"schema_version": 1, "status": "published", "client_id": issue["client_id"],
                "issue_id": issue["issue_id"], "concept_id": product["concept_id"], "revision": issue["revision"],
                "date": issue["date"], "ordinal": index + 1, "available_at": timestamp(issue["scheduled_at"]).isoformat(),
                "evidence_fingerprint": issue["evidence_fingerprint"], "activation_receipt": receipt}
            publication_changes.append(change("published_history", rid, expected_history[rid]))
        publication_changes.append(change("issue_reservations", issue["issue_id"], issue, original_reservation))
        expected_events[key("publication", [issue["issue_id"], receipt])] = digest(publication_changes)
        appeared.update(ids)
        if not old_examples:
            old_examples = deepcopy(products)
    if history != expected_history or len(appeared) != completed * 10:
        raise AssertionError("The original synthetic publication rows are missing or inconsistent.")
    # These IDs and change digests were obtained by the actual hash-linked
    # replay above, not from the disposable index. This also rejects an extra,
    # correctly chained mutation which changes a reservation-only field while
    # leaving the semantic type and publication summaries looking unchanged.
    if memory._verified["ids"] != expected_events or proof["event_count"] != len(expected_events):
        raise AssertionError("The original synthetic producer-event footprint changed.")
    return {"completed": completed, "appeared": appeared, "old_examples": old_examples,
        "head": proof["head"], "event_count": proof["event_count"]}


def simulate_issues(root, count, *, progress=None, resume=False, checkpoint=None):
    """Real allocator + real canonical writes; no replacement history driver."""
    root = Path(root)
    memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
    slots, windows = calendar_slots(count)
    policy = load_policy()
    original = _resume_prefix(memory, slots)
    if original["event_count"] and not resume:
        raise AssertionError("An existing synthetic root requires explicit resume=True.")
    appeared, old_examples = original["appeared"], original["old_examples"]
    completed, reboots = original["completed"], 0
    skipped_receipt_ticks = len(range(0, completed, 97))
    initial_completed = completed
    if resume and progress is not None:
        progress(completed)
    for start in range(completed // 3 * 3, count, 3):
        if completed == count:
            break
        batch_slots = slots[start:start + 3]
        fresh = fictional_types(start * 10, len(batch_slots) * 10)
        memory.commit("synthetic-eligible-" + str(start), _eligible_changes(fresh))
        if checkpoint is not None:
            checkpoint("eligible", completed)
        before = memory.snapshot()
        # Re-offer old rows deliberately. The allocator must not mistake them
        # for New, even after a restart or many years of later history.
        remaining_slots = batch_slots[completed - start:]
        # Retain an actual reservation which survived a crash. Any unsaved
        # later choices may be freshly allocated, but never overwrite this
        # original reservation or manufacture its publication history.
        pending = before["issue_reservations"].get(remaining_slots[0]["issue_id"]) if remaining_slots else None
        result = allocate_issues(fresh + deepcopy(old_examples), remaining_slots,
            before["published_history"], before["issue_reservations"], policy, identity_state=before)
        if pending is not None:
            result["issues"][0] = pending
        for issue in result["issues"]:
            state = memory.snapshot()
            reserve_issue(memory, issue, expected_input_hash=issue["input_hash"],
                expected_snapshot_hash=allocation_snapshot_hash(state), as_of=issue["scheduled_at"])
            if checkpoint is not None:
                checkpoint("reserved", completed)
            ids = {row["concept_id"] for row in issue["products"]}
            if issue["counts"] != {"products": 10, "new": 10, "updates": 0} or ids & appeared:
                raise AssertionError("Actual allocation repeated or underfilled a fictional New type.")
            if issue["projected_ordinal"] != completed + 1:
                raise AssertionError("Actual issue ordinals no longer form one continuous service.")
            if issue["language_state"] != ("bilingual_trial" if completed < 2 else "language_pending"):
                raise AssertionError("A new month/restart reset the trial or silently selected a language.")
            scheduled = timestamp(issue["scheduled_at"])
            # At regular intervals the laptop misses its scheduled local
            # receipt tick. Real server availability still retains that date.
            receipt = _publication_receipt(issue, completed + 1)
            observed = timestamp(receipt["server_observed_at"])
            skipped_receipt_ticks += completed % 97 == 0
            saved = record_publication_receipt(memory, issue["issue_id"], receipt, as_of=observed.isoformat())
            if not saved["published"] or timestamp(saved["available_at"]) != scheduled:
                raise AssertionError("A delayed local tick reset actual synthetic server availability.")
            appeared.update(ids); completed += 1
            if checkpoint is not None:
                checkpoint("published", completed)
            if not old_examples:
                old_examples = deepcopy(issue["products"])
            if progress is not None and completed % 100 == 0:
                progress(completed)
        # Reconstruct actual local readers, not a new counter or copied
        # archive. All canonical originals remain in the same synthetic root.
        if completed % 300 == 0 or completed == count:
            prior = memory.snapshot_with_provenance()
            memory = ResearchMemory(memory.root, Journal(root / "finance"), evidence_type="synthetic")
            restored = memory.snapshot_with_provenance()
            if prior != restored:
                raise AssertionError("A real cold restart changed the canonical prefix or saved history.")
            replay = record_publication_receipt(memory, issue["issue_id"], receipt, as_of=observed.isoformat())
            if replay != saved or memory.snapshot_with_provenance() != restored:
                raise AssertionError("An original publication receipt replay created duplicate history after restart.")
            reboots += 1
    # A recovered legacy run has no durable record of its old cold-read
    # assertions. Count only cold readers actually checked by this invocation.
    # Recheck the retained original head enough times to meet the same seven
    # real-reader assertions, rather than inventing historical restart counts.
    while resume and count == 2000 and reboots < 7:
        prior = memory.snapshot_with_provenance()
        memory = ResearchMemory(memory.root, Journal(root / "finance"), evidence_type="synthetic")
        restored = memory.snapshot_with_provenance()
        if prior != restored:
            raise AssertionError("A resumed real cold reader changed the canonical history.")
        last = restored["state"]["issue_reservations"][slots[-1]["issue_id"]]
        receipt = _publication_receipt(last, count)
        replay = record_publication_receipt(memory, last["issue_id"], receipt, as_of=receipt["server_observed_at"])
        if not replay["published"] or memory.snapshot_with_provenance() != restored:
            raise AssertionError("A resumed original receipt replay created duplicate history.")
        reboots += 1
    state = memory.snapshot()
    history = state["published_history"]
    return {"memory": memory, "state": state, "slots": slots, "windows": windows,
        "issues": completed, "appearances": len(history), "distinct_new": len(appeared),
        "reboots": reboots, "missed_receipt_ticks": skipped_receipt_ticks, "old_examples": old_examples,
        "resumed_issues": initial_completed, "original_head": original["head"]}


class TwoThousandIssueAcceptanceTests(unittest.TestCase):
    def test_actual_calendar_spans_two_thousand_dates_without_minting_money(self):
        slots, windows = calendar_slots(2000)
        self.assertEqual(len(slots), 2000)
        self.assertEqual(len({slot["issue_id"] for slot in slots}), 2000)
        self.assertEqual(slots, sorted(slots, key=lambda row: row["scheduled_at"]))
        self.assertTrue(all(date.fromisoformat(slot["date"]).weekday() in (1, 3, 5) for slot in slots))
        self.assertTrue(all(timestamp(slot["scheduled_at"]).astimezone(ZoneInfo("Asia/Kolkata")).strftime("%H:%M")
                            == "09:00" for slot in slots))
        self.assertTrue(all(12 <= len(window["issues"]) <= 14 for window in windows))
        self.assertTrue(all(Decimal(window["unallocated_usd"]) == 0 and not window["ready_for_scheduling"]
                            and all(Decimal(issue["allocation_usd"]) == 0 for issue in window["issues"])
                            for window in windows))
        first, last = (date.fromisoformat(slots[index]["date"]) for index in (0, -1))
        self.assertGreater((last - first).days, 12 * 365)
        self.assertLess((last - first).days, 14 * 366)

    def test_actual_producers_restart_and_continue_without_repeat_new_on_short_rehearsal(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = simulate_issues(Path(temporary), 31)
            self.assertEqual((result["issues"], result["appearances"], result["distinct_new"]), (31, 310, 310))
            self.assertEqual(result["reboots"], 1)

    def test_explicit_resume_preserves_original_events_after_each_crash_boundary(self):
        for stage, completed in (("eligible", 0), ("reserved", 0), ("published", 1),
                                 ("reserved", 1), ("published", 3), ("eligible", 3),
                                 ("reserved", 6), ("published", 7)):
            with self.subTest(stage=stage, completed=completed), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                def interrupt(actual_stage, actual_completed):
                    if (actual_stage, actual_completed) == (stage, completed):
                        raise RuntimeError("fictional crash")
                with self.assertRaisesRegex(RuntimeError, "fictional crash"):
                    simulate_issues(root, 7, checkpoint=interrupt)
                events = {path.name: path.read_bytes() for path in (root / "research" / "events").glob("*.json")}
                result = simulate_issues(root, 7, resume=True)
                self.assertEqual((result["issues"], result["appearances"], result["distinct_new"]), (7, 70, 70))
                self.assertEqual(result["resumed_issues"], completed)
                self.assertTrue(all((root / "research" / "events" / name).read_bytes() == raw
                                    for name, raw in events.items()))
                finished = {path.name: path.read_bytes() for path in (root / "research" / "events").glob("*.json")}
                replay = simulate_issues(root, 7, resume=True)
                self.assertEqual(replay["resumed_issues"], 7)
                self.assertEqual(finished, {path.name: path.read_bytes()
                    for path in (root / "research" / "events").glob("*.json")})

    def test_existing_root_requires_explicit_resume_without_changing_originals(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            simulate_issues(root, 2)
            before = {path.name: path.read_bytes() for path in (root / "research" / "events").glob("*.json")}
            with self.assertRaisesRegex(AssertionError, "explicit resume"):
                simulate_issues(root, 2)
            self.assertEqual(before, {path.name: path.read_bytes()
                for path in (root / "research" / "events").glob("*.json")})

    def test_resume_refuses_changed_synthetic_type_before_any_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = simulate_issues(root, 2)
            memory = result["memory"]
            original = memory.snapshot()["concepts"]["scale-type-00000"]
            changed = deepcopy(original)
            changed["description"]["function"] = "Changed fictional use"
            memory.commit("fictional-mutated-type", [change("concepts", "scale-type-00000", changed, original)])
            before = memory.snapshot_with_provenance()
            with self.assertRaisesRegex(AssertionError, "fictional types changed"):
                simulate_issues(root, 2, resume=True)
            self.assertEqual(memory.snapshot_with_provenance(), before)

    def test_resume_refuses_incomplete_publication_prefix_before_any_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = simulate_issues(root, 2)
            memory = result["memory"]
            rid, original = next(iter(memory.snapshot()["published_history"].items()))
            changed = deepcopy(original)
            changed["ordinal"] = 99
            memory.commit("fictional-mutated-appearance", [change("published_history", rid, changed, original)])
            before = memory.snapshot_with_provenance()
            with self.assertRaisesRegex(AssertionError, "publication rows are missing"):
                simulate_issues(root, 2, resume=True)
            self.assertEqual(memory.snapshot_with_provenance(), before)

    def test_resume_refuses_chained_reservation_only_mutation_before_any_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = simulate_issues(root, 2)
            memory = result["memory"]
            iid, original = next(iter(memory.snapshot()["issue_reservations"].items()))
            changed = deepcopy(original)
            changed["input_hash"] = "f" * 64
            memory.commit("fictional-mutated-reservation", [change("issue_reservations", iid, changed, original)])
            before = memory.snapshot_with_provenance()
            with self.assertRaisesRegex(AssertionError, "producer-event footprint changed"):
                simulate_issues(root, 2, resume=True)
            self.assertEqual(memory.snapshot_with_provenance(), before)

    def test_explicit_monthly_provider_renewals_preserve_original_uncertainty_across_the_calendar(self):
        # Reuse the established fake-account construction, not its test class
        # as an imported discovery target. The quoted grants are supplied
        # fictional facts, never an assertion about real provider allowances.
        import test_account_pool as fixtures
        fixture = fixtures.AccountPoolTests()
        with patch.object(fixtures, "datetime") as fake_clock:
            fake_clock.now.return_value = datetime(2026, 9, 1, tzinfo=timezone.utc)
            fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        old = fixture.unknown(attempt="scale-original-uncertain")
        original = fixture.accounts["account1"]
        entry = fixture.registry["accounts"][0]
        slots, windows = calendar_slots(2000)
        journal = fixture.journal
        calls, namespaces = [], set()

        class AuthenticatedFictionalAccount:
            def clock(inner): return 0
            def run_list(inner):
                self.assertFalse(journal._locked)
                calls.append("runs")
                return {"data": {"items": []}}
            def identity(inner):
                self.assertFalse(journal._locked)
                calls.append("identity")
                return {"data": {"id": "fictional-provider-1"}}

        for window in windows:
            first = date.fromisoformat(window["issues"][0]["date"]).replace(day=1)
            now = datetime.combine(first, datetime.min.time(), tzinfo=timezone.utc)
            stop = next_month(first)
            prior_bytes = journal.path.read_bytes()
            with journal.locked():
                prior_budgets, prior_attempts = journal.state()
            # Merely planning another calendar month grants no balance and
            # cannot settle an original provider call whose charge is unknown.
            calendar_plan(first.isoformat(), stop.isoformat(), "0")
            self.assertEqual(journal.path.read_bytes(), prior_bytes)
            facts = load_json(fixture.root / entry["handover"])
            facts.update(period="synthetic-" + first.strftime("%Y-%m"),
                period_ends_at=datetime.combine(stop, datetime.min.time(), tzinfo=timezone.utc).isoformat(),
                confirmed_at=now.isoformat(), balance_checked_at=now.isoformat())
            atomic_json(fixture.root / entry["handover"], facts)
            current = bind_registered_account(fixture.root, journal, "account1", lambda _token: AuthenticatedFictionalAccount(),
                now=now, registry=fixture.registry)
            self.assertNotIn(current.namespace, namespaces)
            namespaces.add(current.namespace)
            with journal.locked():
                budgets, attempts = journal.state()
            self.assertEqual(len(budgets), len(prior_budgets) + 1)
            self.assertEqual(attempts, prior_attempts)
            self.assertEqual(attempts[old.attempt_id]["namespace"], original.namespace)
            self.assertIsNone(attempts[old.attempt_id]["charge"])
            self.assertEqual(attempts[old.attempt_id]["read_hold"], "0.02")
            origin = load_json(fixture.root / "config/local/apify_origins" / old.attempt_id / "origin.json")
            recovered = context_for_origin(fixture.root, fixture.registry, origin, now=now)
            self.assertEqual(recovered["binding"].namespace, original.namespace)
            self.assertEqual(recovered["binding"].generation, original.generation)
            self.assertNotEqual(current.namespace, recovered["binding"].namespace)
            # A genuinely cold financial reader must retain the old origin,
            # every separate supplied period and every still-unknown charge.
            journal = Journal(fixture.root / "finance")
            with journal.locked():
                self.assertEqual(journal.state(), (budgets, attempts))
        self.assertEqual(len(calls), len(windows) * 2)
        self.assertEqual(len(namespaces), len(windows))
        self.assertEqual(len(slots), 2000)

    @unittest.skipUnless(os.environ.get("RADAR_SCALE_ACCEPTANCE") == "1",
        "Long real-producer proof: run RADAR_SCALE_ACCEPTANCE=1 with this focused module explicitly.")
    def test_actual_allocator_and_publication_producers_keep_two_thousand_issues_new(self):
        explicit_root = os.environ.get("RADAR_SCALE_ROOT")
        context = nullcontext(explicit_root) if explicit_root else tempfile.TemporaryDirectory()
        with context as temporary:
            started = time.monotonic()
            result = simulate_issues(Path(temporary), 2000, progress=lambda count: print(
                f"Real-producer scale proof: {count}/2000 issues ({count / 20:.0f}%), "
                f"{time.monotonic() - started:.1f} seconds.", flush=True),
                resume=os.environ.get("RADAR_SCALE_RESUME") == "1")
            self.assertEqual(result["issues"], 2000)
            self.assertEqual(result["appearances"], 20000)
            self.assertEqual(result["distinct_new"], 20000)
            self.assertGreaterEqual(result["distinct_new"], 16000)
            self.assertGreaterEqual(result["reboots"], 7)
            self.assertGreater(result["missed_receipt_ticks"], 1)
            history = result["state"]["published_history"]
            self.assertEqual(len({row["issue_id"] for row in history.values()}), 2000)
            self.assertEqual({row["ordinal"] for row in history.values()}, set(range(1, 2001)))
            first, last = (date.fromisoformat(result["slots"][index]["date"]) for index in (0, -1))
            self.assertGreater((last - first).days, 12 * 365)
            self.assertLess((last - first).days, 14 * 366)
            self.assertTrue(all(date.fromisoformat(slot["date"]).weekday() in (1, 3, 5) for slot in result["slots"]))
            self.assertTrue(all(timestamp(slot["scheduled_at"]).astimezone(
                ZoneInfo("Asia/Kolkata")).strftime("%H:%M") == "09:00" for slot in result["slots"]))
            self.assertTrue(all(12 <= len(window["issues"]) <= 14 for window in result["windows"]))
            self.assertTrue(all(Decimal(window["unallocated_usd"]) == 0 for window in result["windows"]))
            # A 2,001st logical issue can continue. Both oldest and latest
            # supplied rows remain seen rather than becoming New again.
            last_products = result["state"]["issue_reservations"][result["slots"][-1]["issue_id"]]["products"]
            upcoming = release_slots((last - timedelta(days=last.weekday()) + timedelta(days=7)).isoformat())[0]
            fresh = fictional_types(20000, 10)
            follow = allocate_issues(fresh + result["old_examples"] + deepcopy(last_products), [upcoming], history,
                result["state"]["issue_reservations"], load_policy(), identity_state=result["state"])["issues"][0]
            self.assertEqual(follow["counts"], {"products": 10, "new": 10, "updates": 0})
            self.assertEqual({row["concept_id"] for row in follow["products"]}, {row["concept_id"] for row in fresh})
            self.assertEqual(draft_ordinal(upcoming["issue_id"], upcoming["date"], history,
                result["state"]["issue_reservations"]), 2001)
            print({"synthetic_issues": result["issues"], "actual_saved_appearances": result["appearances"],
                "distinct_new_types": result["distinct_new"], "cold_restarts": result["reboots"],
                "monthly_calendar_windows": len(result["windows"]),
                "first_release": result["slots"][0]["scheduled_at"],
                "last_release": result["slots"][-1]["scheduled_at"],
                "canonical_head": result["memory"].snapshot_with_provenance()["head"],
                "synthetic_root": str(Path(temporary)), "resumed_original_issues": result["resumed_issues"],
                "original_head": result["original_head"],
                "seconds": round(time.monotonic() - started, 3)}, flush=True)


if __name__ == "__main__":
    unittest.main()
