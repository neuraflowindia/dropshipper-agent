"""Local-only desktop lifecycle tests; no providers, real pools or Windows tasks."""

import io
import json
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar import desktop_control as controls
from product_radar.models import SafetyError
from product_radar.programme import programme_service_state, set_programme_service_state


class DesktopControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="radar-desktop-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.memory = SimpleNamespace(root=self.root / "data/research/captured")

    def test_status_is_read_only_and_does_not_load_research(self):
        with patch.object(controls, "owned_programmes", return_value=[]):
            before = list(self.root.rglob("*"))
            result = controls.status(self.root)
            self.assertEqual(result["state"], "ready")
            self.assertEqual(list(self.root.rglob("*")), before)

    def test_pause_is_the_existing_saved_switch_not_a_second_intent(self):
        with patch.object(controls, "owned_programmes", return_value=[]):
            self.assertEqual(controls.stop(self.root)["state"], "stopped")
            self.assertFalse(programme_service_state(self.memory)["enabled"])
            self.assertEqual(controls.start(self.root)["state"], "ready")
            self.assertTrue(programme_service_state(self.memory)["enabled"])
        self.assertFalse((self.root / "data/research/captured/events.jsonl").exists())

    def test_stop_persists_pause_before_interrupt_and_does_not_lie_about_exit(self):
        row = {"pid": 12345, "start_ticks": "678"}
        def interrupt(actual, sig):
            self.assertFalse(programme_service_state(self.memory)["enabled"])
            self.assertEqual(actual, row)
            self.assertEqual(sig, signal.SIGINT)
            return True
        with patch.object(controls, "owned_programmes", return_value=[row]), patch.object(controls, "signal_identified", side_effect=interrupt):
            result = controls.stop(self.root)
        self.assertEqual(result["state"], "stopping")
        self.assertEqual(result["active_processes"], 1)

    def test_corrupt_saved_control_never_implicitly_resumes_or_signals(self):
        file = self.memory.root / "programme_service/control.json"
        file.parent.mkdir(parents=True)
        file.write_text('{"control":{},"hash":"wrong"}')
        with patch.object(controls, "signal_identified") as interrupt:
            with self.assertRaisesRegex(SafetyError, "CONTROL_INVALID"):
                controls.stop(self.root)
            interrupt.assert_not_called()

    def test_diagnostic_failure_does_not_undo_stop(self):
        with patch.object(controls, "owned_programmes", return_value=[]), patch.object(controls, "record_action", return_value=None):
            result = controls.stop(self.root)
        self.assertFalse(result["enabled"])

    def test_pid_reuse_is_not_signalled(self):
        with patch.object(controls, "process_info", return_value={"pid": 55, "start_ticks": "new"}), patch.object(controls.os, "pidfd_open") as opened:
            self.assertFalse(controls.signal_identified({"pid": 55, "start_ticks": "old"}, signal.SIGINT))
            opened.assert_not_called()

    def test_pid_reuse_after_open_is_not_signalled(self):
        old = {"pid": 55, "start_ticks": "old"}
        with patch.object(controls, "process_info", side_effect=[old, dict(old, start_ticks="new")]), \
                patch.object(controls.os, "pidfd_open", return_value=99), \
                patch.object(controls.os, "close") as closed, \
                patch.object(controls.signal, "pidfd_send_signal") as sent:
            self.assertFalse(controls.signal_identified(old, signal.SIGINT))
            sent.assert_not_called()
            closed.assert_called_once_with(99)

    def test_signal_uses_the_kernel_identity_handle(self):
        row = {"pid": 55, "start_ticks": "old"}
        with patch.object(controls, "process_info", return_value=row), \
                patch.object(controls.os, "pidfd_open", return_value=99), \
                patch.object(controls.os, "close"), \
                patch.object(controls.signal, "pidfd_send_signal") as sent:
            self.assertTrue(controls.signal_identified(row, signal.SIGINT))
            sent.assert_called_once_with(99, signal.SIGINT)

    def test_ownership_requires_python_script_cwd_and_execute(self):
        proc = self.root / "proc"
        proc.mkdir()
        rows = [
            {"pid": 1, "cwd": str(self.root), "argv": ["python", "scripts/run_programme.py", "--tick", "--execute"]},
            {"pid": 2, "cwd": str(self.root), "argv": ["bash", "-c", "scripts/run_programme.py --execute"]},
            {"pid": 3, "cwd": str(self.root / "another-project"), "argv": ["python", "scripts/run_programme.py", "--execute"]},
            {"pid": 4, "cwd": str(self.root), "argv": ["python", "scripts/run_programme.py", "--prepare"]},
            {"pid": 5, "cwd": str(self.root), "argv": ["python", "-c", str(self.root / "scripts/run_programme.py"), "--execute"]},
            {"pid": 6, "cwd": str(self.root), "argv": ["python", "-u", str(self.root / "scripts/run_programme.py"), "--tick", "--execute"]},
        ]
        for row in rows:
            (proc / str(row["pid"])).mkdir()
        with patch.object(controls, "process_info", side_effect=lambda pid, **_: next(row for row in rows if row["pid"] == int(pid))):
            self.assertEqual(sorted(row["pid"] for row in controls.owned_programmes(self.root, proc=proc)), [1, 6])

    def test_ownership_accepts_the_same_folder_spelled_differently(self):
        # The Windows task launches from a lower-case /mnt/d path; the owner's
        # shell may use the original capitals. Same folder, different spelling.
        proc = self.root / "proc"
        proc.mkdir()
        alias = Path(tempfile.mkdtemp(prefix="desktop-alias-"))
        self.addCleanup(shutil.rmtree, alias, True)
        (alias / "link").symlink_to(self.root)
        spelled = alias / "link"
        rows = [{"pid": 7, "cwd": str(spelled), "argv": ["python", "-u", str(spelled / "scripts/run_programme.py"), "--tick", "--execute"]},
                {"pid": 8, "cwd": str(self.root / "missing-folder"), "argv": ["python", "scripts/run_programme.py", "--execute"]}]
        for row in rows:
            (proc / str(row["pid"])).mkdir()
        (self.root / "scripts").mkdir(exist_ok=True)
        (self.root / "scripts/run_programme.py").touch()
        real = controls.Path.resolve
        with patch.object(controls, "process_info", side_effect=lambda pid, **_: next(row for row in rows if row["pid"] == int(pid))), \
                patch.object(controls.Path, "resolve", lambda self, *a, **k: self if "link" in self.parts else real(self, *a, **k)):
            self.assertEqual([row["pid"] for row in controls.owned_programmes(self.root, proc=proc)], [7])

    def test_status_carries_sign_in_needs_and_spend_lines(self):
        with patch.object(controls, "owned_programmes", return_value=[]):
            result = controls.status(self.root)
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual([line["item"] for line in result["spend"]], ["hosting"])  # nothing recorded yet
        self.assertEqual(result["spend_alerts"], [])
        self.assertFalse(result["sign_in"]["window_open"])
        self.assertEqual([row["site"] for row in result["sign_in"]["needs"]],
                         ["google", "google-trends", "indiamart", "dropship-india", "dropdash"])
        self.assertTrue(all(set(row) == {"id", "site", "label", "state"} for row in result["sign_in"]["needs"]))
        self.assertTrue(result["sign_in"]["line"].startswith("Sign-in needed: Google account"))
        self.assertEqual(result["sign_in"]["trends_line"], "Google Trends: waiting for the owner to sign in.")
        from product_radar import signed_in_browser
        signed_in_browser.note_sign_in_window(self.root, True, opened=True)
        signed_in_browser.note_sign_in_window(self.root, False)
        with patch.object(controls, "owned_programmes", return_value=[]):
            self.assertEqual(controls.status(self.root)["sign_in"]["trends_line"],
                             "Google Trends check: 0 of 3 signed-in reads saved complete results.")
        signed_in_browser.record_site_observation(self.root, "indiamart", False)
        before = controls.sign_in_summary(self.root)
        (self.root / "data/operations/signed-in-browser/trends-proof-gate.json").write_text("{corrupt")
        summary = controls.sign_in_summary(self.root)  # a corrupt Trends record never hides the sign-in needs
        self.assertEqual(summary["trends_line"], "The Google Trends check records could not be read. See the project log.")
        self.assertEqual((summary["line"], summary["needs"]), (before["line"], before["needs"]))
        self.assertEqual([row["site"] for row in summary["needs"]], ["indiamart"])

    def test_sign_in_area_shows_one_line_per_platform_needing_attention(self):
        from product_radar import dropship_platforms as platforms
        captured = self.root / "data/research/captured"
        with patch.object(controls, "owned_programmes", return_value=[]):
            self.assertEqual(controls.status(self.root)["sign_in"]["platform_lines"], [])
        for route, reason in (("dropdash", "OWNER_SIGN_IN_REQUIRED"), ("dropship-india", "DROPSHIP_PLAN_REQUIRED"),
                              ("deodap", "DROPSHIP_FEED_UNAVAILABLE")):  # the last is not the owner's to act on
            refresh = platforms.load_refresh(captured, route)
            platforms._save_refresh(captured, route, dict(refresh, wait={"reason": reason, "at": "2026-10-14T10:00:00+00:00"}))
        with patch.object(controls, "owned_programmes", return_value=[]):
            lines = controls.status(self.root)["sign_in"]["platform_lines"]
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("Dropship India shows a paid plan or membership page"))
        self.assertEqual(lines[1], "Dropdash needs you to sign in (Sign in to research sites).")
        (captured / "dropship_platforms/dropdash/refresh.json").write_text("{corrupt")
        summary = controls.sign_in_summary(self.root)  # a corrupt platform record never hides the sign-in needs
        self.assertEqual(summary["platform_lines"], ["The dropshipping platform records could not be read. See the project log."])
        self.assertTrue(summary["line"].startswith("Sign-in needed: Google account"))

    def test_sign_in_area_ends_with_the_opportunity_score_line_and_its_calibration_alert(self):
        from product_radar import opportunity_score as score
        captured = self.root / "data/research/captured"
        with patch.object(controls, "owned_programmes", return_value=[]):
            self.assertEqual(controls.status(self.root)["sign_in"]["score_line"], "Opportunity score: no types scored yet.")
        memory = SimpleNamespace(root=captured, snapshot=lambda: {"batch_runs": {}},
                                 put=lambda *args, **kwargs: None)
        def decisions(quiet, sweet):
            market = {"sources": {site: {"available": False, "positive": None, "used_ids": []}
                                  for site in ("meta", "amazon", "flipkart", "google-trends", "blinkit", "zepto", "instamart")}}
            low = {"level": "low", "rule": "few_listings", "thresholds_version": "t", "counts": None, "evidence_ids": []}
            quiet_row = score.opportunity_fields(score.evaluate_opportunity(
                {"fit_policy_id": "fit-10-v1", "kpi_matches": 6, "kpi_total": 10}, market, presence=None, crowding=low))
            sweet_row = score.opportunity_fields(score.evaluate_opportunity(
                {"fit_policy_id": "fit-10-v1", "kpi_matches": 6, "kpi_total": 10}, market, crowding=low,
                presence={"at": "2026-10-14T10:00:00+00:00", "listed": {"deodap": ["i1"], "dropdash": ["i2"]},
                          "on_list": {"deodap": ["i1"], "dropdash": ["i2"]}, "new_arrival": {}}))
            # Distinct types: the one-time calibration samples each scored type once.
            return {"decisions": [dict(quiet_row, concept_id=f"quiet{n:02d}") for n in range(quiet)]
                    + [dict(sweet_row, concept_id=f"sweet{n:02d}") for n in range(sweet)]}
        saved = score.record_score_summary(memory, decisions(45, 5), at="2026-10-15T10:00:00+00:00")
        with patch.object(controls, "owned_programmes", return_value=[]):
            line = controls.status(self.root)["sign_in"]["score_line"]
        self.assertEqual(line, saved["text"] + "\n" + saved["alert"])
        self.assertTrue(line.startswith("Opportunity score: 50 types scored (5 sweet spot, 0 early bet, 0 proven but crowded, 45 too quiet)"))
        self.assertIn("\nSCORE CALIBRATION NEEDED: 0.9000 of the first 50 scored types are Too quiet", line)
        (captured / "opportunity_score/summary.json").write_text("{corrupt")
        summary = controls.sign_in_summary(self.root)  # a corrupt score record never hides the sign-in needs
        self.assertEqual(summary["score_line"], "The opportunity score records could not be read. See the project log.")
        self.assertTrue(summary["line"].startswith("Sign-in needed: Google account"))

    def test_open_sign_in_window_is_rechecked_at_most_every_thirty_seconds(self):
        from product_radar import signed_in_browser
        signed_in_browser.note_sign_in_window(self.root, True, opened=True)
        with patch.object(signed_in_browser, "check_sign_in_window") as check:
            fresh = controls.sign_in_summary(self.root)
        check.assert_not_called()
        self.assertTrue(fresh["window_open"]); self.assertIn("sign-in window is open", fresh["line"])
        signed_in_browser.note_sign_in_window(self.root, True, at="2026-01-01T10:00:00+00:00")
        closed = {"open": False}
        with patch.object(signed_in_browser, "check_sign_in_window", return_value=closed) as check:
            stale = controls.sign_in_summary(self.root)
        check.assert_called_once()
        self.assertFalse(stale["window_open"])

    def test_unreadable_sign_in_records_do_not_break_the_research_controls(self):
        folder = self.root / "data/operations/signed-in-browser"; folder.mkdir(parents=True)
        (folder / "site-states.json").write_text("{not json")
        with patch.object(controls, "owned_programmes", return_value=[]):
            result = controls.status(self.root)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["sign_in"], {"window_open": False, "needs": [],
                                             "line": "The sign-in records could not be read. See the project log.",
                                             "platform_lines": [],
                                             "trends_line": "Google Trends: waiting for the owner to sign in.",
                                             "score_line": "Opportunity score: no types scored yet."})

    def test_sign_in_action_opens_the_window_and_returns_research_status(self):
        from product_radar import signed_in_browser
        with patch.object(controls, "owned_programmes", return_value=[]), \
                patch.object(signed_in_browser, "open_sign_in_window", return_value="opened") as opened:
            result = controls.open_sign_in(self.root)
        opened.assert_called_once_with(self.root)
        self.assertEqual(result["state"], "ready")
        actions = [json.loads(line)["action"] for path in (self.root / "data/operations/desktop").glob("*.jsonl")
                   for line in path.read_text().splitlines()]
        self.assertEqual(actions, ["sign_in_window_opened"])

    def test_sign_in_action_while_a_read_runs_says_try_again(self):
        from product_radar import signed_in_browser
        with patch.object(controls, "owned_programmes", return_value=[]), \
                patch.object(signed_in_browser, "open_sign_in_window", side_effect=SafetyError("SIGNED_IN_BROWSER_BUSY")):
            result = controls.open_sign_in(self.root)
        self.assertEqual(result["sign_in"]["notice"], "A research read is using the browser. Try again in a minute.")
        self.assertTrue(result["sign_in"]["line"].startswith("Sign-in needed"))
        with patch.object(signed_in_browser, "open_sign_in_window", side_effect=SafetyError("SIGNED_IN_BROWSER_NOT_INSTALLED")), \
                self.assertRaisesRegex(SafetyError, "NOT_INSTALLED"):
            controls.open_sign_in(self.root)

    def test_control_script_allows_the_sign_in_action_only(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import desktop_research_control as script
        output = io.StringIO()
        with patch.object(script, "open_sign_in", return_value={"state": "ready"}) as opened, patch("sys.stdout", output):
            self.assertEqual(script.main(["sign-in"]), 0)
        opened.assert_called_once_with(script.ROOT)
        self.assertEqual(json.loads(output.getvalue()), {"state": "ready"})
        output = io.StringIO()
        with patch.object(script, "open_sign_in", side_effect=SafetyError("SIGNED_IN_BROWSER_NOT_INSTALLED")), \
                patch("sys.stdout", output):
            self.assertEqual(script.main(["sign-in"]), 1)
        self.assertEqual(json.loads(output.getvalue())["reason"], "SIGNED_IN_BROWSER_NOT_INSTALLED")
        for arguments in (["sign-in", "--tick"], ["sign-out"]):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit), patch("sys.stderr", io.StringIO()):
                script.main(arguments)

    def test_stopped_worker_does_not_start_research(self):
        set_programme_service_state(self.memory, enabled=False)
        with patch.object(controls.subprocess, "Popen") as launch:
            self.assertEqual(controls.run_worker(self.root, ["--tick", "--execute"])["state"], "stopped")
            launch.assert_not_called()

    def test_existing_legacy_worker_is_adopted_not_duplicated(self):
        with patch.object(controls, "owned_programmes", return_value=[{"pid": 123}]), patch.object(controls.subprocess, "Popen") as launch:
            self.assertEqual(controls.run_worker(self.root, ["--tick", "--execute"])["state"], "already_running")
            launch.assert_not_called()

    def test_a_worker_already_holding_the_worker_lock_is_not_duplicated(self):
        from product_radar.runtime_storage import runtime_path
        from product_radar.storage import file_lock
        lock = runtime_path(self.root, "data/operations") / "desktop" / "worker.lock"
        with file_lock(lock, 0, "FIXTURE_HELD"), patch.object(controls.subprocess, "Popen") as launch:
            self.assertEqual(controls.run_worker(self.root, ["--tick", "--execute"]), {"state": "already_running"})
            launch.assert_not_called()
        with patch.object(controls, "file_lock", side_effect=SafetyError("SYMLINK_PATH_REFUSED")), \
                self.assertRaisesRegex(SafetyError, "SYMLINK_PATH_REFUSED"):
            controls.run_worker(self.root, ["--tick", "--execute"])  # other lock faults are not hidden

    def test_worker_refuses_unapproved_or_repeated_options(self):
        for arguments in (["--execute"], ["--tick", "--execute", "--publish"], ["--tick", "--execute", "--live-model", "--live-model"]):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(SafetyError, "ARGUMENTS_INVALID"):
                controls.run_worker(self.root, arguments)

    def test_real_cost_free_child_can_be_identified_stopped_and_restarted(self):
        script = self.root / "scripts/run_programme.py"
        script.parent.mkdir()
        script.write_text("import time\ntry:\n time.sleep(1)\nexcept KeyboardInterrupt:\n pass\n")
        child = subprocess.Popen([sys.executable, "-u", str(script), "--tick", "--execute"], cwd=self.root,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        try:
            self.assertIn(child.pid, [row["pid"] for row in controls.owned_programmes(self.root)])
            controls.stop(self.root)
            child.wait(timeout=5)
            self.assertEqual(controls.status(self.root)["state"], "stopped")
            controls.start(self.root)
            self.assertEqual(controls.run_worker(self.root, ["--tick", "--execute"])["exit_code"], 0)
        finally:
            if child.poll() is None:
                child.kill(); child.wait()

    def test_real_stop_during_model_wait_cleans_child_and_exits_without_next_job(self):
        # Exercise the actual SIGINT route in an isolated Python programme.
        # Only manufactured inputs and a local sleeping child; no provider.
        source = str(Path(__file__).resolve().parents[1] / "src")
        test_source = str(Path(__file__).resolve().parent)
        script = self.root / "scripts/run_programme.py"
        script.parent.mkdir()
        model_marker = self.root / "waiting-model.json"
        child_code = ("import json,os,pathlib,sys,time;sys.stdin.buffer.read();"
                      f"pathlib.Path({str(model_marker)!r}).write_text(json.dumps({{'pid':os.getpid()}}));"
                      "time.sleep(30)")
        script.write_text(f"""import sys,subprocess
from pathlib import Path
from unittest.mock import patch
sys.path[:0] = [{source!r}, {test_source!r}]
from product_radar.research_ai import CodexRunner, Jobs
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal
from test_model_interrupt_recovery import CONFIG, PAYLOAD
root = Path({str(self.root)!r})
memory = ResearchMemory(root / 'data/research/captured', Journal(root / 'finance'), evidence_type='synthetic')
actual_popen = subprocess.Popen
def launch(_command, **kwargs):
    return actual_popen([sys.executable, '-c', {child_code!r}], **kwargs)
login = subprocess.CompletedProcess([], 0, b'Logged in using ChatGPT', b'')
try:
    with patch('product_radar.research_ai.shutil.which', return_value='/fixture/codex'), patch('product_radar.research_ai.subprocess.run', return_value=login), patch('product_radar.research_ai.subprocess.Popen', side_effect=launch):
        Jobs(memory, CodexRunner(CONFIG, stop_on_interrupt=True)).run('A', PAYLOAD)
    (root / 'incorrectly-continued').touch()
except KeyboardInterrupt:
    raise SystemExit(130)
""")
        child = subprocess.Popen([sys.executable, "-u", str(script), "--tick", "--execute"], cwd=self.root,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        model_pid = None
        try:
            deadline = time.monotonic() + 10
            while not model_marker.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(model_marker.exists(), "The local model fixture did not reach its wait.")
            model_pid = json.loads(model_marker.read_text())["pid"]
            controls.stop(self.root)
            self.assertEqual(child.wait(timeout=10), 130)
            self.assertEqual(controls.status(self.root)["state"], "stopped")
            self.assertFalse((self.root / "incorrectly-continued").exists())
            self.assertIsNone(controls.process_info(model_pid))
            receipts = list(self.memory.root.glob("ai_jobs/*/execution_failure.json"))
            self.assertEqual(len(receipts), 1)
            self.assertIs(json.loads(receipts[0].read_text())["owned_process_finished"], True)
        finally:
            if child.poll() is None:
                child.kill(); child.wait()
            if model_pid is not None and controls.process_info(model_pid) is not None:
                # This exact, newly created fixture owns its own process group.
                import os
                os.killpg(model_pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
