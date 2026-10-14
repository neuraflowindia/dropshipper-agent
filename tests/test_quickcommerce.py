"""Synthetic transport/capture tests; these are not live product evidence."""

from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.models import SafetyError, digest
from product_radar.quickcommerce import (CREDITS_ENDPOINT, MAX_BYTES, QuickCommerceClient, _NoRedirects,
                                        adapter, map_response, prepare_query, save_capture)
from product_radar.research_memory import ResearchMemory
from product_radar.source_coverage import execute_source_run, prepare_source_tasks, source_coverage
from product_radar.storage import Journal

LOCATION = {"name": "Mumbai reference point", "lat": "19.0760", "lon": "72.8777"}
SECRET = "fictional_test_key_1234567890"


def payload(request, products=None):
    return {"status": "success", "request_id": "synthetic-request", "credits_remaining": 99,
            "data": {"query": request["query"], "platform": request["provider_platform"],
                     "lat": float(request["location"]["lat"]), "lon": float(request["location"]["lon"]),
                     "total_results": 1 if products is None else len(products),
                     "products": products if products is not None else [
                         {"id": "product-1", "name": "Synthetic drain strainer", "brand": "Small maker",
                          "available": True, "offer_price": 89, "mrp": 120, "quantity": "1 piece",
                          "deeplink": "https://blinkit.com/prn/drain-strainer/prid/product-1",
                          "images": ["https://images.example.org/strainer.jpg"],
                          "rating": 4.1, "rating_count": 7}]}}


class Response:
    def __init__(self, body, status=200):
        self.body, self.status, self.closed = body, status, False

    def read(self, maximum):
        return self.body[:maximum]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True


def credits_payload(available=44, used=56):
    """A synthetic free balance reply: one trial pack, never a real account."""
    pack = {"credits_total": available + used, "credits_remaining": available, "credits_used": used, "is_trial": True,
            "purchased_at": "2026-10-03T08:45:38+00:00", "expires_at": "2026-11-02T08:45:38+00:00"}
    return {"summary": {"total_available": available, "total_used": used, "active_packs": 1},
            "active": [pack], "inactive": []}


class Opener:
    """Searches are counted in ``calls``; the free balance checks in ``credit_calls``."""

    def __init__(self, response=None, error=None, credits=None):
        self.response, self.error, self.calls, self.credit_calls = response, error, [], []
        self.credits = credits_payload() if credits is None else credits

    def open(self, request, timeout):
        if request.full_url == CREDITS_ENDPOINT:
            self.credit_calls.append((request, timeout))
            if isinstance(self.credits, Exception):
                raise self.credits
            return Response(json.dumps(self.credits).encode())
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return self.response


class QuickCommerceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
        self.request = prepare_query("blinkit", "drain strainer", deepcopy(LOCATION))

    def client(self, data=None, raw=None):
        response = Response(raw if raw is not None else json.dumps(data if data is not None else payload(self.request)).encode())
        opener = Opener(response)
        return QuickCommerceClient(SECRET, opener=opener), opener

    def test_preparation_normalizes_spaces_and_validates_coordinates_before_dispatch(self):
        self.assertEqual(prepare_query("blinkit", "drain   strainer", LOCATION), self.request)
        for bad in (None, {}, {**LOCATION, "lat": True}, {**LOCATION, "lon": "nan"},
                    {**LOCATION, "lat": "91"}, {**LOCATION, "extra": "not supplied"}):
            with self.assertRaises(SafetyError):
                prepare_query("blinkit", "drain strainer", bad)
        client, opener = self.client()
        for bad in (None, {}, {**self.request, "provider_platform": "Zepto"}):
            with self.assertRaises(SafetyError):
                client.search(bad)
        self.assertEqual(opener.calls, [])

    def test_only_the_header_contains_the_key_and_dispatch_is_called_once(self):
        client, opener = self.client()
        calls = []
        result = client.search(self.request, on_dispatch=lambda: calls.append("started"))
        self.assertEqual(calls, ["started"])
        self.assertTrue(result["dispatch_started"])
        self.assertEqual(result["state"], "captured")
        outgoing, timeout = opener.calls[0]
        self.assertEqual(outgoing.get_header("X-api-key"), SECRET)
        self.assertNotIn(SECRET, outgoing.full_url)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(timeout, 25)
        self.assertEqual(result["products"][0]["price"], {"currency": "INR", "displayed": "89", "mrp": "120"})
        self.assertTrue(opener.response.closed)

    def test_an_explicit_empty_result_is_not_a_connection_failure(self):
        client, _ = self.client(payload(self.request, []))
        result = client.search(self.request)
        self.assertEqual(result["state"], "completed_no_results")
        self.assertEqual(result["products"], [])
        self.assertIsNone(result["reason"])

    def test_wrong_location_platform_or_query_does_not_become_product_evidence(self):
        for field, value in (("platform", "Zepto"), ("query", "unrelated product"), ("lat", 28.6139)):
            data = payload(self.request); data["data"][field] = value
            client, _ = self.client(data)
            result = client.search(self.request)
            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["products"], [])
            self.assertNotIn("raw_response", result)

    def test_price_zero_and_missing_price_remain_different(self):
        data = payload(self.request)
        data["data"]["products"][0]["offer_price"] = 0
        data["data"]["products"][0]["mrp"] = None
        mapped = map_response(data, self.request)
        self.assertEqual(mapped["products"][0]["price"], {"currency": "INR", "displayed": "0", "mrp": None})
        self.assertTrue(mapped["products"][0]["available"])

    def test_invalid_products_are_not_falsely_reported_as_no_results(self):
        data = payload(self.request, [{"id": False, "name": "invalid"}])
        result = map_response(data, self.request)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(len(result["rejected"]), 1)
        self.assertNotEqual(result["state"], "completed_no_results")

    def test_partial_duplicate_invalid_urls_and_bounded_rows_keep_valid_rows(self):
        data = payload(self.request)
        first = data["data"]["products"][0]
        second = {**first, "id": "second", "deeplink": "https://another-shop.example.org/item"}
        data["data"]["products"] = [first, deepcopy(first), second]
        data["data"]["total_results"] = 100
        mapped = map_response(data, self.request)
        self.assertEqual(len(mapped["products"]), 2)
        self.assertTrue(mapped["partial"])
        self.assertIsNone(mapped["products"][1]["url"])
        self.assertEqual({row["reason"] for row in mapped["rejected"]},
                         {"QUICKCOMMERCE_DUPLICATE_ITEM", "QUICKCOMMERCE_PRODUCT_URL_INVALID"})

    def test_negative_location_coordinates_still_require_an_exact_match(self):
        request = prepare_query("blinkit", "drain strainer", {**LOCATION, "lat": "-1"})
        data = payload(request)
        data["data"]["lat"] = -2
        with self.assertRaisesRegex(SafetyError, "QUICKCOMMERCE_LOCATION_CHANGED"):
            map_response(data, request)

    def test_account_limits_and_network_errors_are_distinct_without_logging_bodies(self):
        for status, reason, state in ((401, "AUTH_REJECTED", "blocked"), (402, "CREDITS_EXHAUSTED", "blocked"),
                                     (429, "RATE_LIMITED", "blocked"), (500, "PROVIDER_HTTP_FAILED", "failed")):
            error = HTTPError("https://api.quickcommerceapi.com/v1/search", status, "not logged", {}, io.BytesIO(SECRET.encode()))
            result = QuickCommerceClient(SECRET, opener=Opener(error=error)).search(self.request)
            self.assertEqual(result["state"], state)
            self.assertIn(reason, result["reason"])
            self.assertNotIn(SECRET, json.dumps(result))
            self.assertNotIn("raw_response", result)
        result = QuickCommerceClient(SECRET, opener=Opener(error=URLError(SECRET))).search(self.request)
        self.assertEqual(result["reason"], "QUICKCOMMERCE_NETWORK_FAILED")
        self.assertNotIn(SECRET, json.dumps(result))

    def test_oversized_ambiguous_or_invalid_json_does_not_become_a_capture(self):
        for raw in (b"a" * (MAX_BYTES + 1), b'{"status":"success","status":"failure"}', b"not json"):
            client, _ = self.client(raw=raw)
            result = client.search(self.request)
            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["products"], [])

    def test_credential_redirects_are_refused(self):
        self.assertIsNone(_NoRedirects().redirect_request(None, None, 302, "moved", {}, "https://other.example.org"))

    def test_import_is_immutable_replay_and_not_a_paid_ledger_entry(self):
        client, _ = self.client(); receipt = client.search(self.request)
        first = save_capture(self.memory, "test-acquisition", receipt)
        state = self.memory.snapshot()
        replay = save_capture(self.memory, "test-acquisition", receipt)
        self.assertFalse(first["reused"]); self.assertTrue(replay["reused"])
        self.assertEqual(state, self.memory.snapshot())
        observation = state["observations"][first["observation_ids"][0]]
        self.assertEqual(observation["source"], "blinkit")
        self.assertEqual(observation["facts"]["location"], self.request["location"])
        self.assertEqual(observation["facts"]["list_type"], "quickcommerce_context")
        self.assertFalse(self.memory.journal.path.exists())

    def test_modified_mapping_or_raw_response_is_rejected_without_import(self):
        client, _ = self.client(); receipt = client.search(self.request)
        for modify in ("products", "raw_response"):
            bad = deepcopy(receipt)
            if modify == "products":
                bad["products"][0]["price"]["displayed"] = "1"
            else:
                bad["raw_response"]["data"]["products"][0]["name"] = "different product"
            with self.assertRaisesRegex(SafetyError, "QUICKCOMMERCE_CAPTURE_MAPPING_CHANGED"):
                save_capture(self.memory, "test-acquisition", bad)
        self.assertFalse(self.memory.snapshot()["observations"])

    def test_adapter_keeps_target_interpretation_pending_and_replays_without_access(self):
        client, opener = self.client()
        collect = adapter(self.memory, client)
        dispatched = []
        context = {"route": "blinkit", "request": {"query": "drain strainer", "location": LOCATION},
                   "acquisition_id": "test-acquisition", "concept_ids": ["target-1", "target-2"],
                   "description_hashes": {"target-1": "hash-1", "target-2": "hash-2"},
                   "record_dispatch": lambda: dispatched.append(True)}
        first = collect(context); replay = collect(context)
        self.assertEqual(first, replay)
        self.assertEqual(first["state"], "usable")
        self.assertEqual({row["state"] for row in first["target_results"]}, {"interpretation_pending"})
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(dispatched, [True])
        self.assertEqual(len(first["observation_ids"]), 1)

    def test_connected_controller_saves_actual_attempt_and_does_not_recollect_on_replay(self):
        description = {"label": "Drain strainer", "function": "Filter sink waste", "brand": "generic_or_small"}
        self.memory.put("concepts", "target", {"status": "provisional", "description": description})
        request = {"query": self.request["query"], "location": LOCATION}
        prepare_source_tasks(self.memory, [{"route": "blinkit", "request": request, "concept_ids": ["target"]}], run_id="test-week")
        client, opener = self.client(); collect = adapter(self.memory, client)
        handlers = {"blinkit": {"handler": collect, "reconcile": collect}}
        result = execute_source_run(self.memory, "test-week", handlers=handlers)
        self.assertEqual(result["counts"], {"acquired": 1})
        self.assertTrue(result["results"][0]["actual_attempt"])
        task = self.memory.snapshot()["query_tasks"][result["results"][0]["task_id"]]
        self.assertEqual(task["target_results"]["target"]["state"], "interpretation_pending")
        replay = execute_source_run(self.memory, "test-week", handlers=handlers)
        self.assertEqual(replay["counts"], {"reused": 1})
        self.assertEqual(len(opener.calls), 1)


if __name__ == "__main__":
    unittest.main()
