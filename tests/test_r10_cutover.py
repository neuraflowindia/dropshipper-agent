"""The R10 cutover helper on synthetic saved records; no provider, website or model."""

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import shutil
import sys
import tempfile
from copy import deepcopy
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from product_radar import r10_cutover as cutover
from product_radar.inventory_intake import INTAKE_KIND, current_intake_records
from product_radar.meta_facts import FACTS_VERSION
from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory, key
from product_radar.storage import Journal
import r10_cutover as command

NOW = "2026-10-20T10:00:00+00:00"
BEFORE = "2026-10-14T10:00:00+00:00"
LATER = "2026-10-21T10:00:00+00:00"


def witness(*sources):
    return {"contract": "functional-priority-support-v1", "sources": [{"source": source} for source in sources]}


class CutoverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="r10-cutover-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in cutover.CONFIG_FILES[:-1]:  # the reviewed Meta request file is missing
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / name, self.root / name)
        self.memory = ResearchMemory(self.root / "captured", Journal(self.root / "finance"), evidence_type="synthetic")
        self.accounts = []

    def intake(self, concept_id, support, admitted_at=BEFORE, field="priority_support"):
        binding = {"client_id": "default", "concept_id": concept_id, "meaning_hash": "a" * 64, "eligibility_hash": "b" * 64,
                   "status": "New", "support_dependencies": {}, field: support, "usable_until": None}
        ref = key("reporting-intake", binding)
        self.memory.put("batch_runs", ref, {"id": ref, "kind": INTAKE_KIND, "binding": binding,
                                            "binding_hash": digest(binding), "admitted_at": admitted_at})
        return ref

    def issue(self, issue_id, state, product, reserved_at=BEFORE, **extra):
        self.memory.put("issue_reservations", issue_id, {"issue_id": issue_id, "state": state, "revision": 1,
            "selection_hash": "s-" + issue_id, "reserved_at": reserved_at, "products": [product], **extra})

    def task(self, task_id, route, status, **extra):
        self.memory.put("query_tasks", task_id, {"kind": "all_source_task_v1", "route": route, "status": status,
                                                  "targets": {}, **extra})

    def enable(self, root):
        self.accounts.append(root)
        return {"written": len(self.accounts) == 1, "path": cutover.ACCOUNT_LIST}

    def fixture(self):
        # Every intake admitted under the retired priority tiers is old stock;
        # one under the score whose support has no Meta site is current.
        old = sorted([self.intake("meta-type", witness("amazon", "meta")), self.intake("amazon-type", witness("amazon")),
                      self.intake("unsupported-type", None),
                      self.intake("score-meta-type", {"contract": "functional-score-support-v1", "sources": [{"source": "meta"}]},
                                  field="score_support")])
        self.intake("score-type", {"contract": "functional-score-support-v1", "sources": [{"source": "google-trends"}]},
                    field="score_support")
        old_meta = {"concept_id": "x", "market": {"sources": {"meta": {"used_ids": ["ad"]}}}}
        current_meta = {"concept_id": "y", "market": {"sources": {"meta": {"depth": {"version": FACTS_VERSION}}}}}
        self.issue("issue-old-draft", "reserved", old_meta)
        self.issue("issue-witness-draft", "reserved", {"concept_id": "z", "priority_support": witness("meta")})
        self.issue("issue-current-draft", "reserved", current_meta)
        self.issue("issue-tier-draft", "reserved", dict(current_meta, opportunity_policy_id="opportunity-priority-v2"))
        self.issue("issue-score-draft", "reserved", dict(current_meta, opportunity_policy_id="opportunity-score-v3"))
        self.issue("issue-old-uploaded", "uploaded", old_meta, activation_receipt={"remote_state": "active"})
        self.issue("issue-cancelled", "cancelled", old_meta)
        self.task("trends-old-wait", "google-trends", "failed", next_retry_at=LATER)
        self.task("trends-signed-in-wait", "google-trends", "prepared", next_retry_at=LATER,
                  source_physical_request={"transport": "signed-in-browser"})
        self.task("movers-open", "amazon-movers-and-shakers", "prepared")
        self.task("movers-done", "amazon-movers-and-shakers", "completed_no_results")
        self.memory.put("batch_runs", "movers-owner", {"kind": "source-acquisition-owner-v1",
                                                       "binding": {"route": "amazon-movers-and-shakers"}})
        return old

    def test_check_reads_only_and_names_everything_the_cutover_finds(self):
        old = self.fixture()
        events = sorted((self.memory.root / "events").glob("*.json"))
        found = cutover.cutover_check(self.memory, root=self.root, as_of=NOW)
        self.assertEqual(sorted((self.memory.root / "events").glob("*.json")), events)
        self.assertEqual(found["old_stock_intake_ids"], old)
        self.assertEqual(found["old_stock_reserved_issue_ids"], ["issue-old-draft", "issue-tier-draft", "issue-witness-draft"])
        self.assertEqual(found["old_stock_published_issue_ids_need_owner"], ["issue-old-uploaded"])
        self.assertEqual(found["old_identity_trends_waits"], ["trends-old-wait"])
        self.assertEqual(found["retired_route_open_tasks"], {"amazon-movers-and-shakers": ["movers-open"]})
        self.assertEqual(found["retired_route_saved_records"], {"amazon-movers-and-shakers": 3})
        self.assertEqual(found["missing_config_files"], ["config/benchmarks/meta_first.json"])
        self.assertFalse(found["account_list_present"])
        self.assertIsNone(found["marker"])

    def test_apply_changes_each_item_once_with_its_reason_and_repeats_nothing(self):
        old = self.fixture()
        result, ok = cutover.apply_cutover(self.memory, root=self.root, as_of=NOW, enable_accounts=self.enable)
        self.assertTrue(ok)
        self.assertEqual(result["account_list"]["written"], True)
        self.assertEqual(result["trends_waits_released"], ["trends-old-wait"])
        self.assertEqual(result["intake_expired"], old)
        self.assertEqual(result["issues_cancelled"], ["issue-old-draft", "issue-tier-draft", "issue-witness-draft"])
        self.assertEqual(result["published_issues_need_owner"], ["issue-old-uploaded"])
        self.assertTrue(result["marker_saved"])
        state = self.memory.snapshot()
        for ref in old:
            self.assertEqual(state["batch_runs"][ref]["expired"], {"at": NOW, "reason": cutover.REASON})
        self.assertFalse(current_intake_records(state, {"client_id": "default", "reporting_intake_ids": old}))
        for issue_id in ("issue-old-draft", "issue-tier-draft", "issue-witness-draft"):
            self.assertEqual((state["issue_reservations"][issue_id]["state"], state["issue_reservations"][issue_id]["cancel_reason"]),
                             ("cancelled", cutover.REASON))
        self.assertEqual(state["issue_reservations"]["issue-current-draft"]["state"], "reserved")
        self.assertEqual(state["issue_reservations"]["issue-score-draft"]["state"], "reserved")
        self.assertEqual(state["issue_reservations"]["issue-old-uploaded"]["state"], "uploaded")
        self.assertIsNone(state["query_tasks"]["trends-old-wait"]["next_retry_at"])
        self.assertEqual(state["query_tasks"]["trends-signed-in-wait"]["next_retry_at"], LATER)
        self.assertEqual(cutover.marker(state)["applied_at"], NOW)
        # Records saved after the cutover are the current rule's: never touched.
        newer = self.intake("new-meta-type", witness("meta"), admitted_at=LATER)
        events = len(list((self.memory.root / "events").glob("*.json")))
        again, ok = cutover.apply_cutover(self.memory, root=self.root, as_of=LATER, enable_accounts=self.enable)
        self.assertTrue(ok)
        self.assertEqual((again["trends_waits_released"], again["intake_expired"], again["issues_cancelled"], again["marker_saved"]),
                         ([], [], [], False))
        self.assertEqual(len(list((self.memory.root / "events").glob("*.json"))), events)
        self.assertNotIn("expired", self.memory.snapshot()["batch_runs"][newer])
        self.assertEqual(cutover.cutover_check(self.memory, root=self.root, as_of=LATER)["old_stock_intake_ids"], [])

    def test_a_failed_account_list_is_reported_without_hiding_the_other_steps(self):
        old = self.fixture()
        def refuse(root):
            raise SafetyError("ACCOUNT_FILES_REQUIRED")
        result, ok = cutover.apply_cutover(self.memory, root=self.root, as_of=NOW, enable_accounts=refuse)
        self.assertFalse(ok)
        self.assertEqual(result["account_list"], {"written": False, "reason": "ACCOUNT_FILES_REQUIRED"})
        self.assertEqual(result["intake_expired"], old)

    def test_command_prints_check_and_apply_results(self):
        self.fixture()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(command.main(["check"], root=self.root, memory=self.memory), 0)
        self.assertEqual(json.loads(output.getvalue())["old_identity_trends_waits"], ["trends-old-wait"])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(command.main(["apply"], root=self.root, memory=self.memory, enable_accounts=self.enable), 0)
        self.assertEqual(json.loads(output.getvalue())["trends_waits_released"], ["trends-old-wait"])
        output = io.StringIO()
        with redirect_stdout(output):  # the real account list needs the private files: a clear failure
            self.assertEqual(command.main(["apply"], root=self.root, memory=self.memory), 1)
        self.assertEqual(json.loads(output.getvalue())["account_list"]["reason"], "ACCOUNT_FILES_REQUIRED")
        from unittest.mock import patch
        output = io.StringIO()
        with redirect_stdout(output), patch.object(self.memory, "snapshot", side_effect=SafetyError("RESEARCH_EVENTS_INVALID")):
            self.assertEqual(command.main(["check"], root=self.root, memory=self.memory), 1)
        self.assertEqual(json.loads(output.getvalue()), {"state": "stopped", "reason": "RESEARCH_EVENTS_INVALID"})

from test_research_flow import NOW as TYPE_NOW
from product_radar.research_memory import change


class ScorePreviewTests(unittest.TestCase):
    """Crowding and the score on a store copy: free re-reads of the copy's saved pages only."""

    def setUp(self):
        from test_crowding import TypeFixture
        self.type = TypeFixture(); self.type.setUp(); self.addCleanup(self.type.doCleanups)
        self.store = self.type.root / "store-copy"
        (self.store / "data/research/captured").mkdir(parents=True)

    def copied_page(self, name, route):
        """A saved page whose record names the original store; its bytes exist only in the copy."""
        from test_crowding import FIXTURES
        original = self.type.root / "original-store/data/research/captured/direct_marketplace" / name
        copy = self.store / "data/research/captured/direct_marketplace" / name
        copy.mkdir(parents=True)
        shutil.copy(FIXTURES / (name + ".html"), copy / "rendered.html")
        shutil.copy(FIXTURES / (name + ".snapshot.json"), copy / "snapshot.json")
        capture_id = "capture-" + name
        self.type.memory.commit(key("fixture-capture", capture_id), [change("captures", capture_id, {
            "source": "flipkart" if route == "flipkart-search" else "amazon", "acquisition_id": "acq-" + name,
            "observed_at": TYPE_NOW, "capture_hash": "0" * 64, "mapping_hash": "0" * 64, "mapping_status": "partial",
            "reference": {"route": route, "folder": str(original), "file": "receipt.json", "request": {},
                          "receipt_hash": "0" * 64}})])
        return capture_id, copy

    def test_preview_reads_the_copy_and_scores_every_accepted_type(self):
        import socket
        from unittest.mock import patch
        self.type.make()
        amazon, amazon_copy = self.copied_page("amazon-search-baby-bath-towels", "amazon-search")
        flipkart, _ = self.copied_page("flipkart-search-tealight-candles", "flipkart-search")
        self.type.observe(amazon, "amazon-search", "B0F27RZYLM")
        self.type.observe(flipkart, "flipkart-search", "CANHPF5ZV3HHZWPD")
        self.type.task("amazon-search", capture_ids=[amazon]); self.type.task("flipkart-search", capture_ids=[flipkart])
        jobs = deepcopy(self.type.memory.snapshot()["jobs"])
        def no_network(*_args, **_kwargs):
            raise AssertionError("network used")
        with patch.object(socket.socket, "connect", side_effect=no_network):
            result = cutover.score_preview(self.type.memory, store=self.store, as_of=TYPE_NOW)
        self.assertEqual((result["accepted_types"], result["assessed"], result["five_or_more_meets"]), (1, 1, 1))
        self.assertEqual(result["crowding"], {"low": 1, "medium": 0, "high": 0, "not_checked": 0})
        # The reviewed listings were found (records unchanged, so their review digests hold) and
        # their saved pages were read from the copy.
        from product_radar.crowding import crowding_for
        fact = crowding_for(self.type.memory.snapshot(), self.type.cid)
        self.assertEqual((fact["counts"]["relevant_listings"], fact["counts"]["max_ratings"]), ({"amazon": 1, "flipkart": 1}, 60))
        self.assertEqual(fact["evidence_ids"], ["obs-B0F27RZYLM", "obs-CANHPF5ZV3HHZWPD"])
        state = self.type.memory.snapshot()
        self.assertTrue(state["captures"][amazon]["reference"]["folder"].startswith(str(self.type.root / "original-store")))
        self.assertEqual(result["labels"]["too_quiet"], 1)  # no Meta, Trends, platform or stock facts here
        self.assertEqual((result["score_eligible"], result["top"]), (0, []))
        self.assertTrue(list(amazon_copy.glob("crowding-cards-*.json")))  # the derived file is written in the copy
        self.assertFalse((self.type.root / "original-store").exists())  # the original store is never touched
        self.assertEqual(self.type.memory.snapshot()["jobs"], jobs)  # no model call

    def test_an_original_store_is_refused_and_unreviewed_records_are_counted_for_rereview(self):
        self.type.make()
        amazon, _ = self.copied_page("amazon-search-baby-bath-towels", "amazon-search")
        self.type.observe(amazon, "amazon-search", "B0F27RZYLM")
        state = self.type.memory.snapshot()
        row = dict(state["observations"]["obs-B0F27RZYLM"], source_item="unreviewed:fixture",
                   semantic={"title": "Unreviewed listing", "product_url": "https://example.org/unreviewed"})
        row["semantic_id"] = key("item", row["semantic"])
        self.type.memory.commit(key("fixture-observation", "unreviewed"), [change("observations", "obs-unreviewed", row)])
        self.type.task("amazon-search", capture_ids=[amazon], observation_ids=["obs-B0F27RZYLM", "obs-unreviewed"])
        result = cutover.score_preview(self.type.memory, store=self.store, as_of=TYPE_NOW)
        self.assertEqual({key_: result["rereview"][key_] for key_ in ("types_waiting", "cohorts_waiting", "rounds_estimate")},
                         {"types_waiting": 1, "cohorts_waiting": 2, "rounds_estimate": 2})  # its Amazon page and its Meta read
        provisional = result["provisional_saved_v4_matches"]
        self.assertTrue(provisional["label"].startswith("PROVISIONAL, preview only"))
        self.assertEqual(provisional["crowding"], result["crowding"])  # these fixture reviews are not v4 answers
        self.assertEqual(result["waiting_for_checklist_review"], 0)
        self.type.memory.commit(key("fixture-capture", "plain"), [change("captures", "capture-plain", {
            "source": "amazon", "acquisition_id": "acq-plain", "observed_at": TYPE_NOW, "capture_hash": "0" * 64,
            "mapping_hash": "0" * 64, "mapping_status": "partial", "reference": "synthetic/receipt.json"})])
        # Records that point into the store itself: an original, never previewed (a plain reference is no folder).
        with self.assertRaisesRegex(SafetyError, "SCORE_PREVIEW_STORE_IS_ORIGINAL"):
            cutover.score_preview(self.type.memory, store=self.type.root / "original-store", as_of=TYPE_NOW)

    def test_provisional_matching_is_scoped_to_the_preview(self):
        from product_radar.market_validation import PREVIEW_MATCH_VERSIONS, _functional_match
        from product_radar.source_interpretation import FUNCTIONAL_MATCH_METHOD
        answer = {"relevance": [{"id": "o1", "scope": "narrow_type", "relationship": "relevant_family", "reason": "r",
                                 "limitations": []}]}
        row = {"method": FUNCTIONAL_MATCH_METHOD, "support": "insufficient", "scope": "narrow_type",
               "relationship": "relevant_family", "reason": "r", "limitations": [],
               "job_proof": {"role": "E", "prompt_version": cutover.PROVISIONAL_MATCH_VERSIONS[0], "accepted_item": answer,
                             "accepted_item_hash": digest(answer)}}
        self.assertFalse(_functional_match(row, "o1"))
        token = PREVIEW_MATCH_VERSIONS.set(cutover.PROVISIONAL_MATCH_VERSIONS)
        try:
            self.assertTrue(_functional_match(row, "o1"))
        finally:
            PREVIEW_MATCH_VERSIONS.reset(token)
        self.assertFalse(_functional_match(row, "o1"))

    def test_preview_lists_the_top_types_with_their_four_parts(self):
        self.type.make(with_market=True, count=2)
        result = cutover.score_preview(self.type.memory, store=self.store, as_of=TYPE_NOW)
        self.assertEqual(result["crowding"]["high"], 2)
        self.assertEqual((result["labels"]["proven_but_crowded"], result["score_eligible"]), (2, 2))
        self.assertEqual([row["parts"] for row in result["top"]], [{"want": 6, "early": 0, "room": 0, "product": 25}] * 2)
        self.assertEqual([row["score"] for row in result["top"]], [31, 31])
        self.assertEqual(result["top"][0]["crowding"], "high")
        self.assertEqual(result["top"][0]["concept_id"] < result["top"][1]["concept_id"], True)

    def test_live_and_configured_stores_are_refused(self):
        live = self.type.root / "named-live-store"
        environ = {cutover.PROTECTED_STORES_ENV: str(self.type.root / "other") + __import__("os").pathsep + str(live)}
        self.assertEqual(cutover.preview_store_refused(live, self.type.root, environ), "SCORE_PREVIEW_LIVE_STORE_REFUSED")
        self.assertEqual(cutover.preview_store_refused(live / "data", self.type.root, environ), "SCORE_PREVIEW_LIVE_STORE_REFUSED")
        self.assertIsNone(cutover.preview_store_refused(self.store, self.type.root / "project", environ))
        with __import__("unittest.mock").mock.patch.dict("os.environ", {cutover.PROTECTED_STORES_ENV: str(self.store)}):
            self.assertEqual(cutover.preview_store_refused(self.store, self.type.root / "project"), "SCORE_PREVIEW_LIVE_STORE_REFUSED")
        self.assertEqual(cutover.preview_store_refused(self.type.root, self.type.root), "SCORE_PREVIEW_RUNTIME_STORE_REFUSED")
        self.assertEqual(cutover.preview_store_refused(self.type.root / "data", self.type.root), "SCORE_PREVIEW_RUNTIME_STORE_REFUSED")
        self.assertEqual(cutover.preview_store_refused(self.type.root / "missing", self.store), "SCORE_PREVIEW_STORE_NOT_FOUND")
        self.assertIsNone(cutover.preview_store_refused(self.store, self.type.root / "project"))
        with self.assertRaisesRegex(SafetyError, "SCORE_PREVIEW_PAGE_OUTSIDE_COPY"):
            cutover._rebased("/elsewhere/pages/one", self.store)
        with self.assertRaisesRegex(SafetyError, "SCORE_PREVIEW_PAGE_OUTSIDE_COPY"):
            cutover._rebased("/x/data/research/captured/../../../../etc", self.store)
        self.type.make()
        self.type.memory.commit(key("fixture-capture", "outside"), [change("captures", "outside", {
            "source": "amazon", "acquisition_id": "acq-outside", "observed_at": TYPE_NOW, "capture_hash": "0" * 64,
            "mapping_hash": "0" * 64, "mapping_status": "partial",
            "reference": {"route": "amazon-search", "folder": "/elsewhere/pages/outside", "file": "receipt.json"}})])
        reader, folders = cutover.copy_page_reader(self.store)
        self.assertEqual(folders("/live/data/research/captured/direct_marketplace/one"),
                         str(self.store.resolve() / "data/research/captured/direct_marketplace/one"))
        with self.assertRaisesRegex(SafetyError, "SCORE_PREVIEW_PAGE_OUTSIDE_COPY"):
            reader("/elsewhere/pages/outside", "amazon-search")
        from product_radar.crowding import _page_state
        self.assertEqual(_page_state(self.type.memory.snapshot(), "outside", folders),
                         [["rendered.html", None, None], ["snapshot.json", None, None]])  # unreadable, never read outside

    def test_command_runs_only_on_a_copy(self):
        self.type.make(with_market=True)
        project = self.type.root / "project"; project.mkdir()
        from unittest.mock import patch
        output = io.StringIO()
        with redirect_stdout(output), patch.object(command, "utc_now", return_value=TYPE_NOW):
            self.assertEqual(command.main(["score-preview", "--store", str(self.store)], root=project, memory=self.type.memory), 0)
        self.assertEqual(json.loads(output.getvalue())["labels"]["proven_but_crowded"], 1)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(command.main(["score-preview", "--store", str(project)], root=project), 1)
        self.assertEqual(json.loads(output.getvalue()), {"state": "stopped", "reason": "SCORE_PREVIEW_RUNTIME_STORE_REFUSED"})
        output = io.StringIO()
        with redirect_stdout(output), patch.object(command, "score_preview", side_effect=SafetyError("RESEARCH_EVENTS_INVALID")):
            self.assertEqual(command.main(["score-preview", "--store", str(self.store)], root=project, memory=self.type.memory), 1)
        self.assertEqual(json.loads(output.getvalue()), {"state": "stopped", "reason": "RESEARCH_EVENTS_INVALID"})
        for argv in (["score-preview"], ["check", "--store", str(self.store)]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), \
                    __import__("contextlib").redirect_stderr(io.StringIO()):
                command.main(argv, root=project, memory=self.type.memory)


if __name__ == "__main__":
    unittest.main()
