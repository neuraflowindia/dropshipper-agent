"""Actual saved synthetic pages -> Fv4 -> dated scoped application, no network."""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar.models import SafetyError, digest, timestamp
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, load_json
from product_radar.supplier_capture_store import capture_saved_supplier_bundle, load_saved_supplier_capture
from product_radar.supplier_checks import (prepare_supplier_terms, extract_scoped_supplier_terms,
    prepare_supplier_candidates, extract_supplier_candidates)
from product_radar.supplier_terms import (VERSION, APPLICATION_KIND, current_supplier_terms,
    supplier_payload, supplier_aliases, prepare_scoped_supplier_input, validate_supplier_application,
    validate_scoped_supplier_result)
from product_radar.research_ai import (prompt_version, validate_results, schema_for,
                                     SUPPLIER_PROMPT_VERSION, SCOPED_SUPPLIER_PROMPT_VERSION)

NOW = "2026-10-09T09:00:00+00:00"
BASE = "https://fictional-supplier.example"
COMPANY = "Fictional Fixtures Pvt Ltd. GSTIN 07ABCDE1234F1Z5. We dispatch directly to customers. No subscription required."
A = "Sink caddy, 1 piece. Minimum 10 pieces. INR 100 per piece. This offer cannot be dispatched directly."
B = "Sink caddy pack, pack of 3. Minimum 100 packs. INR 250 per pack."


class Reader:
    def __init__(self, at=NOW, company=COMPANY, a=A, b=B):
        self.at, self.calls = at, []
        self.texts = {BASE + "/about": company, BASE + "/offers/a": a, BASE + "/offers/b": b}

    def __call__(self, request):
        self.calls.append(deepcopy(request))
        return {"schema_version": 1, "id": request["id"], "requested_url": request["url"],
            "final_url": request["url"], "checked_at": self.at, "state": "usable", "reason": "Synthetic saved fixture only.",
            "http_status": 200, "body_complete": True, "title": "Fictional supplier",
            "text": self.texts[request["url"]], "discovered_links": [], "text_truncated": False, "html": "<p>Synthetic</p>"}


def term(field, value, page, quote):
    return {"field": field, "value": value, "basis": "observed", "references": [page],
        "quotes": [{"page_id": page, "excerpt": quote}], "reason": "Observed synthetic scoped statement.",
        "applicability": "applies", "exceptions": []}


class ScopedRunner:
    identity = "synthetic-scoped-supplier-v4"

    def __init__(self, *, invalid_offer=None):
        self.calls, self.invalid_offer = [], invalid_offer

    def __call__(self, role, payload, folder):
        self.calls.append(deepcopy(payload))
        values = []
        for item in payload["items"]:
            page = next(p for p in item["pages"] if p["id"] in item["scope_page_ids"])
            scope = item["scope"]
            value = {"id": item["id"], "scope": deepcopy(scope),
                "relevance": {"relationship": "relevant_company" if scope["scope_kind"] == "company_workflow" else "exact_product",
                    "references": [page["id"]], "reason": "Synthetic scoped applicability, not a verified supplier."},
                "terms": [], "prices": []}
            if scope["scope_kind"] == "company_workflow":
                value["terms"] = [term("direct_customer_dispatch", "Yes", page["id"], "We dispatch directly to customers."),
                    term("subscription_requirement", "Not required", page["id"], "No subscription required.")]
            else:
                is_a = page["url"].endswith("/a")
                value["terms"] = [term("purchase_moq", "10 pieces" if is_a else "100 packs", page["id"], page["text"])]
                if is_a:
                    value["terms"].append(term("direct_customer_dispatch", "No", page["id"], page["text"]))
                value["prices"] = [{"amount": "100" if is_a else "250", "currency": "INR",
                    "unit": "piece" if is_a else "pack of 3", "quantity_condition": "minimum 10 pieces" if is_a else "minimum 100 packs",
                    "price_kind": "product", "qualifier": "Actual fictional offer conditions.",
                    "references": [page["id"]], "quotes": [{"page_id": page["id"], "excerpt": page["text"]}],
                    "reason": "Synthetic offer-local quoted price."}]
                if self.invalid_offer == page["url"]:
                    value["scope"]["offer_ref"] = "invented-offer"
            values.append(value)
        return {"results": values}, {"input_tokens": 1, "output_tokens": 1, "cached_input_tokens": 0}


class CandidateRunner:
    identity = "synthetic-plain-text-scoped-supplier-v4"

    def __init__(self, change=None):
        self.calls, self.change = [], change

    def __call__(self, role, payload, folder):
        self.calls.append(deepcopy(payload)); values = []
        for item in payload["items"]:
            page = next(p for p in item["pages"] if p["id"] == item["primary_page_id"])
            identity_page = next(p for p in item["pages"] if "07ABCDE1234F1Z5" in p["text"])
            company = page["url"].endswith("/about")
            identity = {"kind": "registered_identifier", "scheme": "GSTIN", "value": "07ABCDE1234F1Z5",
                "name": None, "address": None, "page_id": None,
                "quotes": [{"page_id": identity_page["id"], "excerpt": "07ABCDE1234F1Z5"}]}
            selection = {"section_ids": [s["id"] for s in item["section_refs"]],
                "scope_kind": "company_workflow" if company else "product_offer", "company_identity": identity,
                "offer_identity": None, "relevance": {"relationship": "relevant_company" if company else "exact_product",
                    "references": [page["id"]], "reason": "Actual literal fictional page selected."}, "terms": [], "prices": []}
            if company:
                selection["terms"] = [term("direct_customer_dispatch", "Yes", page["id"], "We dispatch directly to customers."),
                    term("subscription_requirement", "Not required", page["id"], "No subscription required.")]
            else:
                a = page["url"].endswith("/a")
                selection["offer_identity"] = {"page_id": page["id"], "label": "Sink caddy" if a else "Sink caddy pack",
                    "purchase_unit": "1 piece" if a else "pack of 3",
                    "quotes": [{"page_id": page["id"], "excerpt": page["text"]}]}
                selection["terms"] = [term("purchase_moq", "10 pieces" if a else "100 packs", page["id"], page["text"])]
                selection["prices"] = [{"amount": "100" if a else "250", "currency": "INR", "unit": "piece" if a else "pack of 3",
                    "quantity_condition": "minimum 10 pieces" if a else "minimum 100 packs", "price_kind": "product",
                    "qualifier": "Fictional literal offer", "references": [page["id"]],
                    "quotes": [{"page_id": page["id"], "excerpt": page["text"]}], "reason": "Actual scoped price."}]
            if self.change:
                self.change(item, selection)
            values.append({"id": item["id"], "selections": [selection], "reason": "Plain text, not structured markup."})
        return {"results": values}, {"input_tokens": 1, "output_tokens": 1, "cached_input_tokens": 0}


class ScopedSupplierTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.memory.put("concepts", "sink", {"status": "active", "description": {"label": "Sink caddy",
            "category": "Home and Kitchen", "function": "Store sink sponges", "mechanism": "Draining holder",
            "features": ["Drains water"], "audience": "Adults", "brand": "generic_or_small",
            "basis": "observed", "reason": "Synthetic description."}})
        self.request = {"concept_id": "sink", "lead": {"id": "fictional-lead",
            "urls": [BASE + "/about", BASE + "/offers/a", BASE + "/offers/b"]}}
        self.capture = capture_saved_supplier_bundle(self.memory, self.request, fetcher=Reader())
        self.choices = self.choices_for(self.capture)

    def choices_for(self, saved):
        capture = load_saved_supplier_capture(self.memory, saved["path"])
        pages = {p["url"]: p for p in capture["bundle"]["item"]["pages"]}
        company = pages[BASE + "/about"]
        identity = {"kind": "registered_identifier", "scheme": "GSTIN", "value": "07ABCDE1234F1Z5",
            "quotes": [{"page_id": company["id"], "excerpt": "GSTIN 07ABCDE1234F1Z5."}]}
        values = [{"scope_kind": "company_workflow", "page_ids": [company["id"]], "company_identity": identity,
            "workflow": "Direct customer dispatch without a subscription", "offer": None}]
        for suffix, label, unit in (("a", "Sink caddy", "1 piece"), ("b", "Sink caddy pack", "pack of 3")):
            page = pages[BASE + "/offers/" + suffix]
            values.append({"scope_kind": "product_offer", "page_ids": [page["id"]], "company_identity": deepcopy(identity),
                "workflow": values[0]["workflow"], "offer": {"page_id": page["id"], "label": label,
                    "purchase_unit": unit, "quotes": [{"page_id": page["id"], "excerpt": page["text"]}]}})
        return values

    def prepare(self, saved=None, choices=None):
        return prepare_supplier_terms(self.memory, (saved or self.capture)["path"], choices or self.choices)

    def extract(self, prepared=None, runner=None, at=NOW):
        with patch("product_radar.supplier_checks.utc_now", return_value=at):
            return extract_scoped_supplier_terms(self.memory, prepared or self.prepare(), runner or ScopedRunner())

    def terms(self, at=NOW):
        return current_supplier_terms(self.memory.snapshot(), concept_id="sink", as_of=at)

    def test_preparation_only_keeps_original_pages_and_dates_outside_model_keys(self):
        before = self.memory.snapshot(); rows = self.prepare()
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all("capture_id" not in str(supplier_payload([r["item"]])) for r in rows))
        self.assertTrue(all(r["application"]["pages"][0]["observed_at"] == NOW for r in rows))
        self.assertEqual(prompt_version("F"), SUPPLIER_PROMPT_VERSION)
        self.assertEqual(prompt_version("F", supplier_payload([rows[0]["item"]])), SCOPED_SUPPLIER_PROMPT_VERSION)

    def test_actual_capture_to_scoped_f_and_per_offer_moq_without_global_borrowing(self):
        runner = ScopedRunner(); result = self.extract(runner=runner)
        self.assertEqual(result["failures"], {}, result)
        self.assertEqual(len(result["accepted"]), 3)
        offers = {r["purchase_unit"]: r for r in self.terms()["all"] if r["scope_kind"] == "product_offer"}
        self.assertEqual(offers["1 piece"]["min_order"], 10)
        self.assertEqual(offers["pack of 3"]["min_order"], 100)
        self.assertEqual(offers["1 piece"]["prices"][0]["moq"], 10)
        self.assertEqual(offers["pack of 3"]["prices"][0]["moq"], 100)
        dispatch = {t["field"]: t for t in offers["1 piece"]["effective_terms"]}["direct_customer_dispatch"]
        self.assertEqual(dispatch["value"], "No")
        self.assertEqual(len(runner.calls), 3)
        self.assertTrue(all(not r["fees"] for r in offers.values()))

    def test_exact_replay_has_no_new_jobs_applications_or_model_calls(self):
        runner = ScopedRunner(); first = self.extract(runner=runner)
        state = self.memory.snapshot(); second = self.extract(runner=runner)
        self.assertEqual(second["failures"], {})
        self.assertEqual(first["application_ids"], second["application_ids"])
        self.assertEqual(self.memory.snapshot(), state)
        self.assertEqual(len(runner.calls), 3)

    def test_old_page_interpreted_later_remains_dated_and_identical_recapture_reuses_f(self):
        later = (timestamp(NOW) + timedelta(days=40)).isoformat()
        runner = ScopedRunner(); self.extract(runner=runner, at=later)
        self.assertEqual(len(self.terms(later)["dated"]), 3)
        refresh = dict(deepcopy(self.request), refresh={"previous_capture_id": self.capture["capture_id"],
            "reason": "A selected report needs current supplier conditions.", "requested_at": later})
        fresh = capture_saved_supplier_bundle(self.memory, refresh, fetcher=Reader(at=later))
        result = self.extract(self.prepare(fresh, self.choices_for(fresh)), runner, at=later)
        self.assertEqual(result["failures"], {}, result)
        self.assertEqual(len(runner.calls), 3)
        current = self.terms(later)
        self.assertEqual(len(current["current"]), 3)
        self.assertTrue(all(row["source_capture_at"] == later for row in current["current"]))
        self.assertEqual(len(self.memory.snapshot()["supplier_assessments"]), 3)
        self.assertEqual(len([r for r in self.memory.snapshot()["source_checks"].values() if r.get("kind") == APPLICATION_KIND]), 6)

    def test_one_offer_change_does_not_repeat_other_offer_or_company_interpretation(self):
        runner = ScopedRunner(); self.extract(runner=runner)
        later = (timestamp(NOW) + timedelta(days=1)).isoformat()
        refresh = dict(deepcopy(self.request), refresh={"previous_capture_id": self.capture["capture_id"],
            "reason": "One selected offer changed.", "requested_at": later})
        fresh = capture_saved_supplier_bundle(self.memory, refresh, fetcher=Reader(at=later, a=A + " Offer A restriction remains."))
        result = self.extract(self.prepare(fresh, self.choices_for(fresh)), runner, at=later)
        self.assertEqual(result["failures"], {}, result)
        self.assertEqual(len(runner.calls), 4)

    def test_invalid_offer_scope_does_not_discard_valid_company_or_other_offer(self):
        result = self.extract(runner=ScopedRunner(invalid_offer=BASE + "/offers/a"))
        self.assertEqual(len(result["accepted"]), 2)
        self.assertEqual(list(result["failures"].values()), ["AI_SUPPLIER_SCOPE_INVALID"])
        self.assertEqual(len(self.terms()["all"]), 2)

    def test_hostname_or_company_name_only_is_not_a_company_identity(self):
        for identity in ({"kind": "hostname", "value": "fictional-supplier.example"},
                         {"kind": "page_identity", "name": "Fictional Fixtures Pvt Ltd"}):
            choice = deepcopy(self.choices[0]); choice["company_identity"] = identity
            with self.assertRaisesRegex(SafetyError, "SUPPLIER_COMPANY_IDENTITY_INVALID"):
                self.prepare(choices=[choice])

    def test_unknown_company_keeps_context_but_cannot_supply_global_dispatch(self):
        choices = deepcopy(self.choices)
        for choice in choices:
            choice["company_identity"] = None
        result = self.extract(self.prepare(choices=choices))
        self.assertEqual(result["failures"], {})
        company = next(r for r in self.terms()["all"] if r["scope_kind"] == "company_workflow")
        self.assertTrue(all(not t["qualified"] for t in company["terms"]))
        offer = next(r for r in self.terms()["all"] if r["purchase_unit"] == "pack of 3")
        self.assertNotIn("direct_customer_dispatch", {t["field"] for t in offer["effective_terms"]})

    def test_mutated_prepared_dates_refused_before_any_model_call(self):
        prepared = self.prepare(); prepared[0]["application"]["pages"][0]["observed_at"] = "2026-10-10T09:00:00Z"
        runner = ScopedRunner(); result = self.extract(prepared, runner)
        self.assertEqual(result["failures"][prepared[0]["item"]["id"]], "SUPPLIER_SCOPED_APPLICATION_CHANGED")
        self.assertEqual(len(runner.calls), 2)

    def test_corrupt_canonical_application_is_named_not_silently_empty(self):
        self.extract(); state = self.memory.snapshot()
        app = next(r for r in state["source_checks"].values() if r.get("kind") == APPLICATION_KIND)
        app["binding"]["pages"][0]["observed_at"] = "2026-10-10T09:00:00Z"
        with self.assertRaisesRegex(SafetyError, "SAVED_SCOPED_SUPPLIER_INVALID"):
            current_supplier_terms(state, concept_id="sink", as_of=NOW)

    def test_company_scope_cannot_take_a_product_moq_or_product_price(self):
        item = self.prepare()[0]["item"]; payload = supplier_payload([item]); encoded = payload["items"][0]
        response, _ = ScopedRunner()(None, payload, None)
        row = response["results"][0]
        page = encoded["scope_page_ids"][0]
        row["terms"].append(term("purchase_moq", "1 piece", page, "We dispatch directly to customers."))
        checked = validate_results("F", payload, response)
        self.assertEqual(checked["failures"][encoded["id"]], "AI_SUPPLIER_MOQ_SCOPE_INVALID")

    def test_aliases_preserve_original_quotes_and_all_scope_refs(self):
        rows = self.prepare(); aliased, maps = supplier_aliases([r["item"] for r in rows])
        for old, new in zip(rows, aliased):
            self.assertEqual([p["text"] for p in old["item"]["pages"]], [p["text"] for p in new["pages"]])
            self.assertTrue(new["id"].startswith("scope_"))
            self.assertIn(new["id"], maps)
        self.assertEqual(schema_for("F"), schema_for("F", {"items": []}))

    def test_company_fee_change_reopens_only_company_not_independent_offers(self):
        runner = ScopedRunner(); self.extract(runner=runner)
        later = (timestamp(NOW) + timedelta(days=1)).isoformat()
        refresh = dict(deepcopy(self.request), refresh={"previous_capture_id": self.capture["capture_id"],
            "reason": "Current company fee changed.", "requested_at": later})
        saved = capture_saved_supplier_bundle(self.memory, refresh,
            fetcher=Reader(at=later, company=COMPANY + " Optional integration fee INR 500."))
        result = self.extract(self.prepare(saved, self.choices_for(saved)), runner, at=later)
        self.assertEqual(result["failures"], {}, result)
        self.assertEqual(len(runner.calls), 4)
        self.assertEqual(runner.calls[-1]["items"][0]["scope"]["scope_kind"], "company_workflow")

    def test_neighbour_quote_cannot_supply_price_to_named_offer(self):
        # A quote for the correct offer plus a separate neighbouring quote
        # containing the requested amount is not one scoped price proof.
        item = deepcopy(self.prepare()[1]["item"])
        page = next(p for p in item["pages"] if p["id"] in item["scope_page_ids"])
        page["text"] += " Neighbouring wall hook costs INR 999 per piece."
        payload = supplier_payload([item]); response, _ = ScopedRunner()(None, payload, None)
        value = response["results"][0]
        value["prices"][0]["amount"] = "999"
        value["prices"][0]["quotes"] = [
            {"page_id": payload["items"][0]["scope_page_ids"][0], "excerpt": A},
            {"page_id": payload["items"][0]["scope_page_ids"][0],
             "excerpt": "Neighbouring wall hook costs INR 999 per piece."}]
        result = validate_results("F", payload, response)
        self.assertEqual(list(result["failures"].values()), ["AI_SUPPLIER_PRICE_SCOPE_INVALID"])

    def test_offer_fees_stay_separate_and_cannot_become_company_rules(self):
        item = deepcopy(self.prepare()[1]["item"])
        page = next(p for p in item["pages"] if p["id"] in item["scope_page_ids"])
        page["text"] += " Sink caddy, 1 piece. Setup fee INR 50 for this offer."
        payload = supplier_payload([item]); response, _ = ScopedRunner()(None, payload, None)
        value = response["results"][0]
        value["prices"] = [{**value["prices"][0], "amount": "50", "price_kind": "setup",
            "quotes": [{"page_id": payload["items"][0]["scope_page_ids"][0],
                        "excerpt": "Sink caddy, 1 piece. Setup fee INR 50 for this offer."}]}]
        result = validate_results("F", payload, response)
        self.assertEqual(result["failures"], {}, result)
        fee = next(iter(result["accepted"].values()))["prices"][0]
        self.assertEqual(fee["price_kind"], "setup")
        self.assertEqual(next(iter(result["accepted"].values()))["scope"]["scope_kind"], "product_offer")

    def test_company_mandatory_fee_derives_scoped_subscription_without_product_price(self):
        item = deepcopy(self.prepare()[0]["item"])
        page = item["pages"][0]
        page["text"] += " Mandatory dispatch membership INR 500 monthly."
        payload = supplier_payload([item]); response, _ = ScopedRunner()(None, payload, None)
        value = response["results"][0]; value["terms"] = []
        value["prices"] = [{"amount": "500", "currency": "INR", "unit": "month",
            "quantity_condition": "Mandatory dispatch membership", "price_kind": "mandatory_recurring",
            "qualifier": "Company workflow fee, not product price.",
            "references": [payload["items"][0]["scope_page_ids"][0]],
            "quotes": [{"page_id": payload["items"][0]["scope_page_ids"][0],
                        "excerpt": "Mandatory dispatch membership INR 500 monthly."}], "reason": "Observed fee."}]
        checked = validate_results("F", payload, response)
        self.assertEqual(checked["failures"], {}, checked)
        result = next(iter(checked["accepted"].values()))
        subscription = next(r for r in result["terms"] if r["field"] == "subscription_requirement")
        self.assertEqual((subscription["value"], subscription["applicability"]), ("Required", "applies"))

    def test_qualified_price_range_revalidates_without_changing_saved_result(self):
        item = deepcopy(self.prepare()[1]["item"])
        page = next(p for p in item["pages"] if p["id"] in item["scope_page_ids"])
        page["text"] += " Sink caddy, 1 piece. INR 100-150 per piece depending on finish."
        payload = supplier_payload([item]); response, _ = ScopedRunner()(None, payload, None)
        value = response["results"][0]; value["prices"][0]["amount"] = "100-150"
        value["prices"][0]["quotes"] = [{"page_id": payload["items"][0]["scope_page_ids"][0],
            "excerpt": "Sink caddy, 1 piece. INR 100-150 per piece depending on finish."}]
        checked = validate_results("F", payload, response)
        result = next(iter(checked["accepted"].values()))
        from product_radar.supplier_terms import original_supplier_result
        _, maps = supplier_aliases([item]); result = original_supplier_result(result, maps[result["id"]])
        self.assertEqual(validate_scoped_supplier_result(item, result), result)
        self.assertEqual(result["prices"][0]["amount_range"], {"minimum": "100", "maximum": "150"})

    def test_crash_after_semantic_save_recovers_application_without_model_repeat(self):
        prepared = self.prepare(); runner = ScopedRunner()
        original = self.memory.commit
        once = [True]
        def interrupted(event_id, changes, **kwargs):
            if once[0] and changes[0]["value"].get("kind") == APPLICATION_KIND:
                once[0] = False
                raise OSError("synthetic local interruption")
            return original(event_id, changes, **kwargs)
        with patch.object(self.memory, "commit", side_effect=interrupted):
            result = self.extract(prepared, runner)
        self.assertEqual(len(result["accepted"]), 2)
        self.assertEqual(len(runner.calls), 3)
        replay = self.extract(prepared, runner)
        self.assertEqual(replay["failures"], {}, replay)
        self.assertEqual(len(replay["accepted"]), 3)
        self.assertEqual(len(runner.calls), 3)

    def test_changed_meaning_inside_actual_commit_refuses_application(self):
        prepared = self.prepare(); original = self.memory.commit; once = [True]
        def change_meaning(event_id, changes, **kwargs):
            if once[0] and changes[0]["value"].get("kind") == APPLICATION_KIND:
                once[0] = False
                old = self.memory.snapshot()["concepts"]["sink"]
                new = deepcopy(old); new["description"]["function"] = "Store bathroom soap"
                self.memory.put("concepts", "sink", new, previous=old)
            return original(event_id, changes, **kwargs)
        with patch.object(self.memory, "commit", side_effect=change_meaning):
            result = self.extract(prepared)
        self.assertEqual(result["accepted"], {})
        self.assertEqual(result["failures"][prepared[0]["item"]["id"]], "SUPPLIER_SCOPED_APPLICATION_CHANGED")
        self.assertFalse([r for r in self.memory.snapshot()["source_checks"].values() if r.get("kind") == APPLICATION_KIND])

    def test_malformed_or_forged_functional_meaning_cannot_enter_f4(self):
        item = deepcopy(self.prepare()[1]["item"])
        item["product"]["primary_use"] = "Unrelated use under the old meaning hash"
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_SCOPED_MEANING_INVALID"):
            supplier_payload([item])

    def test_reversing_page_choices_does_not_create_another_semantic_request(self):
        choice = deepcopy(self.choices[0])
        choice["page_ids"] = [self.choices[0]["page_ids"][0], self.choices[1]["page_ids"][0]]
        first = self.prepare(choices=[choice])[0]
        choice["page_ids"].reverse()
        second = self.prepare(choices=[choice])[0]
        self.assertEqual(first["item"], second["item"])

    def candidate_extract(self, saved=None, runner=None, at=NOW):
        with patch("product_radar.supplier_checks.utc_now", return_value=at):
            return extract_supplier_candidates(self.memory,
                prepare_supplier_candidates(self.memory, (saved or self.capture)["path"]), runner or CandidateRunner())

    def test_plain_text_selection_single_f_stage_actual_scopes_and_exact_replay(self):
        runner = CandidateRunner(); result = self.candidate_extract(runner=runner)
        self.assertEqual(result["failures"], {}, result)
        self.assertEqual(len(result["accepted"]), 3)
        self.assertEqual(len(runner.calls), 3)
        values = self.terms()["all"]
        self.assertEqual(len(values), 3)
        offers = {row["purchase_unit"]: row for row in values if row["scope_kind"] == "product_offer"}
        self.assertEqual(offers["1 piece"]["min_order"], 10)
        self.assertEqual(offers["pack of 3"]["min_order"], 100)
        state = self.memory.snapshot(); replay = self.candidate_extract(runner=runner)
        self.assertEqual(replay["failures"], {}, replay)
        self.assertEqual(self.memory.snapshot(), state)
        self.assertEqual(len(runner.calls), 3)

    def test_plain_text_company_change_reuses_independent_offer_f_owners(self):
        runner = CandidateRunner(); self.candidate_extract(runner=runner)
        later = (timestamp(NOW) + timedelta(days=40)).isoformat()
        refresh = dict(deepcopy(self.request), refresh={"previous_capture_id": self.capture["capture_id"],
            "reason": "Current selected company conditions need a revisit.", "requested_at": later})
        saved = capture_saved_supplier_bundle(self.memory, refresh,
            fetcher=Reader(at=later, company=COMPANY + " Integration is optional."))
        result = self.candidate_extract(saved, runner, later)
        self.assertEqual(result["failures"], {}, result)
        self.assertEqual(len(runner.calls), 4)
        self.assertEqual(len(self.terms(later)["current"]), 3)

    def test_plain_text_target_name_cannot_invent_a_missing_offer_title(self):
        def invented(item, selection):
            if selection["offer_identity"]:
                selection["offer_identity"]["label"] = "Generic target name absent from this page"
        result = self.candidate_extract(runner=CandidateRunner(invented))
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual(set(result["failures"].values()), {"SUPPLIER_OFFER_BINDING_INVALID"})

    def test_plain_text_unknown_identity_is_useful_but_never_global_company_proof(self):
        def unknown(item, selection):
            selection["company_identity"] = {"kind": "unknown", "scheme": None, "value": None,
                "name": None, "address": None, "page_id": None, "quotes": []}
        result = self.candidate_extract(runner=CandidateRunner(unknown))
        self.assertEqual(result["failures"], {}, result)
        values = self.terms()["all"]
        self.assertTrue(all(row["company_ref"] is None for row in values))
        company = next(row for row in values if row["scope_kind"] == "company_workflow")
        self.assertFalse(company["scoped_usable"])
        self.assertTrue(any(row["scoped_usable"] for row in values if row["scope_kind"] == "product_offer"))

    def test_two_actual_plain_text_offers_on_same_page_keep_their_own_moq_prices(self):
        from product_radar.supplier_scope_candidates import candidate_payload, validate_candidate_result, selected_scope
        item = deepcopy(prepare_supplier_candidates(self.memory, self.capture["path"])[1]["item"])
        primary = next(p for p in item["pages"] if p["id"] == item["primary_page_id"])
        primary["text"] = A + "\n" + B
        from product_radar.supplier_scope_candidates import _sections
        item["section_refs"] = _sections(primary)
        payload = candidate_payload([item]); response, _ = CandidateRunner()(None, payload, None)
        first = response["results"][0]["selections"][0]
        first["offer_identity"]["quotes"][0]["excerpt"] = A
        first["terms"][0]["quotes"][0]["excerpt"] = A
        first["prices"][0]["quotes"][0]["excerpt"] = A
        second = deepcopy(first)
        second["offer_identity"].update(label="Sink caddy pack", purchase_unit="pack of 3",
            quotes=[{"page_id": first["offer_identity"]["page_id"], "excerpt": B}])
        second["terms"] = [term("purchase_moq", "100 packs", first["offer_identity"]["page_id"], B)]
        second["prices"][0].update(amount="250", unit="pack of 3", quantity_condition="minimum 100 packs",
            quotes=[{"page_id": first["offer_identity"]["page_id"], "excerpt": B}])
        response["results"][0]["selections"].append(second)
        result = validate_results("F", payload, response)
        self.assertEqual(result["failures"], {}, result)
        scopes = [selected_scope(payload["items"][0], row) for row in next(iter(result["accepted"].values()))["selections"]]
        self.assertEqual([r["result"]["prices"][0]["amount"] for r in scopes], ["100", "250"])
        self.assertEqual([next(t["value"] for t in r["result"]["terms"] if t["field"] == "purchase_moq") for r in scopes],
                         ["10 pieces", "100 packs"])
        self.assertNotEqual(scopes[0]["item"]["scope"]["offer_ref"], scopes[1]["item"]["scope"]["offer_ref"])

    def test_plain_text_uncertain_original_job_is_not_launched_again(self):
        class Unknown(CandidateRunner):
            def __call__(self, role, payload, folder):
                self.calls.append(deepcopy(payload))
                raise SafetyError("AI_JOB_EXECUTION_UNCERTAIN")
        runner = Unknown(); prepared = prepare_supplier_candidates(self.memory, self.capture["path"])
        with patch("product_radar.supplier_checks.utc_now", return_value=NOW):
            first = extract_supplier_candidates(self.memory, prepared, runner)
            second = extract_supplier_candidates(self.memory, prepared, runner)
        self.assertFalse(first["accepted"]); self.assertFalse(second["accepted"])
        self.assertEqual(len(runner.calls), 3)
        self.assertEqual(first["failure_job_ids"], second["failure_job_ids"])

    def test_company_conflicts_are_identical_after_record_map_order_changes(self):
        # Two genuine saved company pages disagree; offer B has no local
        # dispatch claim. Loading the same canonical records in another order
        # must retain the identical primary origin, dates and all source refs.
        reader = Reader()
        reader.texts[BASE + "/policy"] = "Direct customer dispatch is not offered."
        request = {"concept_id": "sink", "lead": {"id": "fictional-conflicting-lead",
            "urls": [BASE + "/about", BASE + "/policy", BASE + "/offers/b"]}}
        saved = capture_saved_supplier_bundle(self.memory, request, fetcher=reader)
        capture = load_saved_supplier_capture(self.memory, saved["path"])
        pages = {p["url"]: p for p in capture["bundle"]["item"]["pages"]}
        identity = {"kind": "registered_identifier", "scheme": "GSTIN", "value": "07ABCDE1234F1Z5",
            "quotes": [{"page_id": pages[BASE + "/about"]["id"], "excerpt": "07ABCDE1234F1Z5"}]}
        choices = [{"scope_kind": "company_workflow", "page_ids": [pages[BASE + suffix]["id"]],
            "company_identity": identity, "workflow": "Direct dispatch", "offer": None} for suffix in ("/about", "/policy")]
        offer = pages[BASE + "/offers/b"]
        choices.append({"scope_kind": "product_offer", "page_ids": [offer["id"]], "company_identity": identity,
            "workflow": "Direct dispatch", "offer": {"page_id": offer["id"], "label": "Sink caddy pack",
                "purchase_unit": "pack of 3", "quotes": [{"page_id": offer["id"], "excerpt": B}]}})
        class ConflictingRunner(ScopedRunner):
            identity = "fictional-conflicting-company-v4"
            def __call__(self, role, payload, folder):
                item = payload["items"][0]
                page = next(p for p in item["pages"] if p["id"] in item["scope_page_ids"])
                if page["url"].endswith("/policy"):
                    self.calls.append(deepcopy(payload))
                    return {"results": [{"id": item["id"], "scope": item["scope"],
                        "relevance": {"relationship": "relevant_company", "references": [page["id"]], "reason": "Fictional company rule."},
                        "terms": [term("direct_customer_dispatch", "No", page["id"], page["text"])], "prices": []}]}, {}
                return super().__call__(role, payload, folder)
        result = self.extract(self.prepare(saved, choices), ConflictingRunner())
        self.assertEqual(result["failures"], {}, result)
        state = self.memory.snapshot()
        first = current_supplier_terms(state, concept_id="sink", as_of=NOW)
        reversed_state = {table: dict(reversed(list(rows.items()))) for table, rows in state.items()}
        second = current_supplier_terms(reversed_state, concept_id="sink", as_of=NOW)
        self.assertEqual(first, second)
        conflict_offer = next(row for row in first["all"] if row["lead_id"] == "fictional-conflicting-lead" and row["scope_kind"] == "product_offer")
        conflict = next(row for row in conflict_offer["effective_terms"] if row["field"] == "direct_customer_dispatch")
        self.assertEqual(conflict["value"], "Conflicting")
        self.assertEqual(len(conflict["support_application_ids"]), 2)


if __name__ == "__main__":
    unittest.main()
