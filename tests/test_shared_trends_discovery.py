"""Actual term-chart owner joined to real renewable direction questions."""

from copy import deepcopy
import unittest
from unittest.mock import Mock

from product_radar.catalogue_discovery import (_validate_receipt, advance_discovery_cycle,
    discovery_wave_readiness, prepare_next_discovery_wave)
from product_radar.models import SafetyError
from product_radar.storage import load_json
from product_radar.work_turns import bind_work_turn, close_work_turn, open_work_turn
from test_catalogue_expansion import fixture
import test_shared_trends as trends_fixture


class SharedTrendsDirectionTests(unittest.TestCase):
    def setUp(self):
        self.source = trends_fixture.SharedTrendsTests(); self.source.setUp()
        self.addCleanup(self.source.doCleanups)
        self.memory = self.source.memory
        self.catalogue = fixture()
        self.catalogue["families"][0]["phrases"][0].update(
            text="chair cover", source="trends", purpose="validation")
        self.handlers = self.source.handlers()

    def prepare(self, *, programme="programme"):
        self.wave = prepare_next_discovery_wave(self.memory, programme, self.catalogue,
            handlers=self.handlers, as_of=self.source.now)
        return self.wave

    def advance(self):
        admission = Mock(side_effect=AssertionError("A chart must not be sent to product-card admission"))
        turn = open_work_turn(self.memory, self.wave["programme_id"], at=self.source.now)
        try:
            with bind_work_turn(self.memory, turn["id"]):
                result = advance_discovery_cycle(self.memory, self.wave["id"], handlers=self.handlers,
                    admission=admission, shared_marketplace=True, as_of=self.source.now)
            admission.assert_not_called()
            return result
        finally:
            self.last_turn = close_work_turn(self.memory, turn["id"], at=self.source.now,
                reason="Finite synthetic type-level chart test")

    def test_chart_direction_completes_with_real_term_proof_without_product_admission(self):
        self.prepare(); result = self.advance()
        self.assertEqual(result["state"], "checked")
        self.assertEqual(result["external_handler_calls_this_round"], 1)
        self.assertEqual(result["captured_cards"], 0)
        state = self.memory.snapshot()
        action = next(iter(state["batch_runs"][self.wave["id"]]["actions"].values()))
        question = state["query_tasks"][self.wave["planned_actions"][0]["id"]]
        self.assertEqual(question["purpose"], "validation")
        self.assertEqual(question["subject"]["kind"], "catalogue_direction")
        self.assertEqual(question["binding"]["question_options"]["search_scope"], "accepted_direction_demand_context")
        self.assertEqual(action["state"], "acquired")
        self.assertEqual(action["capture_ids"], []); self.assertEqual(action["observation_ids"], [])
        self.assertEqual(set(action["term_capture_ref"]), {"id", "hash"})
        self.assertEqual(question["progress"]["scheduling"], "settled")
        self.assertEqual(state["trends_evidence"], {}); self.assertEqual(state["observations"], {})
        self.assertEqual(list(state["concepts"]), ["chair"])

    def test_a_signed_in_read_that_could_not_start_leaves_the_direction_waiting(self):
        from product_radar.signed_in_attempts import SignedInWait
        retry = "2026-10-06T10:02:00+00:00"
        normal = self.source.capture

        def busy(request, aid, **options):
            self.source.calls.append((aid, request))
            raise SignedInWait("SIGNED_IN_BROWSER_BUSY", next_retry_at=retry, started_at=self.source.now,
                               finished_at=self.source.now)
        self.handlers = self.source.handlers(capture=busy)
        self.prepare(); self.advance()
        action = next(iter(self.memory.snapshot()["batch_runs"][self.wave["id"]]["actions"].values()))
        self.assertEqual((action["state"], action["reason"], action["next_retry_at"]),
                         ("waiting", "SIGNED_IN_BROWSER_BUSY", retry))
        self.assertNotIn("acquisition_id", action)
        (owner,) = self.source.owners().values()
        self.assertEqual(owner["state"], "reserved")  # reopened: nothing was sent
        self.source.now = "2026-10-06T10:05:00+00:00"
        self.handlers = self.source.handlers(capture=normal)
        self.advance()  # the same wave, after the recheck time
        action = next(iter(self.memory.snapshot()["batch_runs"][self.wave["id"]]["actions"].values()))
        self.assertEqual(action["state"], "acquired"); self.assertEqual(len(self.source.calls), 2)

    def test_product_check_first_then_direction_reuses_original_with_zero_callback_slots(self):
        self.source.prepare(); self.source.checks(); self.prepare()
        result = self.advance()
        self.assertEqual(result["external_handler_calls_this_round"], 0)
        self.assertEqual(self.last_turn["counters"]["source_callbacks"], 0)
        self.assertEqual(len(self.source.calls), 1); self.assertEqual(len(self.source.owners()), 1)

    def test_direction_first_then_real_product_check_imports_same_original_chart(self):
        self.prepare(); self.advance()
        self.source.prepare(); result = self.source.checks()
        self.assertEqual(result["counts"], {"acquired": 1})
        self.assertEqual(result["handler_calls"], 0); self.assertEqual(len(self.source.calls), 1)
        self.assertEqual(len(self.memory.snapshot()["trends_evidence"]), 1)

    def test_actual_no_data_completes_collection_but_adds_no_positive_demand(self):
        self.source.mode = "empty"; self.prepare(); result = self.advance()
        self.assertEqual(result["state"], "checked")
        action = next(iter(self.memory.snapshot()["batch_runs"][self.wave["id"]]["actions"].values()))
        self.assertEqual(action["state"], "empty"); self.assertIn("term_capture_ref", action)
        self.assertEqual(self.memory.snapshot()["market_reviews"], {})

    def test_wrong_scope_is_retained_insufficient_not_a_successful_empty_chart(self):
        self.source.mode = "wrong_scope"; self.prepare(); self.advance()
        action = next(iter(self.memory.snapshot()["batch_runs"][self.wave["id"]]["actions"].values()))
        self.assertEqual(action["state"], "insufficient")
        self.assertIn("term_capture_ref", action)
        owner = next(iter(self.source.owners().values()))
        self.assertFalse(self.memory.snapshot()["source_checks"][owner["active_response_id"]]["complete"])

    def test_interrupted_direction_original_is_never_replaced_by_product_consumer(self):
        self.source.mode = "unknown"; self.prepare(); self.advance()
        self.source.mode = "usable"; self.source.prepare(); self.source.checks(); self.advance()
        self.assertEqual(len(self.source.calls), 1); self.assertEqual(len(self.source.owners()), 1)

    def test_forged_term_proof_is_rejected_before_local_application(self):
        self.prepare(); self.advance()
        saved = self.memory.snapshot()["batch_runs"][self.wave["id"]]
        question = saved["planned_actions"][0]["questions"][0]
        action = saved["actions"][question["id"]]
        receipt = load_json(action["reference"])
        receipt["term_capture_ref"]["hash"] = "0" * 64
        with self.assertRaisesRegex(SafetyError, "TERM_PROOF_INVALID"):
            _validate_receipt(self.memory, question, action["acquisition_id"], receipt)
        self.assertEqual(len(self.source.calls), 1)

    def test_new_direction_reuses_fresh_physical_chart_without_claiming_same_type(self):
        self.prepare(); self.advance()
        other = deepcopy(self.catalogue["families"][0])
        other.update(id="elastic-cover", label="Elastic chair cover", definition="An elastic fabric chair cover.")
        self.catalogue["families"].append(other)
        self.prepare(programme="other-programme"); result = self.advance()
        self.assertEqual(result["external_handler_calls_this_round"], 0)
        self.assertEqual(len(self.source.calls), 1); self.assertEqual(len(self.source.owners()), 1)
        self.assertEqual(self.memory.snapshot()["market_reviews"], {})

    def test_due_chart_refresh_is_linked_once_and_retains_original_date(self):
        self.prepare(); self.advance(); original = next(iter(self.source.owners()))
        self.source.now = "2026-10-14T10:00:00+00:00"
        hint = discovery_wave_readiness(self.memory, "programme", self.catalogue,
            handlers=self.handlers, as_of=self.source.now)
        self.assertTrue(hint["new_wave_ready"])
        self.prepare(); self.advance()
        owners = self.source.owners()
        self.assertEqual(len(self.source.calls), 2); self.assertEqual(len(owners), 2)
        latest = max(owners.values(), key=lambda row: row["binding"]["generation"])
        self.assertEqual(latest["binding"]["previous_acquisition_id"], original)
        original_response = self.memory.snapshot()["source_checks"][owners[original]["active_response_id"]]
        self.assertEqual(original_response["observed_at"], "2026-10-06T10:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
