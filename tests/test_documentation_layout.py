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
# The owner's agent instruction files live at the project root and are never
# edited by agents. Every other narrative document belongs under docs/.
OWNER_ROOT_DOCUMENTS = {"AGENTS.md", "CLAUDE.md"}
# Dated archives of earlier output are git-ignored, so a fresh checkout has no
# archive/ folder. Links into it are checked wherever the folder exists.
ARCHIVE = ROOT / "archive"


def archive_is_git_ignored():
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    return "/archive/" in {line.strip() for line in lines}


class DocumentationLayoutTests(unittest.TestCase):
    def test_narrative_documents_are_categorized(self):
        self.assertEqual([path.name for path in ROOT.glob("*.md")
                          if path.name not in OWNER_ROOT_DOCUMENTS], [])
        for folder in ("media", "outputs", "preview", "scripts", "tests"):
            with self.subTest(folder=folder):
                self.assertEqual(list((ROOT / folder).rglob("*.md")), [])

    def test_document_relative_links_exist(self):
        # Project links use ordinary inline Markdown. External URLs are not
        # fetched: this check must remain offline and consume no provider credit.
        absent_archive = []
        for document in (ROOT / "docs").rglob("*.md"):
            for destination in re.findall(r"\]\(([^)\n]+)\)", document.read_text(encoding="utf-8")):
                parsed = urlsplit(destination.strip("<>"))
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                target = document.parent / unquote(parsed.path)
                if not ARCHIVE.exists() and target.resolve().is_relative_to(ARCHIVE):
                    absent_archive.append(target)
                    continue
                with self.subTest(document=document.relative_to(ROOT), link=destination):
                    self.assertTrue(target.exists(), f"Missing link target: {target}")
        if absent_archive:
            with self.subTest(archive_links=len(absent_archive)):
                # Only a folder that git never tracks may be absent here.
                self.assertTrue(archive_is_git_ignored(), "archive/ must stay git-ignored")
                self.skipTest(f"{len(absent_archive)} links point into the git-ignored archive/ "
                              "folder, which is absent from this checkout")

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
