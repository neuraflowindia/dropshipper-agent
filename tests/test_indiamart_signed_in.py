"""IndiaMART read in the owner's signed-in research profile (Plan R10 W3).

The installed shared supplier route runs end to end against an offline double
of the native browser serving the synthetic signed-in results page (with the
research account's own details on it) and the saved login wall. Nothing
launches a browser or visits a site; sign-ins, the desktop and pacing are
local records in a temporary folder.
"""

from datetime import timedelta
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from product_radar import signed_in_pacing as pacing
from product_radar.indiamart_redaction import QUARANTINE
from product_radar.models import SafetyError, digest, timestamp
from product_radar.native_public_browser import signed_in_budget
from product_radar.signed_in_browser import note_desktop_unavailable, note_sign_in_window, site_states
from product_radar.source_coverage import (SIGNED_IN_ACCESS_IDENTITY, SIGNED_IN_TRANSPORT, acquisition_access_scope,
    execute_source_run, prepare_source_tasks, source_access_scope, source_access_wait)
from product_radar.source_deadline import SourceActionDeadline
from product_radar.storage import atomic_json, load_json
from product_radar.supplier_route_acquisition import (_DOM, MAX_RESULTS, _ready_expression, create_supplier_handlers,
    supplier_route_url)
from product_radar.supplier_search import NOT_STARTED, current_supplier_search_checks, supplier_request_for_state
import test_shared_supplier_search as shared
from test_indiamart_results import QUERY, SECRETS, WALL, page, visitor_page


class IndiaMartSignedInRouteTests(unittest.TestCase):
    setUp_shared = shared.SharedSupplierTests.setUp
    owners = shared.SharedSupplierTests.owners
    reader = shared.SharedSupplierTests.reader  # plain and throwaway reads must stay unused
    browser = shared.SharedSupplierTests.browser

    def setUp(self):
        self.setUp_shared()
        self.query = QUERY
        self.page, self.native_calls, self.native_state = visitor_page(), [], "captured"

    # The owner's actions and the local records they leave.

    def sign_in(self, at=None):
        """The owner opens the sign-in window, signs in and closes it."""
        note_sign_in_window(self.signed_root, True, opened=True, at=at or self.at)
        note_sign_in_window(self.signed_root, False, at=at or self.at)

    def native(self, url, expression, folder, **options):
        """An offline double of the native browser's signed-in read."""
        self.native_calls.append({"url": url, "expression": expression, **options})
        folder.mkdir(parents=True, exist_ok=True)
        if self.native_state == "desktop-locked":
            note_desktop_unavailable(options["root"], at=self.at)
            output = {"state": "waiting", "reason": "OWNER_DESKTOP_UNAVAILABLE", "navigation_started": False,
                      "stage": "browser_launch", "snapshot": None, "completed_at": self.at}
            atomic_json(folder / "native_result.json", output)
            return output
        if self.native_state == "busy":
            raise SafetyError("SIGNED_IN_BROWSER_BUSY")  # raised only before anything launches
        if self.native_state == "refused":
            raise SafetyError("NATIVE_PUBLIC_ARTIFACT_EXISTS")  # raised only before anything launches
        snapshot = {"url": url, "html": self.page, "text": re.sub(r"<[^>]+>", " ", self.page), "title": "IndiaMART",
                    "links": []}
        output = {"state": "captured", "navigation_started": True, "navigation_started_at": self.at,
                  "navigation_status": 200, "completed_at": self.at, "owned_process_exited": True, "snapshot": snapshot}
        atomic_json(folder / "native_result.json", output)
        atomic_json(folder / "native_dispatch.json", {"url": url, "navigation_started": True, "navigation_started_at": self.at})
        if self.native_state == "interrupted":
            raise TimeoutError("Synthetic wrapper lost after the signed-in child completed")
        return output

    def handlers(self):
        installed = create_supplier_handlers(self.memory, http_reader=self.reader, browser_reader=self.browser,
            respect_access_waits=True, shared_acquisitions=True, signed_in_root=self.signed_root)
        return {"indiamart": installed["indiamart"]}

    def prepare(self, *, run, queries=(QUERY,)):
        self.run = run
        return prepare_source_tasks(self.memory, [{"route": "indiamart", "request": {"query": query, "country": "IN"},
            "concept_ids": [self.cid]} for query in queries], run_id=run, as_of=self.at)["task_ids"]

    def checks(self, handlers, *, bounded_call=None):
        with patch("product_radar.supplier_search.utc_now", return_value=self.at), \
                patch("product_radar.signed_in_pacing.utc_now", return_value=self.at), \
                patch("product_radar.signed_in_browser.utc_now", return_value=self.at), \
                patch("product_radar.indiamart_signed_in.utc_now", return_value=self.at), \
                patch("product_radar.native_public_browser.native_snapshot", side_effect=self.native):
            return execute_source_run(self.memory, self.run, handlers=handlers, as_of=self.at, shared_marketplace=True,
                bounded_call=bounded_call or (lambda callback, context, timeout: callback(context)))

    def advance(self, seconds):
        self.at = (timestamp(self.at) + timedelta(seconds=seconds)).isoformat(timespec="seconds")

    def owner(self):
        return next((aid, row) for aid, row in self.owners().items() if row["binding"]["route"] == "indiamart")

    def response(self, owner):
        return self.memory.snapshot()["source_checks"][owner["active_response_id"]]

    def reads(self):
        path = pacing._path(self.signed_root)
        return load_json(path)["reads"].get("indiamart", []) if path.exists() else []

    def task(self, task_id):
        return self.memory.snapshot()["query_tasks"][task_id]

    def leaks(self):
        """Account details anywhere in the saved research store (quarantine aside)."""
        found = set()
        for path in self.memory.root.rglob("*"):
            if path.is_file() and QUARANTINE not in path.parts:
                text = path.read_text(encoding="utf-8", errors="ignore")
                found.update((path.name, secret) for secret in SECRETS if secret in text)
        return found

    # Planning, gates and the read itself.

    def test_without_a_sign_in_the_route_waits_calmly_and_reads_nothing(self):
        request = supplier_request_for_state(self.memory.snapshot(), "indiamart", QUERY)
        self.assertEqual(request["transport"], SIGNED_IN_TRANSPORT)
        handlers = self.handlers(); task_id = self.prepare(run="no-sign-in")[0]
        for _ in range(3):
            result = self.checks(handlers)
            self.assertEqual((result["handler_calls"], result["counts"]), (0, {"waiting": 1}))
            # The calm wait keeps a recheck time on the question; it is looked at again then.
            recheck = (timestamp(self.at) + timedelta(seconds=120)).isoformat()
            self.assertEqual((result["results"][0]["reason"], result["results"][0]["next_retry_at"]),
                             ("OWNER_SIGN_IN_REQUIRED", recheck))
            self.assertEqual((self.task(task_id)["status"], self.task(task_id)["next_retry_at"]), ("prepared", recheck))
            self.advance(121)
        self.assertFalse(self.task(task_id).get("reconcile_required"))
        hint = handlers["indiamart"]["source_due_hint"]({"request": {"query": QUERY, "country": "IN"}, "as_of": self.at,
                                                          "task_id": "hint"})
        self.assertEqual((hint["ready"], hint["reason"]), (False, "OWNER_SIGN_IN_REQUIRED"))
        self.assertEqual((self.native_calls, self.http_calls, self.browser_calls, self.reads()), ([], [], [], []))
        self.assertEqual(site_states(self.signed_root)["indiamart"]["state"], "sign_in_needed")

    def test_signed_in_read_saves_cards_records_the_sign_in_and_keeps_no_account_details(self):
        self.sign_in(); handlers = self.handlers(); self.prepare(run="signed-in")
        self.assertEqual(self.checks(handlers)["counts"], {"acquired": 1})
        self.assertEqual((self.http_calls, self.browser_calls, len(self.native_calls)), ([], [], 1))
        call = self.native_calls[0]
        # The supplier routes' read-only snapshot, in the signed-in profile, with only the render wait.
        self.assertEqual(call, {"url": supplier_route_url("indiamart", QUERY), "expression": _DOM, "profile": "signed-in",
                                "root": self.signed_root, "ready_expression": _ready_expression("indiamart")})
        self.assertEqual(site_states(self.signed_root)["indiamart"]["state"], "signed_in")
        self.assertEqual(self.reads(), [self.at])
        aid, owner = self.owner()
        self.assertEqual(acquisition_access_scope(owner),
                         source_access_scope(owner["binding"]["request"]["url"], transport=SIGNED_IN_TRANSPORT,
                                             access_identity=SIGNED_IN_ACCESS_IDENTITY))
        response = self.response(owner)
        self.assertEqual((response["source_state"], response["complete"]), ("usable", True))
        state = self.memory.snapshot()
        checks = current_supplier_search_checks(state, self.cid, description_hash=digest(state["concepts"][self.cid]["description"]))
        candidates = [row for check in checks.values() for row in check["candidates"]]
        self.assertEqual(len(candidates), MAX_RESULTS)
        self.assertEqual(candidates[2]["supplier_name"], "Shreeji Steel Works")
        folder = self.memory.root / "supplier_routes" / "shared" / aid
        self.assertEqual(load_json(folder / "page.json")["receipt"]["profile"], "signed-in")
        self.assertEqual(self.leaks(), set())
        # The saved original answers later passes without another read.
        self.prepare(run="signed-in-again")
        self.assertEqual(self.checks(handlers)["handler_calls"], 0)
        self.assertEqual(len(self.native_calls), 1)

    def test_reads_are_spaced_30_seconds_apart_and_stop_at_the_daily_ceiling(self):
        self.sign_in(); handlers = self.handlers()
        self.prepare(run="first", queries=(QUERY,)); self.checks(handlers)
        self.prepare(run="second", queries=("sink caddy organiser",))
        result = self.checks(handlers)
        self.assertEqual((result["handler_calls"], result["counts"]), (0, {"waiting": 1}))
        held = result["results"][0]
        self.assertEqual((held["reason"], held["next_retry_at"]),
                         ("SIGNED_IN_READ_SPACING", (timestamp(self.at) + timedelta(seconds=30)).isoformat()))
        self.advance(30)
        self.assertEqual(self.checks(handlers)["counts"], {"acquired": 1})
        self.assertEqual(len(self.native_calls), 2)
        # The day's ceiling: 150 counted reads in 24 hours hold the next one.
        self.advance(60)
        rows = [(timestamp(self.at) - timedelta(minutes=minute)).isoformat(timespec="seconds") for minute in range(150, 0, -1)]
        atomic_json(pacing._path(self.signed_root), {"schema_version": 1, "reads": {"indiamart": rows}})
        self.prepare(run="third", queries=("sink caddy rack",))
        result = self.checks(handlers)
        self.assertEqual((result["handler_calls"], result["results"][0]["reason"]), (0, "SIGNED_IN_DAILY_CEILING"))
        self.assertEqual(result["results"][0]["next_retry_at"], (timestamp(rows[0]) + timedelta(hours=24)).isoformat())
        self.assertEqual(len(self.native_calls), 2)

    def test_a_wall_while_signed_in_records_the_expiry_and_waits_for_the_owner(self):
        self.sign_in(); handlers = self.handlers()
        self.page = page(WALL)
        self.prepare(run="walled")
        self.assertEqual(self.checks(handlers)["counts"], {"blocked": 1})
        _, owner = self.owner()
        response = self.response(owner)
        self.assertEqual((response["source_state"], response["parse_reason"]), ("blocked", "OWNER_SIGN_IN_REQUIRED"))
        self.assertEqual(site_states(self.signed_root)["indiamart"]["state"], "sign_in_needed")
        # Its access wait belongs to the signed-in identity only, never the public supplier routes.
        state = self.memory.snapshot()
        self.assertEqual(source_access_wait(state, acquisition_access_scope(owner), as_of=self.at)["phase"], "waiting")
        public = source_access_scope(owner["binding"]["request"]["url"], transport="supplier-public",
                                     access_identity="owned-supplier-public")
        self.assertIsNone(source_access_wait(state, public, as_of=self.at))
        # Every other IndiaMART question waits calmly: no repeated failures, no reads,
        # including the walled search itself once its access wait is due.
        self.advance(3600)
        self.prepare(run="after-wall", queries=("sink caddy organiser",))
        for _ in range(2):
            result = self.checks(handlers)
            self.assertEqual((result["handler_calls"], result["counts"]), (0, {"waiting": 1}))
            self.assertEqual(result["results"][0]["reason"], "OWNER_SIGN_IN_REQUIRED")
            self.advance(121)
        self.prepare(run="same-search-again")
        result = self.checks(handlers)
        self.assertEqual((result["handler_calls"], {row["reason"] for row in result["results"] if row["state"] == "waiting"}),
                         (0, {"OWNER_SIGN_IN_REQUIRED"}))
        self.assertEqual(len(self.native_calls), 1)
        # The owner signs in again: the next question reads, and is signed in.
        self.sign_in(); self.page = visitor_page(); self.advance(1)
        self.prepare(run="after-sign-in", queries=("sink caddy rack",))
        self.assertEqual(self.checks(handlers)["counts"], {"acquired": 1})
        self.assertEqual(site_states(self.signed_root)["indiamart"]["state"], "signed_in")

    def test_repeated_walls_never_hold_a_new_sign_in_for_hours(self):
        # The owner's sign-in, not an escalating back-off, governs this wait:
        # each wall waits five minutes, however many came before.
        self.sign_in(); handlers = self.handlers(); self.page = page(WALL)
        for number, query in enumerate((QUERY, "sink caddy organiser", "sink caddy rack"), 1):
            self.prepare(run="wall-" + str(number), queries=(query,))
            self.assertEqual(self.checks(handlers)["counts"], {"blocked": 1})
            aid = next(aid for aid, row in self.owners().items() if row["binding"]["request"]["query"] == query)
            wait = source_access_wait(self.memory.snapshot(), acquisition_access_scope(self.owners()[aid]), as_of=self.at)
            binding = self.memory.snapshot()["source_checks"][wait["failure_id"]]["binding"]
            self.assertEqual((binding["attempt"], binding["wait_seconds"]), (number, 300))
            self.advance(301); self.sign_in()
        self.assertEqual(len(self.native_calls), 3)

    def test_an_expired_sign_in_is_named_expired_and_waits(self):
        self.sign_in(); handlers = self.handlers()
        self.prepare(run="first"); self.checks(handlers)
        self.advance(60); self.page = page(WALL)
        self.prepare(run="walled", queries=("sink caddy organiser",))
        self.assertEqual(self.checks(handlers)["counts"], {"blocked": 1})
        self.assertEqual(site_states(self.signed_root)["indiamart"]["state"], "expired")
        self.advance(60); self.prepare(run="later", queries=("sink caddy rack",))
        self.assertEqual(self.checks(handlers)["results"][0]["reason"], "OWNER_SIGN_IN_REQUIRED")
        self.assertEqual(len(self.native_calls), 2)

    # Starts that never navigated: calm waits, never failures.

    def test_a_not_started_read_reopens_when_the_round_time_has_microseconds(self):
        # The dispatch intent keeps the round's microseconds; the not-started
        # proof must never be dated before it (SOURCE_ACQUISITION_ZERO_PROOF_REQUIRED).
        self.at = "2026-10-05T10:00:00.500000+00:00"
        self.sign_in(); handlers = self.handlers(); self.native_state = "desktop-locked"
        self.prepare(run="microseconds")
        clock = lambda timespec="seconds": timestamp(self.at).isoformat(timespec=timespec)
        with patch("product_radar.supplier_search.utc_now", side_effect=clock), \
                patch("product_radar.signed_in_pacing.utc_now", side_effect=clock), \
                patch("product_radar.signed_in_browser.utc_now", side_effect=clock), \
                patch("product_radar.indiamart_signed_in.utc_now", side_effect=clock), \
                patch("product_radar.native_public_browser.native_snapshot", side_effect=self.native):
            result = execute_source_run(self.memory, self.run, handlers=handlers, as_of=self.at, shared_marketplace=True,
                                        bounded_call=lambda callback, context, timeout: callback(context))
        self.assertEqual(result["results"][0]["reason"], "OWNER_DESKTOP_UNAVAILABLE")
        aid, owner = self.owner()
        self.assertEqual(owner["state"], "reserved")
        proof = load_json(self.memory.root / "supplier_routes" / "shared" / aid / "not-started-1" / "process-exit.json")
        self.assertEqual(proof["started_at"], self.at)

    def test_a_locked_desktop_is_a_calm_wait_that_reads_later(self):
        self.sign_in(); handlers = self.handlers(); self.native_state = "desktop-locked"
        task_id = self.prepare(run="locked")[0]
        before = self.task(task_id)
        result = self.checks(handlers)
        self.assertEqual(result["results"][0], {"task_id": task_id, "state": "waiting", "actual_attempt": False,
            "reason": "OWNER_DESKTOP_UNAVAILABLE", "next_retry_at": "2026-10-05T10:02:00+00:00", "route": "indiamart"})
        task = self.task(task_id)
        self.assertEqual({field: task.get(field) for field in ("status", "reconcile_required", "dispatch_started", "tries")},
                         {field: before.get(field) for field in ("status", "reconcile_required", "dispatch_started", "tries")})
        self.assertEqual(self.reads(), [])  # never navigated: not one of the day's reads
        aid, owner = self.owner()
        # The same acquisition is reopened by its hash-bound "never navigated" proof.
        self.assertEqual(owner["state"], "reserved")
        folder = self.memory.root / "supplier_routes" / "shared" / aid
        proof = load_json(folder / "not-started-1" / "process-exit.json")
        self.assertEqual((proof["request_sent"], proof["reason"]), (False, "OWNER_DESKTOP_UNAVAILABLE"))
        self.assertFalse((folder / NOT_STARTED).exists())
        self.assertFalse((folder / "page.json").exists())
        self.assertFalse((folder / "native").exists())
        aside = [path for path in folder.iterdir() if path.name.startswith("native-not-started-1-")]
        self.assertEqual(len(aside), 1)
        self.assertEqual(load_json(aside[0] / "native_result.json")["reason"], "OWNER_DESKTOP_UNAVAILABLE")
        self.assertEqual(task["next_retry_at"], "2026-10-05T10:02:00+00:00")  # the end of the desktop recheck
        # While the desktop stays unavailable the route waits without starting anything.
        self.advance(60)
        result = self.checks(handlers)
        self.assertEqual((result["handler_calls"], result["results"][0]["state"], result["results"][0]["next_retry_at"]),
                         (0, "waiting", "2026-10-05T10:02:00+00:00"))
        self.assertEqual(len(self.native_calls), 1)
        # After the recheck period the same question reads into a fresh folder.
        self.native_state = "captured"; self.advance(61)
        self.assertEqual(self.checks(handlers)["counts"], {"acquired": 1})
        self.assertEqual(len(self.native_calls), 2)
        self.assertTrue((folder / "native" / "native_result.json").exists())
        self.assertEqual(self.reads(), [self.at])
        self.assertEqual(self.owner()[0], aid)

    def test_a_busy_browser_before_launch_is_a_calm_uncounted_wait(self):
        self.sign_in(); handlers = self.handlers(); self.native_state = "busy"
        task_id = self.prepare(run="busy")[0]
        result = self.checks(handlers)
        self.assertEqual((result["results"][0]["state"], result["results"][0]["reason"]),
                         ("waiting", "SIGNED_IN_BROWSER_BUSY"))
        self.assertFalse(self.task(task_id).get("reconcile_required"))
        self.assertEqual(self.task(task_id)["next_retry_at"], (timestamp(self.at) + timedelta(seconds=120)).isoformat())
        self.assertEqual((self.owner()[1]["state"], self.reads()), ("reserved", []))
        self.native_state = "captured"
        self.assertEqual(self.checks(handlers)["counts"], {"waiting": 1})  # not before its recheck
        self.advance(120)
        self.assertEqual(self.checks(handlers)["counts"], {"acquired": 1})

    def test_a_read_counted_elsewhere_just_before_launch_waits_before_the_launch_guard(self):
        # Another signed-in read was counted after this question passed its
        # hold: the count is taken before the launch guard, so nothing starts.
        self.sign_in(); handlers = self.handlers(); self.prepare(run="raced")
        pacing.reserve_signed_in_read(self.signed_root, "indiamart", at=self.at)
        with patch("product_radar.supplier_search._launch_hold", return_value=None):
            result = self.checks(handlers)
        self.assertEqual((result["results"][0]["state"], result["results"][0]["reason"], self.native_calls),
                         ("waiting", "SIGNED_IN_READ_SPACING", []))
        self.assertEqual((self.owner()[1]["state"], self.owner()[1]["stage_ids"]), ("reserved", []))
        self.assertEqual(self.reads(), [self.at])
        self.advance(30)
        self.assertEqual(self.checks(handlers)["counts"], {"acquired": 1})
        self.assertEqual(len(self.native_calls), 1)

    def test_other_refusals_before_launch_are_failures_not_calm_waits(self):
        # Only the signed-in browser's own waits and a busy record are calm
        # (signed_in_attempts.CALM_CODES); anything else must be seen.
        def refused(url, expression, folder, **options):
            raise PermissionError("Synthetic refusal before any file was written")
        for state, native, reason in (("refused", None, "NATIVE_PUBLIC_ARTIFACT_EXISTS"),
                                      ("captured", refused, "SOURCE_ACTION_FAILED")):
            with self.subTest(reason=reason):
                self.setUp(); self.sign_in(); handlers = self.handlers(); self.native_state = state
                if native:
                    self.native = native
                self.prepare(run="refused-" + state)
                row = self.checks(handlers)["results"][0]
                self.assertNotEqual(row["state"], "waiting")
                self.assertEqual(row["reason"], reason)

    def test_a_collector_called_while_the_owner_is_signed_out_waits_without_any_intent(self):
        handlers = self.handlers(); request = supplier_request_for_state(self.memory.snapshot(), "indiamart", QUERY)
        context = {"task_id": "direct", "acquisition_id": "direct-signed-out", "as_of": self.at, "concept_ids": [self.cid],
                   "request": request}
        with self.assertRaisesRegex(SafetyError, "OWNER_SIGN_IN_REQUIRED") as caught:
            handlers["indiamart"]["handler"](context)
        self.assertIs(caught.exception.calm_wait, True)
        self.assertFalse((self.memory.root / "supplier_routes" / "shared" / "direct-signed-out").exists())

    def test_reopening_checks_its_note_and_its_acquisition(self):
        from product_radar.supplier_search import _reopen_not_started, _uncount
        self.sign_in(); handlers = self.handlers(); self.native_state = "desktop-locked"
        self.prepare(run="note"); self.checks(handlers)
        aid, owner = self.owner()
        folder = self.memory.root / "supplier_routes" / "shared" / aid
        note = {"reason": "OWNER_DESKTOP_UNAVAILABLE", "started_at": self.at, "finished_at": self.at}
        atomic_json(folder / NOT_STARTED, note)  # already reopened: the note is simply cleared
        _reopen_not_started(self.memory, aid, folder, self.at)
        self.assertFalse((folder / NOT_STARTED).exists())
        atomic_json(folder / NOT_STARTED, dict(note, extra=True))
        with self.assertRaisesRegex(SafetyError, "SIGNED_IN_NOT_STARTED_NOTE_INVALID"):
            _reopen_not_started(self.memory, aid, folder, self.at)
        self.native_state = "captured"; self.advance(121); self.checks(handlers)
        atomic_json(folder / NOT_STARTED, note)  # the acquisition has since been read
        with self.assertRaisesRegex(SafetyError, "SIGNED_IN_NOT_STARTED_OWNER_CHANGED"):
            _reopen_not_started(self.memory, aid, folder, self.at)
        # Giving a counted read back: nothing to give, a busy record keeps it, other faults surface.
        self.assertIsNone(_uncount(self.signed_root, "indiamart", None))
        with patch("product_radar.signed_in_pacing.release_signed_in_read", side_effect=SafetyError("SIGNED_IN_RECORD_BUSY")):
            self.assertIsNone(_uncount(self.signed_root, "indiamart", self.at))
        with patch("product_radar.signed_in_pacing.release_signed_in_read", side_effect=SafetyError("SIGNED_IN_PACING_RECORD_INVALID")):
            with self.assertRaisesRegex(SafetyError, "SIGNED_IN_PACING_RECORD_INVALID"):
                _uncount(self.signed_root, "indiamart", self.at)

    def test_a_failed_launch_guard_gives_its_counted_read_back(self):
        self.sign_in(); self.prepare(run="guard")
        with patch("product_radar.source_coverage.guard_shared_source_launch",
                   side_effect=SafetyError("SOURCE_SHARED_CALLER_INTENT_REQUIRED")):
            result = self.checks(self.handlers())
        self.assertEqual((result["results"][0]["state"], result["results"][0]["reason"]),
                         ("blocked", "SOURCE_SHARED_CALLER_INTENT_REQUIRED"))
        self.assertEqual((self.reads(), self.native_calls), ([], []))

    def test_a_failed_reopening_is_a_calm_wait_retried_by_the_next_pass(self):
        self.sign_in(); handlers = self.handlers(); self.native_state = "desktop-locked"
        self.prepare(run="reopen")
        with patch("product_radar.source_acquisitions.recover_source_prelaunch",
                   side_effect=SafetyError("SOURCE_ACQUISITION_ZERO_PROOF_REQUIRED")):
            result = self.checks(handlers)
            self.assertEqual((result["results"][0]["state"], result["results"][0]["reason"]),
                             ("waiting", "SIGNED_IN_START_RECOVERY_PENDING"))
            aid, owner = self.owner()
            folder = self.memory.root / "supplier_routes" / "shared" / aid
            self.assertEqual((owner["state"], (folder / NOT_STARTED).is_file()), ("dispatching", True))
            # Still failing: the next pass after the recheck waits calmly again, reading nothing.
            self.advance(120)
            result = self.checks(handlers)
            self.assertEqual((result["handler_calls"], result["results"][0]["reason"]), (0, "SIGNED_IN_START_RECOVERY_PENDING"))
            self.assertEqual(result["results"][0]["next_retry_at"], (timestamp(self.at) + timedelta(seconds=120)).isoformat())
        self.native_state = "captured"; self.advance(121)
        self.assertEqual(self.checks(handlers)["counts"], {"acquired": 1})
        self.assertFalse((folder / NOT_STARTED).exists())
        self.assertEqual(self.owner()[0], aid)

    def test_an_answer_lost_after_the_browser_returned_is_never_called_not_started(self):
        self.sign_in(); handlers = self.handlers()
        with patch("product_radar.signed_in_browser.record_site_observation", side_effect=SafetyError("SIGNED_IN_RECORD_BUSY")):
            self.prepare(run="lost")
            result = self.checks(handlers)
        self.assertEqual((result["results"][0]["state"], result["results"][0]["reason"]), ("blocked", "SIGNED_IN_RECORD_BUSY"))
        self.assertEqual(self.owner()[1]["state"], "dispatching")  # uncertain: left for original recovery
        self.assertEqual(self.reads(), [self.at])
        self.assertEqual(self.leaks(), set())

    def test_a_deadline_that_never_navigated_gives_its_read_back(self):
        self.sign_in(); handlers = self.handlers()

        def cancelled(url, expression, folder, **options):
            folder.mkdir(parents=True, exist_ok=True)
            atomic_json(folder / "native_outer_deadline.json", {"state": "failed", "reason": "SOURCE_ACTION_TIMEOUT",
                "stage": "outer_deadline", "outer_deadline": True, "requested_url": url, "snapshot": None,
                "navigation_started": False, "navigation_started_at": None, "owned_process_exited": True,
                "completed_at": self.at, "launch_invoked": False})
            raise SourceActionDeadline()
        self.native = cancelled
        self.prepare(run="deadline")
        result = self.checks(handlers)
        self.assertEqual(result["results"][0]["reason"], "SOURCE_ACTION_TIMEOUT")
        self.assertEqual(self.reads(), [])

    # Interrupted reads never leave the account's details behind.

    def test_an_interrupted_read_leaves_no_account_details_and_recovers_from_its_files(self):
        self.sign_in(); handlers = self.handlers(); self.native_state = "interrupted"
        self.prepare(run="late")
        result = self.checks(handlers)
        self.assertEqual((result["results"][0]["reason"], result["results"][0]["reconcile_required"]),
                         ("SOURCE_ACTION_TIMEOUT", True))
        self.assertEqual(self.leaks(), set())
        self.advance(1)
        self.checks(handlers)
        self.assertEqual((self.response(self.owner()[1])["source_state"], len(self.native_calls)), ("usable", 1))
        self.assertEqual(self.leaks(), set())

    def test_a_late_wall_recovered_from_saved_files_keeps_its_signed_in_meaning(self):
        self.sign_in(); handlers = self.handlers(); self.page = page(WALL); self.native_state = "interrupted"
        self.prepare(run="late-wall")
        self.checks(handlers); self.advance(1); self.checks(handlers)
        response = self.response(self.owner()[1])
        self.assertEqual((response["source_state"], response["parse_reason"]), ("blocked", "OWNER_SIGN_IN_REQUIRED"))

    def test_a_raw_result_left_by_a_killed_read_is_cleaned_before_it_is_read(self):
        # As if the process died before the read could clean its own file.
        self.sign_in(); handlers = self.handlers(); self.native_state = "interrupted"
        self.prepare(run="killed")
        with patch("product_radar.indiamart_signed_in.protect_saved"):
            self.checks(handlers)
        self.assertTrue(self.leaks())
        self.advance(1)
        self.checks(handlers)
        self.assertEqual(self.response(self.owner()[1])["source_state"], "usable")
        self.assertEqual(self.leaks(), set())

    def test_a_result_that_cannot_be_cleaned_is_quarantined_and_never_read(self):
        self.sign_in(); handlers = self.handlers()

        def unreadable(url, expression, folder, **options):
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "native_result.json").write_text('{"snapshot": {"html": "Hi Shreeji Verma 9876501234"', encoding="utf-8")
            atomic_json(folder / "native_dispatch.json", {"url": url, "navigation_started": True, "navigation_started_at": self.at})
            raise TimeoutError("Synthetic wrapper lost")
        self.native = unreadable
        self.prepare(run="unreadable")
        self.checks(handlers); self.advance(1)
        result = self.checks(handlers)
        self.assertEqual(result["results"][0]["reason"], "SUPPLIER_ORIGINAL_SEARCH_PENDING")
        aid, _ = self.owner()
        held = list((self.memory.root / "supplier_routes" / "shared" / aid / QUARANTINE).iterdir())
        self.assertEqual(len(held), 1)
        self.assertEqual(self.leaks(), set())

    def test_a_signed_in_read_gets_the_signed_in_browsers_whole_deadline(self):
        self.sign_in(); handlers = self.handlers(); self.prepare(run="deadline-budget")
        timeouts = []

        def bounded(callback, context, timeout):
            timeouts.append(timeout)
            return callback(context)
        self.checks(handlers, bounded_call=bounded)
        self.assertEqual(timeouts, [signed_in_budget()])
        self.assertGreater(signed_in_budget(), 45)


if __name__ == "__main__":
    unittest.main()
