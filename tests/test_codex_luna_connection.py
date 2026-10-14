"""Offline checks for rejecting failed, invented or tool-assisted probe results."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_codex_luna_connection.py"
spec = importlib.util.spec_from_file_location("luna_check", SCRIPT)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)
USAGE = {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 50}


def events(result=None, extra=(), usage=None):
    values = [
        {"type": "thread.started", "thread_id": "fictional"},
        {"type": "turn.started"},
        *extra,
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(check.EXPECTED if result is None else result)}},
        {"type": "turn.completed", "usage": USAGE if usage is None else usage},
    ]
    return "\n".join(json.dumps(value) for value in values).encode()


class CodexLunaConnectionTests(unittest.TestCase):
    def test_success_records_only_usage(self):
        self.assertEqual(check.parse_events(events()), USAGE)

    def test_invented_facts_and_wrong_types_fail(self):
        for changes in ({"monthly_sales": 10000}, {"supplier_url": "https://example.com"},
                        {"decision": "ready"}, {"observed_ads": 7.0},
                        {"evidence_ids": ["invented"]}, {"extra": "ignored"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                check.parse_events(events({**check.EXPECTED, **changes}))

    def test_tool_activity_and_failed_turns_fail(self):
        for value in ({"type": "error"}, {"type": "turn.failed"},
                      {"type": "turn.started"},
                      {"type": "item.started", "item": {"type": "command_execution"}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                check.parse_events(events(extra=[value]))

    def test_malformed_or_incomplete_output_fails(self):
        for raw in (b"", b"null", b"{}", b"not json", b"x" * 65537):
            with self.subTest(raw=raw[:20]), self.assertRaises(ValueError):
                check.parse_events(raw)
        for usage in ({**USAGE, "input_tokens": True}, {**USAGE, "output_tokens": -1},
                      {**USAGE, "cached_input_tokens": 101}):
            with self.assertRaises(ValueError):
                check.parse_events(events(usage=usage))

    def test_child_does_not_inherit_provider_secrets(self):
        with patch.dict(check.os.environ, {"PATH": "/fictional", "HOME": "/fictional-home",
                                          "OPENAI_API_KEY": "private", "CODEX_API_KEY": "private",
                                          "APIFY_TOKEN": "private", "QUICKCOMMERCE_API_KEY": "private"}, clear=True):
            self.assertEqual(check.child_environment(), {"PATH": "/fictional", "HOME": "/fictional-home"})

    def test_api_key_authentication_never_starts_inference(self):
        configuration = json.loads(check.CONFIG.read_text(encoding="utf-8"))
        configuration.pop("executable", None)  # This legacy fixture must not depend on this computer's program.
        with patch.object(check.shutil, "which", return_value="/fictional/codex"), \
                patch.object(check.subprocess, "run", return_value=Mock(returncode=0, stdout=b"Logged in using API key", stderr=b"")), \
                patch.object(check.subprocess, "Popen") as process:
            with self.assertRaises(ValueError):
                check.run_probe(configuration)
            process.assert_not_called()

    def test_legacy_probe_uses_the_shared_path_lookup(self):
        configuration = json.loads(check.CONFIG.read_text(encoding="utf-8"))
        configuration.pop("executable", None)
        signed_out = Mock(returncode=0, stdout=b"Logged in using API key", stderr=b"")
        with patch.object(check.shutil, "which", return_value="/fictional/legacy-codex") as lookup, \
             patch.object(check.subprocess, "run", return_value=signed_out) as login, \
             patch.object(check.subprocess, "Popen") as process:
            with self.assertRaisesRegex(ValueError, "subscription sign-in not confirmed"):
                check.run_probe(configuration)
            lookup.assert_called_once_with("codex")
            self.assertEqual(login.call_args.args[0], ["/fictional/legacy-codex", "login", "status"])
            process.assert_not_called()

    def test_explicit_probe_selection_uses_the_same_validated_binary_without_path_fallback(self):
        configuration = json.loads(check.CONFIG.read_text(encoding="utf-8"))
        signed_out = Mock(returncode=0, stdout=b"Logged in using API key", stderr=b"")
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "codex"
            binary.write_bytes(b"synthetic fixture, never executed")
            binary.chmod(0o700)
            configuration["executable"] = str(binary)
            with patch.object(check.shutil, "which", side_effect=AssertionError("No fallback")), \
                 patch.object(check.subprocess, "run", return_value=signed_out) as login, \
                 patch.object(check.subprocess, "Popen") as process:
                with self.assertRaisesRegex(ValueError, "subscription sign-in not confirmed"):
                    check.run_probe(configuration)
                self.assertEqual(login.call_args.args[0], [str(binary), "login", "status"])
                process.assert_not_called()

    def test_invalid_optional_probe_executable_keeps_safe_diagnostic_and_no_request(self):
        configuration = json.loads(check.CONFIG.read_text(encoding="utf-8"))
        configuration["executable"] = "relative/private-diagnostic-path"
        with patch.object(check.subprocess, "run") as login, \
             patch.object(check.subprocess, "Popen") as process:
            with self.assertRaisesRegex(ValueError, "^unexpected configuration$"):
                check.run_probe(configuration)
            login.assert_not_called(); process.assert_not_called()

    def test_timeout_terminates_only_owned_process_and_does_not_retry(self):
        configuration = json.loads(check.CONFIG.read_text(encoding="utf-8"))
        configuration.pop("executable", None)  # The synthetic process is selected by the patched PATH lookup.
        process = Mock(pid=43210)
        process.communicate.side_effect = check.subprocess.TimeoutExpired("fictional", 90)
        process.wait.side_effect = [check.subprocess.TimeoutExpired("fictional", 5), 0]
        with patch.object(check.shutil, "which", return_value="/fictional/codex"), \
                patch.object(check.subprocess, "run", return_value=Mock(returncode=0, stdout=b"", stderr=b"Logged in using ChatGPT")), \
                patch.object(check.subprocess, "Popen", return_value=process) as start, \
                patch.object(check.os, "killpg") as terminate:
            with self.assertRaises(ValueError):
                check.run_probe(configuration)
            self.assertEqual(start.call_count, 1)
            self.assertEqual(terminate.call_args_list[0].args, (43210, check.signal.SIGTERM))
            self.assertEqual(terminate.call_args_list[1].args, (43210, check.signal.SIGKILL))
            self.assertTrue(start.call_args.kwargs["start_new_session"])

    def test_live_is_explicit_and_errors_are_private(self):
        with patch.object(check, "run_probe") as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(check.main([]), 2)
            run.assert_not_called()
        output = io.StringIO()
        passthrough = lambda inner, config, root: inner
        with patch.object(check, "run_probe", side_effect=RuntimeError("private-diagnostic")), \
                patch.object(check, "watched_codex_runner", side_effect=passthrough), contextlib.redirect_stdout(output):
            self.assertEqual(check.main(["--live"]), 1)
        self.assertNotIn("private-diagnostic", output.getvalue())

    def test_live_probe_runs_only_behind_the_money_guard(self):
        refusing = Mock(side_effect=check.SafetyError("AI_PLAN_LIMIT_CREDITS_PROTECTED"))
        output = io.StringIO()
        with patch.object(check, "run_probe") as run, \
                patch.object(check, "watched_codex_runner", return_value=refusing) as guard, contextlib.redirect_stdout(output):
            self.assertEqual(check.main(["--live"]), 1)
        run.assert_not_called()
        self.assertIn("AI_PLAN_LIMIT_CREDITS_PROTECTED", output.getvalue())
        self.assertEqual(guard.call_args.kwargs["root"], check.ROOT)
        output = io.StringIO()
        with patch.object(check, "run_probe", return_value={"structured_evidence_check": "passed"}) as run, \
                patch.object(check, "watched_codex_runner", side_effect=lambda inner, config, root: inner), \
                contextlib.redirect_stdout(output):
            self.assertEqual(check.main(["--live"]), 0)
        run.assert_called_once()
        self.assertIn("PASS", output.getvalue())


if __name__ == "__main__":
    unittest.main()
