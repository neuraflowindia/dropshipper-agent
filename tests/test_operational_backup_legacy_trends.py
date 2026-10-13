"""Exact historical Trends export layouts in disposable local folders."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar import operational_backup_selection as selection
from product_radar.research_memory import TABLES, ResearchMemory
from product_radar.storage import atomic_bytes, atomic_json
from product_radar.trends_related import parse_related_queries_csv


class LegacyTrendsBackupSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="legacy-trends-selection-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = {table: {} for table in TABLES}

    def fixture(self, *, name="motion-sensor-night-light", scope_verified=True):
        folder = self.root / "media/live/trends" / (name + "-20261006")
        term = name.replace("-", " ")
        raw = ("Category: All categories\n\nWeek," + term + ": (India)\n2026-09-27,17\n").encode()
        metadata = {"term": term, "geo": "IN", "time_range": "today 12-m", "property": "web",
            "kind": "search_term", "collected_at": "2026-10-06T21:46:09.727Z",
            "source_reference": "https://trends.google.com/trends/explore?geo=IN&q=" + term.replace(" ", "%20") + "&hl=en",
            "scope_verified": scope_verified}
        browser = {"term": term, "collected_at": metadata["collected_at"], "status": 200,
            "csv": raw.decode(), "snapshot": {"url": metadata["source_reference"], "download_found": True,
                "text": "India\nPast 12 months\nWeb Search\n" + term}}
        atomic_bytes(folder / "multiTimeline.csv", raw)
        atomic_json(folder / "metadata.json", metadata)
        atomic_json(folder / "browser-capture.json", browser)
        record = {"acquisition_id": "connected-csv-" + sha256(name.encode()).hexdigest()[:40],
            "parsed": {"schema_version": 1, "source": "google_trends", "acquisition_route": "browser_csv",
                "parser_version": "google-trends-csv-v1", "raw_sha256": sha256(raw).hexdigest(),
                "metadata": deepcopy(metadata), "state": "Usable" if scope_verified is True else "Insufficient"}}
        self.state["trends_evidence"]["evidence-" + name] = record
        return folder, metadata, record, raw

    def select(self, **options):
        return selection.select_essential_backup_files(self.state, ({}, {}), root=self.root, multipart=True, **options)

    def rows(self, selected):
        return {row["relative_path"]: row for row in selected["files"]}

    def receipt_fixture(self, *, name="unfamiliar-saved-query", route="connected_playwright_browser_csv",
                        reopened=False, absolute=False, scope_shape="rows", scope_verified=True):
        folder, metadata, record, raw = self.fixture(name=name, scope_verified=scope_verified)
        (folder / "metadata.json").unlink()
        (folder / "browser-capture.json").unlink()
        record["acquisition_id"] = "csv-" + sha256(name.encode()).hexdigest()[:40]
        csv = folder / ("reopened-multiTimeline.csv" if reopened else "multiTimeline.csv")
        if reopened:
            (folder / "multiTimeline.csv").rename(csv)
        scope = {"text": "India\nPast 12 months\nWeb Search", "title": metadata["term"],
                 "url": metadata["source_reference"]}
        if scope_shape != "undated":
            scope["collected_at"] = metadata["collected_at"]
        if scope_shape in ("rows", "undated"):
            scope["rows"] = [["Week", metadata["term"] + ": (India)"], ["2026-09-27", "17"]]
        if scope_shape == "undated":
            scope["download_found"] = True
        scope_path = folder / ("reopened-export-scope.json" if reopened else "scope.json")
        atomic_json(scope_path, scope)
        receipt = {**metadata, "schema_version": 1, "route": route,
                   "csv_sha256": sha256(raw).hexdigest(),
                   "csv_path": str(csv) if absolute else csv.relative_to(self.root).as_posix()}
        if route == "connected_playwright_browser_csv":
            receipt.update(state="Captured", scope_path=scope_path.relative_to(self.root).as_posix())
        else:
            receipt["acquisition_id"] = name + "-20261006"
        path = folder / ("reopened-receipt.json" if reopened else "receipt.json")
        atomic_json(path, receipt)
        return folder, path, receipt, csv, scope_path, record

    def related_fixture(self, *, name="unfamiliar-related-query", scope_verified=True):
        folder, metadata, record, _ = self.fixture(name=name, scope_verified=scope_verified)
        (folder / "multiTimeline.csv").unlink()
        metadata.update(widget="related_queries", displayed_section="Rising")
        raw = ("Category: All categories\n\"" + metadata["term"] + ": (10/6/25 - 10/6/26, India)\"\n\n"
               "TOP\nordinary query,100\n\nRISING\nordinary query,+70%\n").encode()
        atomic_json(folder / "metadata.json", metadata)
        atomic_bytes(folder / "relatedQueries.csv", raw)
        atomic_json(folder / "browser-capture.json", {"term": metadata["term"],
            "collected_at": metadata["collected_at"], "status": 200, "filename": "relatedQueries.csv",
            "csv": raw.decode(), "snapshot": {"url": metadata["source_reference"],
                "text": "India\nPast 12 months\nWeb Search", "widget_text": "Related queries\nRising"}})
        record.update(acquisition_id="connected-related-" + sha256(name.encode()).hexdigest()[:40],
                      evidence_kind="related_queries", base_evidence_id="trends-base-original",
                      base_parsed_hash="b" * 64, parsed=parse_related_queries_csv(raw, metadata))
        return folder, metadata, record, raw

    def test_original_eight_field_metadata_and_canonical_raw_hash_bind_all_three_files(self):
        folder, _, record, _ = self.fixture()
        selected = self.select(); rows = self.rows(selected)
        expected = {(folder / name).relative_to(self.root).as_posix()
            for name in ("metadata.json", "multiTimeline.csv", "browser-capture.json")}
        self.assertEqual(set(rows), expected)
        csv = rows[(folder / "multiTimeline.csv").relative_to(self.root).as_posix()]
        self.assertTrue(csv["required"])
        self.assertEqual(csv["expected_sha256"], record["parsed"]["raw_sha256"])
        self.assertFalse(selected["unsafe"] or selected["unresolved_required"])

    def test_existing_enriched_metadata_form_keeps_its_exact_binding(self):
        folder, metadata, record, _ = self.fixture()
        atomic_json(folder / "metadata.json", {**metadata, "schema_version": 1,
            "route": "connected_tool_browser_csv", "csv_sha256": record["parsed"]["raw_sha256"]})
        selected = self.select()
        self.assertEqual(len(selected["files"]), 3)
        self.assertFalse(selected["unsafe"] or selected["unresolved_required"])

    def test_other_saved_timeline_import_identities_keep_exact_scope_and_raw_hash(self):
        folder, metadata, record, _ = self.fixture()
        for prefix in ("csv-", "reviewed-csv-binding-"):
            for enriched in (False, True):
                with self.subTest(prefix=prefix, enriched=enriched):
                    record["acquisition_id"] = prefix + "1" * 40
                    original = {**metadata, "schema_version": 1, "route": "connected_tool_browser_csv",
                        "csv_sha256": record["parsed"]["raw_sha256"]} if enriched else metadata
                    atomic_json(folder / "metadata.json", original)
                    selected = self.select()
                    self.assertEqual(len(selected["files"]), 3)
                    csv = self.rows(selected)[(folder / "multiTimeline.csv").relative_to(self.root).as_posix()]
                    self.assertEqual(csv["expected_sha256"], record["parsed"]["raw_sha256"])
                    self.assertFalse(selected["unsafe"] or selected["unresolved_required"])

    def test_unrecognized_timeline_import_identities_cannot_bind_a_matching_export(self):
        _, _, record, _ = self.fixture()
        for acquisition in ("unknown-csv-" + "1" * 40, "csv-" + "1" * 39,
                            "csv-" + "1" * 41, "csv-" + "A" * 40,
                            "reviewed-csv-binding-" + "1" * 39,
                            "reviewed-csv-binding-" + "1" * 41):
            with self.subTest(acquisition=acquisition):
                record["acquisition_id"] = acquisition
                selected = self.select()
                self.assertEqual(selected["files"], [])
                self.assertTrue(selected["unresolved_required"])

    def test_original_producer_receipts_bind_declared_csv_and_page_scope_shapes(self):
        cases = ({"scope_shape": "plain"}, {"reopened": True}, {"scope_shape": "rows"},
                 {"scope_shape": "rows"}, {"route": "connected_tool_browser_csv", "absolute": True,
                                           "scope_shape": "undated"})
        for number, options in enumerate(cases):
            with self.subTest(number=number, options=options):
                self.state = {table: {} for table in TABLES}
                _, path, _, csv, scope, record = self.receipt_fixture(name="arbitrary-retained-export-" + str(number), **options)
                selected = self.select(); rows = self.rows(selected)
                self.assertEqual(set(rows), {file.relative_to(self.root).as_posix() for file in (path, csv, scope)})
                self.assertEqual(rows[csv.relative_to(self.root).as_posix()]["expected_sha256"], record["parsed"]["raw_sha256"])
                self.assertFalse(selected["unsafe"] or selected["unresolved_required"])

    def test_producer_receipt_requires_exact_scope_hash_route_and_same_folder_csv(self):
        folder, path, original, csv, _, _ = self.receipt_fixture()
        other = folder.parent / "other-export/multiTimeline.csv"
        private = self.root / "config/local/private-original.csv"
        atomic_bytes(other, csv.read_bytes())
        atomic_bytes(private, csv.read_bytes())
        wrong = ({"term": "different query"}, {"geo": "US"}, {"time_range": "today 3-m"},
                 {"property": "youtube"}, {"kind": "topic"}, {"scope_verified": 1},
                 {"collected_at": "2026-10-06T21:46:10.727Z"},
                 {"source_reference": original["source_reference"] + "&date=today%203-m"},
                 {"route": "unrecognized_browser_csv"}, {"schema_version": True},
                 {"csv_sha256": "a" * 64}, {"csv_sha256": None},
                 {"csv_path": str(other)}, {"csv_path": str(private)},
                 {"csv_path": "../outside/multiTimeline.csv"})
        for fields in wrong:
            with self.subTest(fields=fields):
                atomic_json(path, {**original, **fields})
                selected = self.select()
                self.assertNotIn(csv.relative_to(self.root).as_posix(), self.rows(selected))
                self.assertTrue(selected["unresolved_required"])

    def test_producer_receipt_index_does_not_hash_unrelated_declared_csv(self):
        _, _, _, target, _, _ = self.receipt_fixture(name="exact-receipt-target")
        _, _, _, other, _, _ = self.receipt_fixture(name="unrelated-receipt-export")
        self.state["trends_evidence"].pop("evidence-unrelated-receipt-export")
        original = selection._read
        reads = []
        def read(path, limit, **kwargs):
            reads.append(Path(path))
            if Path(path) == other:
                raise AssertionError("Unrelated receipt does not authorize reading its CSV.")
            return original(path, limit, **kwargs)
        with patch.object(selection, "_read", side_effect=read):
            selected = self.select()
        self.assertIn(target.relative_to(self.root).as_posix(), self.rows(selected))
        self.assertEqual(reads.count(target), 1)
        self.assertNotIn(other, reads)

    def test_producer_receipt_false_scope_is_preserved_and_opaque_paths_are_not_followed(self):
        _, path, receipt, csv, _, record = self.receipt_fixture(scope_verified=False)
        private = self.root / "config/local/auth.json"
        atomic_json(private, {"password": "fictional-never-copy-this"})
        atomic_json(path, {**receipt, "unrelated": {"path": str(private)}})
        before = deepcopy(self.state)
        selected = self.select()
        self.assertIn(csv.relative_to(self.root).as_posix(), self.rows(selected))
        self.assertNotIn(private.relative_to(self.root).as_posix(), self.rows(selected))
        self.assertNotIn("fictional-never-copy-this", str(selected))
        self.assertEqual(self.state, before)
        self.assertEqual(record["parsed"]["state"], "Insufficient")
        self.assertIs(record["parsed"]["metadata"]["scope_verified"], False)

    def test_original_related_export_binds_ten_fields_and_preserves_derived_dates(self):
        folder, _, record, _ = self.related_fixture()
        before = deepcopy(self.state)
        self.assertEqual(record["parsed"]["metadata"]["export_date_range"], {"start": "2025-10-06", "end": "2026-10-06"})
        selected = self.select(); rows = self.rows(selected)
        self.assertEqual(set(rows), {(folder / name).relative_to(self.root).as_posix()
            for name in ("metadata.json", "relatedQueries.csv", "browser-capture.json")})
        self.assertEqual(rows[(folder / "relatedQueries.csv").relative_to(self.root).as_posix()]["expected_sha256"], record["parsed"]["raw_sha256"])
        self.assertFalse(selected["unsafe"] or selected["unresolved_required"])
        self.assertEqual(self.state, before)

    def test_related_export_scope_identity_parser_and_original_bytes_must_match(self):
        folder, metadata, record, raw = self.related_fixture()
        wrong = {"term": "different query", "geo": "US", "time_range": "today 3-m", "property": "youtube",
                 "kind": "topic", "collected_at": "2026-10-06T21:46:10.727Z", "scope_verified": 1,
                 "source_reference": metadata["source_reference"] + "&date=today%203-m",
                 "widget": "interest_over_time", "displayed_section": "Top", "route": "conflicting-route"}
        csv_key = (folder / "relatedQueries.csv").relative_to(self.root).as_posix()
        for field, value in wrong.items():
            with self.subTest(field=field):
                atomic_json(folder / "metadata.json", {**metadata, field: value})
                selected = self.select()
                self.assertNotIn(csv_key, self.rows(selected))
                self.assertTrue(selected["unresolved_required"])
        atomic_json(folder / "metadata.json", metadata)
        for field, value in (("acquisition_id", "unrecognized-related-" + "1" * 40),
                             ("evidence_kind", "timeline"), ("parser_version", "google-trends-related-csv-v1")):
            with self.subTest(field=field):
                target = record["parsed"] if field == "parser_version" else record
                original = target[field]; target[field] = value
                selected = self.select()
                self.assertNotIn(csv_key, self.rows(selected))
                self.assertTrue(selected["unresolved_required"])
                target[field] = original
        atomic_bytes(folder / "relatedQueries.csv", raw + b"changed")
        selected = self.select()
        self.assertNotIn(csv_key, self.rows(selected))
        self.assertIn("BACKUP_SELECTION_HASH_MISMATCH", {row["reason"] for row in selected["unsafe"]})

    def test_related_export_index_does_not_hash_unrelated_csv(self):
        folder, _, _, _ = self.related_fixture(name="target-related-scope")
        other, _, _, _ = self.related_fixture(name="unrelated-related-scope")
        self.state["trends_evidence"].pop("evidence-unrelated-related-scope")
        original = selection._read
        reads = []
        def read(path, limit, **kwargs):
            reads.append(Path(path))
            if Path(path) == other / "relatedQueries.csv":
                raise AssertionError("Do not hash an unrelated related-query export.")
            return original(path, limit, **kwargs)
        with patch.object(selection, "_read", side_effect=read):
            selected = self.select()
        self.assertIn((folder / "relatedQueries.csv").relative_to(self.root).as_posix(), self.rows(selected))
        self.assertEqual(reads.count(folder / "relatedQueries.csv"), 1)
        self.assertNotIn(other / "relatedQueries.csv", reads)

    def test_related_false_scope_remains_insufficient_and_lookup_does_not_hide_interruptions(self):
        folder, _, record, _ = self.related_fixture(scope_verified=False)
        before = deepcopy(self.state)
        selected = self.select()
        self.assertEqual(len(selected["files"]), 3)
        self.assertEqual(self.state, before)
        self.assertEqual(record["parsed"]["state"], "Insufficient")
        with patch.object(selection, "_read", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.select()
        def failed_freeze(raw):
            raise PermissionError("immutable backup store failed")
        with self.assertRaisesRegex(PermissionError, "immutable backup store failed"):
            self.select(freeze_bytes=failed_freeze)

    def test_related_derived_dates_are_verified_even_when_csv_was_already_selected(self):
        folder, _, record, _ = self.related_fixture()
        csv = folder / "relatedQueries.csv"
        record["parsed"]["metadata"]["export_date_range"]["start"] = "2024-10-06"
        selected = selection._Selection(self.state, ({}, {}), self.root, multipart=True)
        selected.add(csv, "source_capture", "other-exact-reference", byte_hash=record["parsed"]["raw_sha256"])
        selected.trends("evidence-unfamiliar-related-query", record)
        self.assertEqual(selected.files[str(csv)]["origins"], ["other-exact-reference"])
        self.assertIn("BACKUP_SELECTION_TRENDS_RELATED_METADATA_CHANGED", {row["reason"] for row in selected.unsafe})
        self.assertIn("BACKUP_SELECTION_TRENDS_RAW_REFERENCE_MISSING", {row["reason"] for row in selected.unresolved})

    def test_related_derived_fields_cannot_be_changed_partial_or_downgraded(self):
        folder, _, record, _ = self.related_fixture()
        original = deepcopy(record["parsed"]["metadata"])
        csv_key = (folder / "relatedQueries.csv").relative_to(self.root).as_posix()
        cases = ({**original, "export_date_format": "day/month/year"},
                 {name: value for name, value in original.items() if name != "export_date_format"},
                 {**original, "route": "different-route"},
                 {**original, "schema_version": 1})
        for metadata in cases:
            with self.subTest(metadata=metadata):
                record["parsed"]["metadata"] = metadata
                selected = self.select()
                self.assertNotIn(csv_key, self.rows(selected))
                self.assertTrue(selected["unresolved_required"])
        record["parsed"]["metadata"] = original

    def test_related_unknown_parsed_schema_cannot_downgrade_to_legacy_shape(self):
        folder, _, record, _ = self.related_fixture()
        original = deepcopy(record["parsed"])
        csv_key = (folder / "relatedQueries.csv").relative_to(self.root).as_posix()
        for extra in ({"acquisition_route": "conflicting-route"}, {"source": "different-source"},
                      {"schema_version": 1}, {"route": "conflicting-route"}, {"unknown": None}):
            with self.subTest(extra=extra):
                record["parsed"] = {**original, **extra}
                selected = self.select()
                self.assertNotIn(csv_key, self.rows(selected))
                self.assertTrue(selected["unresolved_required"])
        record["parsed"] = original

    def test_related_semantic_validation_precedes_registration_and_freeze(self):
        folder, _, record, raw = self.related_fixture()
        record["parsed"]["metadata"]["export_date_range"]["start"] = "2024-10-06"
        frozen = []
        selected = self.select(freeze_bytes=frozen.append)
        self.assertNotIn((folder / "relatedQueries.csv").relative_to(self.root).as_posix(), self.rows(selected))
        self.assertNotIn(raw, frozen)
        self.assertIn("BACKUP_SELECTION_TRENDS_RELATED_METADATA_CHANGED", {row["reason"] for row in selected["unsafe"]})
        self.assertTrue(selected["unresolved_required"])

    def test_valid_related_cached_csv_is_reread_only_for_its_new_proof_and_not_refrozen(self):
        folder, _, record, raw = self.related_fixture()
        csv = folder / "relatedQueries.csv"
        frozen = []
        selected = selection._Selection(self.state, ({}, {}), self.root, multipart=True, freeze_bytes=frozen.append)
        selected.add(csv, "source_capture", "other-exact-reference", byte_hash=record["parsed"]["raw_sha256"])
        original = selection._read
        reads = []
        def read(path, limit, **kwargs):
            reads.append(Path(path))
            return original(path, limit, **kwargs)
        with patch.object(selection, "_read", side_effect=read):
            selected.trends("evidence-unfamiliar-related-query", record)
        self.assertEqual(reads.count(csv), 1)
        self.assertEqual(frozen.count(raw), 1)
        self.assertFalse(selected.unsafe or selected.unresolved)
        self.assertIn("trends_evidence/evidence-unfamiliar-related-query/original_csv", selected.files[str(csv)]["origins"])

    def test_related_parser_interrupt_and_csv_freeze_failure_are_not_hidden(self):
        folder, _, record, raw = self.related_fixture()
        before = deepcopy(self.state)
        for error in (KeyboardInterrupt, SystemExit):
            with self.subTest(error=error):
                with patch("product_radar.trends_related.parse_related_queries_csv", side_effect=error):
                    with self.assertRaises(error):
                        self.select()
        selected = selection._Selection(self.state, ({}, {}), self.root, multipart=True)
        selected.add(folder / "relatedQueries.csv", "source_capture", "other-exact-reference")
        with patch("product_radar.trends_related.parse_related_queries_csv", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                selected.trends("evidence-unfamiliar-related-query", record)
        def freeze(value):
            if value == raw:
                raise PermissionError("related CSV store refused")
        with self.assertRaisesRegex(PermissionError, "related CSV store refused"):
            self.select(freeze_bytes=freeze)
        self.assertEqual(self.state, before)

    def test_unreadable_unrelated_receipt_does_not_stop_healthy_original_selection(self):
        folder, _, _, _, _, _ = self.receipt_fixture(name="healthy-export")
        _, other, _, _, _, _ = self.receipt_fixture(name="unrelated-export")
        self.state["trends_evidence"].pop("evidence-unrelated-export")
        original = selection._read
        def read(path, limit, **kwargs):
            if Path(path) == other:
                raise selection.SafetyError("BACKUP_FILE_UNAVAILABLE")
            return original(path, limit, **kwargs)
        with patch.object(selection, "_read", side_effect=read):
            selected = self.select()
        self.assertIn((folder / "multiTimeline.csv").relative_to(self.root).as_posix(), self.rows(selected))
        self.assertEqual(len(selected["files"]), 3)
        self.assertFalse(selected["unsafe"] or selected["unresolved_required"])

    def test_malformed_receipt_route_is_isolated_and_does_not_stop_healthy_sibling(self):
        _, path, original, malformed_csv, _, _ = self.receipt_fixture(name="malformed-export")
        folder, _, _, _, _, _ = self.receipt_fixture(name="healthy-export")
        for route in ([], {}):
            with self.subTest(route=route):
                atomic_json(path, {**original, "route": route})
                selected = self.select()
                self.assertIn((folder / "multiTimeline.csv").relative_to(self.root).as_posix(), self.rows(selected))
                self.assertNotIn(malformed_csv.relative_to(self.root).as_posix(), self.rows(selected))
                self.assertTrue(selected["unresolved_required"])

    def test_missing_receipt_or_related_original_stays_explicitly_unfinished(self):
        _, _, _, csv, _, _ = self.receipt_fixture()
        csv.unlink()
        selected = self.select()
        self.assertNotIn(csv.relative_to(self.root).as_posix(), self.rows(selected))
        self.assertTrue(selected["unresolved_required"])
        self.state = {table: {} for table in TABLES}
        folder, _, _, _ = self.related_fixture()
        (folder / "relatedQueries.csv").unlink()
        selected = self.select()
        self.assertNotIn((folder / "relatedQueries.csv").relative_to(self.root).as_posix(), self.rows(selected))
        self.assertTrue(selected["unresolved_required"])

    def test_producer_receipt_unsafe_scope_or_csv_symlink_cannot_certify_original(self):
        folder, path, original, csv, _, _ = self.receipt_fixture()
        other_scope = folder.parent / "different-folder/scope.json"
        private_scope = self.root / "config/local/auth.json"
        atomic_json(other_scope, {"url": original["source_reference"]})
        atomic_json(private_scope, {"password": "fictional-secret"})
        for scope in (other_scope, private_scope):
            with self.subTest(scope=scope):
                atomic_json(path, {**original, "scope_path": str(scope)})
                selected = self.select()
                self.assertNotIn(csv.relative_to(self.root).as_posix(), self.rows(selected))
                self.assertTrue(selected["unresolved_required"])
                self.assertNotIn("fictional-secret", str(selected))
        atomic_json(path, original)
        private = self.root / "config/local/private-original.csv"
        atomic_bytes(private, csv.read_bytes())
        csv.unlink(); csv.symlink_to(private)
        selected = self.select()
        self.assertNotIn(csv.relative_to(self.root).as_posix(), self.rows(selected))
        self.assertIn("SYMLINK_PATH_REFUSED", {row["reason"] for row in selected["unsafe"]})
        self.assertTrue(selected["unresolved_required"])

    def test_explicit_new_schema_conflicts_or_partial_fields_cannot_fall_back_to_old_form(self):
        folder, metadata, record, _ = self.fixture()
        raw_hash = record["parsed"]["raw_sha256"]
        for extra in ({"route": "different-route", "csv_sha256": raw_hash},
                      {"route": "connected_tool_browser_csv", "csv_sha256": "f" * 64},
                      {"route": "connected_tool_browser_csv", "csv_sha256": None},
                      {"route": "connected_tool_browser_csv", "csv_sha256": True},
                      {"route": "connected_tool_browser_csv"}, {"csv_sha256": raw_hash},
                      {"route": None, "csv_sha256": None}, {"unexpected_schema": 1}):
            with self.subTest(extra=extra):
                atomic_json(folder / "metadata.json", {**metadata, **extra})
                selected = self.select()
                self.assertEqual(selected["files"], [])
                self.assertIn("BACKUP_SELECTION_TRENDS_RAW_REFERENCE_MISSING",
                    {row["reason"] for row in selected["unresolved_required"]})

    def test_each_scope_field_must_match_the_exact_saved_canonical_metadata(self):
        folder, metadata, _, _ = self.fixture()
        wrong = {"term": "different light", "geo": "US", "time_range": "today 3-m", "property": "youtube",
            "kind": "topic", "collected_at": "2026-10-06T21:46:10.727Z",
            "source_reference": metadata["source_reference"] + "&date=today%203-m", "scope_verified": False}
        for field, value in wrong.items():
            with self.subTest(field=field):
                atomic_json(folder / "metadata.json", {**metadata, field: value})
                selected = self.select()
                self.assertEqual(selected["files"], [])
                self.assertTrue(selected["unresolved_required"])

    def test_matching_false_scope_can_be_preserved_without_upgrading_research(self):
        self.fixture(scope_verified=False)
        before = deepcopy(self.state)
        selected = self.select()
        self.assertEqual(len(selected["files"]), 3)
        self.assertFalse(selected["unsafe"] or selected["unresolved_required"])
        self.assertEqual(self.state, before)
        self.assertIs(self.state["trends_evidence"]["evidence-motion-sensor-night-light"]["parsed"]["metadata"]["scope_verified"], False)
        self.assertEqual(self.state["trends_evidence"]["evidence-motion-sensor-night-light"]["parsed"]["state"], "Insufficient")

    def test_unknown_or_missing_scope_verification_is_not_inferred_from_browser_text(self):
        folder, metadata, record, _ = self.fixture()
        for value in (None, "true", 1, "missing"):
            with self.subTest(value=value):
                original = deepcopy(metadata)
                if value == "missing":
                    original.pop("scope_verified")
                else:
                    original["scope_verified"] = value
                atomic_json(folder / "metadata.json", original)
                record["parsed"]["metadata"] = deepcopy(original)
                selected = self.select()
                self.assertEqual(selected["files"], [])
                self.assertTrue(selected["unresolved_required"])

    def test_changed_or_missing_csv_stays_an_explicit_limitation(self):
        folder, _, _, _ = self.fixture()
        path = folder / "multiTimeline.csv"
        atomic_bytes(path, b"Changed bytes are not the original export.\n")
        selected = self.select()
        self.assertNotIn(path.relative_to(self.root).as_posix(), self.rows(selected))
        self.assertIn("BACKUP_SELECTION_HASH_MISMATCH", {row["reason"] for row in selected["unsafe"]})
        self.assertTrue(selected["unresolved_required"])
        path.unlink()
        selected = self.select()
        self.assertNotIn(path.relative_to(self.root).as_posix(), self.rows(selected))
        self.assertTrue(selected["unresolved_required"])

    def test_exact_scope_index_does_not_read_unrelated_csv_files(self):
        target, _, _, _ = self.fixture()
        unrelated, _, _, _ = self.fixture(name="unrelated-public-query")
        self.state["trends_evidence"].pop("evidence-unrelated-public-query")
        original = selection._read
        reads = []
        def read(path, limit, **kwargs):
            reads.append(Path(path))
            if Path(path) == unrelated / "multiTimeline.csv":
                raise AssertionError("Do not hash an unrelated export while finding this original.")
            return original(path, limit, **kwargs)
        with patch.object(selection, "_read", side_effect=read):
            selected = self.select()
        self.assertIn((target / "multiTimeline.csv").relative_to(self.root).as_posix(), self.rows(selected))
        self.assertEqual(reads.count(target / "multiTimeline.csv"), 1)
        self.assertNotIn(unrelated / "multiTimeline.csv", reads)

    def test_private_metadata_and_symlinked_csv_do_not_become_safe_originals(self):
        folder, metadata, _, _ = self.fixture()
        atomic_json(folder / "metadata.json", {**metadata, "nested": {"api_key": "fictional-private-value"}})
        selected = self.select()
        self.assertEqual(selected["files"], [])
        self.assertTrue(selected["unresolved_required"])
        self.assertNotIn("fictional-private-value", str(selected))
        atomic_json(folder / "metadata.json", metadata)
        path = folder / "multiTimeline.csv"
        private = self.root / "config/local/private-csv.json"
        atomic_bytes(private, path.read_bytes())
        path.unlink(); path.symlink_to(private)
        selected = self.select()
        self.assertNotIn(path.relative_to(self.root).as_posix(), self.rows(selected))
        self.assertIn("SYMLINK_PATH_REFUSED", {row["reason"] for row in selected["unsafe"]})
        self.assertTrue(selected["unresolved_required"])

    def test_selection_is_pure_and_freezes_only_the_exact_original_checked_bytes(self):
        folder, _, _, _ = self.fixture()
        before = {path: path.read_bytes() for path in folder.iterdir()}
        research = deepcopy(self.state)
        with patch.object(ResearchMemory, "snapshot", side_effect=AssertionError("No history replay")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("No source call")), \
                patch("product_radar.storage.atomic_json", side_effect=AssertionError("No selection writes")), \
                patch("product_radar.storage.atomic_bytes", side_effect=AssertionError("No selection writes")):
            selected = self.select()
        self.assertEqual(len(selected["files"]), 3)
        self.assertEqual({path: path.read_bytes() for path in folder.iterdir()}, before)
        seen = []
        frozen = self.select(freeze_bytes=seen.append)
        self.assertEqual(frozen, selected)
        self.assertCountEqual(seen, before.values())
        self.assertEqual(self.state, research)


if __name__ == "__main__":
    unittest.main()
