"""Synthetic joined C contracts: no browser, provider or real model calls."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar.functional_types import (accept_functional_definition, effective_functional_definition,
    functional_role_context)
from product_radar.models import SafetyError, digest
from product_radar.product_queries import (FUNCTIONAL_CONTRACT, FUNCTIONAL_PROMPT_VERSION, FUNCTIONAL_VERSION,
    MODE, SOURCES, current_query_profile, ensure_query_profiles, product_query_terms, query_profile_input,
    query_profile_scope_hash, resolve_query_profile, validate_query_input, validate_query_profile)
from product_radar.programme import _current_profiles
from product_radar.research_ai import Jobs, c_contract, validate_results
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal
from test_catalogue_expansion import fixture as catalogue_fixture
from test_research_flow import FakeRunner, NOW, capture, run_flow


LATER = "2026-10-06T10:00:00+00:00"
DEFINITION = {"report_title": "Draining sink caddy", "primary_use": "Store wet sink essentials",
    "mechanism": "Passive draining tray", "use_context": ["Kitchen sink"],
    "compatibility_constraints": [], "included_distinctions": ["Draining base"],
    "excluded_distinctions": ["Powered cleaning brush"]}


def answer(item, *, example=False):
    result = {"id": item["id"], "core_type": "sink organiser", "subtype": "draining sink caddy",
        "aliases": ["draining sink holder"], "mechanism_anchors": ["passive draining base"],
        "broad_context": ["sink organiser"],
        "queries": [{"source": source, "text": phrase, "scope": scope, "reason": "Synthetic scope judgment."}
                    for source in SOURCES for phrase, scope in (("draining sink caddy", "subtype"),
                                                                 ("sink organiser", "context"))],
        "references": [item["observations"][0]["id"] if example else item["description_ref"]],
        "reason": "Synthetic wording; not market proof."}
    if "meaning_hash" in item:
        result["meaning_hash"] = item["meaning_hash"]
    return result


class Runner:
    identity = "synthetic-functional-query-v1"

    def __init__(self, *, example=False, action=None):
        self.calls, self.example, self.action = [], example, action

    def __call__(self, role, payload, folder):
        self.calls.append((role, deepcopy(payload)))
        result = {"results": [answer(item, example=self.example) for item in payload["items"]]}
        if self.action is not None:
            action, self.action = self.action, None
            action()
        return result, {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 10}


class FunctionalQueryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        capture(self.memory)
        run_flow(self.memory, FakeRunner(), as_of=NOW)
        state = self.memory.snapshot()
        self.cid = next(cid for cid, concept in state["concepts"].items()
                        if concept["description"]["label"] == "Sink caddy")
        self.children = [child_id for child_id, row in state["associations"].items()
                         if row["concept_id"] == self.cid]
        semantic_ids = {state["associations"][child_id]["semantic_id"] for child_id in self.children}
        self.refs = [obs_id for obs_id, row in state["observations"].items() if row["semantic_id"] in semantic_ids]
        self.catalogue = catalogue_fixture()

    def accept(self, definition=None, *, decisions=True, refs=None, at=NOW):
        state = self.memory.snapshot()
        reviewed = ({child_id: {"decision": "same", "reason": "Synthetic explicit membership review."}
                     for child_id in self.children} if decisions is True else decisions)
        return accept_functional_definition(self.memory, self.cid, definition or DEFINITION,
            basis_refs=refs or self.refs, reviewer="synthetic", reviewed_at=at,
            expected_meaning_hash=effective_functional_definition(state, self.cid)["meaning_hash"],
            example_decisions=reviewed)

    def prepare(self, runner=None, **kwargs):
        return ensure_query_profiles(self.memory, runner, concept_ids=[self.cid], catalogue=self.catalogue,
                                     as_of=NOW, **kwargs)

    def test_legacy_input_prompt_and_local_fallback_keep_original_shape(self):
        state = self.memory.snapshot()
        item = query_profile_input(state, self.cid)
        self.assertEqual(set(item), {"id", "description", "description_ref", "observations"})
        self.assertEqual(item["description"], state["concepts"][self.cid]["description"])
        self.assertEqual(query_profile_scope_hash(state, self.cid), digest(item["description"]))
        self.assertEqual(c_contract({"mode": MODE, "items": [item]})["version"], "research-c-query-profile-v2")
        runner = Runner()
        self.assertFalse(self.prepare(runner)["pending"])
        self.assertEqual(runner.calls, [])

    def test_accepted_input_is_meaning_only_and_actual_example_proof_stays_outside_job(self):
        self.accept()
        state = self.memory.snapshot()
        context = functional_role_context(state, self.cid)
        item = query_profile_input(state, self.cid)
        self.assertEqual(item["functional_contract"], FUNCTIONAL_CONTRACT)
        self.assertNotIn("report_title", item["description"])
        self.assertNotIn("proof", item)
        self.assertNotIn("label", item["description"])
        self.assertTrue(context["proof"]["definition_id"])
        self.assertTrue(context["proof"]["examples"])
        for row in item["observations"]:
            self.assertEqual(set(row["source_text"]), {"function", "mechanism", "features", "audience"})
            self.assertNotIn("label", row["source_text"])
        runner = Runner()
        result = self.prepare(runner)
        self.assertEqual(result["pending"], [])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][1]["query_contract"], FUNCTIONAL_CONTRACT)
        record = result["records"][0]
        self.assertEqual(record["version"], FUNCTIONAL_VERSION)
        self.assertEqual(self.memory.snapshot()["jobs"][record["job_id"]]["request"]["prompt_version"], FUNCTIONAL_PROMPT_VERSION)
        self.assertEqual(record["proof_hash"], digest(record["proof"]))
        self.assertEqual(record["meaning_hash"], item["meaning_hash"])

    def test_accepted_meaning_change_wakes_cooperative_programme_before_any_profile(self):
        from product_radar.programme import _cooperative_signature, prepare_programme
        record = prepare_programme(self.memory, "synthetic-before-profile-wake", concept_ids=[self.cid], as_of=NOW)
        self.accept(); before = self.memory.snapshot()
        self.assertIsNone(current_query_profile(before, self.cid))
        first = _cooperative_signature(before, record)
        self.accept(dict(DEFINITION, report_title="Presentation only"), at=LATER)
        self.assertEqual(_cooperative_signature(self.memory.snapshot(), record), first)
        self.accept(dict(DEFINITION, mechanism="Wall mounted draining tray"), at=LATER)
        self.assertNotEqual(_cooperative_signature(self.memory.snapshot(), record), first)

    def test_functional_batch_byte_limit_does_not_block_healthy_siblings(self):
        from product_radar.models import canonical
        from product_radar.product_queries import _payload
        state = self.memory.snapshot(); ids = sorted(state["concepts"])
        for cid in ids:
            children = [child for child, row in state["associations"].items() if row["concept_id"] == cid]
            semantics = {state["associations"][child]["semantic_id"] for child in children}
            refs = [oid for oid, row in state["observations"].items() if row["semantic_id"] in semantics]
            accept_functional_definition(self.memory, cid, dict(DEFINITION, primary_use="Synthetic useful function. " * 60),
                basis_refs=refs, reviewer="synthetic", reviewed_at=NOW,
                expected_meaning_hash=effective_functional_definition(self.memory.snapshot(), cid)["meaning_hash"],
                example_decisions={child: {"decision": "same", "reason": "Synthetic membership."} for child in children})
        state = self.memory.snapshot(); items = [query_profile_input(state, cid) for cid in ids]
        ceiling = (max(len(canonical(_payload([item]))) for item in items) + len(canonical(_payload(items)))) // 2
        runner = Runner()
        with patch("product_radar.research_ai.MAX_JOB_BYTES", ceiling):
            result = ensure_query_profiles(self.memory, runner, concept_ids=ids, catalogue=self.catalogue, as_of=NOW)
        self.assertEqual(result["pending"], [], result)
        self.assertEqual(len(runner.calls), len(ids))

    def test_title_citation_and_review_date_change_keep_profile_and_zero_new_model_calls(self):
        self.accept(refs=[self.refs[0]])
        runner = Runner(); self.prepare(runner)
        before = self.memory.snapshot()
        original_item = query_profile_input(before, self.cid)
        original_job = deepcopy(before["jobs"])
        changed = dict(DEFINITION, report_title="An orderly kitchen sink")
        self.accept(changed, refs=self.refs, at=LATER)
        after = self.memory.snapshot()
        self.assertEqual(query_profile_input(after, self.cid), original_item)
        self.assertEqual(query_profile_scope_hash(after, self.cid), query_profile_scope_hash(before, self.cid))
        self.assertIsNotNone(current_query_profile(after, self.cid))
        self.assertEqual(self.prepare(runner)["reused"], [self.cid])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.memory.snapshot()["jobs"], original_job)

    def test_new_date_price_and_photo_facts_do_not_change_search_semantics(self):
        self.accept(); runner = Runner(); self.prepare(runner)
        before = self.memory.snapshot()
        original = query_profile_input(before, self.cid)
        capture(self.memory, acquisition="later-capture", observed=LATER)
        current = self.memory.snapshot()
        self.assertEqual(query_profile_input(current, self.cid), original)
        changed = deepcopy(current)
        for ref in self.refs:
            changed["observations"][ref]["facts"].update(price="999", image_url="https://example.com/new.jpg")
        self.assertEqual(query_profile_input(changed, self.cid), original)
        self.assertIsNotNone(current_query_profile(changed, self.cid))
        self.prepare(runner)
        self.assertEqual(len(runner.calls), 1)

    def test_changed_mechanism_invalidates_only_affected_profile_and_keeps_original_jobs(self):
        self.accept(); runner = Runner(); self.prepare(runner)
        before = self.memory.snapshot()
        self.accept(dict(DEFINITION, mechanism="Wall mounted draining tray"), at=LATER)
        state = self.memory.snapshot()
        self.assertIsNone(current_query_profile(state, self.cid))
        self.assertIsNone(resolve_query_profile(state, self.cid, catalogue=self.catalogue))
        result = self.prepare(runner)
        self.assertEqual(result["pending"], [])
        self.assertEqual(len(runner.calls), 2)
        for job_id, original in before["jobs"].items():
            self.assertEqual(self.memory.snapshot()["jobs"][job_id], original)

    def test_new_definition_does_not_reuse_legacy_short_label_or_profile(self):
        self.prepare()
        legacy = current_query_profile(self.memory.snapshot(), self.cid)
        self.assertIsNotNone(legacy)
        self.accept()
        state = self.memory.snapshot()
        self.assertIsNone(current_query_profile(state, self.cid))
        self.assertIsNone(resolve_query_profile(state, self.cid, catalogue=self.catalogue))
        self.assertEqual(self.prepare()["pending"][0]["reason"], "PRODUCT_QUERY_PROFILE_REQUIRED")
        with self.assertRaisesRegex(SafetyError, "PRODUCT_QUERY_PROFILE_REQUIRED"):
            product_query_terms(self.memory, self.cid, source="trends", catalogue=self.catalogue)

    def test_unreviewed_examples_are_absent_without_blocking_definition_only_wording(self):
        self.accept(decisions=None)
        context = functional_role_context(self.memory.snapshot(), self.cid)
        self.assertEqual(context["examples"], [])
        self.assertEqual(set(context["proof"]["pending_child_ids"]), set(self.children))
        runner = Runner()
        self.assertEqual(self.prepare(runner)["pending"], [])
        self.assertEqual(runner.calls[0][1]["items"][0]["observations"], [])

    def test_only_explicit_same_examples_enter_input(self):
        decisions = {self.children[0]: {"decision": "same", "reason": "Synthetic same type."},
                     self.children[1]: {"decision": "unclear", "reason": "Synthetic unresolved mechanism."}}
        self.accept(decisions=decisions)
        context = functional_role_context(self.memory.snapshot(), self.cid)
        bound = [row["child_id"] for rows in context["proof"]["examples"].values() for row in rows]
        self.assertEqual(bound, [self.children[0]])
        self.assertEqual(context["proof"]["pending_child_ids"], [self.children[1]])

    def test_used_example_is_current_dependency_but_unused_example_is_not(self):
        self.accept(decisions={self.children[0]: {"decision": "same", "reason": "Synthetic same type."}})
        self.prepare(Runner(example=True))
        old = self.memory.snapshot()["associations"][self.children[0]]
        self.memory.put("associations", self.children[0], dict(old, status="unclear"), previous=old)
        self.assertIsNone(current_query_profile(self.memory.snapshot(), self.cid))
        # A new definition-only answer can use the current accepted boundary;
        # the original example-bound answer is not relabelled or overwritten.
        self.assertEqual(self.prepare(Runner())["pending"], [])
        old = self.memory.snapshot()["associations"][self.children[1]]
        self.memory.put("associations", self.children[1], dict(old, status="unclear"), previous=old)
        self.assertIsNotNone(current_query_profile(self.memory.snapshot(), self.cid))

    def test_narrower_meaning_during_model_wait_cannot_commit_stale_profile(self):
        self.accept()
        runner = Runner(action=lambda: self.accept(dict(DEFINITION, mechanism="Wall mounted draining tray"), at=LATER))
        result = self.prepare(runner)
        self.assertEqual(result["pending"][0]["reason"], "PRODUCT_QUERY_DESCRIPTION_CHANGED")
        self.assertIsNone(current_query_profile(self.memory.snapshot(), self.cid))
        self.assertEqual(self.prepare(runner)["pending"], [])
        self.assertEqual(len(runner.calls), 2)

    def test_scope_hash_and_programme_currentness_use_same_definition(self):
        self.accept(); self.prepare(Runner())
        state = self.memory.snapshot()
        self.assertIn(self.cid, _current_profiles(state))
        changed = deepcopy(state)
        changed["concepts"][self.cid]["description"]["label"] = "A different sales title"
        self.assertEqual(query_profile_scope_hash(state, self.cid), query_profile_scope_hash(changed, self.cid))
        self.assertIn(self.cid, _current_profiles(changed))
        self.accept(dict(DEFINITION, mechanism="Wall mounted draining tray"), at=LATER)
        self.assertNotIn(self.cid, _current_profiles(self.memory.snapshot()))

    def test_mixed_contract_payloads_and_declared_prompt_swaps_are_rejected(self):
        old = query_profile_input(self.memory.snapshot(), self.cid)
        self.accept(); new = query_profile_input(self.memory.snapshot(), self.cid)
        for payload in ({"mode": MODE, "items": [new]},
                        {"mode": MODE, "query_contract": FUNCTIONAL_CONTRACT, "items": [old]}):
            with self.assertRaisesRegex(SafetyError, "PRODUCT_QUERY_INPUT_INVALID"):
                validate_query_input(payload)
        with self.assertRaisesRegex(SafetyError, "AI_C_CONTRACT_INVALID"):
            c_contract({"mode": MODE, "items": [old]}, FUNCTIONAL_PROMPT_VERSION)
        with self.assertRaisesRegex(SafetyError, "AI_C_CONTRACT_INVALID"):
            c_contract({"mode": MODE, "query_contract": FUNCTIONAL_CONTRACT, "items": [new]}, "research-c-query-profile-v2")

    def test_wrong_meaning_and_broad_context_as_narrow_query_are_not_repaired_into_proof(self):
        self.accept(); item = query_profile_input(self.memory.snapshot(), self.cid)
        result = answer(item); result["meaning_hash"] = "0" * 64
        with self.assertRaisesRegex(SafetyError, "PRODUCT_QUERY_PROFILE_INVALID"):
            validate_query_profile(item, result)
        result = answer(item)
        result["queries"][0].update(text="sink organiser", scope="family")
        with self.assertRaisesRegex(SafetyError, "PRODUCT_QUERY_CONTEXT_INVALID"):
            validate_query_profile(item, result)
        payload = {"mode": MODE, "query_contract": FUNCTIONAL_CONTRACT, "items": [item]}
        del result["meaning_hash"]
        self.assertIn(self.cid, validate_results("C", payload, {"results": [result]})["failures"])

    def test_new_c_does_not_modify_original_assessments_or_brand_descriptions(self):
        before = self.memory.snapshot()
        self.accept(); self.prepare(Runner())
        after = self.memory.snapshot()
        for table in ("concepts", "associations", "descriptions", "assessments", "brand_reviews", "published_history"):
            self.assertEqual(after[table], before[table])

    def test_lost_profile_commit_recovers_exact_original_answer_without_new_model_call(self):
        self.accept(); runner = Runner()
        from product_radar.product_queries import _record
        with patch("product_radar.product_queries._record", side_effect=OSError("Synthetic storage interruption")):
            first = self.prepare(runner)
        self.assertEqual(first["pending"][0]["reason"], "PRODUCT_QUERY_LOCAL_IO_FAILED")
        original = self.memory.snapshot()["jobs"][first["job_ids"][0]]["request"]
        second = self.prepare(runner)
        self.assertEqual(second["pending"], [])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.memory.snapshot()["jobs"][second["job_ids"][0]]["request"], original)

    def test_uncertain_original_intent_is_not_relaunched(self):
        self.accept(); item = query_profile_input(self.memory.snapshot(), self.cid)
        class Interrupted:
            identity = Runner.identity
            def __call__(self, role, payload, folder):
                raise TimeoutError("Synthetic missing original answer")
        payload = {"mode": MODE, "query_contract": FUNCTIONAL_CONTRACT, "items": [item]}
        with self.assertRaisesRegex(SafetyError, "AI_LOCAL_EXECUTION_FAILED"):
            Jobs(self.memory, Interrupted()).run("C", payload)
        runner = Runner(); result = self.prepare(runner)
        self.assertEqual(result["pending"][0]["reason"], "AI_JOB_PENDING_RECONCILIATION")
        self.assertEqual(runner.calls, [])

    def test_corrupt_accepted_profile_is_not_used_by_any_reader(self):
        self.accept(); result = self.prepare(Runner())
        state = self.memory.snapshot()
        state["source_checks"][result["records"][0]["reference"]]["profile"]["core_type"] = "Invented type"
        self.assertIsNone(current_query_profile(state, self.cid))
        self.assertIsNone(resolve_query_profile(state, self.cid, catalogue=self.catalogue))
        self.assertNotIn(self.cid, _current_profiles(state))

    def test_connected_source_builder_uses_only_reviewed_narrow_queries(self):
        from product_radar.source_coverage import product_source_request_builder
        self.accept(); self.prepare(Runner())
        state = self.memory.snapshot()
        description = state["concepts"][self.cid]["description"]
        for route in ("meta-ads", "google-trends", "amazon-search", "google-maps"):
            context = {"concept_id": self.cid, "state": state, "route": route,
                       "description": description, "description_hash": digest(description)}
            request = product_source_request_builder(catalogue=self.catalogue)(context)
            self.assertEqual(request.get("query", request.get("term")), "draining sink caddy")
            self.assertEqual(context["query_plan"]["term_count"], 1)
            self.assertEqual(context["query_plan"]["scope"], "subtype")
        # The broad core and vocabulary alias never become automatic narrow
        # fallbacks merely because the first narrow search was sparse.
        self.assertEqual(current_query_profile(state, self.cid)["core_type"], "sink organiser")

    def test_mixed_legacy_and_functional_pending_items_use_distinct_exact_job_contracts(self):
        from test_product_queries import DESCRIPTION
        self.memory.put("concepts", "legacy-long-title", {"description": deepcopy(DESCRIPTION), "status": "provisional"})
        self.accept(); runner = Runner()
        result = ensure_query_profiles(self.memory, runner, concept_ids=[self.cid, "legacy-long-title"],
                                       catalogue=self.catalogue, as_of=NOW)
        self.assertEqual(result["pending"], [])
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual({payload.get("query_contract") for _, payload in runner.calls}, {None, FUNCTIONAL_CONTRACT})
        for _, payload in runner.calls:
            self.assertEqual(len(payload["items"]), 1)
            validate_query_input(payload)

    def test_equivalent_examples_are_one_semantic_projection_not_extra_market_sources(self):
        self.accept()
        context = functional_role_context(self.memory.snapshot(), self.cid)
        self.assertEqual(len(context["examples"]), 1)
        self.assertEqual(len(next(iter(context["proof"]["examples"].values()))), len(self.children))
        self.assertNotIn("source_count", context)
        self.assertNotIn("market_support", context)

    def test_functional_optional_shape_repair_keeps_exact_meaning_and_original_job(self):
        class OptionalMismatch(Runner):
            def __call__(self, role, payload, folder):
                result, usage = super().__call__(role, payload, folder)
                for item in result["results"]:
                    item["aliases"] = ["draining sink holder"] * 5
                return result, usage
        self.accept(); runner = OptionalMismatch()
        result = self.prepare(runner)
        self.assertEqual(result["pending"], [])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(result["records"][0]["method"], "model_contract_repair")
        self.assertEqual(result["records"][0]["profile"]["meaning_hash"],
                         effective_functional_definition(self.memory.snapshot(), self.cid)["meaning_hash"])
        self.assertEqual(self.prepare(runner)["reused"], [self.cid])
        self.assertEqual(len(runner.calls), 1)

