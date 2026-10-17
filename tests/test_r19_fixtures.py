"""The R19 fixtures (trimmed real pages of 16-17 October 2026) load, are listed, and hold no account data.

Their description is a plain-text README.txt: Markdown documents belong under docs/ (test_documentation_layout).
"""

import csv
import gzip
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent / "fixtures" / "r19"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MOBILE = re.compile(r"(?<![\w])(?:\+91[\s-]?|91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?![\w])")


def fixture_files():
    return sorted(path for path in ROOT.rglob("*") if path.is_file() and path.name != "README.txt")


def text_of(path):
    raw = path.read_bytes()
    if path.suffix == ".gz":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8-sig")


def kind_of(path):
    name = path.name[:-3] if path.suffix == ".gz" else path.name
    return name.rsplit(".", 1)[-1]


class R19FixtureTests(unittest.TestCase):
    def test_every_fixture_file_loads(self):
        files = fixture_files()
        self.assertGreaterEqual(len(files), 60)
        for path in files:
            with self.subTest(path=str(path.relative_to(ROOT))):
                text, kind = text_of(path), kind_of(path)
                self.assertIn(kind, ("json", "html", "csv"))
                if kind == "json":
                    self.assertIsInstance(json.loads(text), (dict, list))
                elif kind == "html":
                    parser = HTMLParser()
                    parser.feed(text)
                    parser.close()
                    self.assertIn("<", text)
                else:
                    rows = list(csv.reader(io.StringIO(text)))
                    self.assertGreater(len(rows), 2)

    def test_each_fixture_is_small(self):
        for path in fixture_files():
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertLess(path.stat().st_size, 300 * 1024)

    def test_readme_describes_every_fixture_folder(self):
        readme = (ROOT / "README.txt").read_text(encoding="utf-8")
        folders = {path.parent.relative_to(ROOT).parts[0] for path in fixture_files()
                   if path.parent != ROOT}
        for folder in sorted(folders):
            with self.subTest(folder=folder):
                self.assertIn("`" + folder + "/", readme)
        for path in ROOT.glob("*.json"):
            self.assertIn("`" + path.name + "`", readme)

    def test_no_email_address_or_mobile_number_is_left(self):
        for path in fixture_files():
            with self.subTest(path=str(path.relative_to(ROOT))):
                text = text_of(path)
                self.assertEqual({value for value in EMAIL.findall(text)} - {"user@example.invalid"}, set())
                self.assertEqual(MOBILE.findall(text), [])

    def test_the_34_product_types_of_16_october(self):
        sheet = json.loads(text_of(ROOT / "types-2026-10-16.json"))
        self.assertEqual(sheet["count"], 34)
        self.assertEqual(len({row["concept_id"] for row in sheet["types"]}), 34)
        self.assertTrue(sheet["_notes"])
        examples = sheet["_discussion_paper_examples"]
        self.assertEqual([examples[key]["known_now"] for key in sorted(examples) if key.startswith("concept-")],
                         [36.5, 10.5, 40.5])
        ids = {row["concept_id"] for row in sheet["types"]}
        self.assertLessEqual({key for key in examples if key.startswith("concept-")}, ids)


if __name__ == "__main__":
    unittest.main()
