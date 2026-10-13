"""Offline proof for typed questions, exact reads and independent parsing."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.functional_types import accept_functional_definition, effective_functional_definition
from product_radar.marketplace_browser import build_marketplace_request
from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory, change, key
from product_radar.research_questions import (
    catalogue_direction_subject, parse_fingerprint, physical_request_fingerprint,
    prepare_research_questions, validate_question_record, validate_subject,
)
from product_radar.storage import Journal

NOW = "2026-10-09T10:00:00+00:00"
LATER = "2026-10-10T10:00:00+00:00"
CATEGORY = "Home and Kitchen"


def catalogue():
    return {"schema_version": 1, "revision": "test", "built_at": NOW,
            "sources": [{"id": "page", "url": "https://example.org/storage", "title": "Synthetic page",
                         "checked_at": NOW, "excerpts": ["Test storage only."]}],
            "branches": [{"id": "storage", "parent_id": None, "category": CATEGORY, "name": "Kitchen storage"}],
            "families": [{"id": "caddy", "parent_id": "storage", "category": CATEGORY, "label": "Sink caddy",
                          "definition": "Holds dishwashing tools beside the sink.", "problem": "Wet clutter around the sink.",
                          "included": ["Sink caddy"], "excluded": ["Pot holder"], "aliases": ["Sink organiser"], "overlaps": [],
                          "examples": [{"label": "Sink caddy", "basis": "observed", "sources": ["page"]}],
                          "basis": "observed", "sources": ["page"], "rationale": "Fixture research direction.",
                          "phrases": [{"text": "sink caddy", "source": "meta", "purpose": "discovery", "language": "en",
                                       "country": "IN", "reason": "Specific function search."}],
                          "season": {"kind": "unknown", "months": []}, "review_note": "Fixture only.",
                          "disposition": "usable", "author": "test", "reviewed_at": NOW, "next_review": "After search."}]}


class PhysicalRequestTests(unittest.TestCase):
    def setUp(self):
        self.request = {"url": "https://example.org/search?q=sink+caddy", "query": "sink caddy", "country": "IN"}

    def fingerprint(self, request=None, **kwargs):
        return physical_request_fingerprint("amazon-search", self.request if request is None else request, **kwargs)

    def test_only_documented_url_equivalents_normalise(self):
        request = {**self.request, "url": "https://EXAMPLE.org/search?q=sink+caddy#ignored"}
        self.assertEqual(self.fingerprint(), self.fingerprint(request))
        self.assertNotEqual(self.fingerprint(), self.fingerprint({**request, "url": "https://example.org/search?q=sink%20caddy"}))
        self.assertEqual(self.fingerprint(), self.fingerprint(dict(reversed(list(self.request.items())))))

    def test_every_actual_physical_scope_dimension_is_retained(self):
        variants = [
            {"url": "https://example.org/other?q=sink+caddy"}, {"query": "draining sink caddy"},
            {"country": "US"}, {"language": "hi"}, {"location": {"name": "Delhi", "lat": "28.6", "lon": "77.2"}},
            {"date": "2026-10-09"}, {"timeframe": "today 3-m"}, {"property": "youtube"},
            {"filters": {"active_status": "active"}}, {"sort": "newest"}, {"department": "kitchen/123"},
            {"node": "123"}, {"nodes": ["123", "456"]}, {"page": 2}, {"pages": 3}, {"depth": 2},
            {"pagination": {"cursor": "next-3"}}, {"limit": 50}, {"offset": 10}, {"cursor": "next-2"},
            {"options": {"material": "steel", "api_version": "v2"}}, {"headers": {"Accept-Language": "hi-IN"}},
            {"body": {"urls": [{"url": "https://example.org/list#literal-in-body"}]}}, {"transport": "browser"},
            {"urls": ["https://example.org/a", "https://example.org/b"]},
        ]
        initial = self.fingerprint()
        for extra in variants:
            with self.subTest(extra=extra):
                self.assertNotEqual(initial, self.fingerprint({**self.request, **extra}))
        self.assertNotEqual(initial, self.fingerprint(method="HEAD"))
        self.assertNotEqual(initial, self.fingerprint(method="POST"))
        self.assertNotEqual(initial, self.fingerprint(capture_options={"include_images": True}))
        self.assertNotEqual(initial, physical_request_fingerprint("flipkart-search", self.request))

    def test_list_order_and_distinguishing_numbers_are_not_removed(self):
        self.assertNotEqual(self.fingerprint({**self.request, "nodes": ["1", "2"]}),
                            self.fingerprint({**self.request, "nodes": ["2", "1"]}))
        self.assertNotEqual(self.fingerprint({**self.request, "query": "sink caddy 2 tier"}),
                            self.fingerprint({**self.request, "query": "sink caddy 3 tier"}))

    def test_current_marketplace_builder_contract_is_supported(self):
        first = build_marketplace_request("amazon-bestsellers", department="kitchen/123", limit=30)
        other = build_marketplace_request("amazon-bestsellers", department="kitchen/456", limit=30)
        self.assertNotEqual(physical_request_fingerprint("amazon-bestsellers", first),
                            physical_request_fingerprint("amazon-bestsellers", other))
        stripped = {field: value for field, value in first.items() if field not in ("schema_version", "route")}
        self.assertEqual(physical_request_fingerprint("amazon-bestsellers", first),
                         physical_request_fingerprint("amazon-bestsellers", stripped))

    def test_source_specific_real_options_are_preserved_not_whitelisted_away(self):
        left = {**self.request, "options": {"future_source_option": {"a": ["one", "two"]}}}
        right = {**self.request, "options": {"future_source_option": {"a": ["one", "three"]}}}
        self.assertNotEqual(self.fingerprint(left), self.fingerprint(right))

    def test_unknown_physical_and_consumer_fields_are_rejected(self):
        for name in ("typo_filter", "subject", "concept_id", "programme_id", "week", "prepared_at", "parser_version"):
            with self.subTest(name=name), self.assertRaises(SafetyError):
                self.fingerprint({**self.request, name: "not physical"})

    def test_invalid_parameters_and_credentials_are_rejected_without_values(self):
        values = [None, [], {}, {"query": ""}, {"query": True}, {"query": "x", "country": "india"},
                  {"query": "x", "pages": True}, {"query": "x", "limit": 0}, {"query": "x", "depth": -1},
                  {"query": "x", "filters": []}, {"query": "x", "headers": {"Authorization": "sensitive-value"}},
                  {"url": "https://example.org/?api_key=sensitive-value"}, {"url": "https://user:pass@example.org/"},
                  {"query": "x", "options": {"api_key": "sensitive-value"}}, {"query": "x", "options": {"x": float("nan")}},
                  {"query": "x", "location": {"lat": "91", "lon": "77"}}, {"query": "x", "location": {"lat": True}},
                  {"query": "x", "route": "google-trends"}, {"query": "x", "schema_version": True},
                  {"query": "x", "version": "unknown-builder"}, {"query": "x", "options": {1: "bad key"}}]
        for value in values:
            with self.subTest(value=value), self.assertRaises(SafetyError) as error:
                physical_request_fingerprint("amazon-search", value)
            self.assertNotIn("sensitive-value", str(error.exception))
        for method in ("get", "DELETE", {}, None):
            with self.subTest(method=method), self.assertRaises(SafetyError):
                self.fingerprint(method=method)

    def test_parser_changes_are_separate_from_acquisition(self):
        original = self.fingerprint()
        self.assertNotEqual(parse_fingerprint("a" * 64, "parser-v1"), parse_fingerprint("a" * 64, "parser-v2"))
        self.assertNotEqual(parse_fingerprint("a" * 64, {"version": "v1", "options": {"images": True}}),
                            parse_fingerprint("b" * 64, {"version": "v1", "options": {"images": True}}))
        self.assertEqual(original, self.fingerprint())
        for capture, parser in (("bad", "v1"), ("a" * 64, ""), ("a" * 64, {}), ("a" * 64, [])):
            with self.subTest(capture=capture, parser=parser), self.assertRaises(SafetyError):
                parse_fingerprint(capture, parser)


class QuestionPreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
        self.base = catalogue()
        self.memory.put("concepts", "caddy-product", {"status": "active", "description": {
            "label": "Draining sink caddy", "category": CATEGORY, "function": "Holds dishwashing tools beside the sink.",
            "mechanism": "Draining holder", "audience": "Households", "features": ["draining"],
            "basis": "observed", "brand": "generic_or_small", "reason": "Synthetic fixture."}})

    def tearDown(self):
        self.tmp.cleanup()

    def subject(self):
        effective = effective_functional_definition(self.memory.snapshot(), "caddy-product")
        return {"kind": "concept", "concept_id": "caddy-product", "definition_hash": effective["meaning_hash"]}

    def question(self, **changes):
        return {"subject": self.subject(), "route": "google-trends", "purpose": "validation",
                "request": {"query": "sink caddy", "country": "IN"}, **changes}

    def prepare(self, questions, **kwargs):
        return prepare_research_questions(self.memory, questions, as_of=kwargs.pop("as_of", NOW), catalogue=self.base, **kwargs)

    def events(self):
        return list((self.memory.root / "events").glob("*.json"))

    def test_direction_scope_reuses_one_view_only_for_identical_inputs(self):
        from product_radar.catalogue_expansion import effective_catalogue
        from product_radar.research_questions import _catalogue_direction_scope
        state = self.memory.snapshot()
        current = effective_catalogue(self.base, state)
        with patch("product_radar.catalogue_expansion.effective_catalogue", wraps=effective_catalogue) as expand:
            with _catalogue_direction_scope(state, self.base, current):
                for _ in range(6):
                    subject = catalogue_direction_subject(state, "caddy", catalogue=self.base)
                    self.assertEqual(validate_subject(state, subject, catalogue=self.base), subject)
                self.assertEqual(expand.call_count, 0)
                catalogue_direction_subject(deepcopy(state), "caddy", catalogue=self.base)
                catalogue_direction_subject(state, "caddy", catalogue=deepcopy(self.base))
                self.assertEqual(expand.call_count, 2)
            catalogue_direction_subject(state, "caddy", catalogue=self.base)
            self.assertEqual(expand.call_count, 3)

    def test_direction_scope_keeps_returned_fields_independent_and_validates_membership(self):
        from product_radar.catalogue_expansion import effective_catalogue
        from product_radar.research_questions import _catalogue_direction_scope
        state = self.memory.snapshot()
        current = effective_catalogue(self.base, state)
        before = deepcopy(current)
        with _catalogue_direction_scope(state, self.base, current):
            first = catalogue_direction_subject(state, "caddy", catalogue=self.base)
            first["scope"]["included"].append("Not an accepted field")
            second = catalogue_direction_subject(state, "caddy", catalogue=self.base)
            self.assertNotIn("Not an accepted field", second["scope"]["included"])
            with self.assertRaisesRegex(SafetyError, "RESEARCH_DIRECTION_NOT_ACCEPTED"):
                catalogue_direction_subject(state, "missing", catalogue=self.base)
        self.assertEqual(current, before)

    def test_direction_scope_restores_after_nested_failure(self):
        from product_radar.catalogue_expansion import effective_catalogue
        from product_radar.research_questions import _catalogue_direction_scope
        state = self.memory.snapshot()
        current = effective_catalogue(self.base, state)
        other = deepcopy(self.base)
        other["families"][0]["definition"] = "A different accepted fixture definition."
        with _catalogue_direction_scope(state, self.base, current):
            expected = catalogue_direction_subject(state, "caddy", catalogue=self.base)
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                with _catalogue_direction_scope(state, other, effective_catalogue(other, state)):
                    self.assertNotEqual(catalogue_direction_subject(state, "caddy", catalogue=other), expected)
                    raise RuntimeError("synthetic")
            self.assertEqual(catalogue_direction_subject(state, "caddy", catalogue=self.base), expected)
        with patch("product_radar.catalogue_expansion.effective_catalogue", wraps=effective_catalogue) as expand:
            self.assertEqual(catalogue_direction_subject(state, "caddy", catalogue=self.base), expected)
            self.assertEqual(expand.call_count, 1)

    def test_concept_and_direction_questions_share_read_not_identity(self):
        state = self.memory.snapshot()
        direction = catalogue_direction_subject(state, "caddy", catalogue=self.base)
        before = deepcopy(state)
        with patch("socket.getaddrinfo", side_effect=AssertionError("No network allowed")), \
                patch("subprocess.Popen", side_effect=AssertionError("No processes allowed")):
            result = self.prepare([self.question(), self.question(subject=direction, purpose="discovery")])
        self.assertEqual(result["created"], 2)
        left, right = result["records"]
        self.assertNotEqual(left["id"], right["id"])
        self.assertEqual(left["physical_request_fingerprint"], right["physical_request_fingerprint"])
        self.assertEqual(left["subject_basis"], "legacy_concept")
        self.assertNotIn("concept_id", right["subject"])
        self.assertFalse(result["collector_dispatched"])
        self.assertEqual(result["source_attempts_created"], 0)
        self.assertEqual(result["model_calls_started"], 0)
        after = self.memory.snapshot()
        for table in before:
            if table != "query_tasks":
                self.assertEqual(before[table], after[table], table)
        self.assertEqual(left["collection_channel"], "unknown")
        self.assertEqual(left["incremental_collection_fee"], "unknown")
        self.assertEqual(left["progress"], {"acquisition": "prepared", "interpretation": "not_started", "scheduling": "ready"})

    def test_repeat_later_week_does_not_create_event_or_reset_progress(self):
        first = self.prepare([self.question(programme_id="p1", week="week1")])
        row = first["records"][0]
        progressed = deepcopy(row)
        progressed["progress"] = {"acquisition": "response_saved", "interpretation": "pending", "scheduling": "waiting"}
        self.memory.put("query_tasks", row["id"], progressed, previous=row)
        events = len(self.events())
        second = self.prepare([self.question(programme_id="p2", week="week2", freshness_reason="A later intended consumer.")], as_of=LATER)
        self.assertEqual(second["question_ids"], first["question_ids"])
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["reused"], 1)
        self.assertEqual(second["records"][0], progressed)
        self.assertEqual(len(self.events()), events)

    def test_duplicates_in_one_preparation_are_one_record(self):
        result = self.prepare([self.question(), self.question(), self.question(programme_id="other")])
        self.assertEqual((result["created"], len(result["question_ids"])), (1, 1))

    def test_desired_freshness_is_not_transmitted_date_scope(self):
        first = self.prepare([self.question(freshness_reason="Refresh before intended selection.")])
        second = self.prepare([self.question(freshness_reason="Same question for a later selection.")], as_of=LATER)
        third = self.prepare([self.question(request={"query": "sink caddy", "country": "IN", "date_from": "2026-10-01"})])
        self.assertEqual(first["question_ids"], second["question_ids"])
        self.assertNotEqual(first["question_ids"], third["question_ids"])

    def test_no_dummy_concept_unknown_family_or_forged_scope(self):
        state = self.memory.snapshot()
        for family in ("missing", "caddy-product"):
            with self.subTest(family=family), self.assertRaises(SafetyError):
                catalogue_direction_subject(state, family, catalogue=self.base)
        for disposition in ("proposed", "retired"):
            other = deepcopy(self.base)
            other["families"][0]["disposition"] = disposition
            with self.subTest(disposition=disposition), self.assertRaises(SafetyError):
                catalogue_direction_subject(state, "caddy", catalogue=other)
        direction = catalogue_direction_subject(state, "caddy", catalogue=self.base)
        direction["scope"]["definition"] = "Fake narrow type."
        direction["scope_hash"] = digest(direction["scope"])
        before = len(self.events())
        with self.assertRaisesRegex(SafetyError, "RESEARCH_DIRECTION_SCOPE_CHANGED"):
            self.prepare([self.question(subject=direction)])
        with self.assertRaises(SafetyError):
            self.prepare([self.question(subject={"kind": "concept", "concept_id": "dummy", "definition_hash": "a" * 64})])
        self.assertEqual(len(self.events()), before)

    def test_direction_qualifiers_frozen_but_presentation_not_meaning(self):
        state = self.memory.snapshot()
        first = catalogue_direction_subject(state, "caddy", catalogue=self.base, qualifiers={"mechanism": "draining"})
        second = catalogue_direction_subject(state, "caddy", catalogue=self.base, qualifiers={"mechanism": "suction"})
        self.assertNotEqual(first["scope_hash"], second["scope_hash"])
        self.base["families"][0]["label"] = "Different display title"
        self.assertEqual(validate_subject(state, first, catalogue=self.base), first)
        self.base["families"][0]["excluded"].append("Wall-mounted holder")
        with self.assertRaisesRegex(SafetyError, "RESEARCH_DIRECTION_SCOPE_CHANGED"):
            validate_subject(state, first, catalogue=self.base)

    def test_false_or_changed_concept_meaning_rejected(self):
        subject = self.subject()
        for invalid in ({**subject, "definition_hash": "a" * 64}, {**subject, "definition_basis": "accepted_definition"},
                        {**subject, "unused_extra": True}):
            with self.subTest(invalid=invalid), self.assertRaises(SafetyError):
                self.prepare([self.question(subject=invalid)])
        original = self.memory.snapshot()["concepts"]["caddy-product"]
        changed = deepcopy(original)
        changed["description"]["mechanism"] = "Suction holder"
        self.memory.put("concepts", "caddy-product", changed, previous=original)
        with self.assertRaisesRegex(SafetyError, "RESEARCH_QUESTION_MEANING_CHANGED"):
            self.prepare([self.question(subject=subject)])

    def test_title_only_change_reuses_same_question(self):
        first = self.prepare([self.question()])
        original = self.memory.snapshot()["concepts"]["caddy-product"]
        changed = deepcopy(original)
        changed["description"]["label"] = "New seller marketing title"
        self.memory.put("concepts", "caddy-product", changed, previous=original)
        events = len(self.events())
        second = self.prepare([self.question()])
        self.assertEqual(first["question_ids"], second["question_ids"])
        self.assertEqual(len(self.events()), events)

    def test_acceptance_of_identical_legacy_meaning_does_not_duplicate_question(self):
        first = self.prepare([self.question()])
        legacy = effective_functional_definition(self.memory.snapshot(), "caddy-product")
        definition = {field: legacy[field] for field in ("report_title", "primary_use", "mechanism", "use_context",
                                                        "compatibility_constraints", "included_distinctions", "excluded_distinctions")}
        self.memory.put("observations", "obs", {"semantic_id": "sem", "observed_at": NOW})
        self.memory.put("associations", "assoc", {"concept_id": "caddy-product", "semantic_id": "sem", "status": "resolved"})
        accept_functional_definition(self.memory, "caddy-product", definition, basis_refs=["obs"], reviewer="test",
                                     reviewed_at=NOW, expected_meaning_hash=legacy["meaning_hash"])
        events = len(self.events())
        second = self.prepare([self.question()])
        self.assertEqual(first["question_ids"], second["question_ids"])
        self.assertEqual(len(self.events()), events)
        self.assertEqual(second["records"][0]["subject_basis"], "legacy_concept")

    def test_entire_batch_validation_precedes_any_event(self):
        before = len(self.events())
        with self.assertRaises(SafetyError):
            self.prepare([self.question(), self.question(purpose="not-a-purpose")])
        self.assertEqual(len(self.events()), before)
        self.assertEqual(self.memory.snapshot()["query_tasks"], {})
        for extra in ({"parser_version": "v2"}, {"not_a_question_option": 1}, {"collection_channel": "free"},
                      {"incremental_collection_fee": "probably_free"}, {"programme_id": "not a valid id"}):
            with self.subTest(extra=extra), self.assertRaises(SafetyError):
                self.prepare([self.question(**extra)])
        with self.assertRaises(SafetyError):
            self.prepare([self.question()], as_of="2026-10-09")

    def test_exact_fact_dependencies_and_question_options_are_bound(self):
        state = self.memory.snapshot()
        dependency = {"table": "concepts", "id": "caddy-product", "hash": digest(state["concepts"]["caddy-product"])}
        plain = self.prepare([self.question()])
        bound = self.prepare([self.question(dependencies=[dependency], question_options={"role": "use-context"})])
        self.assertNotEqual(plain["question_ids"], bound["question_ids"])
        self.assertEqual(plain["records"][0]["physical_request_fingerprint"], bound["records"][0]["physical_request_fingerprint"])
        original = state["concepts"]["caddy-product"]
        updated = deepcopy(original)
        updated["description"]["features"].append("reported weight 50g")
        self.memory.put("concepts", "caddy-product", updated, previous=original)
        with self.assertRaisesRegex(SafetyError, "RESEARCH_DEPENDENCY_CHANGED"):
            self.prepare([self.question(dependencies=[dependency])])

    def test_alternative_wording_has_exact_id_and_parent_link_not_mutation(self):
        first = self.prepare([self.question(wording_candidates=[{"text": "sink caddy", "request": {"query": "sink caddy", "country": "IN"}}])])
        parent = first["records"][0]
        second = self.prepare([self.question(parent_question_id=parent["id"], request={"query": "draining sink organiser", "country": "IN"},
            wording_candidates=[{"text": "draining sink organiser", "request": {"query": "draining sink organiser", "country": "IN"}}])])
        child = second["records"][0]
        self.assertNotEqual(parent["wording_candidates"][0]["id"], child["wording_candidates"][0]["id"])
        self.assertEqual(child["binding"]["parent_question_id"], parent["id"])
        self.assertEqual(self.memory.snapshot()["query_tasks"][parent["id"]], parent)
        with self.assertRaisesRegex(SafetyError, "RESEARCH_QUESTION_PARENT_INVALID"):
            self.prepare([self.question(parent_question_id="missing")])

    def test_dependency_is_rechecked_inside_commit_not_only_preflight(self):
        original_commit = self.memory.commit
        original = self.memory.snapshot()["concepts"]["caddy-product"]
        changed = deepcopy(original)
        changed["description"]["mechanism"] = "Different functional mechanism"
        changed_once = False
        def race(event_id, changes, *, validate=None):
            nonlocal changed_once
            if not changed_once:
                changed_once = True
                original_commit("competing-meaning-change", [change("concepts", "caddy-product", changed, original)])
            return original_commit(event_id, changes, validate=validate)
        with patch.object(self.memory, "commit", side_effect=race), self.assertRaisesRegex(SafetyError, "RESEARCH_QUESTION_MEANING_CHANGED"):
            self.prepare([self.question()])
        self.assertEqual(self.memory.snapshot()["query_tasks"], {})

    def test_racing_same_preparation_reuses_original_event(self):
        original_commit = self.memory.commit
        raced = False
        def race(event_id, changes, *, validate=None):
            nonlocal raced
            if not raced:
                raced = True
                original_commit(event_id, changes, validate=validate)
            return original_commit(event_id, changes, validate=validate)
        before = len(self.events())
        with patch.object(self.memory, "commit", side_effect=race):
            result = self.prepare([self.question()])
        self.assertEqual(len(self.events()), before + 1)
        self.assertEqual(len(result["records"]), 1)

    def test_unknown_kind_at_deterministic_id_cannot_be_overwritten(self):
        first = self.prepare([self.question()])
        row = first["records"][0]
        bad = {**row, "kind": "unknown-kind"}
        self.memory.put("query_tasks", row["id"], bad, previous=row)
        with self.assertRaisesRegex(SafetyError, "RESEARCH_QUESTION_BINDING_CONFLICT"):
            self.prepare([self.question()])

    def test_mutated_saved_wording_cannot_dispatch_under_original_binding(self):
        result = self.prepare([self.question(wording_candidates=[{"text": "sink caddy", "request": {"query": "sink caddy", "country": "IN"}}])])
        original = result["records"][0]
        self.assertEqual(validate_question_record(original), original)
        broken = deepcopy(original)
        broken["wording_candidates"][0]["request"]["query"] = "Unrelated changed query"
        with self.assertRaisesRegex(SafetyError, "RESEARCH_QUESTION_BINDING_CONFLICT"):
            validate_question_record(broken)

    def test_empty_batch_is_a_valid_no_op(self):
        before = len(self.events())
        result = self.prepare([])
        self.assertEqual((result["question_ids"], result["created"], result["reused"]), ([], 0, 0))
        self.assertEqual(len(self.events()), before)


if __name__ == "__main__":
    unittest.main()
