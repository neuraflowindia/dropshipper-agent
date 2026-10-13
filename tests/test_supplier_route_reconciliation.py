"""Saved-only supplier/controller recovery; synthetic receipts, never requests."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory
from product_radar.source_coverage import execute_source_run, prepare_source_tasks, route_registry
from product_radar.storage import Journal, atomic_json, load_json
from product_radar.supplier_effort import VERSION as EFFORT_VERSION
from product_radar.supplier_route_acquisition import (PARSER_VERSION, ROUTES, SEARCH_ROUTES, acquire_supplier_search,
    collect_supplier_company_pages, collect_supplier_search, create_supplier_handlers, replay_supplier_search,
    supplier_route_url)
from test_supplier_discovery import CONCEPT, NOW
from test_supplier_route_acquisition import QUERY, Reader, receipt

LATER = "2026-10-08T14:00:00+00:00"


def forbidden(*_args, **_kwargs):
    raise AssertionError("Saved-only recovery must not launch a reader or dispatch callback.")


class SupplierReconciliationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.memory.put("concepts", "paper", deepcopy(CONCEPT))
        self.reader = Reader()
        self.handlers = create_supplier_handlers(self.memory, http_reader=forbidden,
                                                browser_reader=forbidden, page_reader=forbidden)

    def context(self, route="indiamart", acquisition="saved-acquisition", query=QUERY, request=None):
        request = request or {"query": query, "country": "IN"}
        return {"route": route, "acquisition_id": acquisition, "concept_ids": ["paper"],
                "description_hashes": {"paper": digest(CONCEPT["description"])}, "request": request,
                "request_hash": digest(request), "as_of": LATER, "record_dispatch": forbidden}

    def acquire(self, route="indiamart", acquisition="saved-acquisition", **options):
        return acquire_supplier_search(self.memory.root, route=route, query=QUERY,
            acquisition_id=acquisition, http_reader=self.reader, as_of=NOW, **options)

    def search(self):
        return collect_supplier_search(self.memory, route="google-suppliers", concept_ids=["paper"],
                                       query=QUERY, http_reader=self.reader, as_of=NOW)

    def company_context(self, acquisition="saved-company"):
        checks = [cid for cid, row in self.memory.snapshot()["source_checks"].items()
                  if row.get("kind") == "supplier_route_search"]
        request = {"candidate_urls": {"paper": ["https://packaging.example.org/food-paper"]},
                   "candidate_check_ids": {"paper": checks}}
        return self.context("company-pages", acquisition, request=request)

    def collect_company(self, context):
        return collect_supplier_company_pages(self.memory, concept_ids=["paper"],
            acquisition_id=context["acquisition_id"], candidate_urls=context["request"]["candidate_urls"],
            candidate_check_ids=context["request"]["candidate_check_ids"], page_reader=self.reader, as_of=NOW)

    def test_all_five_installed_supplier_routes_expose_explicit_saved_only_reconciliation(self):
        registry = route_registry(self.handlers)
        for route in ROUTES:
            self.assertTrue(registry[route]["connected"])
            self.assertTrue(callable(registry[route]["handler"]))
            self.assertTrue(callable(registry[route]["reconcile"]))

    def test_each_search_route_replays_its_original_capture_and_projects_only_once(self):
        for route in SEARCH_ROUTES:
            with self.subTest(route=route):
                acquisition = "saved-" + route
                original = self.acquire(route, acquisition)
                path = Path(original["reference"])
                before = path.read_bytes()
                context = self.context(route, acquisition)
                first = self.handlers[route]["reconcile"](context)
                state = self.memory.snapshot()
                second = self.handlers[route]["reconcile"](context)
                self.assertEqual(first, second)
                self.assertEqual(self.memory.snapshot(), state)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(first["observed_at"], NOW)
                self.assertTrue(first["dispatch_started"])
                self.assertEqual(first["state"], "interpretation_pending")
                self.assertEqual(len(first["target_results"][0]["evidence_refs"]), 2)
        self.assertEqual(len(self.reader.calls), 4)

    def test_original_unrecognised_http_and_browser_429_replay_stays_blocked_not_empty(self):
        unknown = Reader({supplier_route_url("google-suppliers", QUERY): "A JavaScript search page with no recognised cards."})
        def blocked(item):
            return receipt(item, status=429, reason="SUPPLIER_NATIVE_READ_FAILED")
        original = acquire_supplier_search(self.memory.root, route="google-suppliers", query=QUERY,
            acquisition_id="google-original-429", http_reader=unknown, browser_reader=blocked, as_of=NOW)
        before = Path(original["reference"]).read_bytes()
        result = self.handlers["google-suppliers"]["reconcile"](self.context("google-suppliers", "google-original-429"))
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason"], "SUPPLIER_NATIVE_READ_FAILED")
        self.assertTrue(result["dispatch_started"])
        self.assertEqual(result["observed_at"], NOW)
        self.assertEqual(Path(original["reference"]).read_bytes(), before)
        checks = self.memory.snapshot()["source_checks"]
        effort = next(row for row in checks.values() if row.get("kind") == EFFORT_VERSION)
        self.assertEqual(len(effort["actual_attempt_refs"]), 2)
        self.assertEqual(effort["internal_outcome"], "blocked")

    def test_missing_capture_and_progress_do_not_launch_or_project(self):
        before = self.memory.snapshot()
        with self.assertRaisesRegex(SafetyError, "SOURCE_ORIGINAL_RECEIPT_PENDING"):
            self.handlers["indiamart"]["reconcile"](self.context())
        self.assertEqual(self.memory.snapshot(), before)

    def test_corrupt_capture_is_refused_without_fallback_or_product_projection(self):
        original = self.acquire()
        record = deepcopy(original["record"])
        record["result"]["results"] = []
        atomic_json(Path(original["reference"]), record)
        before = self.memory.snapshot()
        with self.assertRaisesRegex(SafetyError, "SAVED_SUPPLIER_ROUTE_INVALID"):
            self.handlers["indiamart"]["reconcile"](self.context())
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(len(self.reader.calls), 1)

    def test_wrong_query_route_and_acquisition_binding_cannot_donate_a_capture(self):
        original = self.acquire()
        before = self.memory.snapshot()
        for context in (self.context(query="A different family"), self.context("tradeindia")):
            with self.subTest(route=context["route"]):
                with self.assertRaisesRegex(SafetyError, "SAVED_SUPPLIER_ROUTE_INVALID"):
                    self.handlers[context["route"]]["reconcile"](context)
        changed = deepcopy(original["record"])
        changed["acquisition_id"] = "foreign-acquisition"
        atomic_json(Path(original["reference"]), changed)
        with self.assertRaisesRegex(SafetyError, "SAVED_SUPPLIER_ROUTE_INVALID"):
            self.handlers["indiamart"]["reconcile"](self.context())
        self.assertEqual(self.memory.snapshot(), before)

    def test_wrong_controller_request_or_description_hash_is_refused_before_projection(self):
        self.acquire()
        before = self.memory.snapshot()
        for field, value in (("request_hash", "0" * 64), ("description_hashes", {"paper": "0" * 64}),
                             ("route", "tradeindia")):
            context = self.context()
            context[field] = value
            with self.subTest(field=field):
                with self.assertRaises(SafetyError):
                    self.handlers["indiamart"]["reconcile"](context)
        self.assertEqual(self.memory.snapshot(), before)

    def test_hash_valid_but_falsely_derived_result_is_refused(self):
        original = self.acquire()
        changed = deepcopy(original["record"])
        changed["result"] = {"state": "complete", "reason": "A fabricated empty result.", "results": []}
        changed["content_hash"] = digest([changed["request"], changed["records"], changed["result"]])
        atomic_json(Path(original["reference"]), changed)
        with self.assertRaisesRegex(SafetyError, "SAVED_SUPPLIER_ROUTE_INVALID"):
            self.handlers["indiamart"]["reconcile"](self.context())

    def test_saved_partial_progress_finishes_locally_without_missing_browser_fallback(self):
        writer = atomic_json
        unknown = Reader({supplier_route_url("indiamart", QUERY): "Readable but unrecognised original search page."})
        def interrupted(path, value):
            writer(path, value)
            if Path(path).name == "progress.json":
                raise RuntimeError("Synthetic interruption after retained HTTP reply.")
        with patch("product_radar.supplier_route_acquisition.atomic_json", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                acquire_supplier_search(self.memory.root, route="indiamart", query=QUERY,
                    acquisition_id="partial-original", http_reader=unknown, browser_reader=forbidden, as_of=NOW)
        context = self.context(acquisition="partial-original")
        result = self.handlers["indiamart"]["reconcile"](context)
        self.assertEqual(result["state"], "insufficient")
        self.assertTrue(result["dispatch_started"])
        self.assertEqual(result["observed_at"], NOW)
        self.assertEqual(len(unknown.calls), 1)
        before = self.memory.snapshot()
        self.assertEqual(result, self.handlers["indiamart"]["reconcile"](context))
        self.assertEqual(self.memory.snapshot(), before)

    def test_empty_or_corrupt_partial_progress_is_not_proof_of_a_search(self):
        request = {"version": "supplier-public-routes-v1", "route": "indiamart", "query": QUERY,
                   "url": supplier_route_url("indiamart", QUERY), "parser_version": PARSER_VERSION}
        path = self.memory.root / "supplier_routes" / "empty-progress" / "progress.json"
        atomic_json(path, {"request": request, "records": [], "content_hash": digest([request, []])})
        with self.assertRaisesRegex(SafetyError, "SAVED_SUPPLIER_ROUTE_PROGRESS_INVALID"):
            self.handlers["indiamart"]["reconcile"](self.context(acquisition="empty-progress"))
        self.assertEqual(self.memory.snapshot()["source_checks"], {})

    def test_later_different_parser_file_is_not_selected_over_original_result(self):
        original = self.acquire()
        later = deepcopy(original["record"])
        later["result"] = {"state": "complete", "reason": "Do not adopt this different parser file.", "results": []}
        later["content_hash"] = digest([later["request"], later["records"], later["result"]])
        atomic_json(Path(original["reference"]).parent / ("capture-parser-" + str(PARSER_VERSION) + ".json"), later)
        replay = replay_supplier_search(self.memory.root, route="indiamart", query=QUERY, acquisition_id="saved-acquisition")
        self.assertEqual(replay["record"], original["record"])
        self.assertEqual(replay["reference"], original["reference"])

    def test_legacy_saved_parser_decision_is_retained_without_new_reinterpretation(self):
        original = self.acquire()
        old = deepcopy(original["record"])
        old["request"]["parser_version"] = 1
        old["content_hash"] = digest([old["request"], old["records"], old["result"]])
        atomic_json(Path(original["reference"]), old)
        atomic_json(Path(original["reference"]).parent / "progress.json", {
            "request": old["request"], "records": old["records"], "content_hash": digest([old["request"], old["records"]])})
        replay = replay_supplier_search(self.memory.root, route="indiamart", query=QUERY, acquisition_id="saved-acquisition")
        self.assertEqual(replay["record"], old)
        self.assertFalse((Path(original["reference"]).parent / ("capture-parser-" + str(PARSER_VERSION) + ".json")).exists())

    def test_a_later_valid_but_different_response_cannot_replace_retained_original_progress(self):
        original = self.acquire()
        later = deepcopy(original["record"])
        later["records"][0]["receipt"]["checked_at"] = LATER
        later["content_hash"] = digest([later["request"], later["records"], later["result"]])
        atomic_json(Path(original["reference"]), later)
        before = self.memory.snapshot()
        with self.assertRaisesRegex(SafetyError, "SAVED_SUPPLIER_ROUTE_INVALID"):
            self.handlers["indiamart"]["reconcile"](self.context())
        self.assertEqual(self.memory.snapshot(), before)

    def test_corrupt_retained_progress_cannot_be_silently_ignored_on_replay(self):
        original = self.acquire()
        progress = Path(original["reference"]).parent / "progress.json"
        saved = load_json(progress)
        saved["content_hash"] = "0" * 64
        atomic_json(progress, saved)
        with self.assertRaisesRegex(SafetyError, "SAVED_SUPPLIER_ROUTE_PROGRESS_INVALID"):
            self.handlers["indiamart"]["reconcile"](self.context())

    def test_already_projected_original_cannot_be_replaced_by_a_later_valid_capture(self):
        original = self.acquire()
        self.handlers["indiamart"]["reconcile"](self.context())
        before = self.memory.snapshot()
        later = deepcopy(original["record"])
        later["records"][0]["receipt"]["checked_at"] = LATER
        later["content_hash"] = digest([later["request"], later["records"], later["result"]])
        atomic_json(Path(original["reference"]), later)
        atomic_json(Path(original["reference"]).parent / "progress.json", {"request": later["request"],
            "records": later["records"], "content_hash": digest([later["request"], later["records"]])})
        with self.assertRaisesRegex(SafetyError, "SAVED_SUPPLIER_ROUTE_INVALID"):
            self.handlers["indiamart"]["reconcile"](self.context())
        self.assertEqual(self.memory.snapshot(), before)

    def test_company_saved_pass_replays_bound_bundles_and_exact_original_dates(self):
        self.search()
        context = self.company_context()
        first = self.collect_company(context)
        before = self.memory.snapshot()
        original_bytes = Path(first["reference"]).read_bytes()
        replay = self.handlers["company-pages"]["reconcile"](context)
        self.assertEqual(first, replay)
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(Path(first["reference"]).read_bytes(), original_bytes)
        self.assertEqual(len(self.reader.calls), 2)

    def test_company_interrupted_final_pass_rebuilds_only_from_completed_original_bundles(self):
        self.search()
        context = self.company_context()
        writer = atomic_json
        def interrupted(path, value):
            if Path(path).name == "company_pass.json":
                raise RuntimeError("Synthetic interruption before final company pass save.")
            return writer(path, value)
        with patch("product_radar.supplier_route_acquisition.atomic_json", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.collect_company(context)
        result = self.handlers["company-pages"]["reconcile"](context)
        self.assertEqual(result["state"], "interpretation_pending")
        self.assertTrue(result["dispatch_started"])
        self.assertEqual(result["observed_at"], NOW)
        self.assertEqual(len(self.reader.calls), 2)
        before = self.memory.snapshot()
        self.assertEqual(result, self.handlers["company-pages"]["reconcile"](context))
        self.assertEqual(self.memory.snapshot(), before)

    def test_company_local_effort_save_failure_keeps_actual_saved_page_then_repairs_once(self):
        self.search()
        context = self.company_context()
        with patch("product_radar.supplier_capture_store.record_supplier_effort",
                   side_effect=SafetyError("SAVED_FILE_UNAVAILABLE")):
            with self.assertRaisesRegex(SafetyError, "SAVED_FILE_UNAVAILABLE"):
                self.collect_company(context)
        pass_path = self.memory.root / "supplier_routes" / context["acquisition_id"] / "company_pass.json"
        first = load_json(pass_path)["envelope"]
        self.assertTrue(first["dispatch_started"])
        self.assertEqual(first["state"], "interpretation_pending")
        pass_bytes = Path(first["reference"]).read_bytes()
        saved_pass = load_json(first["reference"])
        self.assertEqual(saved_pass["target_failures"]["paper"][0]["reason"], "SAVED_FILE_UNAVAILABLE")
        self.assertEqual(len(saved_pass["supplier_capture_ids"]), 1)
        result = self.handlers["company-pages"]["reconcile"](context)
        self.assertEqual(result, first)
        self.assertEqual(Path(first["reference"]).read_bytes(), pass_bytes)
        self.assertEqual(len(self.reader.calls), 2)
        before = self.memory.snapshot()
        self.assertEqual(result, self.handlers["company-pages"]["reconcile"](context))
        self.assertEqual(self.memory.snapshot(), before)

    def test_controller_company_local_save_failure_replays_original_bundle_without_repeat_page(self):
        self.search()
        context = self.company_context()
        prepare_source_tasks(self.memory, [{"route": "company-pages", "request": context["request"],
                                           "concept_ids": ["paper"]}], run_id="same-company-run", as_of=NOW)
        runner = lambda callback, context, _timeout: callback(context)
        handlers = create_supplier_handlers(self.memory, http_reader=forbidden, browser_reader=forbidden,
                                             page_reader=self.reader)
        with patch("product_radar.supplier_capture_store.record_supplier_effort",
                   side_effect=SafetyError("SAVED_FILE_UNAVAILABLE")):
            failed = execute_source_run(self.memory, "same-company-run", handlers=handlers, as_of=NOW, bounded_call=runner)
        self.assertEqual(failed["counts"], {"blocked": 1})
        task_id, task = next((item_id, row) for item_id, row in self.memory.snapshot()["query_tasks"].items()
                             if row.get("route") == "company-pages")
        self.assertTrue(task["dispatch_started"])
        self.assertTrue(task["reconcile_required"])
        acquisition = task["acquisition_id"]
        raw = self.memory.root / "supplier_routes" / acquisition / "company_pass.json"
        before_bytes = raw.read_bytes()
        result = execute_source_run(self.memory, "same-company-run", handlers=self.handlers, as_of=LATER, bounded_call=runner)
        self.assertEqual(result["counts"], {"acquired": 1})
        current = self.memory.snapshot()["query_tasks"][task_id]
        self.assertFalse(current["reconcile_required"])
        self.assertEqual(current["acquisition_id"], acquisition)
        self.assertEqual(current["tries"], 1)
        self.assertEqual(len(current["receipt_refs"]), 1)
        self.assertEqual(raw.read_bytes(), before_bytes)
        self.assertEqual(len(self.reader.calls), 2)
        self.assertEqual(execute_source_run(self.memory, "same-company-run", handlers=self.handlers,
            as_of=LATER, bounded_call=runner)["handler_calls"], 0)

    def test_company_missing_saved_bundle_refuses_without_making_a_failed_fetch(self):
        self.search()
        context = self.company_context()
        before = self.memory.snapshot()
        with self.assertRaisesRegex(SafetyError, "SOURCE_ORIGINAL_RECEIPT_PENDING"):
            self.handlers["company-pages"]["reconcile"](context)
        self.assertEqual(self.memory.snapshot(), before)
        self.assertFalse((self.memory.root / "supplier_pages").exists())

    def test_company_foreign_or_corrupt_saved_pass_cannot_donate_metadata(self):
        self.search()
        context = self.company_context()
        first = self.collect_company(context)
        original = load_json(first["reference"])
        before = self.memory.snapshot()
        for field, value in (("receipt_hash", "0" * 64), ("schema_version", True), ("acquisition_id", "foreign")):
            changed = deepcopy(original)
            changed[field] = value
            atomic_json(Path(first["reference"]), changed)
            with self.subTest(field=field):
                with self.assertRaisesRegex(SafetyError, "SUPPLIER_COMPANY_PASS_CHANGED"):
                    self.handlers["company-pages"]["reconcile"](context)
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(len(self.reader.calls), 2)

    def test_company_missing_or_corrupt_bundle_never_launches_or_donates_pass(self):
        self.search()
        context = self.company_context()
        result = self.collect_company(context)
        bundle_path = Path(result["target_results"][0]["evidence_refs"][0])
        original = load_json(bundle_path)
        changed = deepcopy(original)
        changed["bundle_hash"] = "0" * 64
        atomic_json(bundle_path, changed)
        before = self.memory.snapshot()
        with self.assertRaisesRegex(SafetyError, "SAVED_SUPPLIER_CAPTURE_INVALID"):
            self.handlers["company-pages"]["reconcile"](context)
        bundle_path.unlink()  # Only this test's explicitly named temporary file.
        with self.assertRaisesRegex(SafetyError, "SOURCE_ORIGINAL_RECEIPT_PENDING"):
            self.handlers["company-pages"]["reconcile"](context)
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(len(self.reader.calls), 2)

    def test_company_changed_frozen_destination_or_product_cannot_reuse_original_pass(self):
        self.search()
        context = self.company_context()
        self.collect_company(context)
        context["request"]["candidate_urls"] = {"paper": []}
        context["request_hash"] = digest(context["request"])
        before = self.memory.snapshot()
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_COMPANY_PASS_CHANGED"):
            self.handlers["company-pages"]["reconcile"](context)
        self.assertEqual(self.memory.snapshot(), before)

    def test_explicit_empty_saved_search_stays_completed_with_proved_original_request(self):
        reader = Reader({supplier_route_url("tradeindia", QUERY): "No products found."})
        acquire_supplier_search(self.memory.root, route="tradeindia", query=QUERY,
            acquisition_id="explicit-empty", http_reader=reader, as_of=NOW)
        result = self.handlers["tradeindia"]["reconcile"](self.context("tradeindia", "explicit-empty"))
        self.assertEqual(result["state"], "completed_no_results")
        self.assertTrue(result["dispatch_started"])
        self.assertEqual(result["observed_at"], NOW)
        self.assertEqual(len(reader.calls), 1)

    def test_saved_uncertain_browser_failure_does_not_become_false_not_attempted(self):
        def uncertain(item):
            return {**receipt(item, status=403, actual=False), "http_status": None, "final_url": None,
                    "execution_stage": "unknown", "reason": "SUPPLIER_NATIVE_READ_FAILED"}
        acquire_supplier_search(self.memory.root, route="google-maps", query=QUERY,
            acquisition_id="uncertain-original", http_reader=Reader(status=403, actual=False),
            browser_reader=uncertain, as_of=NOW)
        result = self.handlers["google-maps"]["reconcile"](self.context("google-maps", "uncertain-original"))
        self.assertIsNone(result["dispatch_started"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["observed_at"], NOW)

    def test_undated_malformed_original_reply_cannot_be_given_a_new_resume_date(self):
        acquire_supplier_search(self.memory.root, route="indiamart", query=QUERY,
            acquisition_id="undated-original", http_reader=lambda _item: None, as_of=NOW)
        before = self.memory.snapshot()
        with self.assertRaisesRegex(SafetyError, "SOURCE_ORIGINAL_RECEIPT_PENDING"):
            self.handlers["indiamart"]["reconcile"](self.context("indiamart", "undated-original"))
        self.assertEqual(self.memory.snapshot(), before)

    def test_controller_local_result_save_failure_resumes_original_and_keeps_sibling_running(self):
        unknown = Reader({supplier_route_url("google-suppliers", QUERY): "JavaScript search page with no recognised cards."})
        def blocked(item):
            return receipt(item, status=429, reason="SUPPLIER_NATIVE_READ_FAILED")
        handlers = create_supplier_handlers(self.memory, http_reader=unknown, browser_reader=blocked, page_reader=forbidden)
        prepare_source_tasks(self.memory, [
            {"route": route, "request": {"query": QUERY, "country": "IN"}, "concept_ids": ["paper"]}
            for route in ("google-suppliers", "google-maps")], run_id="same-original-run", as_of=NOW)
        put = self.memory.put
        def interrupted(table, item_id, row, **options):
            if table == "source_checks" and row.get("kind") == "supplier_route_search" and row.get("route") == "google-suppliers":
                raise SafetyError("SAVED_FILE_UNAVAILABLE")
            return put(table, item_id, row, **options)
        runner = lambda callback, context, _timeout: callback(context)
        with patch.object(self.memory, "put", side_effect=interrupted):
            failed = execute_source_run(self.memory, "same-original-run", handlers=handlers, as_of=NOW, bounded_call=runner)
        self.assertEqual(failed["counts"], {"blocked": 1, "acquired": 1})
        task_id, task = next((item_id, row) for item_id, row in self.memory.snapshot()["query_tasks"].items()
                             if row.get("route") == "google-suppliers")
        self.assertTrue(task["dispatch_started"])
        self.assertTrue(task["reconcile_required"])
        self.assertEqual(task["receipt_refs"], [])
        acquisition, attempts = task["acquisition_id"], deepcopy(task["attempt_refs"])
        raw = self.memory.root / "supplier_routes" / acquisition / "capture.json"
        original_bytes = raw.read_bytes()
        immediate_resume = "2026-10-06T08:01:00+00:00"
        recovered = execute_source_run(self.memory, "same-original-run", handlers=self.handlers,
                                       as_of=immediate_resume, bounded_call=runner)
        task_after = self.memory.snapshot()["query_tasks"][task_id]
        self.assertEqual(recovered["handler_calls"], 1)
        self.assertEqual(task_after["reason"], "SUPPLIER_NATIVE_READ_FAILED")
        self.assertFalse(task_after["reconcile_required"])
        self.assertEqual(task_after["acquisition_id"], acquisition)
        self.assertEqual(task_after["attempt_refs"], attempts)
        self.assertEqual(task_after["tries"], 1)
        self.assertEqual(len(task_after["receipt_refs"]), 1)
        self.assertEqual(raw.read_bytes(), original_bytes)
        self.assertEqual(len(unknown.calls), 2)
        self.assertTrue(task_after["retryable"])
        self.assertEqual(task_after["next_retry_at"], "2026-10-06T08:05:00+00:00")
        state = self.memory.snapshot()
        self.assertEqual(execute_source_run(self.memory, "same-original-run", handlers=self.handlers,
            as_of=immediate_resume, bounded_call=runner)["handler_calls"], 0)
        self.assertEqual(self.memory.snapshot()["source_checks"], state["source_checks"])


if __name__ == "__main__":
    unittest.main()
