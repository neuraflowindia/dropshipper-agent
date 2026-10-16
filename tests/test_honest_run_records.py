"""Honest run records (R18a): platform page visits, Dropdash sign-in signals,
one failure per incident, real access blocks and harmless diagnostics.

Saved fixture pages and in-memory rows only; no website, browser or model.
"""

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from product_radar import dropship_platforms as platforms, operations, platform_signed_in as signed
from product_radar.run_log_summary import summarise_lines
from product_radar.runtime_storage import runtime_path
from product_radar.signed_in_browser import note_sign_in_window, record_site_observation, site_states
from product_radar.source_coverage import _fresh_read, _outcome_log, _single_concept
import test_dropship_platforms as base  # its fixtures only: its own tests are not run again here

CONCEPT = "concept-" + "c" * 40


def run_rows(root, recorder):
    path = runtime_path(root, "data/operations/research-runs") / (recorder.run_id + ".jsonl")
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class PlatformReadRowTests(base.PlatformCase):
    def test_every_feed_request_is_one_row_with_page_kind_seconds_and_items(self):
        with operations.operation_run(self.pacing_root) as recorder:
            self.advance(self.feed(), limit=3)
        rows = [row for row in run_rows(self.pacing_root, recorder) if row["event"] == "platform_read"]
        self.assertEqual([(row["route"], row["page_kind"], row["cycle_kind"], row["state"]) for row in rows],
                         [("deodap", "collections", "full", "read"), ("deodap", "products", "full", "read"),
                          ("deodap", "collection", "lists", "read")])
        self.assertTrue(all(row["http_status"] == 200 and "read_seconds" in row["timings"] for row in rows))
        self.assertEqual(rows[0]["counts"], {"items_found": 0, "next_pages": 1})
        self.assertGreater(rows[1]["counts"]["items_found"], 0)
        self.assertNotIn("url", json.dumps(rows))
        self.assertNotIn("signed_in", rows[0])  # a public feed has no sign-in

    def test_a_refused_feed_is_an_access_block_not_a_failure(self):
        with operations.operation_run(self.pacing_root) as recorder:
            self.advance(self.feed(fetcher=base.FeedDouble(base.deodap_pages(), status=429)))
        rows = [row for row in run_rows(self.pacing_root, recorder) if row["event"] == "platform_read"]
        self.assertEqual([(row["state"], row["reason"], row["http_status"]) for row in rows],
                         [("failed", "DROPSHIP_FEED_REFUSED", 429)])
        self.assertEqual((recorder.counts["failures"], recorder.counts["access_blocks"]), (0, 1))

    def test_platform_visits_count_as_blocks_waits_failures_or_nothing(self):
        def count(**row):
            counted = operations.classify_row("platform_read", row)
            return [name for name in ("failure", "block", "wait") if counted[name]]
        self.assertEqual(count(state="failed", reason="DROPSHIP_FEED_UNAVAILABLE", http_status=403), ["block"])
        self.assertEqual(count(state="failed", reason="OWNER_SIGN_IN_REQUIRED", signed_in=False), ["wait"])
        self.assertEqual(count(state="failed", reason="DROPSHIP_PLAN_REQUIRED"), ["wait"])
        self.assertEqual(count(state="waiting", reason="SIGNED_IN_READ_SPACING"), ["wait"])
        # A category page skipped for the week is no failure of the platform.
        self.assertEqual(count(state="page_skipped", reason="DROPSHIP_PAGE_LAYOUT_UNRECOGNISED"), [])
        self.assertEqual(count(state="read"), [])
        self.assertEqual(count(state="failed", reason="DROPSHIP_PAGE_READ_FAILED"), ["failure"])


class DropdashSignInTests(base.PlatformCase):
    route = "dropdash"

    def setUp(self):
        super().setUp()
        self.pages = dict(base.fixture("dropdash-signed-in-pages-2026-10-16.json"),
                          **base.fixture("dropdash-signed-in-edge-cases-synthetic-2026-10-16.json"))

    def sign_in(self):
        note_sign_in_window(self.pacing_root, True, opened=True, at=base.START)
        note_sign_in_window(self.pacing_root, False, at=base.START)

    def test_a_capture_that_failed_after_landing_on_the_sign_in_page_is_the_sign_in_wall(self):
        self.sign_in()
        record_site_observation(self.pacing_root, self.route, True, route=self.route, observed_at=base.START)

        def sent_to_sign_in(url, expression, folder, **options):
            (folder / "native_result.json").write_text("{}")
            return {"state": "failed", "reason": "Native browser capture failed", "navigation_started": True,
                    "snapshot": None, "primary_frame_id": "F1",
                    "requests": [{"url": base.HOME, "status": 200, "frame": "F1"},
                                 {"url": "https://api.razorpay.com/v1/checkout/public", "status": 200, "frame": "F2"},
                                 {"url": "https://app.dropdash.co/signin", "status": 200, "frame": "F1"}]}
        reader = signed.SignedInReader(self.route, pacing_root=self.pacing_root, browser=sent_to_sign_in)
        with operations.operation_run(self.pacing_root) as recorder:
            outcome = self.advance(reader, self.route)
        self.assertEqual((outcome["state"], outcome["reason"]), ("waiting", "OWNER_SIGN_IN_REQUIRED"))
        self.assertEqual(site_states(self.pacing_root)[self.route]["state"], "expired")
        row = next(row for row in run_rows(self.pacing_root, recorder) if row["event"] == "platform_read")
        self.assertEqual((row["route"], row["state"], row["reason"], row["signed_in"]),
                         ("dropdash", "failed", "OWNER_SIGN_IN_REQUIRED", False))

    def test_a_failed_capture_elsewhere_stays_a_read_failure(self):
        self.sign_in()

        def failed(url, expression, folder, **options):
            (folder / "native_result.json").write_text("{}")
            return {"state": "failed", "reason": "Native browser capture failed", "navigation_started": True,
                    "snapshot": None, "primary_frame_id": "F1", "requests": [{"url": base.HOME, "status": 200, "frame": "F1"}]}
        outcome = self.advance(signed.SignedInReader(self.route, pacing_root=self.pacing_root, browser=failed), self.route)
        self.assertEqual(outcome["reason"], "DROPSHIP_PAGE_READ_FAILED")

    def test_the_account_greeting_counts_as_signed_in_and_its_name_is_still_removed(self):
        self.sign_in()
        page = dict(deepcopy(self.pages["unrecognised"]), text="Welcome, Asha\nNothing else on this page yet.", cards=[])

        def greeting(url, expression, folder, **options):
            (folder / "native_result.json").write_text("{}")
            return {"state": "captured", "navigation_started": True, "snapshot": dict(page, url=url)}
        with operations.operation_run(self.pacing_root) as recorder:
            outcome = self.advance(signed.SignedInReader(self.route, pacing_root=self.pacing_root, browser=greeting),
                                   self.route)
        self.assertEqual(outcome["reason"], "DROPSHIP_PAGE_LAYOUT_UNRECOGNISED")
        self.assertEqual(site_states(self.pacing_root)[self.route]["state"], "signed_in")
        row = next(row for row in run_rows(self.pacing_root, recorder) if row["event"] == "platform_read")
        self.assertIs(row["signed_in"], True)
        saved = b"".join(path.read_bytes() for path in platforms.platform_folder(self.store, self.route).rglob("*")
                         if path.is_file())
        self.assertNotIn(b"Asha", saved)
        self.assertNotIn(b"Asha", json.dumps(run_rows(self.pacing_root, recorder)).encode())

    def test_a_signed_out_page_that_says_welcome_is_not_signed_in(self):
        for header in ("Welcome, Guest\nBrowse products", "Welcome, sellers\nLogin\nSign up", "Welcome,\nRegister now"):
            with self.subTest(header=header):
                self.assertIsNone(signed.signed_in_marker({"text": header + "\nNothing else here.", "cards": []}))
        self.assertIs(signed.signed_in_marker({"text": "B2B Marketplace\nDD Id\nWelcome, [name removed]\nHome",
                                               "cards": []}), True)

    def test_markers_for_the_sign_in_page_address_and_unclear_reads(self):
        self.assertIs(signed.signed_in_marker({"text": "Welcome, [name removed]\nFind products", "cards": []}), True)
        self.assertIs(signed.signed_in_marker({"text": "Welcome, sign in with your password", "password": True}), False)
        self.assertIsNone(signed.signed_in_marker({"text": "x" * 700 + "\nWelcome, someone", "cards": []}))
        result = {"navigation_started": True, "primary_frame_id": None,
                  "requests": [{"url": "https://app.dropdash.co/login?next=/home"}]}
        self.assertTrue(signed.landed_on_sign_in(result))
        self.assertFalse(signed.landed_on_sign_in(dict(result, navigation_started=False)))
        self.assertFalse(signed.landed_on_sign_in(dict(result, requests=[])))
        self.assertFalse(signed.landed_on_sign_in(dict(result, requests=[{"url": "http://[bad"}])))
        self.assertFalse(signed.landed_on_sign_in(None))


class CountingRuleTests(unittest.TestCase):
    def summary(self, rows):
        lines = [json.dumps(dict({"at": "2026-10-17T00:00:00+00:00", "run_id": "run-a"}, **row)) for row in rows]
        return summarise_lines(lines, None)[0]

    def test_one_failed_website_action_is_one_failure_not_three(self):
        rows = [{"event": "source_action_failed", "task_id": "t1", "error_type": "SourceActionDeadline",
                 "error_reported": True, "phase": "transport"},
                {"event": "source_outcome", "task_id": "t1", "phase": "transport", "state": "failed",
                 "reason": "SOURCE_ACTION_TIMEOUT", "actual_attempt": True},
                {"event": "programme_progress", "task_id": "t1", "phase_state": "source_result", "state": "failed",
                 "reason": "SOURCE_ACTION_TIMEOUT"},
                # A raised action whose outcome was saved as "blocked" is still a failure, never a block.
                {"event": "source_action_failed", "task_id": "t2", "error_type": "SafetyError", "error_reported": True},
                {"event": "source_outcome", "task_id": "t2", "phase": "handler", "state": "blocked",
                 "reason": "SOURCE_ACTION_FAILED", "actual_attempt": True, "fresh_read": True, "http_status": 403},
                # An AI call that failed: its phase end is an echo of the one ai_use_finished row.
                {"event": "phase_finished", "phase_state": "runner_call_failed", "error_type": "OSError"},
                {"event": "ai_use_finished", "state": "failed", "reason": "AI_LOCAL_EXECUTION_FAILED"}]
        summary = self.summary(rows)
        self.assertEqual((summary["failures"], summary["access_blocks"], summary["waits"]), (3, 0, 0))
        self.assertEqual(dict(summary["failure_reasons"]), {"SOURCE_ACTION_TIMEOUT": 1, "SOURCE_ACTION_FAILED": 1,
                                                            "AI_LOCAL_EXECUTION_FAILED": 1})
        with operations.operation_run(Path(self._tmp())) as recorder:
            for row in rows:
                recorder.emit(row.pop("event"), **row)
        self.assertEqual((recorder.counts["failures"], recorder.counts["access_blocks"]), (3, 0))

    def test_the_lookup_copy_size_limit_is_neither_a_wait_nor_the_last_reason(self):
        harmless = {"event": "research_lookup_save_outcome", "state": "waiting", "reason": "RESEARCH_INDEX_SIZE_LIMIT"}
        self.assertEqual(self.summary([harmless])["waits"], 0)
        with operations.operation_run(Path(self._tmp())) as recorder:
            recorder.emit("source_outcome", state="waiting", reason="SOURCE_ACCESS_WAIT")
            recorder.emit("phase_finished", phase="research_lookup_save", state="waiting",
                          reason="RESEARCH_INDEX_SIZE_LIMIT")
        self.assertEqual((recorder.last_reason, recorder.counts["deferred"]), ("SOURCE_ACCESS_WAIT", 1))

    def test_a_previous_run_outcome_skips_the_harmless_reason_unless_it_stopped_the_run(self):
        root = Path(self._tmp())
        with operations.operation_run(root) as recorder:
            recorder.emit("source_outcome", state="waiting", reason="SOURCE_ACCESS_WAIT")
            recorder.emit("research_lookup_save_outcome", state="waiting", reason="RESEARCH_INDEX_SIZE_LIMIT")
        outcome = operations.run_outcome(root, recorder.identity["pid"], since=recorder.started_at)
        self.assertEqual(outcome["last_reason"], "SOURCE_ACCESS_WAIT")

    def test_once_per_round_rows_repeat_only_in_a_new_round(self):
        self.assertFalse(operations.record_event_once_per_round("ai_use_held", "X", state="waiting"))  # no run
        with operations.operation_run(Path(self._tmp())) as recorder:
            written = [operations.record_event_once_per_round("ai_use_held", reason, state="waiting", reason=reason)
                       for reason in ("AI_USAGE_READ_FAILED", "AI_USAGE_READ_FAILED", "AI_PAID_CREDITS_USED")]
            operations.record_event("programme_order", programme_id="programme-1")
            written.append(operations.record_event_once_per_round("ai_use_held", "AI_USAGE_READ_FAILED", state="waiting"))
        self.assertEqual(written, [True, False, True, True])

    def test_new_whitelisted_fields_keep_the_safety_rules(self):
        values = operations._fields({"purpose": "C:product_query_profile", "mode": "fit_assessment",
                                     "prompt_version": "research-e-fit10-v2", "page_kind": "page",
                                     "cycle_kind": "lists", "item_count": 3, "input_tokens": 15000,
                                     "model_seconds": 41.25, "http_status": 403, "signed_in": False,
                                     "fresh_read": True, "accounting_saved": True,
                                     "mode_text": "free text", "input_tokens_note": "x",
                                     "output_tokens": -1, "prompt": "Use only the supplied data.",
                                     "page_kind_bad": "has spaces"})
        self.assertEqual(values, {"purpose": "C:product_query_profile", "mode": "fit_assessment",
                                  "prompt_version": "research-e-fit10-v2", "page_kind": "page", "cycle_kind": "lists",
                                  "item_count": 3, "input_tokens": 15000, "model_seconds": 41.25, "http_status": 403,
                                  "signed_in": False, "fresh_read": True, "accounting_saved": True})
        self.assertNotIn("mode", operations._fields({"mode": "two words"}))

    def _tmp(self):
        import tempfile
        temporary = tempfile.TemporaryDirectory(prefix="honest-records-")
        self.addCleanup(temporary.cleanup)
        return temporary.name


class SourceOutcomeLogTests(unittest.TestCase):
    def state(self, response):
        return {"batch_runs": {"source-acquisition-1": {"active_response_id": "response-1"}},
                "source_checks": {"response-1": response}}

    def test_fresh_read_http_answer_and_reason_code(self):
        state = self.state({"access": {"http_status": 403}, "parse_reason": "SUPPLIER_ROUTE_CHALLENGE"})
        self.assertEqual(_outcome_log(state, "source-acquisition-1", {"reason": "blocked"}, fresh=True),
                         {"fresh_read": True, "http_status": 403, "reason": "SUPPLIER_ROUTE_CHALLENGE"})
        worded = self.state({"access": {"http_status": 200}})
        self.assertEqual(_outcome_log(worded, "source-acquisition-1",
                                      {"reason": "Meta displayed an error or access challenge, not an empty search."},
                                      fresh=True)["reason"], "SOURCE_PAGE_CHALLENGE")
        self.assertIsNone(_outcome_log(worded, "source-acquisition-1", {"reason": "usable"}, fresh=False)["reason"])
        self.assertEqual(_outcome_log({"batch_runs": {}, "source_checks": {}}, None, {}, fresh=False),
                         {"fresh_read": False})

    def test_meta_worded_http_refusal_and_a_broken_lookup_never_raise(self):
        worded = self.state({})
        self.assertEqual(_outcome_log(worded, "source-acquisition-1", {"reason": "Meta returned HTTP 429."}, fresh=True)
                         ["http_status"], 429)
        self.assertEqual(_outcome_log({"batch_runs": None}, "source-acquisition-1", {}, fresh=True), {"fresh_read": True})

    def test_replayed_answers_are_never_fresh_reads(self):
        def fresh(**facts):
            values = dict({"local_saved": False, "staged": False, "reconciling": False, "continued": False,
                           "result": {}}, **facts)
            return _fresh_read(**values)
        self.assertTrue(fresh())
        self.assertFalse(fresh(local_saved=True))
        self.assertFalse(fresh(staged=True))  # a staged reply replayed after a restart
        self.assertFalse(fresh(reconciling=True))  # an earlier dispatch's saved receipt
        self.assertTrue(fresh(reconciling=True, continued=True))  # a company page resumed by a real read
        self.assertFalse(fresh(staged=True, reconciling=True, continued=True))
        self.assertFalse(fresh(result={"reused": True}))

    def test_a_question_about_one_product_carries_its_concept_id(self):
        self.assertEqual(_single_concept({"targets": {CONCEPT: "hash"}}), CONCEPT)
        self.assertIsNone(_single_concept({"targets": {CONCEPT: "a", "concept-" + "d" * 40: "b"}}))
        self.assertIsNone(_single_concept(None))


if __name__ == "__main__":
    unittest.main()
