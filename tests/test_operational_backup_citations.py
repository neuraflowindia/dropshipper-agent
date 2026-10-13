"""Historical route observations retain citations, not invented webpage bytes."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from product_radar import operational_backup_selection as selection
from product_radar.models import SafetyError, digest
from product_radar.research_memory import TABLES, ResearchMemory, key
from product_radar.storage import atomic_bytes, atomic_json


class LegacyObservationCitationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="legacy-observation-citations-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = {table: {} for table in TABLES}

    def observation(self, **changes):
        record = {"source": "trends", "route": "available_browser_connection",
            "at": "2026-10-06T06:13:13+00:00", "state": "blocked",
            "reason": "HTTP 429; no chart or series was captured.",
            "reference": "https://trends.google.com/trends/explore?date=today%2012-m&geo=IN&q=sink%20organizer"}
        record.update(changes)
        record_id = key("source-check", [record["source"], record["route"], record["at"], record["reference"]])
        return record_id, record

    def family_observation(self, **changes):
        record = {"source": "trends", "route": "connected_public_browser_family_review",
            "at": "2026-10-06T21:53:19.913Z", "state": "usable",
            "reason": "Product-type interest; not evidence of sales for a particular seller or pack.",
            "reference": "https://trends.google.com/trends/explore?geo=IN&q=motion%20sensor%20light&hl=en",
            "concept_id": "concept-82c395d734247f70e444e805f089486d462ced07",
            "description_hash": "c7502ecfc8ad74c3546ff55b7007609e69e1098cf083d7155f641b276ed22921"}
        record.update(changes)
        return "source-check-65443f62cc88da020b7d948183f583ee7f3661df", record

    def install(self, pair):
        record_id, record = pair
        self.state["source_checks"][record_id] = record
        return record_id, record

    def select(self, **options):
        return selection.select_essential_backup_files(self.state, ({}, {}), root=self.root,
                                                       multipart=True, **options)

    def assert_citation_only(self, result, record_id):
        origin = "source_checks/" + record_id + "/reference"
        self.assertFalse([row for row in result["unresolved_required"] if row["origin"] == origin])
        self.assertFalse([row for row in result["unsafe"] if row["origin"] == origin])
        self.assertIn({"origin": origin, "reason": "BACKUP_SELECTION_CITATION_ONLY", "relative_path": None},
                      result["absent_optional"])
        self.assertIn("Public page citations remain in the supplied canonical records; historical webpage bytes are not included.",
                      result["limitations"])

    def assert_not_citation_only(self, result, record_id):
        origin = "source_checks/" + record_id + "/reference"
        self.assertFalse([row for row in result["absent_optional"]
                          if row["origin"] == origin and row["reason"] == "BACKUP_SELECTION_CITATION_ONLY"])
        self.assertTrue([row for field in ("unresolved_required", "unsafe") for row in result[field]
                         if row["origin"] == origin])

    def test_exact_six_field_writer_identity_preserves_only_the_public_citation(self):
        record_id, _ = self.install(self.observation())
        before = deepcopy(self.state)
        result = self.select()
        self.assert_citation_only(result, record_id)
        self.assertEqual(result["files"], [])
        self.assertEqual(result["research_state_hash"], digest(before))
        self.assertEqual(self.state, before)
        self.assertEqual(selection.validate_essential_backup_selection(result, root=self.root), result)

    def test_exact_eight_field_known_family_review_preserves_only_the_public_citation(self):
        record_id, _ = self.install(self.family_observation())
        before = deepcopy(self.state)
        result = self.select()
        self.assert_citation_only(result, record_id)
        self.assertEqual(result["files"], [])
        self.assertEqual(self.state, before)

    def test_marketplace_six_field_observation_keeps_failed_search_state(self):
        record_id, record = self.install(self.observation(source="marketplace", route="amazon_movers_browser",
            state="insufficient", reference="https://www.amazon.in/gp/movers-and-shakers/kitchen"))
        result = self.select()
        self.assert_citation_only(result, record_id)
        self.assertEqual(record["state"], "insufficient")

    def test_actual_writer_contract_is_recognized_without_replaying_research(self):
        from product_radar.research_operations import save_source_check
        memory = Mock()
        _, original = self.observation()
        record_id = save_source_check(memory, **original)
        table, saved_id, record = memory.put.call_args.args
        self.assertEqual((table, saved_id), ("source_checks", record_id))
        self.install((record_id, record))
        self.assert_citation_only(self.select(), record_id)

    def test_safe_public_addresses_and_ordinary_search_flags_need_no_dns_proof(self):
        for reference in ("http://www.example.com/public?show", "https://93.184.215.14/public",
                          "https://[2606:4700:4700::1111]/public"):
            with self.subTest(reference=reference):
                self.state["source_checks"].clear()
                record_id, _ = self.install(self.observation(reference=reference))
                with patch("socket.getaddrinfo", side_effect=AssertionError("Citation selection does not resolve domains")):
                    self.assert_citation_only(self.select(), record_id)

    def test_all_writer_source_and_state_values_have_the_same_narrow_citation_contract(self):
        for source in ("meta", "marketplace", "trends", "supplier"):
            for state in ("usable", "insufficient", "blocked", "failed"):
                with self.subTest(source=source, state=state):
                    self.state["source_checks"].clear()
                    record_id, _ = self.install(self.observation(source=source, state=state))
                    self.assert_citation_only(self.select(), record_id)

    def test_six_field_identity_is_derived_from_exact_original_values_not_similarity(self):
        record_id, record = self.install(self.observation())
        record["reference"] += "&hl=en"
        self.assert_not_citation_only(self.select(), record_id)
        self.state["source_checks"].clear()
        record_id, record = self.observation()
        self.state["source_checks"]["source-check-" + "f" * 40] = record
        self.assert_not_citation_only(self.select(), "source-check-" + "f" * 40)

    def test_six_field_shape_cannot_waive_explicit_receipt_or_hash_bindings(self):
        for extra in ({"kind": "typed-route-observation"}, {"kind": None}, {"capture_hash": "a" * 64},
                      {"receipt_reference": "data/research/captured/direct_trends/bound/receipt.json"},
                      {"receipt_hash": "a" * 64}, {"generation": 1}, {"unexpected": True}):
            with self.subTest(extra=extra):
                self.state["source_checks"].clear()
                record_id, record = self.observation()
                record.update(extra)
                self.install((record_id, record))
                self.assert_not_citation_only(self.select(), record_id)

    def test_observation_validation_does_not_accept_invalid_core_fields(self):
        changes = ({"source": "google"}, {"source": None}, {"state": "unknown"}, {"state": []},
            {"at": "2026-10-06T06:13:13"}, {"at": "not-a-time"}, {"at": None},
            {"route": " "}, {"route": []}, {"route": "route\nforged"}, {"route": "r" * 4097},
            {"reason": " "}, {"reason": None}, {"reason": "reason\rforged"}, {"reason": "r" * 4097})
        for change in changes:
            with self.subTest(change=change):
                self.state["source_checks"].clear()
                record_id, _ = self.install(self.observation(**change))
                self.assert_not_citation_only(self.select(), record_id)

    def test_nonpublic_or_credentialed_urls_are_not_relabeled_as_citations(self):
        references = (
            "https://user:password@example.com/public", "https://user@example.com/public",
            "https://127.0.0.1/public", "https://10.0.0.1/public", "https://169.254.169.254/public",
            "https://192.168.1.2/public", "https://[::1]/public", "https://[fc00::1]/public",
            "https://[::ffff:127.0.0.1]/public", "https://[64:ff9b::a00:1]/public",
            "https://[2002:0a00:0001::]/public", "https://[ff02::1]/public",
            "https://localhost/public", "https://sub.localhost/public", "https://printer.local/public",
            "https://machine.internal/public", "https://device.lan/public", "https://printer/public",
            "https://127.1/public", "https://2130706433/public", "https://0177.0.0.1/public",
            "https://0x7f000001/public", "https://0x7f.0.0.1/public",
            "https://example.com:8443/public", "ftp://example.com/public",
            "https://example.com/public#access_token=private", "https://example.com/public%0dforged",
            "https://example.com\\@127.0.0.1/public", "https://example.com/has space", "https:///public",
        )
        for reference in references:
            with self.subTest(reference=reference):
                self.state["source_checks"].clear()
                record_id, _ = self.install(self.observation(reference=reference))
                self.assert_not_citation_only(self.select(), record_id)

    def test_secret_query_names_cannot_be_hidden_by_case_encoding_or_separators(self):
        names = ("token", "access_token", "API_KEY", "api-token", "password", "client_secret",
                 "refresh_token", "id_token", "cookie", "set-cookie", "secret_access_key", "authorization",
                 "session", "auth", "code", "signature", "x-amz-signature", "x-goog-credential", "key",
                 "auth_token", "session_id", "sessionid", "accessToken", "refreshToken", "clientSecret",
                 "%61pi_key", "%2561pi_key")
        for name in names:
            with self.subTest(name=name):
                self.state["source_checks"].clear()
                record_id, _ = self.install(self.observation(reference="https://example.com/public?" + name + "=private"))
                result = self.select()
                self.assert_not_citation_only(result, record_id)
                self.assertNotIn("=private", str(result))

    def test_six_field_nonfile_placeholders_and_canonical_ids_keep_existing_reference_semantics(self):
        for reference in ("retained-public-receipt", "supplied-manifest-mapping", "not-a-public-url"):
            with self.subTest(reference=reference):
                self.state["source_checks"].clear()
                record_id, _ = self.install(self.observation(reference=reference))
                self.assert_not_citation_only(self.select(), record_id)
        self.state["source_checks"].clear()
        self.state["problems"]["existing-record"] = {"message": "Canonical record, not a file."}
        record_id, _ = self.install(self.observation(reference="existing-record"))
        result = self.select()
        self.assertFalse([row for row in result["absent_optional"]
                          if row["origin"] == "source_checks/" + record_id + "/reference"])
        self.assertFalse(result["unresolved_required"] or result["unsafe"])

    def test_local_file_reference_stays_required_even_with_exact_six_field_identity(self):
        relative = "data/research/captured/direct_trends/ordinary/receipt.json"
        path = self.root / relative
        atomic_json(path, {"original": "Saved receipt bytes."})
        record_id, _ = self.install(self.observation(reference=relative))
        result = self.select()
        self.assertEqual([row["relative_path"] for row in result["files"]], [relative])
        self.assertTrue(result["files"][0]["required"])
        self.assertFalse([row for row in result["absent_optional"]
                          if row["origin"] == "source_checks/" + record_id + "/reference"])
        path.unlink()
        self.assert_not_citation_only(self.select(), record_id)

    def test_family_scope_identity_fields_and_exact_shape_are_mandatory(self):
        changes = ({"source": "marketplace"}, {"route": "different_family_review"}, {"state": "blocked"},
            {"concept_id": "other-record"}, {"concept_id": "concept-"}, {"concept_id": "concept-XYZ"},
            {"concept_id": "concept-" + "f" * 41}, {"concept_id": None},
            {"description_hash": "a" * 63}, {"description_hash": "A" * 64}, {"description_hash": None},
            {"kind": None}, {"capture_hash": "b" * 64}, {"receipt_reference": "missing-proof.json"})
        for change in changes:
            with self.subTest(change=change):
                self.state["source_checks"].clear()
                record_id, _ = self.install(self.family_observation(**change))
                self.assert_not_citation_only(self.select(), record_id)
        record_id, record = self.family_observation()
        for invalid_id in ("other-check", "source-check-short", "source-check-" + "F" * 40):
            with self.subTest(invalid_id=invalid_id):
                self.state["source_checks"].clear()
                self.state["source_checks"][invalid_id] = record
                self.assert_not_citation_only(self.select(), invalid_id)

    def test_family_review_accepts_only_the_known_trends_india_search_page(self):
        references = (
            "http://trends.google.com/trends/explore?geo=IN&q=light&hl=en",
            "https://www.trends.google.com/trends/explore?geo=IN&q=light&hl=en",
            "https://trends.google.com.evil.example/trends/explore?geo=IN&q=light&hl=en",
            "https://trends.google.com/trends/explore/?geo=IN&q=light&hl=en",
            "https://trends.google.com/trends/explore/other?geo=IN&q=light&hl=en",
            "https://trends.google.com/trends/explore?geo=US&q=light&hl=en",
            "https://trends.google.com/trends/explore?q=light&hl=en",
            "https://trends.google.com/trends/explore?geo=IN&hl=en",
            "https://trends.google.com/trends/explore?geo=IN&q=&hl=en",
            "https://trends.google.com/trends/explore?geo=IN&q=%20%20&hl=en",
            "https://trends.google.com/trends/explore?geo=IN&q=light&q=other&hl=en",
            "https://trends.google.com/trends/explore?geo=IN&geo=IN&q=light&hl=en",
            "https://trends.google.com/trends/explore?geo=IN&q=light&hl=hi",
            "https://trends.google.com/trends/explore?geo=IN&q=light&date=today%203-m&hl=en",
            "https://trends.google.com/trends/explore?geo=IN&q=light&extra=true&hl=en",
        )
        for reference in references:
            with self.subTest(reference=reference):
                self.state["source_checks"].clear()
                record_id, _ = self.install(self.family_observation(reference=reference))
                self.assert_not_citation_only(self.select(), record_id)

    def test_family_historical_description_does_not_need_a_current_unchanged_product(self):
        record_id, record = self.install(self.family_observation())
        self.state["concepts"][record["concept_id"]] = {"description": "A later product description."}
        result = self.select()
        self.assert_citation_only(result, record_id)
        self.assertEqual(record["description_hash"], "c7502ecfc8ad74c3546ff55b7007609e69e1098cf083d7155f641b276ed22921")

    def test_changed_family_timestamp_or_reason_is_not_silently_accepted_when_invalid(self):
        for change in ({"at": "2026-10-06T21:53:19"}, {"at": []}, {"reason": ""},
                       {"reason": "bad\x00reason"}, {"route": "r" * 4097}, {"reference": "x" * 4097}):
            with self.subTest(change=change):
                self.state["source_checks"].clear()
                record_id, _ = self.install(self.family_observation(**change))
                self.assert_not_citation_only(self.select(), record_id)

    def test_healthy_required_file_sibling_continues_when_a_citation_is_rejected(self):
        invalid_id, _ = self.install(self.observation(reference="https://localhost/private"))
        relative = "data/research/captured/direct_trends/healthy/receipt.json"
        atomic_json(self.root / relative, {"public_original": True})
        self.state["source_checks"]["healthy-file-check"] = {"reference": relative}
        result = self.select()
        self.assert_not_citation_only(result, invalid_id)
        self.assertIn(relative, {row["relative_path"] for row in result["files"]})

    def test_seven_legacy_missing_supplier_results_remain_required(self):
        citation_id, _ = self.install(self.observation())
        missing = set()
        for index in range(7):
            folder = "data/research/captured/supplier_routes/native/legacy-original-" + str(index)
            native = self.root / folder / "native_result.json"
            wrapper = self.root / folder / "receipt.json"
            atomic_json(wrapper, {"state": "unavailable", "reason": "SUPPLIER_NATIVE_READ_FAILED",
                                  "native_reference": str(native)})
            self.state["source_checks"]["supplier-original-" + str(index)] = {"reference": str(wrapper)}
            missing.add(native.relative_to(self.root).as_posix())
        result = self.select()
        self.assert_citation_only(result, citation_id)
        self.assertEqual({row["relative_path"] for row in result["unresolved_required"]}, missing)
        self.assertEqual(len(result["files"]), 7)

    def test_existing_explicit_unsaved_supplier_result_stays_optional(self):
        folder = self.root / "data/research/captured/supplier_routes/native/explicit-unsaved"
        wrapper = folder / "receipt.json"
        missing = folder / "native_result.json"
        atomic_json(wrapper, {"state": "unavailable", "native_result_saved": False,
                              "native_reference": str(missing)})
        self.state["source_checks"]["supplier-original"] = {"reference": str(wrapper)}
        result = self.select()
        self.assertFalse(result["unresolved_required"] or result["unsafe"])
        self.assertIn(missing.relative_to(self.root).as_posix(),
                      {row["relative_path"] for row in result["absent_optional"]})

    def test_citation_recognition_has_no_history_network_model_or_file_write_side_effect(self):
        record_id, _ = self.install(self.observation())
        self.install(self.family_observation())
        before = deepcopy(self.state)
        with patch.object(ResearchMemory, "snapshot", side_effect=AssertionError("No history replay")), \
                patch("socket.getaddrinfo", side_effect=AssertionError("No DNS lookup")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("No provider call")), \
                patch("product_radar.public_pages.fetch_public_page", side_effect=AssertionError("No page fetch")), \
                patch("product_radar.storage.atomic_json", side_effect=AssertionError("No selection writes")), \
                patch("product_radar.storage.atomic_bytes", side_effect=AssertionError("No selection writes")):
            result = self.select()
        self.assert_citation_only(result, record_id)
        self.assertEqual(self.state, before)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_citation_notes_are_frozen_and_do_not_hide_modified_selection(self):
        record_id, _ = self.install(self.observation())
        result = self.select()
        self.assert_citation_only(result, record_id)
        result["absent_optional"] = []
        with self.assertRaisesRegex(SafetyError, "BACKUP_SELECTION_FINGERPRINT_INVALID"):
            selection.validate_essential_backup_selection(result, root=self.root)

    def test_interruptions_and_caller_freeze_errors_still_propagate(self):
        self.install(self.observation())
        relative = "data/research/captured/direct_trends/healthy/receipt.json"
        atomic_bytes(self.root / relative, b'{"public_original": true}\n')
        self.state["source_checks"]["healthy-file-check"] = {"reference": relative}
        for error in (KeyboardInterrupt(), RuntimeError("caller freeze failed")):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)):
                    self.select(freeze_bytes=lambda raw: (_ for _ in ()).throw(error))

    def test_no_citation_note_is_added_when_selection_has_no_legacy_web_observation(self):
        result = self.select()
        self.assertFalse([row for row in result["absent_optional"] if row["reason"] == "BACKUP_SELECTION_CITATION_ONLY"])
        self.assertFalse([text for text in result["limitations"] if "historical webpage bytes" in text])


if __name__ == "__main__":
    unittest.main()
