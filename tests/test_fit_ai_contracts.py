"""Mode-bound scoring/writing replay fixtures; no model/provider requests."""

from copy import deepcopy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.fit_policy import ACTIVE_CRITERION_IDS, FIT_POLICY_ID, evaluate_fit
from product_radar.models import SafetyError, canonical
from product_radar.research_ai import (CodexRunner, FIT_E_SCHEMA, FIT_G_SCHEMA, FIT_NARRATION_PROMPT_VERSION,
    FIT_PROMPT_VERSION, INSTRUCTIONS, Jobs, MODEL, PROMPT_VERSION, SCHEMAS, _execution_binding,
    classify_failure, e_contract, g_contract, instructions_for, prepare_retry, prompt_version, schema_for, validate_results)
from product_radar.research_memory import ResearchMemory, key
from product_radar.storage import Journal, atomic_json, load_json
from test_fit_policy import criteria


def fit_payload(*, ids=None, item_ids=("product-one",)):
    requested = list(ACTIVE_CRITERION_IDS) if ids is None else ids
    return {"mode": "fit_assessment" if ids is None else "fit_partial_review",
            "fit_policy_id": FIT_POLICY_ID, "requested_criterion_ids": requested,
            "items": [{"id": item_id, "description": {"function": "Drain wet kitchen utensils", "mechanism": "Drain holes"},
                       "observations": [{"id": "public-feature-observation", "source_text": {"title": "Draining insert"}}]}
                      for item_id in item_ids]}


def fit_result(item_id="product-one", *, ids=None, count=5):
    rows = criteria(count)
    if ids is not None:
        rows = [row for row in rows if row["id"] in ids]
    return {"id": item_id, "criteria": rows, "relevance": [], "compliance_note": "", "missing_questions": [],
            "audience_positioning": {"positioning": "women_centric" if count == 10 else "unclear",
                                     "reason": "Practical supplied buyer context." if ids is None or 13 in ids else
                                               "Not requested in this review."}}


class FitRunner:
    identity = "fit-contract-fixture-v1"
    def __init__(self): self.calls = []
    def __call__(self, role, payload, folder):
        self.calls.append(deepcopy(payload))
        return {"results": [fit_result(item["id"], ids=payload["requested_criterion_ids"]) for item in payload["items"]]}, None


class FitContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")

    def test_default_legacy_roles_and_explicit_new_modes_are_distinct(self):
        self.assertEqual(prompt_version("A"), PROMPT_VERSION)
        self.assertEqual(prompt_version("E"), PROMPT_VERSION)
        self.assertEqual(prompt_version("G"), "research-g-v2")
        payload = fit_payload()
        self.assertEqual(prompt_version("E", payload), FIT_PROMPT_VERSION)
        self.assertIs(schema_for("E", payload), FIT_E_SCHEMA)
        self.assertNotIn("audience_positioning", SCHEMAS["E"]["properties"]["results"]["items"]["properties"])

    def test_five_points_with_five_unknowns_qualifies_without_extra_proof(self):
        payload, response = fit_payload(), {"results": [fit_result()]}
        original = deepcopy(response)
        checked = validate_results("E", payload, response)
        outcome = evaluate_fit(checked["accepted"]["product-one"]["criteria"], FIT_POLICY_ID)
        self.assertEqual((outcome["fit_tier"], outcome["unknown"], outcome["eligible"]), ("decent", 5, True))
        self.assertEqual(response, original)

    def test_not_stated_negative_is_unknown_without_a_free_point(self):
        value = fit_result()
        value["criteria"][-1].update(state="Does not meet", basis="not_stated", references=[],
                                    reason="Buyer shares were not supplied.")
        original = deepcopy(value)
        checked = validate_results("E", fit_payload(), {"results": [value]})
        row = checked["accepted"]["product-one"]["criteria"][-1]
        self.assertEqual(row["state"], "Unclear")
        self.assertIn("AI_FIT_NEGATIVE_NORMALISED:product-one:13", checked["diagnostics"])
        self.assertEqual(evaluate_fit(checked["accepted"]["product-one"]["criteria"], FIT_POLICY_ID)["kpi_matches"], 5)
        self.assertEqual(value, original)

    def test_missing_current_row_is_unknown_with_a_named_diagnostic(self):
        value = fit_result(); value["criteria"] = value["criteria"][:-1]
        checked = validate_results("E", fit_payload(), {"results": [value]})
        self.assertEqual([row["id"] for row in checked["accepted"]["product-one"]["criteria"]], list(ACTIVE_CRITERION_IDS))
        self.assertEqual(checked["accepted"]["product-one"]["criteria"][-1]["state"], "Unclear")
        self.assertIn("AI_FIT_CRITERION_MISSING:product-one:13", checked["diagnostics"])

    def test_exact_partial_rows_do_not_rewrite_retained_criteria(self):
        payload = fit_payload(ids=[7, 13])
        checked = validate_results("E", payload, {"results": [fit_result(ids=[7, 13])]})
        self.assertEqual([row["id"] for row in checked["accepted"]["product-one"]["criteria"]], [7, 13])
        invalid = fit_result(ids=[7, 13]); invalid["criteria"].append(criteria()[0])
        self.assertEqual(validate_results("E", payload, {"results": [invalid]})["failures"]["product-one"],
                         "AI_CRITERION_ID_INVALID")

    def test_one_invalid_reference_or_duplicate_keeps_healthy_siblings(self):
        payload = fit_payload(item_ids=("healthy", "broken"))
        for mutate in (lambda row: row["criteria"][0].update(references=["invented"]),
                       lambda row: row["criteria"].append(deepcopy(row["criteria"][0])),
                       lambda row: row["criteria"][0].update(id=3),
                       lambda row: row["criteria"][0].update(basis="not_stated", references=[])):
            bad = fit_result("broken"); mutate(bad)
            result = validate_results("E", payload, {"results": [fit_result("healthy"), bad]})
            self.assertEqual(set(result["accepted"]), {"healthy"})
            self.assertEqual(set(result["failures"]), {"broken"})

    def test_malformed_saved_evidence_keeps_healthy_item_results(self):
        for observations in (None, [{}], [{"id": []}], [{"id": "duplicate"}, {"id": "duplicate"}]):
            payload = fit_payload(item_ids=("healthy", "broken"))
            payload["items"][1]["observations"] = observations
            response = {"results": [fit_result("healthy"), fit_result("broken")]}
            with self.subTest(observations=observations):
                result = validate_results("E", payload, response)
                self.assertEqual(set(result["accepted"]), {"healthy"})
                self.assertEqual(result["failures"], {"broken": "AI_EVIDENCE_INPUT_INVALID"})

    def test_audience_positioning_is_not_functional_identity_or_a_product_gate(self):
        payload = fit_payload()
        for positioning, state in (("women_centric", "Meets"), ("men_centric", "Does not meet"),
                                   ("unisex", "Does not meet"), ("unclear", "Unclear")):
            value = fit_result()
            value["audience_positioning"].update(positioning=positioning, reason="Specific Indian buyer/use context.")
            value["criteria"][-1].update(state=state, basis="inferred" if state != "Unclear" else "not_stated",
                                         references=["public-feature-observation"] if state != "Unclear" else [])
            checked = validate_results("E", payload, {"results": [value]})
            self.assertIn("product-one", checked["accepted"])
            self.assertTrue(evaluate_fit(checked["accepted"]["product-one"]["criteria"], FIT_POLICY_ID)["eligible"])
        self.assertEqual(payload["items"][0]["description"], {"function": "Drain wet kitchen utensils", "mechanism": "Drain holes"})
        value["criteria"][-1].update(state="Meets", basis="inferred", references=["public-feature-observation"])
        self.assertEqual(validate_results("E", payload, {"results": [value]})["failures"]["product-one"],
                         "AI_FIT_AUDIENCE_CONTRADICTION")

    def test_partial_review_without_audience_does_not_invent_an_audience_decision(self):
        payload, value = fit_payload(ids=[7]), fit_result(ids=[7])
        self.assertIn("product-one", validate_results("E", payload, {"results": [value]})["accepted"])
        value["audience_positioning"]["positioning"] = "women_centric"
        self.assertEqual(validate_results("E", payload, {"results": [value]})["failures"]["product-one"],
                         "AI_FIT_AUDIENCE_NOT_REQUESTED")

    def test_marketplace_relevance_keeps_its_old_version_and_empty_criteria(self):
        payload = {"mode": "marketplace_relevance_only", "criteria": [], "items": fit_payload()["items"]}
        response = fit_result(); response.pop("audience_positioning"); response["criteria"] = []
        self.assertEqual(prompt_version("E", payload), PROMPT_VERSION)
        self.assertIs(schema_for("E", payload), SCHEMAS["E"])
        checked = validate_results("E", payload, {"results": [response]}, declared_version=PROMPT_VERSION)
        self.assertEqual(checked["accepted"]["product-one"]["criteria"], [])
        self.assertEqual(checked["accepted"]["product-one"]["relevance"][0]["relationship"], "unclear")

    def test_legacy_full_and_repair_preserve_thirteen_original_rows(self):
        for payload in ({"items": fit_payload()["items"]},
                        {"items": fit_payload()["items"], "checklist_repair": {"scope": "existing-repair"}}):
            response = fit_result(); response.pop("audience_positioning"); response["criteria"] = []
            checked = validate_results("E", payload, {"results": [response]}, declared_version=PROMPT_VERSION)
            rows = checked["accepted"]["product-one"]["criteria"]
            self.assertEqual([row["id"] for row in rows], list(range(1, 14)))
            self.assertEqual(rows[2]["state"], "Not assessed")

    def test_unknown_or_contradictory_contract_is_refused_before_dispatch(self):
        cases = [(dict(fit_payload(), mode="invented-mode"), None),
                 (dict(fit_payload(), fit_policy_id="invented-policy"), None),
                 (fit_payload(), PROMPT_VERSION),
                 ({"items": fit_payload()["items"]}, FIT_PROMPT_VERSION),
                 (dict(fit_payload(), requested_criterion_ids=[1, 2, 3]), None),
                 (dict(fit_payload(), requested_criterion_ids=list(ACTIVE_CRITERION_IDS)[::-1]), None),
                 ({"items": fit_payload()["items"], "fit_policy_id": None}, None)]
        runner = FitRunner()
        for payload, version in cases:
            with self.subTest(payload=payload, version=version):
                with self.assertRaises(SafetyError):
                    Jobs(self.memory, runner).run("E", payload, declared_version=version)
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.memory.snapshot()["jobs"], {})

    def test_malformed_job_inputs_never_create_an_intent_or_dispatch(self):
        runner = FitRunner()
        for items in ([None], [{}], [{"id": []}], [{"id": "non-json", "opaque": object()}]):
            with self.subTest(items=items), self.assertRaisesRegex(SafetyError, "AI_JOB_INPUT_INVALID"):
                Jobs(self.memory, runner).run("E", dict(fit_payload(), items=items))
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.memory.snapshot()["jobs"], {})

    def test_completed_fit_replays_across_chunk_sizes_with_no_new_calls(self):
        runner = FitRunner()
        payload = fit_payload(item_ids=("one", "two"))
        original = Jobs(self.memory, runner).run("E", payload)
        self.assertEqual(len(runner.calls), 1)
        replay = Jobs(self.memory, runner).run("E", dict(payload, items=payload["items"][:1]))
        self.assertEqual(set(replay[1]["accepted"]), {"one"})
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.memory.snapshot()["jobs"][original[0]]["request"]["prompt_version"], FIT_PROMPT_VERSION)
        self.assertEqual(Jobs(self.memory, runner).run("E", payload), original)

    def test_relevance_job_identity_and_saved_response_survive_fit_interpretation(self):
        current = fit_payload()
        relevance = {"mode": "marketplace_relevance_only", "criteria": [], "items": current["items"]}
        class Runner(FitRunner):
            def __call__(self, role, payload, folder):
                if payload["mode"] == "marketplace_relevance_only":
                    self.calls.append(deepcopy(payload))
                    row = fit_result(); row.pop("audience_positioning"); row["criteria"] = []
                    return {"results": [row]}, None
                return super().__call__(role, payload, folder)
        runner = Runner()
        first = Jobs(self.memory, runner).run("E", relevance)
        saved = (self.memory.root / "ai_jobs" / first[0] / "result.json").read_bytes()
        expected_request = {"role": "E", "payload": relevance, "model": MODEL, "prompt_version": PROMPT_VERSION,
                            "runner": runner.identity}
        self.assertEqual(first[0], key("job", expected_request))
        Jobs(self.memory, runner).run("E", current)
        self.assertEqual(Jobs(self.memory, runner).run("E", relevance), first)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual((self.memory.root / "ai_jobs" / first[0] / "result.json").read_bytes(), saved)

    def test_malformed_runner_return_retains_exact_intent_and_does_not_stop_other_work(self):
        class Runner(FitRunner):
            def __call__(self, role, payload, folder):
                if payload["items"][0]["id"] == "broken":
                    self.calls.append(deepcopy(payload)); return None
                return super().__call__(role, payload, folder)
        runner = Runner()
        broken = fit_payload(item_ids=("broken",))
        with self.assertRaisesRegex(SafetyError, "AI_RUNNER_RESULT_INVALID"):
            Jobs(self.memory, runner).run("E", broken)
        job_id = next(iter(self.memory.snapshot()["jobs"]))
        folder = self.memory.root / "ai_jobs" / job_id
        intent = (folder / "intent.json").read_bytes()
        failure = load_json(folder / "failure.json")
        self.assertEqual(failure["execution_certainty"], "runner_returned")
        self.assertIsNone(failure["usage"])
        with self.assertRaisesRegex(SafetyError, "AI_JOB_PENDING_RECONCILIATION"):
            Jobs(self.memory, runner).run("E", broken)
        with self.assertRaisesRegex(SafetyError, "AI_RETRY_RECONCILIATION_REQUIRED"):
            prepare_retry(self.memory, job_id, "A malformed return is not permission to repeat an unknown attempt.")
        healthy = Jobs(self.memory, runner).run("E", fit_payload(item_ids=("healthy",)))
        self.assertIn("healthy", healthy[1]["accepted"])
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual((folder / "intent.json").read_bytes(), intent)
        self.assertFalse((folder / "result.json").exists())

    def test_malformed_raised_runner_exception_and_non_json_return_are_controlled(self):
        for item_id, value in (("type-error", TypeError("private callback diagnostic")),
                               ("value-error", ValueError("private callback diagnostic")),
                               ("object-response", ({"results": [object()]}, None)),
                               ("nonfinite-usage", ({"results": []}, {"amount": float("nan")}))):
            def runner(role, payload, folder):
                if isinstance(value, Exception): raise value
                return value
            with self.subTest(item_id=item_id), self.assertRaisesRegex(SafetyError, "AI_RUNNER_RESULT_INVALID"):
                Jobs(self.memory, runner).run("E", fit_payload(item_ids=(item_id,)))
        for folder in (self.memory.root / "ai_jobs").iterdir():
            failure = load_json(folder / "failure.json")
            self.assertNotIn("private", canonical(failure).decode())
            self.assertIsNone(failure["usage"])

    def test_canonical_memory_write_error_is_not_classified_as_runner_failure(self):
        runner = FitRunner()
        with patch.object(self.memory, "put", side_effect=SafetyError("RESEARCH_CHAIN_CORRUPTED")):
            with self.assertRaisesRegex(SafetyError, "RESEARCH_CHAIN_CORRUPTED"):
                Jobs(self.memory, runner).run("E", fit_payload())
        self.assertEqual(runner.calls, [])
        self.assertFalse((self.memory.root / "ai_jobs").exists())

    def test_result_metadata_cannot_push_saved_artifact_past_read_limit(self):
        from product_radar.research_ai import MAX_JOB_BYTES
        def runner(role, payload, folder):
            return {"results": [], "padding": "x" * (MAX_JOB_BYTES - 100)}, None
        with self.assertRaisesRegex(SafetyError, "AI_OUTPUT_SIZE_LIMIT"):
            Jobs(self.memory, runner).run("E", fit_payload())
        job_id = next(iter(self.memory.snapshot()["jobs"]))
        self.assertFalse((self.memory.root / "ai_jobs" / job_id / "result.json").exists())

    def test_quota_exhaustion_and_request_throttling_are_not_the_same_diagnosis(self):
        for message in (b"usage limit reached", b"quota exceeded", b"insufficient_quota"):
            self.assertEqual(classify_failure(message), "AI_SUBSCRIPTION_LIMIT")
        for message in (b"rate limit exceeded", b"rate_limit_exceeded", b"too many requests", b"http 429"):
            self.assertEqual(classify_failure(message), "AI_RATE_LIMITED")
        self.assertEqual(classify_failure(b"unrecognised provider message"), "AI_PROVIDER_REQUEST_FAILED")
        runner = FitRunner()
        job_id, _ = Jobs(self.memory, runner).run("E", fit_payload())
        # A completed response still wins: a later label cannot authorise a
        # retry merely because the classified cause is transient.
        folder = self.memory.root / "ai_jobs" / job_id
        atomic_json(folder / "failure.json", {"code": "AI_RATE_LIMITED", "usage": None})
        with self.assertRaisesRegex(SafetyError, "AI_COMPLETED_RESULT_REUSE_REQUIRED"):
            prepare_retry(self.memory, job_id, "Do not repeat already completed work.")

    def test_fit_binding_does_not_let_old_rejected_work_block_a_new_policy(self):
        runner = FitRunner()
        current = fit_payload()
        previous = {"items": current["items"]}
        old_binding = Jobs(self.memory, runner).blocked_items("E", previous, set())
        self.assertEqual(old_binding, set())
        # A pending current exact job still blocks itself, not a relevance or
        # old full-checklist interpretation of the same product description.
        context = {k: v for k, v in current.items() if k != "items"}
        from product_radar.models import digest
        binding = digest(["E", current["items"][0], context, MODEL, FIT_PROMPT_VERSION, runner.identity])
        self.assertEqual(Jobs(self.memory, runner).blocked_items("E", current, {binding}), {"product-one"})
        self.assertEqual(Jobs(self.memory, runner).blocked_items("E", previous, {binding}), set())

    def test_g_v1_v2_and_v3_schema_selection_is_explicit(self):
        for version in ("research-g-v1", "research-g-v2"):
            self.assertIs(schema_for("G", {}, version), SCHEMAS["G"])
            self.assertEqual(prompt_version("G", {}, version), version)
        payload = {"fit_policy_id": FIT_POLICY_ID, "criterion_ids": list(ACTIVE_CRITERION_IDS)}
        self.assertIs(schema_for("G", payload), FIT_G_SCHEMA)
        self.assertEqual(prompt_version("G", payload), FIT_NARRATION_PROMPT_VERSION)
        self.assertNotIn("checklist_phrases", SCHEMAS["G"]["properties"]["results"]["items"]["properties"])
        for wrong in ("research-g-v1", "research-g-v2", "not-a-version"):
            with self.assertRaises(SafetyError):
                g_contract(payload, wrong)
        with self.assertRaises(SafetyError):
            g_contract({}, FIT_NARRATION_PROMPT_VERSION)
        with self.assertRaises(SafetyError):
            g_contract({"fit_policy_id": None})

    def test_malformed_g_item_context_does_not_stop_healthy_results(self):
        item = {"id": "healthy", "kind": "product", "issue_id": "fixture", "language": "en",
                "facts": [{"id": "feature"}], "required_limitation": "", "update_fact": ""}
        value = {"id": "healthy", "issue_id": "fixture", "language": "en", "title": "",
                 "reason": "{{title}} has a practical use.", "points": [], "limitation": "", "update_sentence": "",
                 "references": ["feature"], "checklist_phrases": [{"id": number, "text": "Supplied judgment."}
                                                                   for number in ACTIVE_CRITERION_IDS]}
        payload = {"fit_policy_id": FIT_POLICY_ID, "items": [item, dict(item, id="broken", facts=None)]}
        result = validate_results("G", payload, {"results": [value, dict(value, id="broken")]})
        self.assertEqual(set(result["accepted"]), {"healthy"})
        self.assertEqual(result["failures"], {"broken": "AI_REPORT_INPUT_INVALID"})

    def test_g_v3_product_and_title_phrase_slots_dispatch_the_declared_validator(self):
        item = {"id": "product-one", "kind": "product", "issue_id": "fixture-issue", "language": "en"}
        value = {"id": item["id"], "issue_id": item["issue_id"], "language": "en", "title": "", "reason": "{{title}} is useful.",
                 "points": [], "limitation": "", "update_sentence": "", "references": [],
                 "checklist_phrases": [{"id": number, "text": "Supplied practical reason."} for number in ACTIVE_CRITERION_IDS]}
        payload = {"fit_policy_id": FIT_POLICY_ID, "items": [item]}
        with patch("product_radar.report_narration.validate_narration_result", side_effect=lambda item, value, **kw: value) as check:
            result = validate_results("G", payload, {"results": [value]})
            self.assertIn(item["id"], result["accepted"])
            self.assertEqual(check.call_args.kwargs, {"declared_version": FIT_NARRATION_PROMPT_VERSION})
            for phrases in (value["checklist_phrases"][:-1], value["checklist_phrases"] + [{"id": 3, "text": "Retired"}],
                            [*value["checklist_phrases"][:-1], value["checklist_phrases"][0]]):
                bad = dict(value, checklist_phrases=phrases)
                self.assertEqual(validate_results("G", payload, {"results": [bad]})["failures"][item["id"]],
                                 "AI_REPORT_CHECKLIST_IDS_INVALID")
            unordered = dict(value, checklist_phrases=list(reversed(value["checklist_phrases"])))
            normalized = validate_results("G", payload, {"results": [unordered]})["accepted"][item["id"]]
            self.assertEqual([row["id"] for row in normalized["checklist_phrases"]], list(ACTIVE_CRITERION_IDS))
            title_item = dict(item, kind="report_title")
            title = dict(value, title="Product picks", reason="", checklist_phrases=[])
            self.assertIn(item["id"], validate_results("G", dict(payload, items=[title_item]), {"results": [title]})["accepted"])

    def test_intent_contract_not_today_default_binds_g_v1_execution(self):
        payload = {"items": [{"id": "title", "kind": "report_title"}]}
        request = {"role": "G", "payload": payload, "model": MODEL, "prompt_version": "research-g-v1", "runner": "fixture"}
        folder = self.memory.root / "ai_jobs" / key("job", request)
        atomic_json(folder / "intent.json", {"request": request, "created_at": "2026-10-07T00:00:00+00:00"})
        with patch("product_radar.research_ai.NARRATION_PROMPT_VERSION", "research-g-v2"):
            binding = _execution_binding("G", payload, folder, "fixture")
        self.assertEqual(binding["job_id"], key("job", request))
        with self.assertRaisesRegex(SafetyError, "AI_EXECUTION_INTENT_MISMATCH"):
            _execution_binding("G", dict(payload, language="different"), folder, "fixture")

    def test_real_runner_wrapper_selects_new_e_schema_and_prompt_without_provider(self):
        response = {"results": [fit_result()]}
        events = b"\n".join(canonical(event) for event in [
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": canonical(response).decode()}},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}}]) + b"\n"
        script = "import sys;sys.stdin.buffer.read();sys.stdout.buffer.write(" + repr(events) + ")"
        captured, real_popen = [], subprocess.Popen
        def launch(command, **kwargs):
            self.assertEqual(command[0], "/fixture/codex")
            schema = load_json(Path(command[command.index("--output-schema") + 1]))
            captured.append(schema)
            return real_popen([sys.executable, "-c", script], **kwargs)
        login = subprocess.CompletedProcess([], 0, b"Logged in using ChatGPT", b"")
        runner = CodexRunner({"model": MODEL, "reasoning_effort": "high", "authentication": "chatgpt", "paid_fallback": False})
        with patch("product_radar.research_ai.shutil.which", return_value="/fixture/codex"), \
                patch("product_radar.research_ai.subprocess.run", return_value=login), \
                patch("product_radar.research_ai.subprocess.Popen", side_effect=launch):
            job_id, result = Jobs(self.memory, runner).run("E", fit_payload())
        self.assertIn("product-one", result["accepted"])
        self.assertEqual(captured, [FIT_E_SCHEMA])
        self.assertEqual(load_json(self.memory.root / "ai_jobs" / job_id / "intent.json")["request"]["prompt_version"], FIT_PROMPT_VERSION)
        self.assertIn("ordinary task", instructions_for("E", fit_payload()))
        self.assertIn("Separate buyer from end user", instructions_for("E", fit_payload()))
        self.assertIs(instructions_for("E", {}), INSTRUCTIONS["E"])


if __name__ == "__main__":
    unittest.main()
