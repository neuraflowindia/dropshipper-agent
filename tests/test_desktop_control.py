"""Local-only desktop lifecycle tests; no providers, real pools or Windows tasks."""

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

    def test_stopped_worker_does_not_start_research(self):
        set_programme_service_state(self.memory, enabled=False)
        with patch.object(controls.subprocess, "Popen") as launch:
            self.assertEqual(controls.run_worker(self.root, ["--tick", "--execute"])["state"], "stopped")
            launch.assert_not_called()

    def test_existing_legacy_worker_is_adopted_not_duplicated(self):
        with patch.object(controls, "owned_programmes", return_value=[{"pid": 123}]), patch.object(controls.subprocess, "Popen") as launch:
            self.assertEqual(controls.run_worker(self.root, ["--tick", "--execute"])["state"], "already_running")
            launch.assert_not_called()

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
