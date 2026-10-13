"""Explicit local CLI selection; no model, login, network or installed task."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from product_radar.models import SafetyError
from product_radar.research_ai import CodexRunner, Jobs, MODEL
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal


CONFIG = {"model": MODEL, "reasoning_effort": "high", "authentication": "chatgpt", "paid_fallback": False}
PAYLOAD = {"items": [{"id": "public-listing", "source_text": "Synthetic saved public service listing."}]}
RESPONSE = {"results": [{"id": "public-listing", "kind": "service", "products": [], "reason": "Synthetic fixture."}]}
USAGE = {"input_tokens": 20, "cached_input_tokens": 0, "output_tokens": 10}


class CodexExecutableTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.binary = self.root / "installed-codex"
        self.binary.write_bytes(b"synthetic fixture, never executed")
        self.binary.chmod(0o700)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "money"), evidence_type="synthetic")

    def configured(self):
        return CodexRunner({**CONFIG, "executable": str(self.binary)})

    def test_legacy_config_keeps_its_original_path_lookup(self):
        runner = CodexRunner(CONFIG)
        with patch("product_radar.research_ai.shutil.which", return_value="/fixture/path-codex") as lookup:
            self.assertEqual(runner._binary(), "/fixture/path-codex")
            lookup.assert_called_once_with("codex")
        self.assertEqual(runner.identity, "codex-chatgpt-high-v1")

    def test_explicit_installed_binary_does_not_consult_path(self):
        with patch("product_radar.research_ai.shutil.which", side_effect=AssertionError("No silent PATH fallback")):
            runner = self.configured()
            self.assertEqual(runner._binary(), str(self.binary))
        self.assertEqual(runner.identity, CodexRunner(CONFIG).identity)

    def test_invalid_selection_is_rejected_without_login_or_model(self):
        for value in (None, True, 1, [], "", "relative/codex", "~/codex", "/bad\nprogram", "/bad\x00program"):
            with self.subTest(value=value), \
                 patch("product_radar.research_ai.subprocess.run") as login, \
                 patch("product_radar.research_ai.subprocess.Popen") as model:
                with self.assertRaisesRegex(SafetyError, "^AI_CONFIGURATION_CHANGED$"):
                    CodexRunner({**CONFIG, "executable": value})
                login.assert_not_called(); model.assert_not_called()

    def test_missing_directory_or_nonexecutable_selection_never_falls_back(self):
        nonexecutable = self.root / "not-executable"
        nonexecutable.write_bytes(b"not executable")
        nonexecutable.chmod(0o600)
        for value in (self.root / "missing", self.root, nonexecutable):
            with self.subTest(value=value), \
                 patch("product_radar.research_ai.shutil.which", side_effect=AssertionError("No fallback")):
                with self.assertRaisesRegex(SafetyError, "^AI_CODEX_NOT_INSTALLED$"):
                    CodexRunner({**CONFIG, "executable": str(value)})

    def test_installed_symlink_is_valid_like_the_existing_npm_wrapper(self):
        alias = self.root / "codex"
        alias.symlink_to(self.binary)
        runner = CodexRunner({**CONFIG, "executable": str(alias)})
        self.assertEqual(runner._binary(), str(alias))

    def test_removed_binary_is_rechecked_and_not_replaced_by_another_program(self):
        runner = self.configured()
        self.binary.unlink()
        with patch("product_radar.research_ai.shutil.which", side_effect=AssertionError("No fallback")), \
             patch("product_radar.research_ai.subprocess.run") as login, \
             patch("product_radar.research_ai.subprocess.Popen") as model:
            with self.assertRaisesRegex(SafetyError, "^AI_CODEX_NOT_INSTALLED$"):
                Jobs(self.memory, runner).run("A", PAYLOAD)
            login.assert_not_called(); model.assert_not_called()

    def test_explicit_binary_is_used_for_both_login_check_and_isolated_launch(self):
        runner = self.configured()
        process = Mock(returncode=0, stdin=None)
        process.poll.return_value = 0
        process.wait.return_value = 0
        login_result = subprocess.CompletedProcess([], 0, b"Logged in using ChatGPT", b"")
        with patch("product_radar.research_ai.shutil.which", side_effect=AssertionError("No fallback")), \
             patch("product_radar.research_ai.subprocess.run", return_value=login_result) as login, \
             patch("product_radar.research_ai.subprocess.Popen", return_value=process) as launch, \
             patch("product_radar.research_ai.parse_codex_output", return_value=(RESPONSE, USAGE)):
            _job_id, result = Jobs(self.memory, runner).run("A", PAYLOAD)
        self.assertEqual(login.call_args.args[0], [str(self.binary), "login", "status"])
        command = launch.call_args.args[0]
        self.assertEqual(command[0], str(self.binary))
        self.assertEqual(command[command.index("--model") + 1], MODEL)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        for flag in ("--ignore-user-config", "--ephemeral", "--skip-git-repo-check"):
            self.assertIn(flag, command)
        disabled = {command[index + 1] for index, item in enumerate(command) if item == "--disable"}
        self.assertEqual(disabled, {"shell_tool", "unified_exec", "multi_agent", "apps", "hooks", "plugins", "remote_plugin", "goals"})
        self.assertEqual(result["accepted"]["public-listing"], RESPONSE["results"][0])

    def test_configured_binary_does_not_change_saved_job_identity_or_repeat_a_completion(self):
        with patch.object(CodexRunner, "__call__", return_value=(RESPONSE, USAGE)) as model:
            original = Jobs(self.memory, CodexRunner(CONFIG)).run("A", PAYLOAD)
            replay = Jobs(self.memory, self.configured()).run("A", PAYLOAD)
            self.assertEqual(replay, original)
            self.assertEqual(model.call_count, 1)
        self.assertEqual(len(self.memory.snapshot()["jobs"]), 1)


if __name__ == "__main__":
    unittest.main()
