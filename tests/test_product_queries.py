"""Shared product-type wording, synthetic only; no public requests or real AI."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.models import SafetyError, digest
from product_radar.product_queries import (LEGACY_PROMPT_VERSION, MODE, PROMPT_VERSION, SOURCES, current_query_profile,
    ensure_query_profiles, product_query_terms, query_profile_input, repair_query_contract, validate_query_profile)
from product_radar.research_ai import (CATALOGUE_PROMPT_VERSION, Jobs, instructions_for,
                                     prompt_version, schema_for, validate_results)
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal
from product_radar.supplier_discovery import discover_saved_supplier_leads, plan_supplier_search
from test_catalogue_expansion import fixture as catalogue_fixture
from test_supplier_discovery import Reader

NOW = "2026-10-07T09:00:00+00:00"
DESCRIPTION = {"label": "EXAMPLE STORE Premium Expandable Door Hanging Pull-Up Bar for Men, Home Exercise, Black",
    "function": "Perform pull-ups using a bar mounted in a doorway", "mechanism": "Telescopic adjustable bar",
    "audience": "Adults", "category": "Travel and Outdoor", "features": ["doorway mounted", "adjustable width"],
    "brand": "generic_or_small", "basis": "observed", "reason": "Synthetic product description only."}


def answer(item):
    return {"id": item["id"], "core_type": "pull-up bar", "subtype": "doorway pull-up bar",
        "aliases": ["telescopic pull-up bar", "door adjustable pull-up bar"],
        "mechanism_anchors": ["doorway mounted", "adjustable width"], "broad_context": ["pull-up bar"],
        "queries": [{"source": source, "text": text, "scope": scope, "reason": "Synthetic related-type wording."}
                    for source in SOURCES for text, scope in (("doorway pull-up bar", "subtype"),
                        ("telescopic pull-up bar", "family"), ("pull-up bar", "context"))],
        "references": [item["description_ref"]], "reason": "Search the object and mechanism, not manufacturer marketing."}


class Runner:
    identity = "synthetic-shared-query-v1"

    def __init__(self, mutate=None):
        self.calls, self.mutate = [], mutate

    def __call__(self, role, payload, folder):
        self.calls.append((role, deepcopy(payload)))
        assert role == "C" and payload["mode"] == MODE
        values = [answer(item) for item in payload["items"]]
        if self.mutate:
            self.mutate(values)
        return {"results": values}, {"input_tokens": 20, "cached_input_tokens": 0, "output_tokens": 20}


class ProductQueryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.catalogue = catalogue_fixture()
        self.memory.put("concepts", "bar", {"description": deepcopy(DESCRIPTION), "status": "provisional"})

    def prepare(self, runner=None, **options):
        return ensure_query_profiles(self.memory, runner, catalogue=self.catalogue, as_of=NOW, **options)

    def test_explicit_empty_list_or_tuple_returns_exact_empty_result_without_read_or_lock(self):
        expected = {"records": [], "reused": [], "pending": [], "job_ids": [],
            "recovered_job_ids": [], "contract_repairs": [], "local_failures": [],
            "provider_requests": 0, "client_ready": False}
        before = self.memory.snapshot_with_provenance()
        runner = Runner()
        for selected in ([], ()):
            with self.subTest(selected=selected), \
                    patch.object(self.memory, "snapshot", side_effect=AssertionError("Empty scope read history")), \
                    patch("product_radar.product_queries.Journal", side_effect=AssertionError("Empty scope acquired a lock")):
                result = self.prepare(runner, concept_ids=selected)
            self.assertEqual(result, expected)
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.memory.snapshot_with_provenance(), before)
        self.assertFalse((self.memory.root / "query_profiles").exists())

    def test_none_still_reads_all_concepts_and_prepares_the_missing_profile(self):
        runner = Runner()
        with patch.object(self.memory, "snapshot", wraps=self.memory.snapshot) as snapshots:
            result = self.prepare(runner, concept_ids=None)
        self.assertGreater(snapshots.call_count, 0)
        self.assertEqual([row["concept_id"] for row in result["records"]], ["bar"])
        self.assertEqual(result["pending"], [])
        self.assertEqual(result["reused"], [])
        self.assertEqual(len(runner.calls), 1)

    def test_empty_scope_does_not_claim_existing_profiles_were_reused_or_completed(self):
        runner = Runner()
        first = self.prepare(runner)
        self.assertEqual([row["concept_id"] for row in first["records"]], ["bar"])
        before = self.memory.snapshot_with_provenance()
        for selected in ([], ()):
            with self.subTest(selected=selected), \
                    patch.object(self.memory, "snapshot", side_effect=AssertionError("Empty scope read history")):
                result = self.prepare(runner, concept_ids=selected)
            self.assertTrue(all(result[name] == [] for name in (
                "records", "reused", "pending", "job_ids", "recovered_job_ids", "contract_repairs", "local_failures")))
            self.assertEqual(result["provider_requests"], 0)
            self.assertIs(result["client_ready"], False)
        self.assertEqual(self.memory.snapshot_with_provenance(), before)
        self.assertEqual(len(runner.calls), 1)

    def test_empty_or_malformed_non_sequence_scope_is_not_treated_as_an_empty_list(self):
        for selected in ({}, set(), "", "bar", range(0), False, 0, [None], [{}],
                         (None,), ["bar", "bar"], ("bar", "bar")):
            with self.subTest(selected=selected), \
                    patch.object(self.memory, "snapshot", side_effect=AssertionError("Invalid scope read history")), \
                    patch("product_radar.product_queries.Journal", side_effect=AssertionError("Invalid scope acquired a lock")):
                with self.assertRaisesRegex(SafetyError, "^PRODUCT_QUERY_CONCEPTS_INVALID$"):
                    self.prepare(concept_ids=selected)

    def test_explicit_empty_scope_still_validates_date_before_returning(self):
        for selected in ([], ()):
            for invalid_date in ("not a date", "2026-10-11", object()):
                with self.subTest(selected=selected, date=invalid_date), \
                        patch.object(self.memory, "snapshot", side_effect=AssertionError("Invalid date read history")), \
                        patch("product_radar.product_queries.Journal", side_effect=AssertionError("Invalid date acquired a lock")):
                    with self.assertRaisesRegex(SafetyError, "^INVALID_TIME$"):
                        ensure_query_profiles(self.memory, concept_ids=selected, as_of=invalid_date)

    def test_explicit_empty_scope_still_validates_limit_before_returning(self):
        for selected in ([], ()):
            for invalid_limit in (True, False, 0, -1, 201, 1.0, "20", None):
                with self.subTest(selected=selected, limit=invalid_limit), \
                        patch.object(self.memory, "snapshot", side_effect=AssertionError("Invalid limit read history")), \
                        patch("product_radar.product_queries.Journal", side_effect=AssertionError("Invalid limit acquired a lock")):
                    with self.assertRaisesRegex(SafetyError, "^PRODUCT_QUERY_LIMIT_INVALID$"):
                        self.prepare(concept_ids=selected, limit=invalid_limit)

    def test_long_manufacturer_title_uses_cached_type_and_subtype_not_retail_name(self):
        runner = Runner()
        result = self.prepare(runner)
        self.assertEqual(result["pending"], [])
        self.assertEqual(len(runner.calls), 1)
        for source in SOURCES:
            rows = product_query_terms(self.memory, "bar", source=source, catalogue=self.catalogue)
            self.assertEqual([row["text"] for row in rows], ["doorway pull-up bar", "telescopic pull-up bar"])
            self.assertTrue(all("EXAMPLE" not in row["text"] for row in rows))
        self.assertEqual(self.memory.snapshot()["concepts"]["bar"]["description"], DESCRIPTION)
        count = len(self.memory.snapshot()["source_checks"])
        self.assertEqual(self.prepare(runner)["reused"], ["bar"])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(self.memory.snapshot()["source_checks"]), count)

    def test_context_is_separate_and_not_used_by_default_supplier_or_trends_queries(self):
        self.prepare(Runner())
        default = product_query_terms(self.memory, "bar", source="trends", catalogue=self.catalogue)
        wider = product_query_terms(self.memory, "bar", source="trends", include_context=True, catalogue=self.catalogue)
        self.assertEqual(len(default), 2)
        self.assertEqual(wider[-1]["scope"], "context")
        self.assertEqual(wider[-1]["text"], "pull-up bar")
        plan = plan_supplier_search(self.memory, "bar")
        self.assertEqual(plan["queries"], ["doorway pull-up bar India manufacturer wholesaler supplier",
                                         "telescopic pull-up bar India dropshipping supplier single piece"])

    def test_all_platform_aliases_consume_same_profile_without_more_model_calls(self):
        runner = Runner(); self.prepare(runner)
        for source in ("amazon", "amazon_search", "flipkart", "flipkart_search", "blinkit", "zepto", "instamart",
                       "google", "google_search", "google_maps", "indiamart", "tradeindia"):
            self.assertEqual(product_query_terms(self.memory, "bar", source=source)[0]["text"], "doorway pull-up bar")
        self.assertEqual(len(runner.calls), 1)

    def test_catalogue_alias_is_zero_model_work_and_does_not_rewrite_description(self):
        old = self.memory.snapshot()["concepts"]["bar"]
        description = dict(DESCRIPTION, label="Sink organiser", category="Home and Kitchen")
        self.memory.put("concepts", "bar", dict(old, description=description), previous=old)
        runner = Runner()
        result = self.prepare(runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual(result["records"][0]["method"], "catalogue")
        self.assertEqual(product_query_terms(self.memory, "bar", source="meta")[0]["text"], "Sink caddy")

    def test_simple_accepted_type_is_available_without_model_or_write(self):
        old = self.memory.snapshot()["concepts"]["bar"]
        self.memory.put("concepts", "bar", dict(old, description=dict(DESCRIPTION, label="2 litre insulated flask")), previous=old)
        before = self.memory.snapshot()
        rows = product_query_terms(self.memory, "bar", source="supplier", catalogue=self.catalogue)
        self.assertEqual(rows[0]["text"], "2 litre insulated flask")
        self.assertEqual(self.memory.snapshot(), before)

    def test_catalogue_long_label_uses_actual_short_alias_without_model_or_truncation(self):
        long_label = "Synthetic accepted product family wording " * 8
        self.catalogue["families"][0]["label"] = long_label
        old = self.memory.snapshot()["concepts"]["bar"]
        self.memory.put("concepts", "bar", dict(old, description=dict(DESCRIPTION,
            label="Sink organiser", category="Home and Kitchen")), previous=old)
        runner = Runner()
        result = self.prepare(runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual(result["records"][0]["profile"]["core_type"], "Sink organiser")

    def test_catalogue_oversized_optional_alias_does_not_block_healthy_search_wording(self):
        self.catalogue["families"][0]["aliases"].append("Synthetic long alias " * 20)
        old = self.memory.snapshot()["concepts"]["bar"]
        self.memory.put("concepts", "bar", dict(old, description=dict(DESCRIPTION,
            label="Sink organiser", category="Home and Kitchen")), previous=old)
        result = self.prepare(Runner())
        self.assertEqual(result["pending"], [])
        self.assertEqual(result["records"][0]["profile"]["aliases"], ["Sink organiser"])

    def test_preparing_simple_type_retains_meaningful_numbers_without_model_calls(self):
        old = self.memory.snapshot()["concepts"]["bar"]
        self.memory.put("concepts", "bar", dict(old, description=dict(DESCRIPTION, label="2 litre insulated flask")), previous=old)
        runner = Runner()
        result = self.prepare(runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual(result["records"][0]["method"], "short_label")
        self.assertEqual(result["records"][0]["profile"]["core_type"], "2 litre insulated flask")

    def test_awkward_label_without_profile_is_pending_not_full_title_or_demand_failure(self):
        result = self.prepare()
        self.assertEqual(result["pending"], [{"concept_id": "bar", "reason": "PRODUCT_QUERY_PROFILE_REQUIRED"}])
        with self.assertRaisesRegex(SafetyError, "PRODUCT_QUERY_PROFILE_REQUIRED"):
            product_query_terms(self.memory, "bar", source="meta", catalogue=self.catalogue)
        self.assertFalse(result["client_ready"])
        self.assertEqual(result["provider_requests"], 0)

    def test_changed_mechanism_invalidates_profile_not_original_jobs(self):
        runner = Runner(); self.prepare(runner)
        old = self.memory.snapshot()["concepts"]["bar"]
        changed = dict(old, description=dict(DESCRIPTION, mechanism="Bolted wall mount, not a telescopic doorway bar"))
        self.memory.put("concepts", "bar", changed, previous=old)
        self.assertIsNone(current_query_profile(self.memory.snapshot(), "bar"))
        self.prepare(runner)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(len(self.memory.snapshot()["jobs"]), 2)

    def test_supplied_description_reference_required_and_foreign_observation_rejected(self):
        item = query_profile_input(self.memory.snapshot(), "bar")
        row = answer(item)
        for refs in ([], ["other-product-observation"], [item["description_ref"], item["description_ref"]]):
            row["references"] = refs
            with self.assertRaisesRegex(SafetyError, "PRODUCT_QUERY_REFERENCE_INVALID"):
                validate_query_profile(item, row)

    def test_unknown_source_duplicate_terms_and_hidden_context_are_rejected(self):
        item = query_profile_input(self.memory.snapshot(), "bar")
        for mutate, code in (
            (lambda row: row["queries"][0].update(source="unknown"), "PRODUCT_QUERY_QUERIES_INVALID"),
            (lambda row: row["queries"].__setitem__(1, deepcopy(row["queries"][0])), "PRODUCT_QUERY_TERMS_DUPLICATED"),
            (lambda row: row["queries"][2].update(text="home fitness equipment"), "PRODUCT_QUERY_CONTEXT_INVALID"),
            (lambda row: row.update(core_type="https://invented.example/product"), "PRODUCT_QUERY_PHRASE_INVALID")):
            row = answer(item); mutate(row)
            with self.assertRaisesRegex(SafetyError, code):
                validate_query_profile(item, row)

    def test_omitted_optional_query_slots_fill_from_reviewed_type_not_full_title(self):
        item = query_profile_input(self.memory.snapshot(), "bar")
        row = answer(item); row["queries"] = []
        result = validate_query_profile(item, row)
        self.assertEqual(len(result["queries"]), 5)
        self.assertTrue(all(query["text"] == "doorway pull-up bar" for query in result["queries"]))

    def test_new_c_mode_has_own_version_schema_and_legacy_c_remains_valid(self):
        self.assertEqual(prompt_version("C", {"items": []}), CATALOGUE_PROMPT_VERSION)
        self.assertEqual(prompt_version("C", {"mode": MODE, "items": []}), PROMPT_VERSION)
        self.assertIn("subtype", schema_for("C", {"mode": MODE})["properties"]["results"]["items"]["properties"])
        self.assertNotIn("subtype", schema_for("C", {"items": []})["properties"]["results"]["items"]["properties"])
        self.assertIn("retrieval planning", instructions_for("C", {"mode": MODE}))
        for payload, version in (({"mode": MODE}, CATALOGUE_PROMPT_VERSION), ({}, PROMPT_VERSION), ({"mode": "unknown"}, None)):
            with self.assertRaisesRegex(SafetyError, "AI_C_CONTRACT_INVALID"):
                prompt_version("C", payload, version)

    def test_bad_profile_sibling_does_not_block_good_product(self):
        self.memory.put("concepts", "other", {"description": dict(DESCRIPTION, label=DESCRIPTION["label"] + " variant"), "status": "provisional"})
        def mutate(values):
            values[0]["references"] = ["fabricated"]
        result = self.prepare(Runner(mutate))
        self.assertEqual(len(result["pending"]), 1)
        self.assertEqual(len(result["records"]), 1)
        self.assertIsNotNone(current_query_profile(self.memory.snapshot(), "other"))

    def test_reusing_saved_ai_job_after_profile_commit_failure_needs_no_new_call(self):
        runner = Runner()
        with patch("product_radar.product_queries._record", side_effect=SafetyError("SYNTHETIC_COMMIT_INTERRUPTED")):
            first = self.prepare(runner)
        self.assertEqual(len(first["pending"]), 1)
        second = self.prepare(runner)
        self.assertEqual(len(second["records"]), 1)
        self.assertEqual(len(runner.calls), 1)

    def test_automatic_finite_batches_advance_beyond_completed_first_products(self):
        for n in range(3):
            self.memory.put("concepts", "item" + str(n), {"description": dict(DESCRIPTION, label="Short product " + str(n)), "status": "provisional"})
        runner = Runner()
        first = self.prepare(runner, limit=1)
        second = self.prepare(runner, limit=1)
        self.assertEqual(len(first["records"]), 1)
        self.assertEqual(len(second["records"]), 1)
        self.assertNotEqual(first["records"][0]["concept_id"], second["records"][0]["concept_id"])

    def test_invalid_product_sibling_does_not_prevent_good_profile(self):
        self.memory.put("concepts", "aaa-bad", {"description": {"label": None}, "status": "provisional"})
        result = self.prepare(Runner())
        self.assertEqual(result["pending"][0]["concept_id"], "aaa-bad")
        self.assertEqual(len(result["records"]), 1)

    def test_failed_planning_cannot_starve_later_siblings_or_other_failed_questions(self):
        self.memory.put("concepts", "aaa-bad", {"description": {"label": None}, "status": "provisional"})
        self.memory.put("concepts", "zzz-bad", {"description": {"label": None}, "status": "provisional"})
        runner = Runner()
        first = self.prepare(runner, limit=1)
        second = self.prepare(runner, limit=1)
        third = self.prepare(runner, limit=1)
        fourth = self.prepare(runner, limit=1)
        fifth = self.prepare(runner, limit=1)
        self.assertEqual(first["pending"][0]["concept_id"], "aaa-bad")
        self.assertEqual(second["records"][0]["concept_id"], "bar")
        self.assertEqual(third["pending"][0]["concept_id"], "zzz-bad")
        self.assertEqual(fourth["pending"][0]["concept_id"], "aaa-bad")
        self.assertEqual(fifth["pending"][0]["concept_id"], "zzz-bad")
        self.assertEqual(len(runner.calls), 1)

    def test_failed_catalogue_commit_preserves_a_healthy_sibling(self):
        old = self.memory.snapshot()["concepts"]["bar"]
        self.memory.put("concepts", "bar", dict(old, description=dict(DESCRIPTION,
            label="Sink organiser", category="Home and Kitchen")), previous=old)
        self.memory.put("concepts", "zzz-good", {"description": dict(DESCRIPTION,
            label="2 litre insulated flask"), "status": "provisional"})
        from product_radar.product_queries import _record
        def interrupt_first(memory, item, *args, **kwargs):
            if item["id"] == "bar":
                raise SafetyError("SYNTHETIC_LOCAL_COMMIT_INTERRUPTED")
            return _record(memory, item, *args, **kwargs)
        with patch("product_radar.product_queries._record", side_effect=interrupt_first):
            result = self.prepare(Runner())
        self.assertEqual(result["pending"][0]["concept_id"], "bar")
        self.assertEqual(result["records"][0]["concept_id"], "zzz-good")

    def test_invalid_concept_container_is_a_clear_input_error_not_a_type_crash(self):
        for values in ([{}], ["bar", "bar"], "bar"):
            with self.subTest(values=values):
                with self.assertRaisesRegex(SafetyError, "PRODUCT_QUERY_CONCEPTS_INVALID"):
                    self.prepare(concept_ids=values)

    def test_old_frozen_supplier_query_replay_survives_new_type_profile(self):
        reader = Reader()
        # Freeze wording equivalent to the earlier implementation's full retail
        # label/function strategy, then install the new compact profile.
        with patch("product_radar.product_queries.product_query_terms", return_value=[
                {"text": DESCRIPTION["label"]}, {"text": DESCRIPTION["function"]}]):
            first = discover_saved_supplier_leads(self.memory, "bar", fetcher=reader)
        self.prepare(Runner())
        changed = plan_supplier_search(self.memory, "bar")
        self.assertNotEqual(changed["queries"], first["plan"]["queries"])
        replay = discover_saved_supplier_leads(self.memory, "bar", fetcher=reader, search_id=first["search_id"])
        self.assertTrue(replay["reused"])
        self.assertEqual(replay["plan"], first["plan"])
        self.assertEqual(len(reader.calls), 2)

    def test_optional_context_shape_repairs_without_changing_product_choices_or_new_model_call(self):
        def mutate(values):
            for value in values:
                value["queries"][2]["text"] = "home fitness equipment"
        runner = Runner(mutate)
        result = self.prepare(runner)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(result["pending"], [])
        self.assertEqual(result["records"][0]["method"], "model_contract_repair")
        self.assertEqual(result["records"][0]["profile"]["subtype"], "doorway pull-up bar")
        receipt = Path(result["contract_repairs"][0]["reference"])
        self.assertTrue(receipt.exists())
        self.assertIn("omitted_unlisted_optional_context", receipt.read_text())
        self.assertNotIn("home fitness equipment", [row["text"] for row in result["records"][0]["profile"]["queries"]])
        self.assertEqual(self.prepare(runner)["reused"], ["bar"])
        self.assertEqual(len(runner.calls), 1)

    def test_optional_list_overflow_is_bounded_without_removing_query_mechanism(self):
        def mutate(values):
            values[0]["mechanism_anchors"] = ["doorway mounted", "adjustable width", "telescopic bar", "door frame", "fifth optional anchor"]
            values[0]["aliases"].append(values[0]["aliases"][0])
        result = self.prepare(Runner(mutate))
        self.assertEqual(result["pending"], [])
        profile = result["records"][0]["profile"]
        self.assertEqual(len(profile["mechanism_anchors"]), 4)
        self.assertEqual(profile["aliases"], ["telescopic pull-up bar", "door adjustable pull-up bar"])
        self.assertEqual(profile["queries"][1]["text"], "telescopic pull-up bar")

    def test_valid_observation_ref_suffices_but_truncated_hash_is_not_completed(self):
        self.memory.put("observations", "obs-real", {"semantic_id": "semantic-bar", "semantic": {"title": "Doorway pull-up bar"}})
        self.memory.put("associations", "assoc-real", {"concept_id": "bar", "semantic_id": "semantic-bar", "status": "resolved"})
        def mutate(values):
            values[0]["references"] = [values[0]["references"][0][:-1], "obs-real"]
        result = self.prepare(Runner(mutate))
        self.assertEqual(result["pending"], [])
        self.assertEqual(result["records"][0]["profile"]["references"], ["obs-real"])
        self.assertIn("retained_only_actual_supplied_references", Path(result["contract_repairs"][0]["reference"]).read_text())

    def test_entirely_fabricated_reference_envelope_remains_pending(self):
        runner = Runner(lambda values: values[0].update(references=["foreign-observation"]))
        result = self.prepare(runner)
        self.assertEqual(result["records"], [])
        self.assertEqual(result["pending"][0]["reason"], "PRODUCT_QUERY_REFERENCE_INVALID")
        self.assertEqual(self.prepare(runner)["records"], [])
        self.assertEqual(len(runner.calls), 1)

    def test_saved_legacy_response_is_reapplied_exactly_before_new_prompt_or_changed_batch(self):
        self.memory.put("concepts", "zzz-other", {"description": dict(DESCRIPTION, label=DESCRIPTION["label"] + " extra"), "status": "provisional"})
        state = self.memory.snapshot()
        payload = {"mode": MODE, "items": [query_profile_input(state, cid) for cid in ("bar", "zzz-other")]}
        runner = Runner(lambda values: [value["queries"][2].update(text="optional unmatched context") for value in values])
        old_id, _ = Jobs(self.memory, runner).run("C", payload, declared_version=LEGACY_PROMPT_VERSION)
        old_path = self.memory.root / "ai_jobs" / old_id / "result.json"
        original = old_path.read_bytes()
        result = self.prepare(concept_ids=["bar"])
        self.assertEqual(result["pending"], [])
        self.assertEqual(result["recovered_job_ids"], [old_id])
        self.assertEqual(result["records"][0]["input"], payload["items"][0])
        self.assertEqual(old_path.read_bytes(), original)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(self.memory.snapshot()["jobs"]), 1)

    def test_saved_response_application_survives_new_observation_without_new_model_call(self):
        state = self.memory.snapshot()
        payload = {"mode": MODE, "items": [query_profile_input(state, "bar")]}
        runner = Runner()
        old_id, _ = Jobs(self.memory, runner).run("C", payload)
        self.memory.put("observations", "obs-new", {"semantic_id": "semantic-new", "semantic": {"title": "Another doorway pull-up bar"}})
        self.memory.put("associations", "assoc-new", {"concept_id": "bar", "semantic_id": "semantic-new", "status": "resolved"})
        result = self.prepare(runner)
        self.assertEqual(result["recovered_job_ids"], [old_id])
        self.assertEqual(result["records"][0]["input"], payload["items"][0])
        self.assertEqual(len(runner.calls), 1)

    def test_unfinished_legacy_intent_is_not_bypassed_with_new_prompt_or_model(self):
        state = self.memory.snapshot()
        payload = {"mode": MODE, "items": [query_profile_input(state, "bar")]}
        class Interrupted(Runner):
            def __call__(self, *args):
                raise SafetyError("AI_NETWORK_UNAVAILABLE")
        with self.assertRaisesRegex(SafetyError, "AI_NETWORK_UNAVAILABLE"):
            Jobs(self.memory, Interrupted()).run("C", payload, declared_version=LEGACY_PROMPT_VERSION)
        runner = Runner()
        result = self.prepare(runner)
        self.assertEqual(result["pending"][0]["reason"], "AI_JOB_PENDING_RECONCILIATION")
        self.assertEqual(runner.calls, [])
        self.assertEqual(len(self.memory.snapshot()["jobs"]), 1)

    def test_busy_pending_progress_write_does_not_abort_or_starve_other_products(self):
        self.memory.put("concepts", "aaa-bad", {"description": {"label": None}, "status": "provisional"})
        original_put = self.memory.put
        def busy(table, record_id, value, **kwargs):
            if table == "source_checks" and value.get("kind") == "product_query_profile_pending":
                raise SafetyError("RUNNER_ALREADY_ACTIVE")
            return original_put(table, record_id, value, **kwargs)
        with patch.object(self.memory, "put", side_effect=busy):
            first = self.prepare(Runner(), limit=1)
        self.assertEqual(first["pending"][0]["concept_id"], "aaa-bad")
        self.assertEqual(first["local_failures"][0]["stage"], "canonical_progress")
        second = self.prepare(Runner(), limit=1)
        self.assertEqual(second["records"][0]["concept_id"], "bar")

    def test_local_io_error_in_one_profile_commit_keeps_saved_answer_and_healthy_sibling(self):
        self.memory.put("concepts", "zzz-good", {"description": dict(DESCRIPTION, label=DESCRIPTION["label"] + " extra"), "status": "provisional"})
        runner = Runner()
        from product_radar.product_queries import _record
        def fail_first(memory, item, *args, **kwargs):
            if item["id"] == "bar":
                raise OSError("synthetic storage fault")
            return _record(memory, item, *args, **kwargs)
        with patch("product_radar.product_queries._record", side_effect=fail_first):
            first = self.prepare(runner)
        self.assertEqual(first["pending"][0]["reason"], "PRODUCT_QUERY_LOCAL_IO_FAILED")
        self.assertEqual(first["records"][0]["concept_id"], "zzz-good")
        second = self.prepare(runner)
        self.assertEqual(second["records"][0]["concept_id"], "bar")
        self.assertEqual(len(runner.calls), 1)

    def test_malformed_optional_fields_can_use_short_phrase_already_in_saved_title(self):
        runner = Runner(lambda values: values[0].pop("mechanism_anchors"))
        result = self.prepare(runner)
        self.assertEqual(result["pending"], [])
        self.assertEqual(result["records"][0]["method"], "grounded_description")
        self.assertEqual(result["records"][0]["profile"]["core_type"], "pull-up bar")
        self.assertEqual(result["records"][0]["profile"]["references"], [result["records"][0]["input"]["description_ref"]])

    def test_title_containing_actual_compact_catalogue_phrase_is_free_retrieval_not_identity_merge(self):
        old = self.memory.snapshot()["concepts"]["bar"]
        description = dict(DESCRIPTION, label="EXAMPLE Premium Sink organiser in Black for Every Kitchen with Extra Slots",
                           category="Home and Kitchen")
        self.memory.put("concepts", "bar", dict(old, description=description), previous=old)
        before = self.memory.snapshot()
        result = product_query_terms(self.memory, "bar", source="supplier", catalogue=self.catalogue)
        self.assertEqual(result[0]["text"], "Sink organiser")
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(self.memory.snapshot()["concepts"]["bar"]["description"], description)

    def test_v2_prompt_states_exact_list_context_and_reference_contract_without_changing_legacy(self):
        legacy = instructions_for("C", {"mode": MODE}, LEGACY_PROMPT_VERSION)
        current = instructions_for("C", {"mode": MODE})
        self.assertNotIn("Output contract details", legacy)
        self.assertIn("must exactly repeat", current)
        self.assertIn("One actual observation id is sufficient", current)


if __name__ == "__main__":
    unittest.main()
