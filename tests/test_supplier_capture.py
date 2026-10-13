"""Fictional public receipts test retrieval bounds, never supplier capabilities."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.models import SafetyError, utc_now
from product_radar.report_selection import _suppliers
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal
from product_radar.supplier_capture import capture_supplier_bundle
from product_radar.suppliers import extract_supplier_terms, validate_supplier_input

NOW = "2026-10-06T08:00:00+00:00"
PRODUCT = {"concept_id": "concept-sink", "description": {"label": "Sink caddy", "function": "Store sink sponges"}}
BASE = "https://supplier.example.org"


class Reader:
    def __init__(self, responses=None):
        self.responses, self.calls = responses or {}, []

    def __call__(self, item):
        self.calls.append(deepcopy(item))
        result = {"schema_version": 1, "id": item["id"], "requested_url": item["url"], "final_url": item["url"],
                  "checked_at": NOW, "state": "usable", "reason": "Fictional fixture, not current supplier terms.",
                  "http_status": 200, "body_complete": True, "title": "Fictional supplier", "text": "No fulfilment claim is made.",
                  "discovered_links": [], "text_truncated": False, "html": "<p>Fixture</p>"}
        override = self.responses.get(item["url"], {})
        if isinstance(override, Exception):
            raise override
        result.update(deepcopy(override))
        return result


def bundle(reader, urls=None, product=None, lead_id="lead-one"):
    return capture_supplier_bundle(product or PRODUCT, {"id": lead_id, "urls": urls or [BASE + "/product/caddy"]}, fetcher=reader)


class SupplierCaptureTests(unittest.TestCase):
    def test_pages_are_f_compatible_and_original_receipts_dates_remain_separate(self):
        reader = Reader()
        result = bundle(reader)
        self.assertEqual(result["state"], "usable")
        validate_supplier_input(result["item"])
        self.assertEqual(set(result["item"]["pages"][0]), {"id", "url", "title", "text"})
        self.assertEqual(result["item"]["product"], dict(PRODUCT["description"], concept_id=PRODUCT["concept_id"]))
        self.assertEqual(result["concept_id"], PRODUCT["concept_id"])
        self.assertEqual(result["lead_id"], "lead-one")
        self.assertEqual(result["receipts"][0]["checked_at"], NOW)
        self.assertEqual(result["receipts"][0]["html"], "<p>Fixture</p>")
        self.assertFalse(result["client_ready"])
        self.assertNotIn("subscription", result)

    def test_unique_seed_urls_fetch_once_and_do_not_invent_terms_urls(self):
        reader = Reader()
        result = bundle(reader, [BASE + "/product/caddy", BASE + "/product/caddy#details", BASE + "/product/caddy"])
        self.assertEqual(len(reader.calls), 1)
        self.assertEqual(result["retrieval"], {"requested": 3, "fetched": 1, "linked_page": None})

    def test_three_seeds_plus_one_linked_page_bound_and_no_recursive_crawl(self):
        seeds = [BASE + "/product/" + str(n) for n in range(3)]
        reader = Reader({url: {"discovered_links": [BASE + "/pages/faq", BASE + "/pages/pricing"]} for url in seeds})
        reader.responses[BASE + "/pages/pricing"] = {"discovered_links": [BASE + "/pages/shipping", BASE + "/pages/terms"]}
        result = bundle(reader, seeds)
        self.assertEqual(len(reader.calls), 4)
        self.assertEqual(reader.calls[-1]["url"], BASE + "/pages/pricing")
        self.assertEqual(len(result["item"]["pages"]), 4)
        self.assertEqual(result["retrieval"]["linked_page"], BASE + "/pages/pricing")

    def test_actual_php_pricing_and_faq_links_are_recognized_without_url_guessing(self):
        # Exact URL shapes from saved source proof, with wholly simulated
        # responses here; this is not a new live supplier check.
        deo = "https://dropshipping.deodap.com"
        reader = Reader({deo + "/": {"discovered_links": [deo + "/pricing.php", deo + "/faqs.php"]}})
        direct = bundle(reader, [deo + "/"])
        self.assertEqual(direct["retrieval"]["linked_page"], deo + "/pricing.php")
        self.assertEqual(len(reader.calls), 2)
        for suffix in ("php", "html", "htm", "aspx", "asp"):
            with self.subTest(suffix=suffix):
                reader = Reader({BASE + "/product/caddy": {"discovered_links": [BASE + "/faqs." + suffix,
                                       BASE + "/pricing." + suffix, BASE + "/products/pricing-calculator.php"]}})
                result = bundle(reader)
                self.assertEqual(reader.calls[-1]["url"], BASE + "/pricing." + suffix)
                self.assertEqual(result["retrieval"]["fetched"], 2)
        reader = Reader({BASE + "/product/caddy": {"discovered_links": [BASE + "/faqs.php"]}})
        self.assertEqual(bundle(reader)["retrieval"]["linked_page"], BASE + "/faqs.php")

    def test_failed_optional_page_keeps_useful_seed_and_does_not_try_another_link(self):
        reader = Reader({BASE + "/product/caddy": {"discovered_links": [BASE + "/pricing", BASE + "/faq"]},
                         BASE + "/pricing": {"state": "unavailable", "reason": "PUBLIC_HTTP_UNAVAILABLE", "http_status": 403}})
        result = bundle(reader)
        self.assertEqual(result["state"], "partial")
        self.assertEqual(len(result["item"]["pages"]), 1)
        self.assertEqual(len(result["receipts"]), 2)
        self.assertEqual(len(reader.calls), 2)
        self.assertEqual(result["failures"][0]["reason"], "PUBLIC_HTTP_UNAVAILABLE")

    def test_failed_seed_does_not_discard_successful_sibling(self):
        reader = Reader({BASE + "/one": {"state": "unavailable", "reason": "PUBLIC_FETCH_DEADLINE"}})
        result = bundle(reader, [BASE + "/one", BASE + "/two"])
        self.assertEqual(result["state"], "partial")
        self.assertEqual(len(result["item"]["pages"]), 1)
        self.assertEqual(len(result["receipts"]), 2)

    def test_no_usable_pages_has_named_state_and_no_f_item(self):
        result = bundle(Reader({BASE + "/product/caddy": {"state": "unavailable", "reason": "PUBLIC_TEXT_UNAVAILABLE"}}))
        self.assertEqual(result["state"], "no_usable_pages")
        self.assertIsNone(result["item"])
        self.assertEqual(len(result["receipts"]), 1)

    def test_external_contact_checkout_auth_and_credential_links_are_not_followed(self):
        unsafe = ["https://external.example.org/pricing", BASE + "/contact-us/pricing", BASE + "/checkout/faq",
                  BASE + "/account/terms", BASE + "/callback/pricing", BASE + "/login/faq",
                  BASE + "/login.php/faq", BASE + "/contact-form/terms", BASE + "/customer-login/pricing",
                  BASE + "/pricing?token=do-not-log", "https://user:do-not-log@supplier.example.org/pricing",
                  BASE + "/pricing?callback=do-not-log", BASE + "/products/pricing-calculator"]
        reader = Reader({BASE + "/product/caddy": {"discovered_links": unsafe + [BASE + "/shipping-policy"]}})
        result = bundle(reader)
        self.assertEqual(reader.calls[-1]["url"], BASE + "/shipping-policy")
        self.assertEqual(len(reader.calls), 2)
        self.assertEqual(result["state"], "usable")

    def test_invalid_secret_seed_is_not_sent_or_echoed_and_sibling_can_continue(self):
        reader = Reader()
        result = bundle(reader, [BASE + "/pricing?api_key=do-not-log", BASE + "/product/caddy"])
        self.assertEqual(len(reader.calls), 1)
        self.assertEqual(result["state"], "partial")
        self.assertNotIn("do-not-log", repr(result))
        self.assertIsNone(result["receipts"][0]["requested_url"])
        self.assertTrue(result["receipts"][0]["no_request"])

    def test_seed_redirect_uses_actual_final_origin_for_discovered_terms(self):
        canonical = "https://www.supplier.example.org"
        reader = Reader({BASE + "/product/caddy": {"final_url": canonical + "/product/caddy",
                         "discovered_links": [BASE + "/pricing", canonical + "/faq"]}})
        result = bundle(reader)
        self.assertEqual(reader.calls[-1]["url"], canonical + "/faq")
        self.assertEqual(result["item"]["pages"][0]["url"], canonical + "/product/caddy")

    def test_linked_redirect_to_different_origin_is_retained_but_not_used_in_f(self):
        reader = Reader({BASE + "/product/caddy": {"discovered_links": [BASE + "/faq"]},
                         BASE + "/faq": {"final_url": "https://external.example.org/faq"}})
        result = bundle(reader)
        self.assertEqual(result["state"], "partial")
        self.assertEqual(len(result["item"]["pages"]), 1)
        self.assertEqual(result["receipts"][-1]["final_url"], "https://external.example.org/faq")
        self.assertEqual(result["failures"][-1]["reason"], "SUPPLIER_LINKED_ORIGIN_CHANGED")

    def test_redirected_final_page_is_not_fetched_again_as_a_seed_or_link(self):
        reader = Reader({BASE + "/old": {"final_url": BASE + "/faq", "discovered_links": [BASE + "/faq"]}})
        result = bundle(reader, [BASE + "/old", BASE + "/faq"])
        self.assertEqual(len(reader.calls), 1)
        self.assertIsNone(result["retrieval"]["linked_page"])

    def test_bad_receipt_or_exception_is_isolated_and_exception_secret_not_echoed(self):
        reader = Reader({BASE + "/one": RuntimeError("do-not-log"), BASE + "/two": {"id": "wrong-source"}})
        result = bundle(reader, [BASE + "/one", BASE + "/two", BASE + "/three"])
        self.assertEqual(result["state"], "partial")
        self.assertEqual(len(result["item"]["pages"]), 1)
        self.assertEqual(len(result["receipts"]), 3)
        self.assertNotIn("do-not-log", repr(result))

    def test_citation_id_collision_is_refused_before_seed_fetches(self):
        reader = Reader()
        with patch("product_radar.supplier_capture.digest", return_value="a" * 64), \
                self.assertRaisesRegex(SafetyError, "SUPPLIER_PAGE_ID_COLLISION"):
            bundle(reader, [BASE + "/one", BASE + "/two"])
        self.assertEqual(reader.calls, [])

    def test_saved_receipt_replay_keeps_original_dates_ids_and_source_text(self):
        reader = Reader()
        first = bundle(reader)
        saved = deepcopy(first["receipts"][0])
        replayed = bundle(lambda item: deepcopy(saved))
        self.assertEqual(replayed["item"], first["item"])
        self.assertEqual(replayed["receipts"], first["receipts"])
        self.assertEqual(replayed["input_hash"], first["input_hash"])

    def test_different_product_lead_pairs_do_not_share_the_f_item_id(self):
        first = bundle(Reader())
        second = bundle(Reader(), product={**PRODUCT, "concept_id": "concept-another"})
        third = bundle(Reader(), lead_id="lead-another")
        self.assertEqual(len({row["item"]["id"] for row in (first, second, third)}), 3)

    def test_trusted_concept_id_is_flattened_with_current_description(self):
        description = {**PRODUCT["description"], "concept_id": "unrelated-concept", "features": ["Metal"], "category": "Home and Kitchen"}
        result = bundle(Reader(), product={**PRODUCT, "description": description})
        expected = {**description, "concept_id": PRODUCT["concept_id"]}
        self.assertEqual(result["item"]["product"], expected)
        self.assertEqual(description["concept_id"], "unrelated-concept")
        text = bundle(Reader(), product={**PRODUCT, "description": "Sink caddy"})
        self.assertEqual(text["item"]["product"], {"concept_id": PRODUCT["concept_id"], "description": "Sink caddy"})

    def test_accepted_f_input_joins_current_report_supplier_scope(self):
        captured = bundle(Reader())
        def runner(role, payload, folder):
            self.assertEqual(role, "F")
            values = [{"id": item["id"], "relevance": {"relationship": "exact_product",
                       "references": [item["pages"][0]["id"]], "reason": "Synthetic exact-product fixture."},
                       "terms": [], "prices": []} for item in payload["items"]]
            return {"results": values}, {"input_tokens": 1, "output_tokens": 1, "cached_input_tokens": 0}
        with tempfile.TemporaryDirectory() as folder:
            memory = ResearchMemory(Path(folder) / "research", Journal(Path(folder) / "finance"), evidence_type="synthetic")
            interpreted = extract_supplier_terms(memory, [captured["item"]], runner)
            self.assertIn(captured["item"]["id"], interpreted["accepted"])
            records = list(memory.snapshot()["supplier_assessments"].items())
            joined = _suppliers(records, PRODUCT["concept_id"], PRODUCT["description"], utc_now())
            self.assertEqual(joined["status"], "complete")
            self.assertEqual(joined["checked"], 1)
            self.assertEqual(joined["leads"][0]["lead_id"], captured["item"]["id"])
            self.assertEqual(joined["leads"][0]["subscription"], "not_stated")
            self.assertEqual(joined["source_refs"][0]["original_page_id"], captured["item"]["pages"][0]["id"])
            # Another concept or changed product scope cannot inherit it.
            self.assertEqual(_suppliers(records, "different-concept", PRODUCT["description"], utc_now())["checked"], 0)
            self.assertEqual(_suppliers(records, PRODUCT["concept_id"], {**PRODUCT["description"], "function": "Different function"}, utc_now())["checked"], 0)

    def test_bad_bounds_identifiers_or_description_fail_before_fetch(self):
        reader = Reader()
        for product, lead in ((PRODUCT, {"id": "lead-one", "urls": []}),
                              (PRODUCT, {"id": "lead-one", "urls": [BASE + "/" + str(n) for n in range(4)]}),
                              ({"concept_id": "bad id", "description": {}}, {"id": "lead-one", "urls": [BASE]}),
                              ({"concept_id": "concept-one", "description": None}, {"id": "lead-one", "urls": [BASE]})):
            with self.subTest(product=product, lead=lead), self.assertRaises(SafetyError):
                capture_supplier_bundle(product, lead, fetcher=reader)
        self.assertEqual(reader.calls, [])


if __name__ == "__main__":
    unittest.main()
