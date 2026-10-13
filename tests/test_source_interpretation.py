"""Saved source matching contracts; fixtures do not establish real demand."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.models import SafetyError, digest
from product_radar.research_ai import (PROMPT_VERSION, SOURCE_RELEVANCE_PROMPT_VERSION, SOURCE_SUPPORT_PROMPT_VERSION,
                                     e_contract, validate_results)
from product_radar.research_memory import ResearchMemory, import_capture, key
from product_radar.research_sources import map_research_rows
from product_radar.source_coverage import execute_source_run, prepare_source_tasks
from product_radar.source_interpretation import review_source_results, review_supplier_results
from product_radar.storage import Journal
from product_radar.storage import atomic_json
from test_research_sources import amazon_row, request
from test_supplier_capture_store import Reader, BASE as SUPPLIER_BASE
from test_suppliers import FixtureRunner
from product_radar.supplier_capture_store import capture_saved_supplier_bundle

NOW = "2026-10-08T10:00:00+00:00"


class Runner:
    identity = "source-matching-fixture-v1"
    def __init__(self, relationship="relevant_family", missing=False, invalid=False, mutate=None):
        self.calls = []
        self.relationship, self.missing, self.invalid, self.mutate = relationship, missing, invalid, mutate

    def __call__(self, role, payload, folder):
        self.calls.append(deepcopy(payload))
        rows = []
        for item in payload["items"]:
            relevance = [{"id": name, "relationship": self.relationship,
                          "reason": "Synthetic product function and mechanism comparison."}
                         for name in item["reference_aliases"]]
            if self.missing:
                relevance = relevance[:-1]
            if self.invalid and not payload.get("checklist_repair"):
                relevance[0]["id"] = "invented-reference"
            result = {"id": item["id"], "criteria": [], "relevance": relevance,
                      "compliance_note": "", "missing_questions": []}
            if payload.get("relevance_style") == "short-references-support-v3":
                result["source_support"] = [{"source": source, "judgment": "neutral" if self.relationship in
                    ("exact_product", "relevant_family") else "unclear",
                    "basis": "observed" if self.relationship in ("exact_product", "relevant_family") else "not_stated",
                    "references": [name for name, ref in item["reference_aliases"].items()
                                   if next(row for row in item["observations"] if row["id"] == ref)["source"] == source]
                                   if self.relationship in ("exact_product", "relevant_family") else [],
                    "reason": "Synthetic current source context, not a sales claim."}
                    for source in sorted({row["source"] for row in item["observations"]})]
            rows.append(result)
        if self.mutate:
            self.mutate()
        return {"results": rows}, {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}


class SupplierRunner(FixtureRunner):
    def __init__(self, relationship):
        super().__init__()
        self.relationship = relationship

    def __call__(self, role, payload, folder):
        result, usage = super().__call__(role, payload, folder)
        for row in result["results"]:
            row["relevance"]["relationship"] = self.relationship
        return result, usage


class DurableRunner:
    """Local fixture mirrors the real runner's saved completion before return."""
    def __init__(self, inner):
        self.inner = inner
        self.identity = inner.identity

    @property
    def calls(self):
        return self.inner.calls

    def __call__(self, role, payload, folder):
        response, usage = self.inner(role, payload, folder)
        atomic_json(folder / "completion.json", {"response": response, "usage": usage, "exit_code": 0})
        return response, usage


class SourceInterpretationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.memory = ResearchMemory(base / "research", Journal(base / "finance"), evidence_type="synthetic")
        for cid in ("first", "second"):
            self.memory.put("concepts", cid, {"status": "active", "description": {
                "label": "Sink caddy", "category": "Home and Kitchen", "function": "Store sink essentials",
                "mechanism": "manual", "audience": "adult", "features": ["draining"], "basis": "observed",
                "brand": "generic_or_small", "reason": "Synthetic fixture."}})

    def acquired(self, *, ids=None, route="amazon-search", query="sink caddy", count=2):
        planned = prepare_source_tasks(self.memory, [{"route": route, "request": {"query": query, "country": "IN"},
                 "concept_ids": ids or ["first"]}], run_id="full-list", as_of=NOW)
        def handler(context):
            context["record_dispatch"]()
            values = [dict(amazon_row(), title=f"Sink caddy holder {n}", asin=f"B01234567{n}",
                           link=f"https://www.amazon.in/dp/B01234567{n}") for n in range(count)]
            mapped = map_research_rows(values, request())
            imported = import_capture(self.memory, source="amazon", acquisition_id=context["acquisition_id"],
                          observed_at=NOW, mapped=mapped, capture_hash=digest(values), reference={"fixture": "synthetic"})
            state = self.memory.snapshot()
            observation_ids = sorted(obs_id for obs_id, row in state["observations"].items()
                                     if row["capture_id"] == imported["capture_id"])
            return {"schema_version": 1, "route": route, "acquisition_id": context["acquisition_id"],
                    "observed_at": NOW, "state": "usable", "dispatch_started": True,
                    "reference": context["acquisition_id"] + "/receipt.json", "capture_ids": [imported["capture_id"]],
                    "observation_ids": observation_ids, "target_results": [], "reason": "Synthetic saved source rows.",
                    "retryable": False}
        execute_source_run(self.memory, "full-list", handlers={route: handler}, as_of=NOW,
                           bounded_call=lambda callback, context, timeout: callback(context))
        return planned["task_ids"][0]

    def test_versions_are_explicit_and_old_contract_keeps_old_behavior(self):
        old = {"mode": "marketplace_relevance_only", "criteria": [], "items": []}
        self.assertEqual(e_contract(old)["version"], PROMPT_VERSION)
        new = dict(old, relevance_style="short-references-v2")
        self.assertEqual(e_contract(new)["version"], SOURCE_RELEVANCE_PROMPT_VERSION)
        with self.assertRaisesRegex(SafetyError, "AI_E_CONTRACT_INVALID"):
            e_contract(new, PROMPT_VERSION)

    def test_short_aliases_bind_real_rows_without_changing_checklist_or_recollecting(self):
        task_id = self.acquired(); before = self.memory.snapshot(); runner = Runner()
        result = review_source_results(self.memory, runner, run_id="full-list", as_of=NOW)
        after = self.memory.snapshot()
        self.assertEqual(result["remaining_questions"], 0)
        self.assertEqual(after["query_tasks"][task_id]["target_results"]["first"]["state"], "completed_relevant")
        self.assertEqual(before["assessments"], after["assessments"])
        self.assertEqual(before["captures"], after["captures"])
        self.assertEqual(result["source_calls"], 0)
        self.assertEqual(len(runner.calls), 1)
        record = next(iter(after["market_reviews"].values()))
        self.assertEqual({row["id"] for row in record["relevance"]}, set(before["observations"]))

    def test_shared_capture_reviews_each_target_independently_and_replays_without_model(self):
        task_id = self.acquired(ids=["first", "second"]); runner = Runner()
        review_source_results(self.memory, runner, run_id="full-list", as_of=NOW)
        self.assertEqual(set(self.memory.snapshot()["query_tasks"][task_id]["target_results"]), {"first", "second"})
        self.assertEqual(len(self.memory.snapshot()["market_reviews"]), 2)
        calls = len(runner.calls)
        result = review_source_results(self.memory, runner, run_id="full-list", as_of=NOW)
        self.assertEqual(result["remaining_questions"], 0)
        self.assertEqual(len(runner.calls), calls)

    def test_all_unrelated_entries_mean_no_match_in_that_capture_not_missing_checks(self):
        task_id = self.acquired()
        result = review_source_results(self.memory, Runner("unrelated"), run_id="full-list", as_of=NOW)
        target = self.memory.snapshot()["query_tasks"][task_id]["target_results"]["first"]
        self.assertEqual(result["remaining_questions"], 0)
        self.assertEqual(target["state"], "completed_no_results")
        self.assertIn("not proof of absence", target["reason"])

    def test_explicit_uncertainty_is_separate_from_no_results(self):
        task_id = self.acquired()
        review_source_results(self.memory, Runner("unclear"), run_id="full-list", as_of=NOW)
        target = self.memory.snapshot()["query_tasks"][task_id]["target_results"]["first"]
        self.assertEqual(target["state"], "insufficient")
        self.assertIn("did not establish", target["reason"])

    def test_bounded_slice_continues_remaining_entries_without_omission_as_a_verdict(self):
        task_id = self.acquired(count=3); runner = Runner("unrelated")
        first = review_source_results(self.memory, runner, run_id="full-list", per_product=1, as_of=NOW)
        self.assertEqual(first["remaining_questions"], 1)
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id]["target_results"]["first"]["state"], "interpretation_pending")
        review_source_results(self.memory, runner, run_id="full-list", per_product=1, as_of=NOW)
        last = review_source_results(self.memory, runner, run_id="full-list", per_product=1, as_of=NOW)
        self.assertEqual(last["remaining_questions"], 0)
        self.assertEqual(len(runner.calls), 3)

    def test_invalid_reference_has_only_one_cached_repair_using_same_facts(self):
        task_id = self.acquired(); runner = Runner(invalid=True)
        result = review_source_results(self.memory, runner, run_id="full-list", as_of=NOW)
        self.assertEqual(result["remaining_questions"], 0)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(runner.calls[0]["items"], runner.calls[1]["items"])
        self.assertIn("do not target positive", runner.calls[1]["checklist_repair"]["rules"])
        review_source_results(self.memory, runner, run_id="full-list", as_of=NOW)
        self.assertEqual(len(runner.calls), 2)

    def test_omitted_result_is_not_silently_finalised_as_unknown_and_bad_repair_does_not_loop(self):
        self.acquired(); runner = Runner(missing=True)
        result = review_source_results(self.memory, runner, run_id="full-list", as_of=NOW)
        self.assertEqual(result["remaining_questions"], 1)
        self.assertEqual(result["outcomes"][0]["reason"], "AI_RELEVANCE_RESULT_MISSING")
        review_source_results(self.memory, runner, run_id="full-list", as_of=NOW)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(self.memory.snapshot()["market_reviews"], {})

    def test_description_change_during_model_keeps_result_but_does_not_apply_to_new_product(self):
        task_id = self.acquired()
        def mutate():
            old = self.memory.snapshot()["concepts"]["first"]
            new = deepcopy(old); new["description"]["mechanism"] = "electric"
            self.memory.put("concepts", "first", new, previous=old)
        result = review_source_results(self.memory, Runner(mutate=mutate), run_id="full-list", as_of=NOW)
        self.assertIn("SOURCE_TARGET_DESCRIPTION_CHANGED", {row.get("reason") for row in result["outcomes"]})
        self.assertEqual(self.memory.snapshot()["market_reviews"], {})
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id]["target_results"]["first"]["state"], "interpretation_pending")

    def test_local_apply_conflict_replays_reshaped_original_job_without_another_model_turn(self):
        self.acquired(ids=["first", "second"]); runner = Runner()
        original = self.memory.commit
        def conflict(event_id, changes, *, validate=None):
            if any(row["table"] == "market_reviews" for row in changes):
                raise SafetyError("RUNNER_ALREADY_ACTIVE")
            return original(event_id, changes, validate=validate)
        with patch.object(self.memory, "commit", side_effect=conflict):
            result = review_source_results(self.memory, runner, run_id="full-list", as_of=NOW)
        self.assertEqual(result["remaining_questions"], 2)
        self.assertEqual(len(runner.calls), 1)
        first = review_source_results(self.memory, runner, run_id="full-list", limit=1, as_of=NOW)
        self.assertEqual(first["remaining_questions"], 1)
        final = review_source_results(self.memory, runner, run_id="full-list", limit=1, as_of=NOW)
        self.assertEqual(final["remaining_questions"], 0)
        self.assertEqual(len(runner.calls), 1)
        jobs = {row["job_id"] for row in self.memory.snapshot()["market_reviews"].values()}
        self.assertEqual(jobs, set(self.memory.snapshot()["jobs"]))

    def six_products(self):
        template = self.memory.snapshot()["concepts"]["first"]
        ids = ["first", "second", "third", "fourth", "fifth", "sixth"]
        for cid in ids[2:]:
            self.memory.put("concepts", cid, deepcopy(template))
        self.acquired(ids=ids)
        return ids

    def test_source_pre_intent_file_error_isolates_chunk_and_allows_healthy_chunk(self):
        self.six_products(); runner = Runner(); blocked = False
        def write(path, value, **kwargs):
            nonlocal blocked
            if Path(path).name == "intent.json" and not blocked:
                blocked = True
                raise FileNotFoundError("synthetic private path must not appear in outcomes")
            return atomic_json(path, value, **kwargs)
        with patch("product_radar.research_ai.atomic_json", side_effect=write):
            result = review_source_results(self.memory, runner, run_id="full-list", limit=6, as_of=NOW)
        self.assertEqual(result["remaining_questions"], 5)
        self.assertEqual(len(runner.calls), 1)
        waiting = [row for row in result["outcomes"] if row["state"] == "waiting"]
        self.assertEqual(len(waiting), 5)
        self.assertEqual({row["reason"] for row in waiting}, {"SOURCE_REVIEW_LOCAL_FILE_FAILED"})
        self.assertEqual(len(self.memory.snapshot()["market_reviews"]), 1)
        replay = review_source_results(self.memory, runner, run_id="full-list", limit=6, as_of=NOW)
        self.assertEqual(replay["remaining_questions"], 0)
        self.assertEqual(len(runner.calls), 2)  # Previously undispatched work only.

    def test_source_saved_completion_recovers_failed_result_save_without_model_relaunch(self):
        self.six_products(); runner = DurableRunner(Runner()); failed_folder = None
        def write(path, value, **kwargs):
            nonlocal failed_folder
            path = Path(path)
            if path.name == "result.json" and failed_folder is None:
                failed_folder = path.parent
                raise OSError("synthetic result save failure")
            if path.parent == failed_folder and path.name == "failure.json":
                raise OSError("synthetic failure receipt save failure")
            return atomic_json(path, value, **kwargs)
        with patch("product_radar.research_ai.atomic_json", side_effect=write):
            result = review_source_results(self.memory, runner, run_id="full-list", limit=6, as_of=NOW)
        self.assertEqual(result["remaining_questions"], 5)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual({row["reason"] for row in result["outcomes"] if row["state"] == "waiting"},
                         {"SOURCE_REVIEW_LOCAL_FILE_FAILED"})
        before = {name: (failed_folder / name).read_bytes() for name in ("intent.json", "completion.json")}
        cold = ResearchMemory(self.memory.root, self.memory.journal, evidence_type="synthetic")
        recovered = review_source_results(cold, runner, run_id="full-list", limit=6, as_of=NOW)
        self.assertEqual(recovered["remaining_questions"], 0)
        self.assertEqual(len(runner.calls), 2)
        self.assertTrue((failed_folder / "result.json").is_file())
        for name, value in before.items():
            self.assertEqual((failed_folder / name).read_bytes(), value)
        self.assertIn(failed_folder.name, {row["job_id"] for row in cold.snapshot()["market_reviews"].values()})

    def test_source_uncertain_saved_intent_never_blindly_relaunches_model(self):
        self.six_products(); runner = Runner(); failed_folder = None
        def write(path, value, **kwargs):
            nonlocal failed_folder
            path = Path(path)
            if path.name == "result.json" and failed_folder is None:
                failed_folder = path.parent
                raise OSError("synthetic lost result")
            if path.parent == failed_folder and path.name == "failure.json":
                raise OSError("synthetic lost failure receipt")
            return atomic_json(path, value, **kwargs)
        with patch("product_radar.research_ai.atomic_json", side_effect=write):
            result = review_source_results(self.memory, runner, run_id="full-list", limit=6, as_of=NOW)
        self.assertEqual(result["remaining_questions"], 5)
        self.assertEqual(len(runner.calls), 2)
        cold = ResearchMemory(self.memory.root, self.memory.journal, evidence_type="synthetic")
        replay = review_source_results(cold, runner, run_id="full-list", limit=6, as_of=NOW)
        self.assertEqual(replay["remaining_questions"], 5)
        self.assertEqual({row["reason"] for row in replay["outcomes"] if row["state"] == "waiting"},
                         {"AI_JOB_PENDING_RECONCILIATION"})
        self.assertEqual(len(runner.calls), 2)
        self.assertFalse((failed_folder / "result.json").exists())

    def test_source_item_save_file_error_keeps_sibling_and_replays_original_batch(self):
        self.acquired(ids=["first", "second"]); runner = Runner(); original = self.memory.commit
        def write(event_id, changes, *, validate=None):
            if any(row["table"] == "market_reviews" and row["value"]["concept_id"] == "first" for row in changes):
                raise OSError("synthetic first-item save failure")
            return original(event_id, changes, validate=validate)
        with patch.object(self.memory, "commit", side_effect=write):
            result = review_source_results(self.memory, runner, run_id="full-list", as_of=NOW)
        self.assertEqual(result["remaining_questions"], 1)
        self.assertEqual({row["concept_id"] for row in self.memory.snapshot()["market_reviews"].values()}, {"second"})
        self.assertEqual(len(runner.calls), 1)
        job = next(iter(self.memory.snapshot()["jobs"]))
        raw = (self.memory.root / "ai_jobs" / job / "result.json").read_bytes()
        cold = ResearchMemory(self.memory.root, self.memory.journal, evidence_type="synthetic")
        replay = review_source_results(cold, runner, run_id="full-list", limit=1, as_of=NOW)
        self.assertEqual(replay["remaining_questions"], 0)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual((cold.root / "ai_jobs" / job / "result.json").read_bytes(), raw)
        self.assertEqual({row["job_id"] for row in cold.snapshot()["market_reviews"].values()}, {job})

    def test_source_bind_file_error_keeps_sibling_and_reuses_saved_relevance(self):
        from product_radar.source_coverage import bind_source_relevance
        task = self.acquired(ids=["first", "second"]); runner = Runner()
        def bind(memory, task_id, targets, **kwargs):
            if targets[0]["concept_id"] == "first":
                raise FileNotFoundError("synthetic first-target receipt failure")
            return bind_source_relevance(memory, task_id, targets, **kwargs)
        with patch("product_radar.source_interpretation.bind_source_relevance", side_effect=bind):
            result = review_source_results(self.memory, runner, run_id="full-list", as_of=NOW)
        self.assertEqual(result["remaining_questions"], 1)
        targets = self.memory.snapshot()["query_tasks"][task]["target_results"]
        self.assertEqual(targets["first"]["state"], "interpretation_pending")
        self.assertEqual(targets["second"]["state"], "completed_relevant")
        self.assertIn("SOURCE_REVIEW_LOCAL_FILE_FAILED", {row.get("reason") for row in result["outcomes"]})
        replay = review_source_results(self.memory, runner, run_id="full-list", as_of=NOW)
        self.assertEqual(replay["remaining_questions"], 0)
        self.assertEqual(len(runner.calls), 1)

    def test_source_integrity_errors_are_not_hidden_as_local_item_waits(self):
        self.acquired()
        for code in ("RESEARCH_EVENTS_INVALID", "RESEARCH_CAPTURE_CONFLICT", "SYMLINK_PATH_REFUSED"):
            with self.subTest(code=code), patch("product_radar.source_interpretation.Jobs.run", side_effect=SafetyError(code)):
                with self.assertRaisesRegex(SafetyError, code):
                    review_source_results(self.memory, Runner(), run_id="full-list", as_of=NOW)
        # A trusted binding error must also propagate rather than appear as
        # successful source completion.
        with patch("product_radar.source_interpretation.bind_source_relevance", side_effect=SafetyError("RESEARCH_EVENTS_INVALID")):
            with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
                review_source_results(self.memory, Runner(), run_id="full-list", as_of=NOW)

    def test_skipping_one_failed_target_does_not_skip_other_source_questions_for_that_product(self):
        skipped = self.acquired(query="sink caddy")
        healthy = self.acquired(query="draining sink holder")
        runner = Runner()
        result = review_source_results(self.memory, runner, run_id="full-list", as_of=NOW,
                                       skip_targets=[(skipped, "first")])
        self.assertEqual(result["remaining_questions"], 0)
        # The healthy identical saved meaning may also settle the first source
        # without rerunning a failed model input or making another request.
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.memory.snapshot()["query_tasks"][healthy]["target_results"]["first"]["state"], "completed_relevant")

    def supplier_capture(self):
        return capture_saved_supplier_bundle(self.memory, {"concept_id": "first", "lead": {
            "id": "fixture-lead", "urls": [SUPPLIER_BASE + "/product/caddy"]}}, fetcher=Reader())

    def supplier_search_task(self, urls):
        route = "google-suppliers"
        planned = prepare_source_tasks(self.memory, [{"route": route, "request": {"query": "sink caddy", "country": "IN"},
            "concept_ids": ["first"]}], run_id="full-list", as_of=NOW)
        check_id = key("fixture-supplier-search", urls)
        def handler(context):
            context["record_dispatch"]()
            self.memory.put("source_checks", check_id, {"kind": "supplier_route_search", "route": route,
                "concept_id": "first", "description_hash": context["description_hashes"]["first"],
                "candidates": [{"url": url, "title": "Synthetic supplier destination"} for url in urls]})
            return {"schema_version": 1, "route": route, "acquisition_id": context["acquisition_id"],
                "observed_at": NOW, "state": "usable", "dispatch_started": True,
                "reference": "synthetic-supplier-search/receipt.json", "capture_ids": [], "observation_ids": [],
                "evidence_refs": [check_id], "reason": "Synthetic actual-search receipt.",
                "target_results": [{"concept_id": "first", "description_hash": context["description_hashes"]["first"],
                    "state": "interpretation_pending", "evidence_refs": [check_id],
                    "reason": "Returned destination pages need interpretation."}], "retryable": False}
        execute_source_run(self.memory, "full-list", handlers={route: handler}, as_of=NOW,
            bounded_call=lambda callback, context, timeout: callback(context))
        return planned["task_ids"][0], check_id

    def supplier_company_task(self, captures, check_id, urls, *, execute=True, outer_state="usable", run_id="full-list"):
        route = "company-pages"
        request = {"candidate_urls": {"first": urls}, "candidate_check_ids": {"first": [check_id]}, "country": "IN"}
        prepare_source_tasks(self.memory, [{"route": route, "request": request, "concept_ids": ["first"]}],
            run_id=run_id, as_of=NOW)
        if execute:
            def handler(context):
                context["record_dispatch"]()
                refs = [row["path"] for row in captures]
                return {"schema_version": 1, "route": route, "acquisition_id": context["acquisition_id"],
                    "observed_at": NOW, "state": outer_state, "dispatch_started": True,
                    "reference": refs[0], "capture_ids": [], "observation_ids": [], "evidence_refs": refs,
                    "reason": "Synthetic bounded supplier-page pass.", "target_results": [{"concept_id": "first",
                        "description_hash": context["description_hashes"]["first"],
                        "state": "failed" if outer_state == "failed" else "interpretation_pending",
                        "evidence_refs": refs, "reason": "Synthetic original page result."}], "retryable": False}
            execute_source_run(self.memory, run_id, handlers={route: handler}, as_of=NOW,
                bounded_call=lambda callback, context, timeout: callback(context))
        return key("source-task", [run_id, route, request])

    def supplier_failed_capture(self, url):
        return capture_saved_supplier_bundle(self.memory, {"concept_id": "first", "lead": {
            "id": key("fixture-failed-lead", url), "urls": [url]}},
            fetcher=Reader({url: {"state": "unavailable", "reason": "PUBLIC_FETCH_DEADLINE"}}))

    def test_saved_supplier_interpretation_replays_original_pages_and_preserves_dates(self):
        captured = self.supplier_capture(); runner = FixtureRunner()
        with patch("product_radar.suppliers.utc_now", return_value=NOW):
            first = review_supplier_results(self.memory, runner, as_of=NOW)
        self.assertEqual(len(first["accepted"]), 1)
        self.assertEqual(first["remaining_inputs"], 0)
        self.assertEqual(first["source_calls"], 0)
        saved = next(iter(self.memory.snapshot()["supplier_assessments"].values()))
        self.assertEqual(set(saved["source_dates"].values()), {"2026-10-06T08:00:00+00:00"})
        second = review_supplier_results(self.memory, runner, as_of=NOW)
        self.assertEqual(second["reused_capture_ids"], [captured["capture_id"]])
        self.assertEqual(len(runner.calls), 1)

    def test_live_supplier_completion_binds_company_and_original_search_in_same_call(self):
        later = "2026-10-08T10:00:10+00:00"
        captured = self.supplier_capture(); url = SUPPLIER_BASE + "/product/caddy"
        search, check = self.supplier_search_task([url])
        company = self.supplier_company_task([captured], check, [url])
        before = self.memory.snapshot(); capture_bytes = Path(captured["path"]).read_bytes()
        runner = SupplierRunner("exact_product")
        with patch("product_radar.source_interpretation.utc_now", side_effect=[NOW, later]), \
                patch("product_radar.suppliers.utc_now", return_value=later):
            result = review_supplier_results(self.memory, runner)
        after = self.memory.snapshot()
        self.assertEqual({(row["task_id"], row["state"]) for row in result["source_bindings"]},
                         {(company, "completed_relevant"), (search, "completed_relevant")})
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(result["source_calls"], 0)
        self.assertEqual(result["remaining_inputs"], 0)
        assessment = next(iter(after["supplier_assessments"].values()))
        self.assertEqual(assessment["checked_at"], later)
        self.assertEqual(set(assessment["source_dates"].values()), {"2026-10-06T08:00:00+00:00"})
        self.assertEqual(assessment["input_hash"], digest(assessment["input"]))
        self.assertEqual(assessment["input"]["product"], dict(before["concepts"]["first"]["description"], concept_id="first"))
        self.assertEqual(after["concepts"], before["concepts"])
        for task_id in (search, company):
            self.assertEqual(after["query_tasks"][task_id]["request"], before["query_tasks"][task_id]["request"])
            self.assertEqual(after["query_tasks"][task_id]["targets"], before["query_tasks"][task_id]["targets"])
            self.assertEqual(after["query_tasks"][task_id]["receipt_refs"], before["query_tasks"][task_id]["receipt_refs"])
        for ref, receipt in before["source_checks"].items():
            self.assertEqual(after["source_checks"][ref], receipt)
        self.assertEqual({after["source_checks"][row["receipt_id"]]["at"] for row in result["source_bindings"]}, {later})
        self.assertEqual(Path(captured["path"]).read_bytes(), capture_bytes)
        original_result = (self.memory.root / "ai_jobs" / assessment["job_id"] / "result.json").read_bytes()
        # Exact saved meaning and completed target receipts need neither
        # another F call nor another source request on the following pass.
        with patch("product_radar.source_interpretation.utc_now", return_value=later), \
                patch("product_radar.suppliers.extract_supplier_terms", side_effect=AssertionError("unexpected F call")), \
                patch("product_radar.supplier_capture_store.capture_supplier_bundle", side_effect=AssertionError("unexpected source call")):
            replay = review_supplier_results(self.memory, runner)
        self.assertEqual(replay["selected_capture_ids"], [])
        self.assertEqual(replay["reused_capture_ids"], [captured["capture_id"]])
        self.assertEqual(replay["source_bindings"], [])
        self.assertEqual(replay["source_calls"], 0)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.memory.snapshot(), after)
        self.assertEqual((self.memory.root / "ai_jobs" / assessment["job_id"] / "result.json").read_bytes(), original_result)
        self.assertEqual(Path(captured["path"]).read_bytes(), capture_bytes)

    def test_explicit_supplier_cutoff_does_not_adopt_later_completion_on_review_or_replay(self):
        later = "2026-10-08T10:00:10+00:00"
        captured = self.supplier_capture(); url = SUPPLIER_BASE + "/product/caddy"
        search, check = self.supplier_search_task([url])
        company = self.supplier_company_task([captured], check, [url])
        runner = SupplierRunner("exact_product")
        with patch("product_radar.source_interpretation.utc_now", side_effect=AssertionError("historical cutoff must stay fixed")), \
                patch("product_radar.suppliers.utc_now", return_value=later):
            result = review_supplier_results(self.memory, runner, as_of=NOW)
            completed = self.memory.snapshot()
            replay = review_supplier_results(self.memory, runner, as_of=NOW)
        self.assertEqual(result["source_bindings"], [])
        self.assertEqual(replay["source_bindings"], [])
        self.assertEqual(self.memory.snapshot(), completed)
        self.assertEqual(len(runner.calls), 1)
        for task_id in (search, company):
            self.assertEqual(completed["query_tasks"][task_id]["target_results"]["first"]["state"], "interpretation_pending")
        assessment = next(iter(completed["supplier_assessments"].values()))
        self.assertEqual(assessment["checked_at"], later)
        with patch("product_radar.suppliers.extract_supplier_terms", side_effect=AssertionError("unexpected F call")), \
                patch("product_radar.supplier_capture_store.capture_supplier_bundle", side_effect=AssertionError("unexpected source call")):
            later_view = review_supplier_results(self.memory, runner, as_of=later)
        self.assertEqual({row["task_id"] for row in later_view["source_bindings"]}, {company, search})
        self.assertEqual(later_view["reused_capture_ids"], [captured["capture_id"]])
        self.assertEqual(later_view["source_calls"], 0)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.memory.snapshot()["supplier_assessments"], completed["supplier_assessments"])

    def test_live_supplier_completion_clock_is_validated_before_binding(self):
        captured = self.supplier_capture(); url = SUPPLIER_BASE + "/product/caddy"
        _, check = self.supplier_search_task([url])
        company = self.supplier_company_task([captured], check, [url])
        runner = SupplierRunner("exact_product")
        with patch("product_radar.source_interpretation.utc_now", side_effect=[NOW, "not-a-time"]), \
                patch("product_radar.suppliers.utc_now", return_value=NOW):
            with self.assertRaisesRegex(SafetyError, "INVALID_TIME"):
                review_supplier_results(self.memory, runner)
        state = self.memory.snapshot()
        self.assertEqual(len(state["supplier_assessments"]), 1)
        self.assertEqual(state["query_tasks"][company]["target_results"]["first"]["state"], "interpretation_pending")
        # The original completed answer survives a bad local clock and is
        # bound from saved data once that clock is valid; no model repeat.
        with patch("product_radar.source_interpretation.utc_now", return_value=NOW), \
                patch("product_radar.suppliers.extract_supplier_terms", side_effect=AssertionError("unexpected F call")):
            recovered = review_supplier_results(self.memory, runner)
        self.assertEqual({row["state"] for row in recovered["source_bindings"]}, {"completed_relevant"})
        self.assertEqual(len(runner.calls), 1)

    def test_invalid_explicit_supplier_cutoff_does_not_fall_back_to_live_time(self):
        with patch("product_radar.source_interpretation.utc_now", side_effect=AssertionError("explicit cutoff must not become live")):
            with self.assertRaisesRegex(SafetyError, "INVALID_TIME"):
                review_supplier_results(self.memory, FixtureRunner(), as_of="")

    def test_supplier_review_named_product_scope_does_not_spend_slots_on_other_saved_products(self):
        self.supplier_capture(); runner = FixtureRunner()
        result = review_supplier_results(self.memory, runner, concept_ids=["second"], as_of=NOW)
        self.assertEqual(result["selected_capture_ids"], [])
        self.assertEqual(result["remaining_inputs"], 0)
        self.assertEqual(self.memory.snapshot()["supplier_assessments"], {})
        with self.assertRaisesRegex(SafetyError, "SOURCE_REVIEW_TARGETS_INVALID"):
            review_supplier_results(self.memory, runner, concept_ids=["unknown"], as_of=NOW)

    def test_supplier_helper_versions_saved_f_meaning_without_rewriting_or_recollecting(self):
        from product_radar.supplier_capture_store import load_saved_supplier_input
        from product_radar.suppliers import extract_supplier_terms
        captured = self.supplier_capture(); saved = load_saved_supplier_input(self.memory, captured["path"])
        runner = SupplierRunner("unrelated")
        capture_bytes = Path(captured["path"]).read_bytes()
        with patch("product_radar.research_ai.SUPPLIER_PROMPT_VERSION", "research-f-v2"), \
                patch("product_radar.suppliers.utc_now", return_value=NOW):
            extract_supplier_terms(self.memory, [saved["item"]], runner,
                                   source_dates={saved["item"]["id"]: saved["page_dates"]})
        before = self.memory.snapshot()
        original_answers = {job_id: (self.memory.root / "ai_jobs" / job_id / "result.json").read_bytes()
                            for job_id in before["jobs"]}
        with patch("product_radar.suppliers.utc_now", return_value=NOW):
            current = review_supplier_results(self.memory, runner, as_of=NOW)
        self.assertEqual(current["selected_capture_ids"], [captured["capture_id"]])
        self.assertEqual(current["reused_capture_ids"], [])
        self.assertEqual(len(runner.calls), 2)
        after = self.memory.snapshot()
        self.assertEqual(len(after["supplier_assessments"]), 2)
        for record_id, record in before["supplier_assessments"].items():
            self.assertEqual(after["supplier_assessments"][record_id], record)
        for job_id, raw in original_answers.items():
            self.assertEqual((self.memory.root / "ai_jobs" / job_id / "result.json").read_bytes(), raw)
        self.assertEqual(Path(captured["path"]).read_bytes(), capture_bytes)
        replay = review_supplier_results(self.memory, runner, as_of=NOW)
        self.assertEqual(replay["selected_capture_ids"], [])
        self.assertEqual(replay["reused_capture_ids"], [captured["capture_id"]])
        self.assertEqual(len(runner.calls), 2)

    def test_company_page_task_settles_only_after_its_exact_captured_supplier_is_reviewed(self):
        captured = self.supplier_capture()
        route = "company-pages"
        planned = prepare_source_tasks(self.memory, [{"route": route, "request": {"candidate_urls": {
            "first": [SUPPLIER_BASE + "/product/caddy"]}, "country": "IN"}, "concept_ids": ["first"]}],
            run_id="full-list", as_of=NOW)
        def handler(context):
            context["record_dispatch"]()
            return {"schema_version": 1, "route": route, "acquisition_id": context["acquisition_id"],
                "observed_at": NOW, "state": "usable", "dispatch_started": True,
                "reference": captured["path"], "capture_ids": [], "observation_ids": [],
                "evidence_refs": [captured["path"]], "reason": "Synthetic captured company page.",
                "target_results": [{"concept_id": "first", "description_hash": context["description_hashes"]["first"],
                    "state": "interpretation_pending", "evidence_refs": [captured["path"]],
                    "reason": "The captured supplier still needs interpretation."}], "retryable": False}
        execute_source_run(self.memory, "full-list", handlers={route: handler}, as_of=NOW,
                           bounded_call=lambda callback, context, timeout: callback(context))
        task_id = planned["task_ids"][0]
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id]["target_results"]["first"]["state"], "interpretation_pending")
        with patch("product_radar.suppliers.utc_now", return_value=NOW):
            result = review_supplier_results(self.memory, FixtureRunner(), as_of=NOW)
        self.assertEqual(result["source_bindings"][0]["state"], "completed_relevant")
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id]["target_results"]["first"]["state"], "completed_relevant")

    def test_corrupt_supplier_capture_is_reported_without_fetching_or_interpreting(self):
        captured = self.supplier_capture(); runner = FixtureRunner()
        from product_radar.storage import atomic_json, load_json
        saved = load_json(captured["path"], 8 * 1024 * 1024)
        saved["input_hash"] = "incorrect-fixture-hash"
        atomic_json(captured["path"], saved)
        result = review_supplier_results(self.memory, runner, as_of=NOW)
        self.assertEqual(len(result["skipped_captures"]), 1)
        self.assertEqual(result["accepted"], {})
        self.assertEqual(runner.calls, [])

    def test_supplier_capture_read_file_error_isolates_healthy_capture(self):
        from product_radar.supplier_capture_store import load_saved_supplier_capture
        failed = self.supplier_capture()
        healthy = capture_saved_supplier_bundle(self.memory, {"concept_id": "second", "lead": {
            "id": "healthy-fixture-lead", "urls": [SUPPLIER_BASE + "/product/caddy"]}}, fetcher=Reader())
        runner = FixtureRunner()
        def read(memory, path, **kwargs):
            if Path(path) == Path(failed["path"]):
                raise FileNotFoundError("synthetic private receipt path")
            return load_saved_supplier_capture(memory, path, **kwargs)
        with patch("product_radar.supplier_capture_store.load_saved_supplier_capture", side_effect=read), \
                patch("product_radar.suppliers.utc_now", return_value=NOW):
            result = review_supplier_results(self.memory, runner, as_of=NOW)
        self.assertEqual(result["selected_capture_ids"], [healthy["capture_id"]])
        self.assertEqual(result["skipped_captures"], [{"capture_id": failed["capture_id"],
            "state": "not_interpreted", "reason": "SUPPLIER_REVIEW_LOCAL_FILE_FAILED"}])
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual(len(runner.calls), 1)
        with patch("product_radar.suppliers.utc_now", return_value=NOW):
            recovered = review_supplier_results(self.memory, runner, as_of=NOW)
        self.assertEqual(recovered["selected_capture_ids"], [failed["capture_id"]])
        self.assertEqual(recovered["reused_capture_ids"], [healthy["capture_id"]])
        self.assertEqual(len(runner.calls), 2)  # Previously unread input only.

    def test_supplier_capture_trusted_store_errors_are_not_hidden_as_missing_pages(self):
        self.supplier_capture()
        for code in ("RESEARCH_EVENTS_INVALID", "SYMLINK_PATH_REFUSED"):
            with self.subTest(code=code), patch("product_radar.supplier_capture_store.load_saved_supplier_capture", side_effect=SafetyError(code)):
                with self.assertRaisesRegex(SafetyError, code):
                    review_supplier_results(self.memory, FixtureRunner(), as_of=NOW)

    def test_supplier_bind_file_error_does_not_stop_other_task_and_reuses_saved_answer(self):
        from product_radar.source_coverage import bind_source_relevance
        captured = self.supplier_capture(); url = SUPPLIER_BASE + "/product/caddy"
        search, check = self.supplier_search_task([url])
        company = self.supplier_company_task([captured], check, [url])
        runner = SupplierRunner("exact_product")
        def bind(memory, task_id, targets, **kwargs):
            if task_id == company:
                raise OSError("synthetic company-target receipt failure")
            return bind_source_relevance(memory, task_id, targets, **kwargs)
        with patch("product_radar.source_interpretation.bind_source_relevance", side_effect=bind), \
                patch("product_radar.suppliers.utc_now", return_value=NOW):
            result = review_supplier_results(self.memory, runner, as_of=NOW)
        self.assertEqual({(row["task_id"], row["state"]) for row in result["source_bindings"]},
                         {(company, "waiting"), (search, "completed_relevant")})
        self.assertEqual(next(row["reason"] for row in result["source_bindings"] if row["state"] == "waiting"),
                         "SUPPLIER_REVIEW_LOCAL_FILE_FAILED")
        self.assertEqual(len(runner.calls), 1)
        cold = ResearchMemory(self.memory.root, self.memory.journal, evidence_type="synthetic")
        replay = review_supplier_results(cold, runner, as_of=NOW)
        self.assertEqual(replay["source_bindings"][0]["state"], "completed_relevant")
        self.assertEqual(replay["source_bindings"][0]["task_id"], company)
        self.assertEqual(len(runner.calls), 1)

    def test_supplier_search_all_reviewed_unrelated_is_completed_not_missing(self):
        captured = self.supplier_capture()
        search, check = self.supplier_search_task([SUPPLIER_BASE + "/product/caddy"])
        company = self.supplier_company_task([captured], check, [SUPPLIER_BASE + "/product/caddy"])
        with patch("product_radar.suppliers.utc_now", return_value=NOW):
            result = review_supplier_results(self.memory, SupplierRunner("unrelated"), as_of=NOW)
        self.assertEqual({row["state"] for row in result["source_bindings"]}, {"completed_no_results"})
        for task in (search, company):
            self.assertEqual(self.memory.snapshot()["query_tasks"][task]["target_results"]["first"]["state"], "completed_no_results")
        self.assertIn("not proof", self.memory.snapshot()["query_tasks"][search]["target_results"]["first"]["reason"])

    def test_reviewed_redirected_supplier_page_retains_original_search_destination_binding(self):
        requested, final = SUPPLIER_BASE + "/product/caddy", SUPPLIER_BASE + "/listing/caddy"
        captured = capture_saved_supplier_bundle(self.memory, {"concept_id": "first", "lead": {
            "id": "redirected-fixture-lead", "urls": [requested]}}, fetcher=Reader({requested: {"final_url": final}}))
        search, check = self.supplier_search_task([requested])
        self.supplier_company_task([captured], check, [requested])
        with patch("product_radar.suppliers.utc_now", return_value=NOW):
            review_supplier_results(self.memory, SupplierRunner("exact_product"), as_of=NOW)
        self.assertEqual(self.memory.snapshot()["query_tasks"][search]["target_results"]["first"]["state"], "completed_relevant")

    def test_mixed_reviewed_unrelated_and_failed_company_pages_close_insufficient_once(self):
        usable = self.supplier_capture(); failed = self.supplier_failed_capture(SUPPLIER_BASE + "/unreadable")
        urls = [SUPPLIER_BASE + "/product/caddy", SUPPLIER_BASE + "/unreadable"]
        search, check = self.supplier_search_task(urls)
        company = self.supplier_company_task([usable, failed], check, urls)
        runner = SupplierRunner("unrelated")
        with patch("product_radar.suppliers.utc_now", return_value=NOW):
            result = review_supplier_results(self.memory, runner, as_of=NOW)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual({row["state"] for row in result["source_bindings"]}, {"insufficient"})
        for task in (search, company):
            self.assertEqual(self.memory.snapshot()["query_tasks"][task]["target_results"]["first"]["state"], "insufficient")
        self.assertIn(company, self.memory.snapshot()["query_tasks"][search]["target_results"]["first"]["reason"])
        replay = review_supplier_results(self.memory, runner, as_of=NOW)
        self.assertEqual(replay["source_bindings"], [])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(replay["source_calls"], 0)

    def test_all_failed_company_reads_close_original_search_without_model_or_absence_claim(self):
        url = SUPPLIER_BASE + "/unreadable"
        failed = self.supplier_failed_capture(url)
        search, check = self.supplier_search_task([url])
        company = self.supplier_company_task([failed], check, [url], outer_state="failed")
        runner = SupplierRunner("exact_product")
        result = review_supplier_results(self.memory, runner, as_of=NOW)
        self.assertEqual(runner.calls, [])
        self.assertEqual(result["source_bindings"][0]["state"], "insufficient")
        self.assertEqual(self.memory.snapshot()["query_tasks"][search]["target_results"]["first"]["state"], "insufficient")
        self.assertEqual(self.memory.snapshot()["query_tasks"][company]["target_results"]["first"]["state"], "failed")

    def test_unattempted_or_other_run_company_pass_does_not_close_search(self):
        url = SUPPLIER_BASE + "/unreadable"
        failed = self.supplier_failed_capture(url)
        search, check = self.supplier_search_task([url])
        self.supplier_company_task([failed], check, [url], execute=False)
        review_supplier_results(self.memory, SupplierRunner("unrelated"), as_of=NOW)
        self.assertEqual(self.memory.snapshot()["query_tasks"][search]["target_results"]["first"]["state"], "interpretation_pending")
        self.supplier_company_task([failed], check, [url], run_id="another-run", outer_state="failed")
        review_supplier_results(self.memory, SupplierRunner("unrelated"), as_of=NOW)
        self.assertEqual(self.memory.snapshot()["query_tasks"][search]["target_results"]["first"]["state"], "interpretation_pending")

    def test_bounded_company_slice_does_not_call_uninspected_search_destinations_absent(self):
        captured = self.supplier_capture()
        urls = [SUPPLIER_BASE + "/product/caddy", SUPPLIER_BASE + "/not-inspected"]
        search, check = self.supplier_search_task(urls)
        company = self.supplier_company_task([captured], check, urls)
        with patch("product_radar.suppliers.utc_now", return_value=NOW):
            review_supplier_results(self.memory, SupplierRunner("unrelated"), as_of=NOW)
        state = self.memory.snapshot()
        self.assertEqual(state["query_tasks"][company]["target_results"]["first"]["state"], "completed_no_results")
        self.assertEqual(state["query_tasks"][search]["target_results"]["first"]["state"], "insufficient")

    def test_corrupt_sibling_capture_stays_pending_not_failed_page_proof(self):
        from product_radar.storage import atomic_json, load_json
        captured = self.supplier_capture(); failed = self.supplier_failed_capture(SUPPLIER_BASE + "/unreadable")
        urls = [SUPPLIER_BASE + "/product/caddy", SUPPLIER_BASE + "/unreadable"]
        _, check = self.supplier_search_task(urls)
        company = self.supplier_company_task([captured, failed], check, urls)
        raw = load_json(failed["path"], 8 * 1024 * 1024); raw["bundle_hash"] = "corrupt"
        atomic_json(failed["path"], raw)
        with patch("product_radar.suppliers.utc_now", return_value=NOW):
            review_supplier_results(self.memory, SupplierRunner("unrelated"), as_of=NOW)
        self.assertEqual(self.memory.snapshot()["query_tasks"][company]["target_results"]["first"]["state"], "interpretation_pending")


if __name__ == "__main__":
    unittest.main()
