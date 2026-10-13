"""Visual transport checks using owned local fixture processes, never a model."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.models import SafetyError, canonical
from product_radar.research_ai import CodexRunner, Jobs, MODEL, PROMPT_VERSION, prompt_version
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, load_json
from product_radar.visual_identity import PROMPT_VERSION as D_VERSION, prepare_visual_comparison
from test_visual_identity import description

CONFIG = {"model": MODEL, "reasoning_effort": "high", "authentication": "chatgpt", "paid_fallback": False}
NOW = "2026-10-06T20:00:00+00:00"


class VisualRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.photos = self.root / "public-product-images"
        self.photos.mkdir()
        self.images = []
        for number, (owner, colour) in enumerate((("child-a", "red"), ("concept-a", "blue")), 1):
            path = self.photos / f"source-{number}.jpg"
            Image.new("RGB", (12, 8), colour).save(path)
            self.images.append({"id": f"image-{number}", "owner_id": owner, "path": path.name,
                                "sha256": sha256(path.read_bytes()).hexdigest(),
                                "source_kind": "public_product_image", "source_ref": f"capture-{number}",
                                "source_url": f"https://supplier.example/source-{number}.jpg", "observed_at": NOW})
        self.payload = prepare_visual_comparison(
            "child-a", "semantic-a", description(),
            [{"id": "concept-a", "description": description("Blue sink caddy")}],
            list(reversed(self.images)), media_root=self.photos)["request"]["payload"]
        self.response = {"results": [{"id": "child-a", "decision": "same", "match_id": "concept-a",
                       "reason": "Synthetic transport result, not actual visual inference.",
                       "matching_features": [{"image_ids": ["image-1", "image-2"], "feature": "Same tray structure"}],
                       "differing_features": []}]}
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.runner = CodexRunner(CONFIG)
        self.commands, self.isolated_paths, self.processes = [], [], []

    def launch(self, response=None):
        response = self.response if response is None else response
        events = b"\n".join(canonical(event) for event in [
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": canonical(response).decode()}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 10}}]) + b"\n"
        real_popen = subprocess.Popen
        def fixture_process(command, **kwargs):
            self.assertEqual(command[0], "/fixture/codex")
            self.commands.append(command)
            temporary = Path(command[command.index("--cd") + 1])
            image_paths = [Path(command[n + 1]) for n, value in enumerate(command) if value == "--image"]
            self.assertEqual([path.name for path in image_paths], ["image-1.jpg", "image-2.jpg"])
            for image_path, source in zip(image_paths, self.images):
                self.assertEqual(image_path.parent, temporary)
                self.assertNotEqual(image_path.parent, self.photos)
                self.assertEqual(image_path.read_bytes(), (self.photos / source["path"]).read_bytes())
                self.isolated_paths.append(image_path)
            self.assertEqual(load_json(Path(command[command.index("--output-schema") + 1]))["properties"]["results"]
                             ["items"]["properties"]["decision"]["enum"], ["same", "different", "unclear"])
            self.assertIs(kwargs["start_new_session"], True)
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
            self.assertNotIn("VISUAL_TEST_SECRET", kwargs["env"])
            script = ("import sys;data=sys.stdin.buffer.read();"
                      "assert b'Do not use tools' in data;assert b'attachment_order' in data;"
                      "sys.stdout.buffer.write(" + repr(events) + ")")
            process = real_popen([sys.executable, "-c", script], **kwargs)
            self.processes.append(process)
            return process
        login = subprocess.CompletedProcess([], 0, b"Logged in using ChatGPT", b"")
        with patch("product_radar.research_ai.PUBLIC_PRODUCT_IMAGES", self.photos), \
             patch("product_radar.research_ai.shutil.which", return_value="/fixture/codex"), \
             patch("product_radar.research_ai.subprocess.run", return_value=login), \
             patch("product_radar.research_ai.subprocess.Popen", side_effect=fixture_process):
            return Jobs(self.memory, self.runner).run("D", self.payload)

    def test_public_images_attached_in_bound_order_as_exact_isolated_bytes(self):
        job_id, result = self.launch()
        self.assertEqual(result["accepted"]["child-a"], self.response["results"][0])
        self.assertEqual(result["failures"], {})
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(self.processes[0].returncode, 0)
        self.assertTrue(all(not path.exists() for path in self.isolated_paths))
        saved = load_json(self.memory.root / "ai_jobs" / job_id / "result.json")
        self.assertEqual(saved["response"], self.response)
        self.assertEqual(saved["usage"]["input_tokens"], 10)
        self.assertEqual(self.memory.snapshot()["jobs"][job_id]["request"]["prompt_version"], D_VERSION)

    def test_image_route_preserves_fixed_model_read_only_no_tools_and_clean_environment(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-never-use", "VISUAL_TEST_SECRET": "synthetic"}):
            self.launch()
        command = self.commands[0]
        for flag in ("--ignore-user-config", "--ephemeral", "--skip-git-repo-check", "--json"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertEqual(command[command.index("--model") + 1], "gpt-6-luna")
        configs = [command[n + 1] for n, value in enumerate(command) if value == "--config"]
        self.assertIn('web_search="disabled"', configs)
        self.assertIn('approval_policy="never"', configs)
        self.assertIn('model_reasoning_effort="high"', configs)
        disabled = {command[n + 1] for n, value in enumerate(command) if value == "--disable"}
        self.assertTrue({"shell_tool", "unified_exec", "multi_agent", "apps", "hooks", "plugins", "remote_plugin", "goals"} <= disabled)

    def test_saved_job_replay_reuses_response_without_second_model_or_login(self):
        first = self.launch()
        with patch("product_radar.research_ai.PUBLIC_PRODUCT_IMAGES", self.photos), \
             patch.object(CodexRunner, "__call__", side_effect=AssertionError("saved replay must not launch a model")), \
             patch("product_radar.research_ai.subprocess.run", side_effect=AssertionError("saved replay must not log in")):
            self.assertEqual(Jobs(self.memory, self.runner).run("D", deepcopy(self.payload)), first)
            self.assertEqual(Jobs(self.memory).run("D", deepcopy(self.payload)), first)
        self.assertEqual(len(self.processes), 1)

    def test_missing_and_changed_photo_rejected_before_job_creation_or_model(self):
        path = self.photos / self.images[0]["path"]
        original = path.read_bytes()
        for missing in (True, False):
            if missing:
                path.unlink()
            else:
                path.write_bytes(original + b"changed")
            with patch("product_radar.research_ai.PUBLIC_PRODUCT_IMAGES", self.photos), \
                 patch("product_radar.research_ai.subprocess.Popen", side_effect=AssertionError("invalid image must not launch")), \
                 patch("product_radar.research_ai.subprocess.run", side_effect=AssertionError("invalid image must not log in")):
                with self.assertRaises(SafetyError):
                    Jobs(self.memory, self.runner).run("D", self.payload)
            self.assertEqual(self.memory.snapshot()["jobs"], {})

    def test_absolute_outside_and_parent_paths_rejected_before_launch(self):
        # The outside image is synthetic fixture data, not a personal image.
        outside = self.root / "outside.jpg"
        Image.new("RGB", (12, 8), "green").save(outside)
        for path in (str(outside), "../outside.jpg"):
            payload = deepcopy(self.payload)
            payload["images"][0]["path"] = path
            with patch("product_radar.research_ai.PUBLIC_PRODUCT_IMAGES", self.photos), \
                 patch("product_radar.research_ai.subprocess.Popen", side_effect=AssertionError("outside paths must not launch")):
                with self.assertRaisesRegex(SafetyError, "VISUAL_IMAGE_PATH_INVALID"):
                    Jobs(self.memory, self.runner).run("D", payload)
            self.assertEqual(self.memory.snapshot()["jobs"], {})

    def test_direct_runner_also_rejects_changed_source_before_login_or_launch(self):
        (self.photos / self.images[0]["path"]).write_bytes(b"not original bytes")
        with patch("product_radar.research_ai.PUBLIC_PRODUCT_IMAGES", self.photos), \
             patch("product_radar.research_ai.subprocess.run", side_effect=AssertionError("invalid image must not log in")), \
             patch("product_radar.research_ai.subprocess.Popen", side_effect=AssertionError("invalid image must not launch")):
            with self.assertRaisesRegex(SafetyError, "VISUAL_IMAGE_HASH_MISMATCH"):
                self.runner("D", self.payload, self.root / "unused-job")

    def test_saved_invalid_reference_answer_is_not_accepted_or_recalled(self):
        response = deepcopy(self.response)
        response["results"][0]["match_id"] = "invented-concept"
        first = self.launch(response)
        self.assertEqual(first[1]["accepted"], {})
        self.assertEqual(first[1]["failures"], {"child-a": "VISUAL_SAME_UNSUPPORTED"})
        with patch("product_radar.research_ai.PUBLIC_PRODUCT_IMAGES", self.photos), \
             patch.object(CodexRunner, "__call__", side_effect=AssertionError("saved answer must not be repeated")):
            self.assertEqual(Jobs(self.memory, self.runner).run("D", self.payload), first)

    def test_visual_contract_does_not_change_existing_text_prompt_versions(self):
        self.assertEqual(PROMPT_VERSION, "research-abe-v1")
        self.assertEqual({role: prompt_version(role) for role in ("A", "B", "E")},
                         {"A": "research-abe-v1", "B": "research-b-functional-v2", "E": "research-abe-v1"})
        self.assertEqual(prompt_version("D"), D_VERSION)


if __name__ == "__main__":
    unittest.main()
