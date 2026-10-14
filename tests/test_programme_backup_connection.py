"""Optional backup hooks stay outside research authority and progress."""

from copy import deepcopy
from io import StringIO
import json
import unittest
from unittest.mock import patch

import test_programme as fixtures
from product_radar.models import SafetyError
from product_radar.programme import (STAGES, _refresh_owner_backup, _save, _signature, load_programme,
                                    run_programme_housekeeping, set_programme_service_state)
from product_radar.research_memory import key
from product_radar.storage import Journal
from product_radar.work_turns import (bind_work_turn, close_work_turn, open_work_turn,
                                      reserve_source_callback, reserve_stage_items)


class ProgrammeBackupTests(unittest.TestCase):
    setUp = fixtures.ProgrammeTests.setUp
    product = fixtures.ProgrammeTests.product
    prepare = fixtures.ProgrammeTests.prepare
    noops = fixtures.ProgrammeTests.noops
    advance = fixtures.ProgrammeTests.advance
    cli = fixtures.ProgrammeCliTests.cli

    def test_saved_cycle_boundary_calls_copy_under_same_programme_guard(self):
        self.prepare(); calls = []
        def copy(_memory, record, context):
            # The saved checkpoint must contain all completed original steps.
            self.assertEqual(load_programme(self.memory, record["programme_id"])["last_results"], record["last_results"])
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                with Journal(self.memory.root / "programme_writer").locked():
                    pass
            calls.append(context["at"])
            return {"state": "saved", "files": 4, "bytes": 300, "destination": "data/backups"}
        value = self.advance(backup_step=copy)
        self.assertEqual(len(calls), 2)  # Cycle and final checks share the daily gate.
        self.assertEqual(value["cycles_this_tick"], 1)
        self.assertEqual(value["owner_summary"]["local_backup"]["files"], 4)
        self.assertEqual(self.memory.snapshot()["published_history"], {})

    def test_backup_failure_does_not_stop_research_or_manufacture_progress(self):
        self.prepare(); done = []
        def step(_memory, _record, _context):
            done.append(True)
            return {"state": "nothing_new"}
        steps = {name: step for name in self.noops()}
        def broken(*_args):
            raise RuntimeError("Do not expose private exception body")
        result = self.advance(steps=steps, backup_step=broken)
        self.assertEqual(len(done), len(STAGES))
        self.assertEqual(result["cycles_this_tick"], 1)
        self.assertEqual(result["owner_summary"]["local_backup"]["reason"], "PROGRAMME_LOCAL_BACKUP_FAILED")
        self.assertNotIn("private", json.dumps(result["owner_summary"]["local_backup"]))

    def test_backup_bookkeeping_is_not_research_progress(self):
        record = self.prepare(); state = self.memory.snapshot()
        changed = deepcopy(record)
        changed["owner_backup_status"] = {"state": "saved", "files": 100, "bytes": 2048}
        self.assertEqual(_signature(state, record), _signature(state, changed))

    def test_owner_pause_prevents_optional_copy(self):
        self.prepare(); set_programme_service_state(self.memory, enabled=False, as_of=self.now)
        with patch("product_radar.operational_backup_schedule.run_due_operational_backup",
                   side_effect=AssertionError("Paused copy")):
            result = self.advance(backup_step=lambda *_args: self.fail("Paused callback"))
        self.assertEqual(result["tick_state"], "owner_paused")

    def test_pause_after_work_is_saved_skips_end_cycle_and_final_copy(self):
        self.prepare()
        def pause(_memory, _record, _context):
            set_programme_service_state(self.memory, enabled=False, as_of=self.now)
            return {"state": "nothing_new"}
        steps = self.noops(); steps["private_staging"] = pause
        self.advance(steps=steps, backup_step=lambda *_args: self.fail("Late-paused copy"))

    def test_read_only_cli_does_not_even_configure_or_check_backup(self):
        self.prepare(); module = self.cli()
        before = self.memory.snapshot()
        with patch("sys.stdout", new=StringIO()), \
             patch("product_radar.operational_backup_schedule.create_backup_housekeeping_step",
                   side_effect=AssertionError("Read-only factory")), \
             patch.object(module, "run_programme_housekeeping", side_effect=AssertionError("Read-only backup")):
            self.assertEqual(module.main(["--programme-id", "whole-programme"], memory=self.memory, root=self.root), 0)
        self.assertEqual(self.memory.snapshot(), before)

    def test_executing_cli_only_configures_callback_and_injects_it(self):
        self.prepare(); module = self.cli(); callback = lambda *_args: self.fail("Construction must not copy")
        with patch("sys.stdout", new=StringIO()), \
             patch("product_radar.operational_backup_schedule.create_backup_housekeeping_step", return_value=callback) as factory, \
             patch.object(module, "advance_programme", return_value={}) as advance:
            self.assertEqual(module.main(["--programme-id", "whole-programme", "--execute"], memory=self.memory,
                root=self.root, handlers={}, steps=self.noops(), discovery_step=lambda *_args: {},
                private_staging=lambda *_args: {}), 0)
        factory.assert_called_once_with(self.root)
        self.assertIs(advance.call_args.kwargs["backup_step"], callback)

    def test_retry_wait_housekeeping_cannot_run_research_or_reset_retry(self):
        record = self.prepare(); record["next_retry_at"] = "9998-10-08T12:00:00+00:00"; _save(self.memory, record)
        before = self.memory.snapshot(); module = self.cli(); output = StringIO(); called = []
        def copy(_memory, current, _context):
            called.append(current["source_run_id"])
            return {"state": "saved", "files": 2, "bytes": 100}
        with patch("sys.stdout", new=output), patch.object(module, "due_programme", return_value=record), \
             patch.object(module, "advance_programme", side_effect=AssertionError("Waiting research")), \
             patch("product_radar.research_ai.CodexRunner", side_effect=AssertionError("Waiting model setup")):
            self.assertEqual(module.main(["--tick", "--execute", "--live-model"], memory=self.memory,
                                        root=self.root, backup_step=copy), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["state"], "waiting_for_saved_retry")
        self.assertEqual(result["local_backup"]["state"], "saved")
        self.assertEqual(called, ["existing-source-run"])
        current = load_programme(self.memory, "whole-programme")
        self.assertEqual(current["next_retry_at"], record["next_retry_at"])
        self.assertEqual(current["cycles"], record["cycles"])
        after = self.memory.snapshot()
        for table in ("captures", "query_tasks", "jobs", "published_history", "issue_reservations"):
            self.assertEqual(after[table], before[table])

    def cooperative_waiting_backup(self, *, fails=False):
        record = self.prepare()
        record["cycles"] = 7
        record["next_retry_at"] = "9998-10-08T12:00:00+00:00"
        record["owner_summary"] = {"as_of": self.now,
            "cost_warnings": [{"code": "ALL_IN_RESEARCH_COST_UNKNOWN"}]}
        _save(self.memory, record)
        turn = open_work_turn(self.memory, record["programme_id"], at=self.now)
        with bind_work_turn(self.memory, turn["id"]):
            reserve_source_callback(self.memory, "already-spent-source", at=self.now)
            reserve_stage_items(self.memory, "photos", "already-spent-photo", 1, at=self.now)
        close_work_turn(self.memory, turn["id"], at=self.now, reason="saved_wait")
        before = self.memory.snapshot()
        with self.memory.journal.locked():
            financial_before = self.memory.journal.export_bytes()
        calls = []
        def copy(memory, current, _context):
            writer_reason = None
            try:
                with Journal(memory.root / "programme_writer").locked():
                    pass
            except SafetyError as error:
                writer_reason = str(error)
            calls.append({"writer_reason": writer_reason,
                "checkpoint_saved": load_programme(memory, current["programme_id"]) == current})
            if fails:
                raise OSError("Private backup failure detail must not appear in the response")
            return {"state": "saved", "files": 2, "bytes": 100, "destination": "data/backups"}
        def forbidden(*_args, **_kwargs):
            self.fail("An all-waiting invocation must not run research or private delivery")
        readiness = {"ready": False, "reason": "PROGRAMME_WAITING_FOR_SAVED_TRIGGER",
            "next_retry_at": record["next_retry_at"], "next_trigger": "saved_input_reply_or_connection_change"}
        module = self.cli(); output = StringIO()
        with patch("sys.stdout", new=output), \
             patch("product_radar.programme.programme_turn_readiness", return_value=readiness), \
             patch("product_radar.programme._refresh_inventory_boundary", side_effect=lambda _memory, current, **_kwargs: current), \
             patch("product_radar.programme._advance_programme_turn_locked", side_effect=forbidden) as execute, \
             patch("product_radar.programme.programme_owner_summary", side_effect=forbidden) as summary, \
             patch.object(module, "advance_ready_programme_turn", wraps=module.advance_ready_programme_turn) as advance, \
             patch.object(module, "run_programme_housekeeping", wraps=module.run_programme_housekeeping) as housekeeping:
            exit_code = module.main(["--programme-id", record["programme_id"], "--execute", "--cooperative-turns"],
                memory=self.memory, root=self.root, runner=forbidden, handlers={"meta-ads": forbidden},
                steps={name: forbidden for name in self.noops()}, discovery_step=forbidden,
                private_staging=forbidden, backup_step=copy)
        self.assertEqual(exit_code, 0)
        advance.assert_called_once()
        housekeeping.assert_called_once()
        execute.assert_not_called()
        summary.assert_not_called()  # Waiting keeps the dated cost summary, not a new billing poll.
        self.assertEqual(calls, [{"writer_reason": "RUNNER_ALREADY_ACTIVE", "checkpoint_saved": True}])
        result = json.loads(output.getvalue())
        self.assertEqual(result["state"], "all_waiting")
        self.assertFalse(result["round_completed"])
        self.assertEqual(result["completed_rounds_this_invocation"], 0)
        self.assertEqual(result["next_retry_at"], record["next_retry_at"])
        self.assertFalse(result["client_publication_attempted"])
        current = load_programme(self.memory, record["programme_id"])
        self.assertEqual({name: value for name, value in current.items() if name != "owner_backup_status"}, record)
        self.assertEqual(current["owner_backup_status"], result["local_backup"])
        programme_key = key("programme", record["programme_id"])
        after = self.memory.snapshot()
        for table, rows in before.items():
            self.assertEqual({name: value for name, value in after[table].items() if table != "batch_runs" or name != programme_key},
                             {name: value for name, value in rows.items() if table != "batch_runs" or name != programme_key}, table)
        with self.memory.journal.locked():
            self.assertEqual(self.memory.journal.export_bytes(), financial_before)
        return result, output.getvalue()

    def test_cooperative_all_waiting_runs_backup_under_writer_without_new_work_or_counter_reset(self):
        result, _output = self.cooperative_waiting_backup()
        self.assertEqual(result["local_backup"]["state"], "saved")
        self.assertEqual(result["local_backup"]["files"], 2)
        self.assertEqual(result["local_backup"]["bytes"], 100)

    def test_cooperative_all_waiting_backup_failure_is_recorded_once_without_looping(self):
        result, output = self.cooperative_waiting_backup(fails=True)
        self.assertEqual(result["local_backup"]["state"], "waiting")
        self.assertEqual(result["local_backup"]["reason"], "PROGRAMME_LOCAL_BACKUP_FAILED")
        self.assertNotIn("Private backup failure detail", output)

    def test_retry_wait_uses_same_writer_guard_and_does_not_steal_busy_leader(self):
        self.prepare(); calls = []
        with Journal(self.memory.root / "programme_writer").locked():
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                run_programme_housekeeping(self.memory, "whole-programme", root=self.root,
                    backup_step=lambda *_args: calls.append(True), as_of=self.now)
        self.assertEqual(calls, [])

    def test_paused_housekeeping_preserves_old_success_and_does_not_copy(self):
        record = self.prepare(); record["owner_backup_status"] = {"state": "saved", "files": 2}; _save(self.memory, record)
        set_programme_service_state(self.memory, enabled=False, as_of=self.now)
        result = run_programme_housekeeping(self.memory, "whole-programme", root=self.root,
            backup_step=lambda *_args: self.fail("Paused housekeeping"), as_of=self.now)
        self.assertEqual(result["state"], "owner_paused")
        self.assertEqual(load_programme(self.memory, "whole-programme")["owner_backup_status"], record["owner_backup_status"])

    def test_external_action_or_independent_backup_claim_is_not_accepted(self):
        record = self.prepare()
        for extra in ({"provider_calls": 1}, {"model_calls": 1}, {"financial_writes": 1},
                      {"independent_backup_proved": True}, {"original_layout_resume_proved": True},
                      {"destination": "other-storage"}):
            status = _refresh_owner_backup(self.memory, record,
                {"at": self.now, "backup_step": lambda *_args: {"state": "saved", **extra}})
            self.assertEqual(status["reason"], "PROGRAMME_BACKUP_RESULT_INVALID")


if __name__ == "__main__":
    unittest.main()
