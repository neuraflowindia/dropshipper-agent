"""Actual company adapters/controllers with local owned transport fixtures."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from product_radar.company_pages import CompanyPagePending, shared_company_reader
from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory, key
from product_radar.source_coverage import execute_source_run, prepare_source_tasks
from product_radar.storage import Journal, atomic_json, load_json
from product_radar.supplier_capture_store import capture_saved_supplier_bundle, load_saved_supplier_input
from product_radar.supplier_route_acquisition import collect_supplier_search, create_supplier_handlers
from product_radar.supplier_route_acquisition import collect_supplier_company_pages
from test_supplier_discovery import CONCEPT, NOW
from test_supplier_route_acquisition import Reader, QUERY, receipt

URL = "https://factory.example.org/wrapping-paper"
OTHER = "https://healthy.example.org/wrapping-paper"
LATER = "2026-10-08T14:00:00+00:00"


class OwnedReader:
    def __init__(self, *, status=200, text="Factory wrapping paper and shipping information.", closed=True, at=NOW):
        self.calls = []
        self.status, self.text, self.closed = status, text, closed
        self.at = at

    def __call__(self, item):
        self.calls.append(deepcopy(item))
        value = receipt(item, body=self.text, status=self.status)
        value.update(transport_contract="public-page-owned-http-v1", owned_worker_exited=self.closed,
                     checked_at=self.at, attempt_started_at=self.at)
        if self.status == 429: value["retry_after"] = "3600"
        return value


def forbidden(*_args, **_kwargs):
    raise AssertionError("An original-only replay must not start another reader.")


class SharedCompanyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.memory.put("concepts", "paper", deepcopy(CONCEPT))
        other = deepcopy(CONCEPT); other["description"]["label"] = "Wrapping paper sheets"
        self.memory.put("concepts", "paper-two", other)

    def fetcher(self, reader, *, owner="first-caller", at=NOW, **options):
        return shared_company_reader(self.memory, http_reader=reader, as_of=at, owner_id=owner, **options)

    def nominate(self, cid, urls):
        body = "".join('<a href="' + url + '"><h3>Food wrap paper supplier</h3></a>' for url in urls)
        search = Reader({"https://www.google.com/search?q=Food+wrap+paper+India+manufacturer+wholesaler+supplier": body})
        # Use the real route builder rather than relying on query URL spelling.
        from product_radar.supplier_route_acquisition import supplier_route_url
        search.bodies[supplier_route_url("google-suppliers", QUERY)] = body
        collect_supplier_search(self.memory, route="google-suppliers", concept_ids=[cid], query=QUERY,
                                http_reader=search, as_of=NOW)
        state = self.memory.snapshot()
        checks = [rid for rid, row in state["source_checks"].items()
                  if row.get("kind") == "supplier_route_search" and row.get("concept_id") == cid]
        return {"candidate_urls": {cid: urls}, "candidate_check_ids": {cid: checks}}

    def test_two_callers_and_next_week_share_one_actual_http_read(self):
        reader = OwnedReader()
        first = self.fetcher(reader)({"id": "page-one", "url": URL})
        raw = list((self.memory.root / "supplier_routes" / "company_shared").glob("*/page.json"))[0]
        before = raw.read_bytes()
        restarted = ResearchMemory(self.memory.root, self.memory.journal, evidence_type="synthetic")
        second = shared_company_reader(restarted, http_reader=forbidden, as_of=LATER,
            owner_id="another-meaning")({"id": "page-two", "url": URL + "#details"})
        self.assertEqual(len(reader.calls), 1)
        self.assertEqual(first["checked_at"], second["checked_at"])
        self.assertEqual(first["company_source_original"], second["company_source_original"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(raw.read_bytes(), before)
        self.assertEqual(restarted.snapshot()["observations"], {})

    def test_a_saved_company_page_lost_in_a_power_cut_is_ready_and_read_once_more(self):
        # R15: the company-page readiness check takes the set-aside path, like the other routes.
        from product_radar.company_pages import company_source_hints
        reader = OwnedReader()
        self.fetcher(reader)({"id": "page-one", "url": URL})
        (page,) = (self.memory.root / "supplier_routes" / "company_shared").glob("*/page.json")
        page.write_bytes(b"")  # a cut-short write left the saved original empty
        due_hint, _ = company_source_hints(self.memory)
        context = {"task_id": "no-such-task", "request": {"candidate_urls": {"paper": [URL]}}, "as_of": LATER}
        self.assertTrue(due_hint(context)["ready"])
        self.assertFalse(due_hint(dict(context, reconciling=True))["ready"])  # replaying saved replies only: not this
        later = OwnedReader(at=LATER)
        self.fetcher(later, owner="second", at=LATER)({"id": "page-two", "url": URL})
        self.assertEqual((len(reader.calls), len(later.calls)), (1, 1))  # one fresh read, nothing more
        owners = sorted((row["binding"]["generation"], row["state"]) for row in self.memory.snapshot()["batch_runs"].values()
                        if row.get("kind") == "source-acquisition-owner-v1")
        self.assertEqual(owners, [(1, "set_aside"), (2, "response_saved")])
        self.assertEqual(page.read_bytes(), b"")  # nothing deleted or rewritten
        # A changed (not emptied) saved page is still refused, as before.
        (fresh,) = [path for path in (self.memory.root / "supplier_routes" / "company_shared").glob("*/page.json") if path != page]
        fresh.write_text('{"edited": true}')
        with self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_ARTIFACT_CHANGED"):
            due_hint(context)

    def test_unknown_original_does_not_repeat_or_switch_to_browser(self):
        reader = OwnedReader(closed=False)
        for fetcher in (self.fetcher(reader, browser_reader=forbidden),
                        self.fetcher(reader, owner="second", browser_reader=forbidden)):
            with self.assertRaisesRegex(CompanyPagePending, "SUPPLIER_COMPANY_ORIGINAL_PENDING"):
                fetcher({"id": "requested-page", "url": URL})
        self.assertEqual(len(reader.calls), 1)

    def test_actual_host_refusal_paces_other_url_and_does_not_switch_browser(self):
        reader = OwnedReader(status=429)
        result = self.fetcher(reader, browser_reader=forbidden)({"id": "first", "url": URL})
        self.assertEqual(result["http_status"], 429)
        with self.assertRaisesRegex(CompanyPagePending, "SOURCE_ACCESS_WAIT"):
            self.fetcher(reader, owner="other", browser_reader=forbidden)({"id": "second", "url": URL + "?more=1"})
        healthy = OwnedReader()
        self.fetcher(healthy, owner="healthy")({"id": "third", "url": OTHER})
        self.assertEqual(len(reader.calls), 1); self.assertEqual(len(healthy.calls), 1)

    def test_unreadable_closed_http_can_use_one_distinct_browser_owner(self):
        reader = OwnedReader(text="")
        def browser(item):
            result = receipt(item, body="Supplier wrapping paper prices and shipping.")
            result.update(owned_process_exited=True)
            return result
        first = self.fetcher(reader, browser_reader=browser)({"id": "one", "url": URL})
        second = self.fetcher(forbidden, owner="second", browser_reader=forbidden)({"id": "two", "url": URL})
        owners = [row for row in self.memory.snapshot()["batch_runs"].values()
                  if row.get("kind") == "source-acquisition-owner-v1"]
        self.assertEqual(len(owners), 2); self.assertEqual(len(reader.calls), 1)
        self.assertEqual(first["company_source_original"], second["company_source_original"])

    def test_no_original_saved_only_cannot_start_any_reader(self):
        with self.assertRaises(CompanyPagePending):
            self.fetcher(forbidden, browser_reader=forbidden, saved_only=True)({"id": "page", "url": URL})
        self.assertFalse((self.memory.root / "supplier_routes" / "company_shared").exists())

    def test_saved_old_company_capture_is_adopted_without_reading_again(self):
        reader = OwnedReader()
        captured = capture_saved_supplier_bundle(self.memory, {"concept_id": "paper",
            "lead": {"id": "old-known-lead", "urls": [URL]}}, fetcher=reader)
        path = Path(captured["path"]); original = path.read_bytes()
        current = self.fetcher(forbidden, owner="new-caller")({"id": "new-page", "url": URL})
        self.assertEqual(len(reader.calls), 1); self.assertEqual(current["checked_at"], NOW)
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(current["company_source_original"]["acquisition_id"].startswith("supplier-company-legacy-"))

    def test_old_http_bytes_without_worker_closure_can_be_reused_but_do_not_invent_closure(self):
        reader = OwnedReader()
        def old_reader(item):
            value = reader(item); value.pop("transport_contract"); value.pop("owned_worker_exited")
            return value
        capture_saved_supplier_bundle(self.memory, {"concept_id": "paper",
            "lead": {"id": "old-unlabelled-lead", "urls": [URL]}}, fetcher=old_reader)
        current = self.fetcher(forbidden, owner="new-caller", browser_reader=forbidden)({"id": "new-page", "url": URL})
        self.assertEqual(current["state"], "usable"); self.assertEqual(len(reader.calls), 1)
        self.assertNotIn("owned_worker_exited", current)
        state = self.memory.snapshot(); response = state["source_checks"][current["company_source_original"]["response_id"]]
        self.assertFalse(response["complete"]); self.assertFalse(response["access"]["owned_worker_exited"])

    def test_due_actual_access_failure_can_make_one_paced_new_generation(self):
        reader = OwnedReader(status=429)
        old = self.fetcher(reader, browser_reader=forbidden)({"id": "old", "url": URL})
        recovered = OwnedReader(at=LATER)
        new = self.fetcher(recovered, owner="due", at=LATER)({"id": "new", "url": URL})
        self.assertEqual(len(reader.calls), 1); self.assertEqual(len(recovered.calls), 1)
        self.assertNotEqual(old["company_source_original"]["acquisition_id"], new["company_source_original"]["acquisition_id"])
        self.assertEqual(new["state"], "usable")

    def test_seven_day_source_refresh_keeps_prior_generation_and_saved_only_never_renews(self):
        reader = OwnedReader()
        first = self.fetcher(reader)({"id": "first", "url": URL})
        before_due = "2026-10-13T07:59:59+00:00"
        self.fetcher(forbidden, at=before_due, owner="before-due")({"id": "before", "url": URL})
        due = "2026-10-13T08:00:00+00:00"
        renewed = OwnedReader(at=due)
        second = self.fetcher(renewed, at=due, owner="due")({"id": "second", "url": URL})
        self.fetcher(forbidden, at=due, owner="same-period")({"id": "third", "url": URL})
        replay = self.fetcher(forbidden, at="2026-10-21T08:00:00+00:00", owner="saved-only", saved_only=True)(
            {"id": "fourth", "url": URL})
        self.assertEqual(len(reader.calls), 1); self.assertEqual(len(renewed.calls), 1)
        self.assertNotEqual(first["company_source_original"]["acquisition_id"], second["company_source_original"]["acquisition_id"])
        self.assertEqual(replay["checked_at"], due)

    def test_installed_company_pass_reaches_due_refresh_instead_of_reusing_old_product_bundle_forever(self):
        request = self.nominate("paper", [URL])
        first_reader = OwnedReader()
        first = collect_supplier_company_pages(self.memory, concept_ids=["paper"], acquisition_id="first-company-pass",
            as_of=NOW, page_reader=first_reader, browser_reader=forbidden, shared_acquisitions=True, **request)
        first_pass = load_json(first["reference"])
        original_paths = {cid: (self.memory.root / "supplier_pages" / cid / "capture.json")
                          for cid in first_pass["supplier_capture_ids"]}
        original_bytes = {cid: path.read_bytes() for cid, path in original_paths.items()}
        due = "2026-10-13T08:00:00+00:00"; second_reader = OwnedReader(at=due)
        second = collect_supplier_company_pages(self.memory, concept_ids=["paper"], acquisition_id="due-company-pass",
            as_of=due, page_reader=second_reader, browser_reader=forbidden, shared_acquisitions=True, **request)
        second_pass = load_json(second["reference"])
        self.assertEqual(len(first_reader.calls), 1); self.assertEqual(len(second_reader.calls), 1)
        self.assertNotEqual(first_pass["supplier_capture_ids"], second_pass["supplier_capture_ids"])
        frozen = next(iter(second_pass["capture_requests"].values()))
        self.assertEqual(frozen["refresh"]["previous_capture_id"], first_pass["supplier_capture_ids"][0])
        self.assertEqual(second["observed_at"], due)
        for cid, path in original_paths.items(): self.assertEqual(path.read_bytes(), original_bytes[cid])
        same_period = collect_supplier_company_pages(self.memory, concept_ids=["paper"], acquisition_id="same-period-company-pass",
            as_of=due, page_reader=forbidden, browser_reader=forbidden, shared_acquisitions=True, **request)
        self.assertEqual(load_json(same_period["reference"])["supplier_capture_ids"], second_pass["supplier_capture_ids"])
        replay = collect_supplier_company_pages(self.memory, concept_ids=["paper"], acquisition_id="due-company-pass",
            as_of="2026-10-21T08:00:00+00:00", page_reader=forbidden, browser_reader=forbidden,
            shared_acquisitions=True, saved_only=True, **request)
        self.assertEqual(replay, second)

    def test_redirect_refusal_is_paced_at_the_actual_responding_host(self):
        reader = OwnedReader(status=429)
        def redirected(item):
            value = reader(item); value["final_url"] = "https://destination.example.org/limit"
            return value
        self.fetcher(redirected, browser_reader=forbidden)({"id": "redirect", "url": URL})
        with self.assertRaisesRegex(CompanyPagePending, "SOURCE_ACCESS_WAIT"):
            self.fetcher(forbidden, owner="same-final-host", browser_reader=forbidden)(
                {"id": "blocked", "url": "https://destination.example.org/another-page"})
        fresh = OwnedReader()
        self.fetcher(fresh, owner="original-host-other-url")({"id": "allowed", "url": URL + "?other=1"})
        self.assertEqual(len(fresh.calls), 1)

    def test_changed_original_bytes_are_refused_without_refetch(self):
        reader = OwnedReader()
        self.fetcher(reader)({"id": "page", "url": URL})
        raw = list((self.memory.root / "supplier_routes" / "company_shared").glob("*/page.json"))[0]
        record = load_json(raw); record["receipt"]["text"] = "Changed original text."
        atomic_json(raw, record)
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_COMPANY_ORIGINAL_INVALID|SOURCE_ACQUISITION_ARTIFACT_CHANGED"):
            self.fetcher(forbidden, owner="second")({"id": "next", "url": URL})

    def test_installed_controller_pending_original_keeps_healthy_capture_then_recovers_locally(self):
        request = self.nominate("paper", [URL, OTHER])
        prepare_source_tasks(self.memory, [{"route": "company-pages", "request": request,
            "concept_ids": ["paper"]}], run_id="company-pending", as_of=NOW)
        http = OwnedReader()
        def pages(item):
            if item["url"] == URL:
                value = http(item); value.update(state="unavailable", text="", body_complete=True)
                return value
            return http(item)
        browser_calls = []
        def pending_browser(item):
            browser_calls.append(deepcopy(item))
            value = receipt(item, body="", status=200)
            value.update(state="unavailable", owned_process_exited=False)
            return value
        handlers = create_supplier_handlers(self.memory, http_reader=forbidden, page_reader=pages,
            browser_reader=pending_browser, shared_acquisitions=True, respect_access_waits=True)
        runner = lambda callback, context, _timeout: callback(context)
        first = execute_source_run(self.memory, "company-pending", handlers=handlers, as_of=NOW, bounded_call=runner)
        task_id, task = next((rid, row) for rid, row in self.memory.snapshot()["query_tasks"].items()
                            if row.get("run_id") == "company-pending" and row.get("route") == "company-pages")
        self.assertEqual(first["counts"], {"blocked": 1}); self.assertTrue(task["reconcile_required"])
        captures = list((self.memory.root / "supplier_pages").glob("*/capture.json"))
        self.assertEqual(len(captures), 1)
        self.assertEqual(load_saved_supplier_input(self.memory, captures[0])["item"]["pages"][0]["url"], OTHER)
        self.assertEqual(len(http.calls), 2); self.assertEqual(len(browser_calls), 1)
        aid = browser_calls[0]["id"]
        owner_id = next(rid for rid, row in self.memory.snapshot()["batch_runs"].items()
            if row.get("kind") == "source-acquisition-owner-v1" and key("supplier-company-read", rid) == aid)
        native = self.memory.root / "supplier_routes" / "company_shared" / owner_id / "native"
        output = {"navigation_started": True, "navigation_started_at": NOW, "completed_at": NOW,
            "owned_process_exited": True, "state": "captured", "navigation_status": 200,
            "snapshot": {"url": URL, "html": "<p>Factory wrapping paper.</p>", "text": "Factory wrapping paper.", "links": []}}
        atomic_json(native / "native_result.json", output)
        atomic_json(native / "native_dispatch.json", {"url": URL, "navigation_started": True, "navigation_started_at": NOW})
        original_page = native.parent / "page.json"; original_bytes = original_page.read_bytes()
        replay_handlers = create_supplier_handlers(self.memory, http_reader=forbidden, page_reader=forbidden,
            browser_reader=forbidden, shared_acquisitions=True, respect_access_waits=True)
        resumed = execute_source_run(self.memory, "company-pending", handlers=replay_handlers, as_of=LATER, bounded_call=runner)
        self.assertEqual(resumed["counts"], {"acquired": 1})
        current = self.memory.snapshot()["query_tasks"][task_id]
        self.assertFalse(current["reconcile_required"]); self.assertEqual(current["acquisition_id"], task["acquisition_id"])
        self.assertEqual(len(list((self.memory.root / "supplier_pages").glob("*/capture.json"))), 2)
        self.assertEqual(original_page.read_bytes(), original_bytes)
        self.assertEqual(len(http.calls), 2); self.assertEqual(len(browser_calls), 1)
        self.assertEqual(self.memory.snapshot()["observations"], {})


if __name__ == "__main__":
    unittest.main()
