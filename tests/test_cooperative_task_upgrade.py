"""Read-only native definition/upgrade planning; no Windows registration."""

from contextlib import redirect_stdout
from io import StringIO
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from product_radar.models import SafetyError
from product_radar.programme_schedule import (NAMESPACE, compare_native_task, native_task_xml,
    plan_cooperative_task_upgrade)

ROOT = Path(__file__).resolve().parents[1]


class CooperativeTaskUpgradeTests(unittest.TestCase):
    def xml(self, **kwargs):
        return native_task_xml("/mnt/d/a project", "Ubuntu", "S-1-5-21-1-2-3-1001",
            live_model=True, funded_quickcommerce_fallback=True, as_of="2026-10-09T10:00:00+00:00", **kwargs)

    def cli(self):
        spec = importlib.util.spec_from_file_location("cooperative_task_definition_cli", ROOT / "scripts/run_programme.py")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module

    def test_definition_option_only_adds_one_exact_execution_flag(self):
        old = ET.fromstring(self.xml()); new = ET.fromstring(self.xml(cooperative_turns=True))
        ns = {"t": NAMESPACE}
        action = "t:Actions/t:Exec/t:Arguments"
        self.assertEqual(new.findtext(action, namespaces=ns), old.findtext(action, namespaces=ns) + " --cooperative-turns")
        new.find(action, ns).text = old.findtext(action, namespaces=ns)
        self.assertEqual(ET.tostring(old), ET.tostring(new))

    def test_upgrade_keeps_original_times_identity_and_model_funding_permissions(self):
        old = self.xml()
        expected = native_task_xml("/mnt/d/a project", "Ubuntu", "S-1-5-21-1-2-3-1001",
            live_model=True, funded_quickcommerce_fallback=True, cooperative_turns=True,
            as_of="2026-10-10T10:00:00+00:00")
        plan = plan_cooperative_task_upgrade(old, expected)
        self.assertTrue(plan["changed"])
        self.assertTrue(compare_native_task(plan["xml"], expected)["matches"])
        root = ET.fromstring(plan["xml"]); original = ET.fromstring(old)
        ns = {"t": NAMESPACE}; action = "t:Actions/t:Exec/t:Arguments"
        root.find(action, ns).text = original.findtext(action, namespaces=ns)
        self.assertEqual(ET.tostring(root), ET.tostring(original))
        self.assertFalse(plan_cooperative_task_upgrade(plan["xml"], expected)["changed"])

    def test_upgrade_refuses_other_task_permission_timing_or_owner_changes(self):
        expected = self.xml(cooperative_turns=True)
        ns = {"t": NAMESPACE}
        fields = [("t:Settings/t:MultipleInstancesPolicy", "Parallel"),
            ("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", "PT1M"),
            ("t:Principals/t:Principal/t:UserId", "S-1-5-21-9-9-9-1001"),
            ("t:Actions/t:Exec/t:Command", "powershell.exe"),
            ("t:Settings/t:Enabled", "false")]
        for path, value in fields:
            changed = ET.fromstring(self.xml()); changed.find(path, ns).text = value
            with self.subTest(path=path), self.assertRaisesRegex(SafetyError, "UPGRADE_REFUSED"):
                plan_cooperative_task_upgrade(ET.tostring(changed, encoding="unicode"), expected)
        for field in ("--live-model", "--funded-quickcommerce-fallback"):
            with self.subTest(field=field), self.assertRaisesRegex(SafetyError, "UPGRADE_REFUSED"):
                plan_cooperative_task_upgrade(self.xml().replace(field, ""), expected)
        with self.assertRaisesRegex(SafetyError, "UPGRADE_REFUSED"):
            plan_cooperative_task_upgrade(self.xml(), self.xml())

    def test_task_preview_can_include_option_without_running_any_research(self):
        output = StringIO()
        with redirect_stdout(output), patch("product_radar.programme.advance_ready_programme_turn", side_effect=AssertionError("must not run")):
            result = self.cli().main(["--task-xml", "--cooperative-turns", "--distribution", "Ubuntu",
                "--windows-user", "S-1-5-21-1-2-3-1001"], root=ROOT)
        self.assertEqual(result, 0)
        self.assertIn("--cooperative-turns", output.getvalue())

    def test_upgrade_cli_is_pure_stdin_to_exact_plan(self):
        payload = {"actual_xml": self.xml(), "expected_xml": self.xml(cooperative_turns=True)}
        output = StringIO()
        with patch("sys.stdin", new=StringIO(json.dumps(payload))), redirect_stdout(output):
            result = self.cli().main(["--plan-cooperative-upgrade"], root=ROOT)
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue()), plan_cooperative_task_upgrade(**payload))

    def test_new_option_requires_actual_boolean(self):
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaisesRegex(SafetyError, "INPUT_INVALID"):
                self.xml(cooperative_turns=value)


if __name__ == "__main__": unittest.main()
