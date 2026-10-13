"""Owned renderer invocation and immutable staging, with no real browser call."""

import base64
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
spec = importlib.util.spec_from_file_location("render_live_pdf", ROOT / "scripts" / "render_live_pdf.py")
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)
from product_radar.models import SafetyError
from product_radar.storage import atomic_bytes


def pdf_bytes(*, landscape=False, empty=False):
    with fitz.open() as document:
        page = document.new_page(width=842 if landscape else 595, height=595 if landscape else 842)
        if not empty:
            page.insert_text((50, 50), "Synthetic fixture PDF only.")
        return document.tobytes()


class FakeNative:
    def __init__(self, *, valid=True, timeout=False, mutate=None):
        self.commands = []
        self.valid, self.timeout, self.mutate = valid, timeout, mutate

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if command[0] == "wslpath":
            return SimpleNamespace(returncode=0, stdout=command[-1], stderr="")
        if self.timeout:
            raise subprocess.TimeoutExpired(command, 10)
        script = base64.b64decode(command[-1]).decode("utf-16le")
        payload = re.search(r"FromBase64String\('([^']+)'\)", script)[1]
        request = json.loads(base64.b64decode(payload))
        output = re.search(r"--print-to-pdf=([^ ]+)", request["arguments"])[1].strip('"')
        atomic_bytes(Path(output), pdf_bytes() if self.valid else b"invalid PDF")
        if self.mutate:
            self.mutate()
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


class RendererTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.html = self.root / "report.html"
        self.pdf = self.root / "report.pdf"
        atomic_bytes(self.html, b"<html><body>Synthetic local report</body></html>")

    def render(self, runner):
        original = Path.is_file
        chrome = renderer.CHROME_CANDIDATES[0]
        with patch.object(Path, "is_file", autospec=True, side_effect=lambda path: True if path == chrome else original(path)):
            return renderer.render(self.html, self.pdf, chrome, runner=runner)

    def test_owned_process_invocation_creates_complete_checked_pdf(self):
        native = FakeNative()
        result = self.render(native)
        self.assertEqual(result["pages"], 1)
        self.assertTrue(result["portrait"])
        self.assertTrue(self.pdf.exists())
        script = base64.b64decode(native.commands[-1][-1]).decode("utf-16le")
        self.assertIn("taskkill.exe /PID $owned.Id /T /F", script)
        self.assertNotIn("/IM", script)
        self.assertIn("-NoProfile", native.commands[-1])
        self.assertEqual(len(result["html_sha256"]), 64)

    def test_existing_pdf_is_never_deleted_or_overwritten(self):
        atomic_bytes(self.pdf, b"Existing accepted bytes")
        with self.assertRaisesRegex(SafetyError, "ALREADY_EXISTS"):
            self.render(FakeNative())
        self.assertEqual(self.pdf.read_bytes(), b"Existing accepted bytes")

    def test_invalid_native_pdf_does_not_create_destination(self):
        with self.assertRaisesRegex(SafetyError, "PDF_INVALID"):
            self.render(FakeNative(valid=False))
        self.assertFalse(self.pdf.exists())

    def test_timeout_does_not_create_partial_destination(self):
        with self.assertRaisesRegex(SafetyError, "TIMEOUT"):
            self.render(FakeNative(timeout=True))
        self.assertFalse(self.pdf.exists())

    def test_changed_html_during_render_cannot_be_accepted(self):
        with self.assertRaisesRegex(SafetyError, "HTML_CHANGED"):
            self.render(FakeNative(mutate=lambda: atomic_bytes(self.html, b"changed")))
        self.assertFalse(self.pdf.exists())

    def test_landscape_and_blank_output_are_rejected(self):
        for options in ({"landscape": True}, {"empty": True}):
            with self.assertRaisesRegex(SafetyError, "LAYOUT_INVALID"):
                renderer.inspect_pdf(pdf_bytes(**options))

    def test_active_html_is_not_run_in_a_browser(self):
        atomic_bytes(self.html, b"<html><script>doSomething()</script></html>")
        native = FakeNative()
        with self.assertRaisesRegex(SafetyError, "HTML_INVALID"):
            self.render(native)
        self.assertEqual(native.commands, [])


if __name__ == "__main__":
    unittest.main()
