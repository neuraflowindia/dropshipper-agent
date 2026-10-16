"""The row expander (``scripts/expand_catalogue_rows.py``): compact heading
rows become catalogue headings in the house style; every refused row is
listed and nothing is written. Local files only.
"""

from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from product_radar import catalogue_rows as rows
from product_radar.catalogue import CATALOGUE_LIMIT, load_catalogue, validate_catalogue
from product_radar import map_materials
from product_radar.map_materials import SETTINGS_LIMIT, validate_settings
from product_radar.models import SafetyError
from product_radar.storage import load_json
import expand_catalogue_rows as script

AT = "2026-10-16T06:00:00Z"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import frozen_research_config  # noqa: E402
# Exact expectations use the r10 settings these tests were written against, not the growing live map.
setUpModule, tearDownModule = frozen_research_config.module_fixtures()


def row(label, sub_area="branch-hk-preparation-tools", **changes):
    value = {"sub_area_id": sub_area, "label": label, "purpose": "strip leaves from " + label.casefold(),
             "included": [label.casefold().rstrip("s")], "excluded": ["herb scissors"],
             "meta_phrase": label.casefold() + " tool", "season": "evergreen"}
    value.update(changes)
    return value


SUB_AREA = {"new_sub_area": {"id": "branch-hk-spice-and-herb-tools", "name": "Spice and herb tools",
                             "routing_words": ["herb", "spice grinder"]}}


class Case(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.catalogue = load_catalogue()
        self.settings = load_json(map_materials.SETTINGS, SETTINGS_LIMIT)

    def expand(self, values):
        return rows.expand_rows(self.catalogue, self.settings, [("rows.jsonl:" + str(n), value)
                                                                for n, value in enumerate(values, 1)], at=AT)

    def refused(self, values):
        with self.assertRaises(rows.RowsRefused) as caught:
            self.expand(values)
        self.assertEqual(str(caught.exception), "CATALOGUE_ROWS_REFUSED")
        return caught.exception.problems


class ExpandTests(Case):
    def test_a_row_becomes_a_heading_in_the_house_style(self):
        catalogue, settings, _ = self.expand([row("Herb leaf strippers", purpose="To strip leaves from herb stems.",
                                                  trends_phrase="herb stripper")])
        family = catalogue["families"][-1]
        existing = next(value for value in self.catalogue["families"] if value["basis"] == "plausible_extension")
        self.assertEqual(set(family), set(existing))
        self.assertEqual(family["id"], "hk-herb-leaf-strippers")
        self.assertEqual((family["parent_id"], family["category"]), ("branch-hk-preparation-tools", "Home and Kitchen"))
        self.assertEqual(family["definition"], "Products whose primary purpose is to strip leaves from herb stems. "
                         "Functional mechanisms and audiences are retained; seller, colour and pack-size changes remain "
                         "within the family.")
        self.assertEqual(family["problem"], "People need to strip leaves from herb stems.")
        self.assertEqual(family["excluded"], ["herb scissors", rows.STANDARD_EXCLUSION])
        self.assertEqual(family["excluded"][-1], existing["excluded"][-1])  # the same standard sentence
        self.assertEqual(family["examples"], [{"label": "herb leaf stripper", "basis": "plausible_extension", "sources": []}])
        self.assertEqual((family["aliases"], family["overlaps"], family["sources"], family["basis"], family["disposition"]),
                         ([], [], [], "plausible_extension", "usable"))
        self.assertEqual([(phrase["source"], phrase["purpose"], phrase["text"]) for phrase in family["phrases"]],
                         [("meta", "discovery", "herb leaf strippers tool"), ("marketplace", "validation", "herb leaf strippers"),
                          ("trends", "validation", "herb stripper")])
        for new, old in zip(family["phrases"][1:], existing["phrases"][1:]):
            self.assertEqual(new["reason"], old["reason"])  # the same reason texts as the existing headings
        self.assertEqual(family["phrases"][0]["reason"], "Look for ads demonstrating how to strip leaves from herb "
                         "stems; broad category and offer-only searches would be less focused.")
        self.assertIn("not demand evidence", family["rationale"])
        self.assertIn("differs from herb scissors", family["rationale"])
        self.assertEqual(family["review_note"], rows.REVIEW_NOTE)
        self.assertEqual((family["author"], family["reviewed_at"], family["next_review"]), (rows.AUTHOR, AT, "2026-11-16"))
        self.assertEqual((catalogue["revision"], catalogue["built_at"]), ("2026-10-16-r11-map", AT))
        self.assertEqual(settings, self.settings)  # no new sub-area: the settings stay
        validate_catalogue(catalogue)

    def test_new_sub_areas_seasons_and_baby_notes(self):
        values = [SUB_AREA, row("Herb leaf strippers", sub_area="branch-hk-spice-and-herb-tools"),
                  row("Baby winter sleep sacks", sub_area="branch-bb-sleep-and-soothing", purpose="keep a baby warm",
                      meta_phrase="baby winter sleep sack", season={"kind": "seasonal", "months": [12, 1, 11]})]
        catalogue, settings, _ = self.expand(values)
        branch = next(value for value in catalogue["branches"] if value["id"] == "branch-hk-spice-and-herb-tools")
        self.assertEqual(branch, {"id": "branch-hk-spice-and-herb-tools", "parent_id": "branch-hk",
                                  "category": "Home and Kitchen", "name": "Spice and herb tools"})
        kitchen = [value["id"] for value in catalogue["branches"] if value["category"] == "Home and Kitchen"]
        self.assertEqual(kitchen[-1], branch["id"])  # placed after its category's sub-areas
        self.assertEqual(settings["branch_words"]["branch-hk-spice-and-herb-tools"], ["herb", "spice grinder"])
        validate_settings(settings, catalogue=catalogue)
        baby = catalogue["families"][-1]
        self.assertEqual(baby["season"], {"kind": "seasonal", "months": [1, 11, 12]})
        self.assertTrue(baby["review_note"].startswith(rows.BABY_NOTE + rows.SEASON_NOTE))
        self.assertEqual(baby["id"], "bb-baby-winter-sleep-sacks")

    def test_ids_stay_unique_and_short(self):
        long_label = "Very long " + " ".join(["adjustable"] * 12) + " holders"
        catalogue, _, _ = self.expand([row("Herb leaf strippers"), row("Herb-leaf strippers!", meta_phrase="other"),
                                       row(long_label[:80], meta_phrase="long holder")])
        ids = [value["id"] for value in catalogue["families"][-3:]]
        self.assertEqual(ids[:2], ["hk-herb-leaf-strippers", "hk-herb-leaf-strippers-2"])
        self.assertLessEqual(len(ids[2]), 80)
        self.assertEqual(len(set(value["id"] for value in catalogue["families"])), len(catalogue["families"]))

    def test_refusals_name_every_bad_row(self):
        existing = self.catalogue["families"][0]
        alias_catalogue = deepcopy(self.catalogue)
        alias_catalogue["families"][1]["aliases"] = ["Spice drawer inserts"]
        problems = self.refused([
            row("Herb leaf strippers", sub_area="branch-hk-unknown"),
            row(existing["label"].upper()),  # normalised duplicate of an existing heading
            row("Kitchen pet bowls"),  # an excluded kind (pets)
            row("Lunch bag", included=["phone case pouch"]),  # an excluded kind in its example
            row("Twice listed heading"), row("twice  LISTED heading"),  # duplicate within the rows
            row("Bad link", meta_phrase="https://example.com"),
            row("Bad season", season={"kind": "seasonal", "months": [13]}),
            row("Bad season two", season="summer"),
            dict(row("Extra field"), colour="red"),
            row("No examples", included=[]),
            {"new_sub_area": {"id": "branch-xx-things", "name": "Things", "routing_words": ["thing"]}},
            {"new_sub_area": {"id": "branch-hk-preparation-tools", "name": "Again", "routing_words": ["again"]}},
            {"new_sub_area": {"id": "branch-hk-loud", "name": "Loud", "routing_words": ["LOUD"]}},
            {"new_sub_area": {"id": "branch-hk-x", "name": "X", "routing_words": ["x"]}, "extra": 1},
            "not a row"])
        codes = [problem.split(": ", 1)[1].split(" (")[0] for problem in problems]
        self.assertEqual(codes, ["CATALOGUE_ROW_SUB_AREA_ID_INVALID", "CATALOGUE_ROW_SUB_AREA_EXISTS",
                                 "CATALOGUE_ROW_ROUTING_WORDS_INVALID", "CATALOGUE_ROW_SUB_AREA_INVALID",
                                 "CATALOGUE_ROW_SUB_AREA_UNKNOWN", "CATALOGUE_ROW_LABEL_DUPLICATE",
                                 "CATALOGUE_ROW_EXCLUDED_KIND", "CATALOGUE_ROW_EXCLUDED_KIND",
                                 "CATALOGUE_ROW_LABEL_DUPLICATE", "CATALOGUE_ROW_PHRASE_INVALID",
                                 "CATALOGUE_ROW_SEASON_INVALID", "CATALOGUE_ROW_SEASON_INVALID",
                                 "CATALOGUE_ROW_FIELDS_INVALID", "CATALOGUE_ROW_EXAMPLES_INVALID",
                                 "CATALOGUE_ROW_FIELDS_INVALID"])
        self.assertTrue(problems[0].startswith("rows.jsonl:12: "))
        self.catalogue = alias_catalogue
        self.assertEqual(self.refused([row("spice DRAWER inserts")])[0].split(": ")[1].split(" (")[0],
                         "CATALOGUE_ROW_LABEL_DUPLICATE")  # an alias counts too

    def test_the_excluded_neighbours_may_name_excluded_kinds(self):
        catalogue, _, _ = self.expand([row("Cushioned floor mats", sub_area="branch-hd-soft-furnishings-and-protection",
                                           excluded=["pet beds"], meta_phrase="cushioned floor mat")])
        self.assertEqual(catalogue["families"][-1]["excluded"][0], "pet beds")

    def test_gate_and_phrase_warnings_do_not_refuse(self):
        meta = next(phrase["text"] for phrase in self.catalogue["families"][0]["phrases"] if phrase["source"] == "meta")
        _, _, warnings = self.expand([row("Zesting widgets", meta_phrase="zesting widget"),
                                      row("Garden kneeling pads", meta_phrase="garden kneeler"),
                                      row("Copy phrase holders", meta_phrase=meta)])
        self.assertIn("rows.jsonl:1: the client-category gate does not place Meta phrase 'zesting widget'", warnings)
        self.assertIn("rows.jsonl:2: the client-category gate places it in Home and Decor, not Home and Kitchen: "
                      "Meta phrase 'garden kneeler'", warnings)
        self.assertIn("rows.jsonl:3: another heading already uses the Meta phrase '" + meta + "'", warnings)

    def test_a_result_that_breaks_the_catalogue_or_settings_is_refused(self):
        settings = deepcopy(self.settings)
        settings["branch_words"].pop("branch-hk-preparation-tools")
        self.settings = settings
        self.assertEqual(self.refused([row("Herb leaf strippers")]), ["result: PRODUCT_MAP_SETTINGS_INVALID"])
        with patch.object(rows.json, "dumps", return_value="x" * (CATALOGUE_LIMIT + 1)):
            with self.assertRaises(rows.RowsRefused) as caught:
                rows.encode_catalogue(self.catalogue)
        self.assertTrue(caught.exception.problems[0].startswith("result: CATALOGUE_TOO_LARGE"))

    def test_helpers(self):
        self.assertEqual(rows.purpose_phrase("To Keep things tidy."), "keep things tidy")
        self.assertEqual(rows.purpose_phrase("  tidy   drawers "), "tidy drawers")
        self.assertEqual(rows.purpose_phrase("UV-sterilise bottles"), "UV-sterilise bottles")
        for bad in ("to", "x", "a\nb", None, "see https://x.y"):
            with self.assertRaisesRegex(SafetyError, "CATALOGUE_ROW_PURPOSE_INVALID"):
                rows.purpose_phrase(bad)
        self.assertEqual(rows.next_review("2026-01-31T06:00:00Z"), "2026-02-28")
        self.assertEqual(rows.next_review("2026-12-15T06:00:00Z"), "2027-01-15")
        with self.assertRaisesRegex(SafetyError, "CATALOGUE_ROW_LABEL_INVALID"):
            rows.family_id("Home and Kitchen", "!!", set())
        for bad in ({"kind": "seasonal", "months": []}, {"kind": "seasonal", "months": [1, 1]},
                    {"kind": "seasonal", "months": [True]}, {"kind": "evergreen", "months": []}):
            with self.assertRaisesRegex(SafetyError, "CATALOGUE_ROW_SEASON_INVALID"):
                rows.season_value(bad)
        self.assertTrue(rows.now().endswith("+00:00"))


class ScriptTests(Case):
    def run_script(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = script.main([str(value) for value in args])
        return code, out.getvalue(), err.getvalue()

    def write_rows(self, name, values, extra=""):
        path = self.folder / name
        path.write_text("\n".join(json.dumps(value) for value in values) + "\n" + extra + "\n")
        return path

    def test_writes_the_catalogue_and_settings_stably(self):
        first = self.write_rows("one.jsonl", [SUB_AREA, row("Herb leaf strippers", sub_area="branch-hk-spice-and-herb-tools")])
        second = self.write_rows("two.jsonl", [row("Garlic peeling tubes", meta_phrase="garlic peeler tube")], extra="   ")
        output, settings = self.folder / "catalogue.json", self.folder / "product_map.json"
        code, out, err = self.run_script(first, second, "--output", output, "--settings-output", settings, "--at", AT)
        self.assertEqual(code, 0, err)
        self.assertIn("2 new headings and 1 new sub-areas; 226 headings in revision 2026-10-16-r11-map", out)
        written = load_json(output, CATALOGUE_LIMIT)
        validate_catalogue(written)
        self.assertEqual([value["label"] for value in written["families"][-2:]], ["Herb leaf strippers", "Garlic peeling tubes"])
        text = output.read_text()
        self.assertTrue(text.startswith('{\n "branches": [\n  {\n   "category"'))  # sorted keys, indent 1
        self.assertEqual(text, rows.encode_catalogue(written).decode())
        saved = load_json(settings, SETTINGS_LIMIT)
        validate_settings(saved, catalogue=written)
        self.assertIn('\n    "branch-hk-spice-and-herb-tools": ["herb", "spice grinder"]\n  }\n}\n', settings.read_text())
        # The settings file already has this layout: rewriting it changes nothing.
        self.assertEqual(rows.encode_settings(self.settings), map_materials.SETTINGS.read_bytes())

    def test_dry_run_and_refusals_write_nothing(self):
        good = self.write_rows("good.jsonl", [row("Herb leaf strippers")])
        output = self.folder / "catalogue.json"
        code, out, _ = self.run_script(good, "--output", output, "--dry-run", "--at", AT)
        self.assertEqual((code, output.exists()), (0, False))
        self.assertIn("Dry run: nothing was written.", out)
        bad = self.write_rows("bad.jsonl", [row("Kitchen pet bowls")], extra="{not json")
        code, _, err = self.run_script(bad, "--output", output)
        self.assertEqual((code, output.exists()), (1, False))
        self.assertIn("bad.jsonl:2: CATALOGUE_ROW_JSON_INVALID", err)
        bad = self.write_rows("bad.jsonl", [row("Kitchen pet bowls")])
        code, _, err = self.run_script(bad, "--output", output)
        self.assertEqual(code, 1)
        self.assertIn("bad.jsonl:1: CATALOGUE_ROW_EXCLUDED_KIND (Kitchen pet bowls)", err)
        code, _, err = self.run_script(good, "--catalogue", self.folder / "missing.json", "--output", output)
        self.assertEqual((code, output.exists()), (1, False))
        self.assertIn("Refused; nothing was written", err)

    def test_the_catalogue_is_written_first_and_put_back_if_the_settings_fail(self):
        rows_file = self.write_rows("one.jsonl", [SUB_AREA, row("Herb leaf strippers", sub_area="branch-hk-spice-and-herb-tools")])
        output, settings = self.folder / "catalogue.json", self.folder / "product_map.json"
        output.write_bytes(b"previous catalogue")
        written, real = [], script._write

        def write(path, encoded):
            written.append(path.name)
            if path == settings:
                raise OSError("disk full")
            real(path, encoded)
        with patch.object(script, "_write", side_effect=write):
            code, _, err = self.run_script(rows_file, "--output", output, "--settings-output", settings, "--at", AT)
        self.assertEqual(code, 1)
        self.assertEqual(written, ["catalogue.json", "product_map.json", "catalogue.json"])
        self.assertEqual((output.read_bytes(), settings.exists()), (b"previous catalogue", False))
        self.assertIn("the settings could not be saved", err)
        fresh = self.folder / "fresh.json"  # no previous catalogue: the new one is removed again
        with patch.object(script, "_write", side_effect=write):
            code, _, _ = self.run_script(rows_file, "--output", fresh, "--settings-output", settings, "--at", AT)
        self.assertEqual((code, fresh.exists()), (1, False))
        code, _, _ = self.run_script(rows_file, "--output", output, "--settings-output", settings, "--at", AT)
        self.assertEqual(code, 0)
        self.assertEqual(oct(output.stat().st_mode & 0o777), "0o644")  # an ordinary readable file

    def test_settings_untouched_without_new_sub_areas(self):
        good = self.write_rows("good.jsonl", [row("Herb leaf strippers")])
        output, settings = self.folder / "catalogue.json", self.folder / "product_map.json"
        code, _, _ = self.run_script(good, "--output", output, "--settings-output", settings, "--at", AT)
        self.assertEqual((code, output.exists(), settings.exists()), (0, True, False))


class ScaleTests(Case):
    def test_two_thousand_headings_fit_and_validate(self):
        values = []
        branches = [value["id"] for value in self.catalogue["branches"] if value["parent_id"] is not None]
        for number in range(1800):
            values.append(row("Synthetic heading number " + str(number), sub_area=branches[number % len(branches)],
                              purpose="hold synthetic thing " + str(number), included=["synthetic holder " + str(number)],
                              meta_phrase="synthetic holder " + str(number)))
        catalogue, _, _ = self.expand(values)
        self.assertEqual(len(catalogue["families"]), 2024)
        encoded = rows.encode_catalogue(catalogue)
        self.assertLess(len(encoded), CATALOGUE_LIMIT)
        self.assertGreater(len(encoded), 4 * 1024 * 1024)  # the old 4 MB bound would have refused it


if __name__ == "__main__":
    unittest.main()
