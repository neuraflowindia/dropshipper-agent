"""Regression checks for document moves; never render or overwrite client outputs."""

from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
GUIDES = ROOT / "docs/client-preview/guides"


class DocumentationLayoutTests(unittest.TestCase):
    def test_narrative_documents_are_categorized(self):
        self.assertEqual(list(ROOT.glob("*.md")), [])
        for folder in ("media", "outputs", "preview", "scripts", "tests"):
            with self.subTest(folder=folder):
                self.assertEqual(list((ROOT / folder).rglob("*.md")), [])

    def test_document_relative_links_exist(self):
        # Project links use ordinary inline Markdown. External URLs are not
        # fetched: this check must remain offline and consume no provider credit.
        for document in (ROOT / "docs").rglob("*.md"):
            for destination in re.findall(r"\]\(([^)\n]+)\)", document.read_text(encoding="utf-8")):
                parsed = urlsplit(destination.strip("<>"))
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                target = document.parent / unquote(parsed.path)
                with self.subTest(document=document.relative_to(ROOT), link=destination):
                    self.assertTrue(target.exists(), f"Missing link target: {target}")

    def test_support_document_regeneration_preserves_approved_content(self):
        with tempfile.TemporaryDirectory(prefix="neuraflow-doc-layout-") as temporary:
            staging = Path(temporary)
            preview = staging / "preview"
            guides = staging / "guides"
            subprocess.run(
                [sys.executable, str(ROOT / "scripts/generate_client_preview_package.py"),
                 "--preview-dir", str(preview), "--docs-dir", str(guides)],
                cwd=ROOT, check=True, capture_output=True, text=True,
            )
            for name in ("brochure.html", "brochure.css", "offer-summary.html"):
                with self.subTest(source=name):
                    self.assertEqual((preview / name).read_bytes(),
                                     (ROOT / "preview/brochure" / name).read_bytes())
            expected = sorted(GUIDES.glob("*.md"))
            self.assertEqual(len(expected), 2)
            self.assertEqual(sorted(p.name for p in guides.glob("*.md")),
                             [p.name for p in expected])
            for source in expected:
                with self.subTest(guide=source.name):
                    self.assertEqual((guides / source.name).read_bytes(), source.read_bytes())

    def test_all_package_tools_expose_document_destination(self):
        for script in ("generate_client_preview_package.py", "render_client_preview_package.py",
                       "validate_client_preview_package.py"):
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / script), "--help"],
                cwd=ROOT, check=True, capture_output=True, text=True,
            )
            with self.subTest(script=script):
                self.assertIn("--docs-dir", result.stdout)


if __name__ == "__main__":
    unittest.main()
