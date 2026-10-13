"""Independent shared-marketplace recovery checks, using synthetic local captures."""

from copy import deepcopy
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import test_shared_marketplace_acquisitions as shared
import test_programme as programme_fixtures
import product_radar.programme as programme
from product_radar.marketplace_browser import capture_marketplace
from product_radar.research_ai import Jobs
from product_radar.source_coverage import create_marketplace_handlers, execute_source_run, prepare_source_tasks
from test_marketplace_browser import snapshot, transport
from test_research_flow import FakeRunner
from product_radar.work_turns import DEFAULT_POLICY, bind_work_turn, close_work_turn, open_work_turn


class SharedMarketplaceReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = shared.SharedMarketplaceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_original_caller_resumes_when_global_intent_save_did_not_happen(self):
        fixture = self.fixture
        handlers = fixture.handlers()
        fixture.prepare(handlers)

        # The caller saved its callback admission, but the installed collector
        # failed before the shared owner could save any dispatch intent.
        with patch("product_radar.source_acquisitions.mark_source_dispatching",
                   side_effect=OSError("Synthetic pre-intent local save failure")):
            fixture.checks(handlers)

        self.assertEqual(fixture.calls, [])
        self.assertEqual(len(fixture.owners()), 1)
        acquisition_id, owner = next(iter(fixture.owners().items()))
        self.assertEqual(owner["state"], "reserved")
        task = next(iter(fixture.memory.snapshot()["query_tasks"].values()))
        self.assertTrue(task["reconcile_required"])
        self.assertEqual(task["generation"], 1)

        # A new callback may resume this exact provably never-dispatched owner.
        # It must not abandon it, mint a replacement, or remain waiting forever.
        result = fixture.checks(handlers)
        self.assertEqual(result["counts"], {"acquired": 1})
        self.assertEqual(len(fixture.calls), 1)
        self.assertEqual(fixture.calls[0][0], acquisition_id)
        self.assertEqual(list(fixture.owners()), [acquisition_id])
        self.assertEqual(next(iter(fixture.memory.snapshot()["query_tasks"].values()))["generation"], 1)

    def test_pre_intent_recovery_needs_a_fresh_callback_slot(self):
        fixture = self.fixture
        handlers = fixture.handlers()
        fixture.prepare(handlers)
        policy = deepcopy(DEFAULT_POLICY)
        policy.update(source_callbacks=1, exploration_slots=0)
        first = open_work_turn(fixture.memory, "programme", at=shared.NOW, policy=policy)
        with bind_work_turn(fixture.memory, first["id"]):
            with patch("product_radar.source_acquisitions.mark_source_dispatching",
                       side_effect=OSError("Synthetic pre-intent local save failure")):
                fixture.checks(handlers)
            fixture.checks(handlers)
        self.assertEqual(fixture.calls, [])
        self.assertEqual(fixture.memory.snapshot()["batch_runs"][first["id"]]["counters"]["source_callbacks"], 1)
        acquisition_id = next(iter(fixture.owners()))
        close_work_turn(fixture.memory, first["id"], at=shared.NOW, reason="Synthetic bounded first turn.")
        following = open_work_turn(fixture.memory, "programme", at=shared.NOW, policy=policy)
        with bind_work_turn(fixture.memory, following["id"]):
            fixture.checks(handlers)
        self.assertEqual([aid for aid, _request in fixture.calls], [acquisition_id])
        self.assertEqual(fixture.memory.snapshot()["batch_runs"][following["id"]]["counters"]["source_callbacks"], 1)

    def test_dispatch_intent_without_result_never_becomes_new_capture(self):
        fixture = self.fixture
        handlers = fixture.handlers(failure=TimeoutError("Synthetic result lost after global intent"))
        fixture.prepare(handlers)
        fixture.checks(handlers)
        acquisition_id = next(iter(fixture.owners()))
        self.assertEqual(fixture.owners()[acquisition_id]["state"], "dispatching")
        for _ in range(2):
            fixture.checks(fixture.handlers())
            fixture.discovery(fixture.handlers())
        self.assertEqual([aid for aid, _request in fixture.calls], [acquisition_id])
        self.assertEqual(list(fixture.owners()), [acquisition_id])

    def test_healthy_other_caller_services_never_started_original_when_target_changes(self):
        fixture = self.fixture
        handlers = fixture.handlers()
        fixture.prepare(handlers)
        with patch("product_radar.source_acquisitions.mark_source_dispatching",
                   side_effect=OSError("Synthetic pre-intent local save failure")):
            fixture.checks(handlers)
        acquisition_id = next(iter(fixture.owners()))
        old = fixture.memory.snapshot()["concepts"]["sink"]
        updated = deepcopy(old)
        updated["description"]["label"] = "Changed assessment target"
        fixture.memory.put("concepts", "sink", updated, previous=old)
        self.assertEqual(fixture.checks(handlers)["handler_calls"], 0)
        result = fixture.discovery(handlers)
        self.assertEqual(result["external_handler_calls_this_round"], 1)
        self.assertEqual([aid for aid, _request in fixture.calls], [acquisition_id])
        self.assertEqual(list(fixture.owners()), [acquisition_id])

    def test_foreign_sparse_scope_is_not_silently_rewritten_to_india(self):
        fixture = self.fixture
        handlers = fixture.handlers()
        fixture.prepare(handlers, request={"query": "sink caddy", "country": "US", "limit": 30})
        result = fixture.checks(handlers)
        self.assertEqual(fixture.calls, [])
        self.assertEqual(fixture.owners(), {})
        self.assertEqual(result["results"][0]["reason"], "SOURCE_MARKETPLACE_SPARSE_SCOPE_UNSUPPORTED")


class CooperativeMarketplaceWakeReviewTests(unittest.TestCase):
    def setUp(self):
        self.programme_fixture = programme_fixtures.ProgrammeTests()
        self.programme_fixture.setUp()
        self.addCleanup(self.programme_fixture.doCleanups)
        self.programme_fixture.now = shared.NOW
        self.programme_fixture.prepare()
        self.fixture = shared.SharedMarketplaceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.memory = self.programme_fixture.memory
        self.fixture.source_root = self.fixture.memory.root / "direct_marketplace"

    def test_original_marketplace_receipt_arrival_wakes_settled_programme(self):
        fixture, programme_fixture = self.fixture, self.programme_fixture
        held = programme_fixture.root / "held-original"
        def capture_then_hold(request, acquisition_id, **options):
            fixture.calls.append((acquisition_id, deepcopy(request)))
            with patch("product_radar.marketplace_browser.utc_now", return_value=shared.NOW):
                capture_marketplace(request, acquisition_id, **options,
                    snapshot_reader=lambda req: transport(snapshot(req), navigation_started_at=shared.NOW))
            # Hold only this temporary fixture's original artifacts, so their
            # later arrival changes no canonical records or original content.
            Path(options["data_root"], acquisition_id).rename(held)
            raise TimeoutError("Synthetic original files have not arrived")
        handlers = create_marketplace_handlers(fixture.memory, data_root=fixture.source_root,
            capture=capture_then_hold, shared_acquisitions=True)
        prepare_source_tasks(fixture.memory, [{"route": "amazon-search", "request": {"query": "sink caddy"},
            "concept_ids": ["first"]}], run_id="existing-source-run", as_of=shared.NOW)
        steps = programme_fixture.noops()
        steps["acquire"] = lambda memory, _record, _context: execute_source_run(memory, "existing-source-run",
            handlers=handlers, as_of=shared.NOW, shared_marketplace=True,
            bounded_call=lambda callback, context, _timeout: callback(context))
        options = {"root": programme_fixture.root, "programme_id": "whole-programme", "as_of": shared.NOW,
                   "handlers": handlers, "steps": steps}
        programme.advance_ready_programme_turn(fixture.memory, **options)
        programme.advance_ready_programme_turn(fixture.memory, **options)
        settled = programme.advance_ready_programme_turn(fixture.memory, **options)
        self.assertFalse(settled["round_completed"])
        acquisition_id, request = fixture.calls[0]
        before = fixture.memory.snapshot()

        # Only the original browser artifacts arrive, without a canonical
        # journal write or a replacement collection. A wake hint is not proof
        # of usable evidence; the installed reconciler must still validate it.
        shutil.copytree(held, fixture.source_root / acquisition_id, dirs_exist_ok=True)
        self.assertEqual(fixture.memory.snapshot(), before)
        record = programme.load_programme(fixture.memory, "whole-programme")
        readiness = programme.programme_turn_readiness(fixture.memory, record, root=programme_fixture.root,
            handlers=handlers, steps=steps, as_of=shared.NOW)
        self.assertTrue(readiness["ready"], "A newly arrived original source receipt must wake saved-only recovery.")
        recovered = programme.advance_ready_programme_turn(fixture.memory, **options)
        self.assertTrue(recovered["round_completed"])
        self.assertEqual(recovered["work_turn"]["counters"]["source_callbacks"], 0)
        task = next(iter(fixture.memory.snapshot()["query_tasks"].values()))
        self.assertEqual(task["status"], "acquired")
        self.assertEqual([aid for aid, _request in fixture.calls], [acquisition_id])

    def test_original_observation_description_reply_wakes_its_own_programme(self):
        memory, fixture = self.fixture.memory, self.programme_fixture
        runner = FakeRunner()
        supplied = {"items": [{"id": "observation-not-a-product-id", "source_text": {"title": "Blue sink caddy"}}]}
        origin = open_work_turn(memory, "whole-programme", at=shared.NOW)
        with bind_work_turn(memory, origin["id"]):
            job_id, _result = Jobs(memory, runner).run("A", supplied)
        close_work_turn(memory, origin["id"], at=shared.NOW, reason="Synthetic original description turn.")
        folder = memory.root / "ai_jobs" / job_id
        held = folder / "held-result.json"
        (folder / "result.json").rename(held)
        options = {"root": fixture.root, "programme_id": "whole-programme", "as_of": shared.NOW,
                   "handlers": {}, "steps": fixture.noops()}
        programme.advance_ready_programme_turn(memory, **options)
        self.assertFalse(programme.advance_ready_programme_turn(memory, **options)["round_completed"])
        held.rename(folder / "result.json")
        record = programme.load_programme(memory, "whole-programme")
        readiness = programme.programme_turn_readiness(memory, record, root=fixture.root,
            handlers={}, steps=fixture.noops(), as_of=shared.NOW)
        self.assertTrue(readiness["ready"], "Original A-stage replies use observation IDs, not product IDs.")
        applied = []
        steps = fixture.noops()
        def saved_only(memory, _record, _context):
            applied.append(Jobs(memory, runner).run("A", supplied)[0])
            return {"state": "saved_original_reply_applied"}
        steps["profiles"] = saved_only
        recovered = programme.advance_ready_programme_turn(memory, **dict(options, steps=steps))
        self.assertTrue(recovered["round_completed"])
        self.assertEqual(applied, [job_id])
        self.assertEqual(recovered["work_turn"]["counters"]["model_executions"], 0)
        self.assertEqual(len(runner.calls), 1)


if __name__ == "__main__":
    unittest.main()
