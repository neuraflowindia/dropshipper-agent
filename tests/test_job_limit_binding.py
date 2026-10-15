"""The shared model-job size limit has one owner and is read when used.

These modules are imported lazily. A first import while a test temporarily
changes research_ai.MAX_JOB_BYTES must not keep that value for later work.
Fresh copies are loaded apart from the already imported modules; nothing in
sys.modules is replaced.
"""

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import product_radar
from product_radar import research_ai
from product_radar.models import SafetyError
from product_radar.storage import atomic_json


LAZY_MODULES = ("checklist_repair", "report_narration", "supplier_quote_repair")


def first_import(name):
    path = Path(product_radar.__file__).parent / (name + ".py")
    spec = importlib.util.spec_from_file_location("product_radar._first_import_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class JobLimitBindingTests(unittest.TestCase):
    def test_first_import_under_a_temporary_limit_keeps_no_copy(self):
        with patch.object(research_ai, "MAX_JOB_BYTES", 64):
            modules = {name: first_import(name) for name in LAZY_MODULES}
        for name, module in modules.items():
            with self.subTest(module=name):
                self.assertFalse(hasattr(module, "MAX_JOB_BYTES"))

    def test_quote_repair_reads_the_current_limit_after_a_temporary_one(self):
        with patch.object(research_ai, "MAX_JOB_BYTES", 64):
            module = first_import("supplier_quote_repair")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "intent.json"
            atomic_json(path, {"padding": "x" * 1024})
            self.assertEqual(len(module._load_original(path)["padding"]), 1024)
            with patch.object(research_ai, "MAX_JOB_BYTES", 64), \
                    self.assertRaisesRegex(SafetyError, "SAVED_FILE_SIZE_LIMIT"):
                module._load_original(path)


if __name__ == "__main__":
    unittest.main()
