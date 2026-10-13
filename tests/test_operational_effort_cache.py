"""Offline derivative effort checkpoints; no live store, model or money read."""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from product_radar.models import SafetyError, digest
from product_radar.operational_audit import model_effort
from product_radar.operational_effort_cache import (load_cached_model_effort,
    refresh_model_effort_checkpoint)
from product_radar.storage import atomic_json, load_json


NOW = "2026-10-08T10:00:00+00:00"
LATER = "2026-10-08T10:00:10+00:00"
CHECKED = "2026-10-08T11:00:00+00:00"
USAGE = {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10}


class OperationalEffortCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.memory = SimpleNamespace(root=self.root / "research", evidence_type="synthetic")
        self.state = {"jobs": {}}
        self.cache = self.memory.root / "owner_measurements/model_effort.json"

    def job(self, job_id, *, saved=True, attempt="initial", usage=USAGE):
        request = {"role": "E", "payload": {"items": [{"id": job_id,
                   "text": "PRIVATE_PROMPT_MUST_NOT_CACHE"}]}, "prompt_version": "fictional-v1",
                   "model": "fictional-only", "runner": "fictional-only"}
        self.state["jobs"][job_id] = {"request": request, "binding_keys": {}}
        if not saved:
            return None
        base = self.memory.root / "ai_jobs" / job_id
        folder = base if attempt == "initial" else base / attempt
        atomic_json(folder / "intent.json", {"request": request, "created_at": NOW})
        atomic_json(folder / "runner_execution.json", {"request_hash": digest(request), "job_id": job_id,
                    "attempt": attempt, "started_at": NOW})
        atomic_json(folder / "result.json", {"request_hash": digest(request), "response": {"results": []},
                    "usage": usage, "completed_at": LATER})
        return folder

    def refresh(self, **changes):
        return refresh_model_effort_checkpoint(self.memory, self.state, as_of=CHECKED, **changes)

    def reseal(self, change):
        value = load_json(self.cache, 32 * 1024 * 1024)
        change(value)
        value["hash"] = digest({name: row for name, row in value.items() if name != "hash"})
        atomic_json(self.cache, value)

    def test_missing_cache_is_unknown_without_receipt_probes_or_writes(self):
        self.job("job-a")
        with patch("product_radar.operational_effort_cache._fingerprint", side_effect=AssertionError("No probe")), \
                patch("product_radar.operational_effort_cache.model_effort", side_effect=AssertionError("No receipt read")):
            value = load_cached_model_effort(self.memory, self.state, as_of=CHECKED)
        self.assertEqual(value["state"], "unknown")
        self.assertIsNone(value["model"])
        self.assertEqual(value["coverage"]["unprocessed_jobs"], 1)
        self.assertFalse(value["coverage"]["all_current_jobs_probed"])
        self.assertFalse(self.cache.exists())

    def test_finite_slice_aggregate_matches_existing_audit_and_keeps_partial_coverage(self):
        for name in ("job-a", "job-b", "job-c"):
            self.job(name)
        with patch("product_radar.operational_effort_cache.model_effort", wraps=model_effort) as reader:
            value = self.refresh(job_limit=2)
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(value["refresh"]["probed_jobs"], 2)
        self.assertEqual(value["state"], "partial_cached")
        self.assertEqual(value["coverage"]["unprocessed_jobs"], 1)
        self.assertEqual(value["model"]["saved_job_attempts"], 2)
        self.assertEqual(value["model"]["recorded_duration_sum_seconds"], 20)
        self.assertEqual(value["model"]["recorded_tokens"]["input_tokens"], 200)
        expected = model_effort(self.memory, {"jobs": {name: self.state["jobs"][name] for name in ("job-a", "job-b")}})
        for name in ("rows", "states", "recorded_tokens", "duration_receipts", "recorded_duration_sum_seconds"):
            self.assertEqual(value["model"][name], expected[name])

    def test_restart_continues_frozen_fair_sweep_and_new_jobs_wait_for_next_wrap(self):
        for name in ("job-b", "job-d", "job-f", "job-h"):
            self.job(name)
        self.assertEqual(self.refresh(job_limit=2)["refresh"]["last_job_id"], "job-d")
        self.job("job-a"); self.job("job-g")
        second = self.refresh(job_limit=2)
        self.assertEqual(second["refresh"]["last_job_id"], "job-h")
        self.assertEqual(second["refresh"]["remaining_sweep_jobs"], 0)
        self.assertEqual(second["coverage"]["unprocessed_jobs"], 2)
        visited = []
        for number in range(3):
            value = self.refresh(job_limit=2)
            visited.extend(row["job_id"] for row in value["model"]["rows"])
            self.job("job-new-" + str(number), saved=False)
        self.assertIn("job-a", visited); self.assertIn("job-g", visited)
        # Continuous admissions do not extend the already frozen sweep.
        self.assertEqual(value["refresh"]["remaining_sweep_jobs"], 0)

    def test_unchanged_validated_rows_skip_receipt_body_reads(self):
        self.job("job-a"); self.refresh()
        with patch("product_radar.operational_effort_cache.model_effort", side_effect=AssertionError("Must reuse")):
            value = self.refresh()
        self.assertEqual(value["refresh"]["reused_jobs"], 1)
        self.assertEqual(value["refresh"]["reduced_jobs"], 0)
        self.assertEqual(value["model"]["recorded_tokens"], USAGE)

    def test_read_only_summary_does_not_refresh_receipts_or_mutate_the_checkpoint(self):
        folder = self.job("job-a"); self.refresh()
        original = self.cache.read_bytes()
        result = load_json(folder / "result.json")
        result["usage"] = dict(USAGE, input_tokens=200)
        atomic_json(folder / "result.json", result)
        with patch("product_radar.operational_effort_cache._fingerprint", side_effect=AssertionError("No probe")), \
                patch("product_radar.operational_effort_cache.model_effort", side_effect=AssertionError("No body")):
            value = load_cached_model_effort(self.memory, self.state, as_of=CHECKED)
        self.assertEqual(value["model"]["recorded_tokens"], USAGE)
        self.assertIn("does not recheck", value["receipt_freshness"])
        self.assertEqual(original, self.cache.read_bytes())

    def test_same_job_row_receipt_completion_is_detected_by_metadata(self):
        folder = self.job("job-a"); original_state = deepcopy(self.state)
        (folder / "result.json").unlink()
        self.refresh()
        atomic_json(folder / "completion.json", {"response": {"results": []}, "usage": USAGE, "exit_code": 0})
        with patch("product_radar.operational_effort_cache.model_effort", wraps=model_effort) as reader:
            value = self.refresh()
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(value["model"]["usage_receipts"], 1)
        self.assertEqual(value["model"]["duration_receipts"], 0)
        self.assertEqual(self.state, original_state)

    def test_all_five_receipt_identities_and_retry_list_invalidate_reuse(self):
        folder = self.job("job-a"); self.refresh()
        for filename in ("intent.json", "runner_execution.json", "result.json", "completion.json", "failure.json", "retries.json"):
            with self.subTest(filename=filename):
                path = folder / filename
                if path.exists():
                    original = path.read_bytes()
                    path.write_bytes(original + b" ")
                else:
                    atomic_json(path, {})
                with patch("product_radar.operational_effort_cache.model_effort", wraps=model_effort) as reader:
                    value = self.refresh()
                self.assertEqual(reader.call_count, 1)
                self.assertEqual(value["refresh"]["reduced_jobs"], 1)

    def test_restored_mtime_does_not_hide_new_inode_or_ctime(self):
        folder = self.job("job-a"); self.refresh()
        path = folder / "result.json"; before = path.stat()
        receipt = load_json(path); receipt["usage"] = dict(USAGE, input_tokens=101)
        atomic_json(path, receipt)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        value = self.refresh()
        self.assertEqual(value["refresh"]["reduced_jobs"], 1)
        self.assertEqual(value["model"]["recorded_tokens"]["input_tokens"], 101)

    def test_new_actual_retry_is_separate_and_never_double_counted_on_replay(self):
        self.job("job-a"); self.refresh()
        self.job("job-a", attempt="retry-1", usage=None)
        first = self.refresh(); second = self.refresh()
        self.assertEqual(first["model"]["saved_job_attempts"], 2)
        self.assertEqual(first["model"]["usage_receipts"], 1)
        self.assertEqual(first["model"]["attempts_without_usable_token_counts"], 1)
        self.assertEqual(second["model"], first["model"])
        self.assertEqual(second["refresh"]["reused_jobs"], 1)

    def test_twenty_retries_are_bounded_and_noncanonical_retry_is_unknown(self):
        self.job("job-a")
        for number in range(1, 21):
            self.job("job-a", attempt="retry-" + str(number))
        value = self.refresh()
        self.assertEqual(value["model"]["saved_job_attempts"], 21)
        self.assertEqual(value["refresh"]["probed_jobs"], 1)
        self.job("job-a", attempt="retry-21")
        with patch("product_radar.operational_effort_cache.model_effort", side_effect=AssertionError("Outside scope")):
            value = self.refresh()
        self.assertEqual(value["state"], "unknown")
        self.assertEqual(value["unavailable"]["job-a"], "MODEL_EFFORT_RETRY_SCOPE_INVALID")
        self.assertIsNone(value["model"])

    def test_canonical_request_change_makes_old_measurement_unknown(self):
        self.job("job-a"); self.refresh()
        self.state["jobs"]["job-a"]["request"]["model"] = "changed-model"
        value = load_cached_model_effort(self.memory, self.state, as_of=CHECKED)
        self.assertEqual(value["coverage"]["unprocessed_jobs"], 1)
        self.assertIsNone(value["model"])
        refreshed = self.refresh()
        self.assertEqual(refreshed["model"]["states"], {"receipt_needs_review": 1})
        self.assertEqual(refreshed["model"]["duration_receipts"], 0)
        self.assertEqual(refreshed["model"]["usage_receipts"], 0)

    def test_corrupt_cache_is_unknown_and_refresh_rebuilds_a_partial_slice(self):
        self.job("job-a"); self.job("job-b"); self.refresh()
        value = load_json(self.cache); value["entries"]["job-a"]["rows"][0]["duration_seconds"] = 999
        atomic_json(self.cache, value)
        loaded = load_cached_model_effort(self.memory, self.state, as_of=CHECKED)
        self.assertEqual(loaded["state"], "unknown")
        self.assertIsNone(loaded["model"])
        rebuilt = self.refresh(job_limit=1)
        self.assertEqual(rebuilt["state"], "partial_cached")
        self.assertEqual(rebuilt["coverage"]["unprocessed_jobs"], 1)
        self.assertEqual(rebuilt["model"]["recorded_duration_sum_seconds"], 10)

    def test_resealed_invalid_reduced_rows_do_not_hide_healthy_rows(self):
        self.job("job-a"); self.job("job-b"); self.refresh()
        self.reseal(lambda value: value["entries"]["job-a"]["rows"][0].update(duration_seconds=True))
        loaded = load_cached_model_effort(self.memory, self.state, as_of=CHECKED)
        self.assertEqual(loaded["state"], "partial_cached")
        self.assertEqual(loaded["coverage"]["unprocessed_jobs"], 1)
        self.assertEqual(loaded["model"]["saved_job_attempts"], 1)
        self.assertEqual(loaded["model"]["recorded_duration_sum_seconds"], 10)

    def test_duplicate_attempts_and_wrong_job_owner_are_rejected(self):
        self.job("job-a"); self.refresh()
        self.reseal(lambda value: value["entries"]["job-a"]["rows"].append(deepcopy(value["entries"]["job-a"]["rows"][0])))
        self.assertIsNone(load_cached_model_effort(self.memory, self.state)["model"])
        self.refresh()
        self.reseal(lambda value: value["entries"]["job-a"]["rows"][0].update(job_id="foreign-job"))
        self.assertIsNone(load_cached_model_effort(self.memory, self.state)["model"])

    def test_wrong_namespace_cache_cannot_donate_counts(self):
        self.job("job-a"); self.refresh()
        original = self.cache.read_bytes()
        self.memory.evidence_type = "captured"
        loaded = load_cached_model_effort(self.memory, self.state)
        self.assertEqual(loaded["state"], "unknown")
        self.assertIsNone(loaded["model"])
        self.assertEqual(original, self.cache.read_bytes())

    def test_receipt_changed_during_reduction_is_not_certified(self):
        folder = self.job("job-a")
        def changing(memory, state):
            measured = model_effort(memory, state)
            receipt = load_json(folder / "result.json"); receipt["usage"] = dict(USAGE, input_tokens=200)
            atomic_json(folder / "result.json", receipt)
            return measured
        with patch("product_radar.operational_effort_cache.model_effort", side_effect=changing):
            value = self.refresh()
        self.assertEqual(value["state"], "unknown")
        self.assertIsNone(value["model"])
        self.assertEqual(value["unavailable"]["job-a"], "MODEL_EFFORT_RECEIPTS_CHANGED")

    def test_one_unreadable_job_does_not_block_a_healthy_sibling_or_rotation(self):
        self.job("job-a"); self.job("job-b")
        def reader(memory, state):
            if "job-a" in state["jobs"]:
                raise OSError("Do not disclose local details")
            return model_effort(memory, state)
        with patch("product_radar.operational_effort_cache.model_effort", side_effect=reader):
            value = self.refresh(job_limit=2)
        self.assertEqual(value["refresh"]["probed_jobs"], 2)
        self.assertEqual(value["refresh"]["last_job_id"], "job-b")
        self.assertEqual(value["coverage"]["unavailable_jobs"], 1)
        self.assertEqual(value["model"]["saved_job_attempts"], 1)
        self.assertNotIn("local details", json.dumps(value))

    def test_refuses_private_roots_and_cache_symlinks(self):
        self.memory.root = self.root / "config/research"
        with self.assertRaisesRegex(SafetyError, "PRIVATE_PATH"):
            self.refresh()
        self.memory.root = self.root / "research"
        self.job("job-a")
        self.cache.parent.mkdir(parents=True)
        outside = self.root / "untouched.json"; atomic_json(outside, {"owner": "unchanged"})
        self.cache.symlink_to(outside)
        with self.assertRaisesRegex(SafetyError, "SYMLINK"):
            self.refresh()
        self.assertEqual(load_json(outside), {"owner": "unchanged"})

    def test_job_retry_and_receipt_symlinks_are_not_followed(self):
        self.job("job-a"); target = self.root / "outside"; target.mkdir()
        base = self.memory.root / "ai_jobs/job-a"
        (base / "retry-1").symlink_to(target, target_is_directory=True)
        value = self.refresh()
        self.assertEqual(value["unavailable"]["job-a"], "SYMLINK_PATH_REFUSED")
        (base / "retry-1").unlink()
        receipt = base / "result.json"; receipt.unlink()
        receipt.symlink_to(target / "foreign.json")
        value = self.refresh()
        self.assertEqual(value["unavailable"]["job-a"], "SYMLINK_PATH_REFUSED")

    def test_keeps_original_state_and_receipt_bytes_and_does_not_cache_prompts(self):
        self.job("job-a")
        state = deepcopy(self.state)
        original = {path: path.read_bytes() for path in (self.memory.root / "ai_jobs").rglob("*.json")}
        self.refresh()
        self.assertEqual(self.state, state)
        self.assertTrue(all(path.read_bytes() == raw for path, raw in original.items()))
        raw = self.cache.read_text()
        for text in ("PRIVATE_PROMPT_MUST_NOT_CACHE", "fictional-only", "response", str(self.memory.root)):
            self.assertNotIn(text + '"' if text == "response" else text, raw)
        self.assertFalse((self.memory.root / "events").exists())

    def test_cache_save_failure_is_optional_and_does_not_discard_observed_rows(self):
        self.job("job-a")
        with patch("product_radar.operational_effort_cache.atomic_json", side_effect=OSError("private details")):
            value = self.refresh()
        self.assertFalse(value["refresh"]["saved"])
        self.assertEqual(value["model"]["recorded_tokens"], USAGE)
        self.assertFalse(self.cache.exists())
        self.assertEqual(value["diagnostics"][-1]["reason"], "MODEL_EFFORT_CACHE_SAVE_FAILED")

    def test_unprocessed_prepared_jobs_are_not_presented_as_complete_zero_effort(self):
        self.job("job-a", saved=False); self.job("job-b", saved=False)
        value = self.refresh(job_limit=1)
        self.assertEqual(value["state"], "partial_cached")
        self.assertFalse(value["coverage"]["all_current_jobs_probed"])
        self.assertEqual(value["model"]["saved_job_attempts"], 0)
        value = self.refresh(job_limit=1)
        self.assertEqual(value["state"], "complete_cached")
        self.assertTrue(value["coverage"]["all_current_jobs_probed"])
        self.assertEqual(value["model"]["saved_job_attempts"], 0)

    def test_empty_current_scope_and_removed_jobs_do_not_donate_old_effort(self):
        self.job("job-a"); self.refresh()
        self.state["jobs"] = {}
        loaded = load_cached_model_effort(self.memory, self.state)
        self.assertEqual(loaded["model"]["saved_job_attempts"], 0)
        refreshed = self.refresh()
        self.assertEqual(refreshed["refresh"]["probed_jobs"], 0)
        self.assertEqual(load_json(self.cache)["entries"], {})

    def test_default_slice_is_twenty_five_jobs_and_invalid_limits_are_rejected(self):
        for number in range(30):
            self.job("job-" + str(number).zfill(3), saved=False)
        value = self.refresh()
        self.assertEqual(value["refresh"]["probed_jobs"], 25)
        self.assertEqual(value["coverage"]["unprocessed_jobs"], 5)
        for limit in (0, -1, True, 1.5):
            with self.assertRaisesRegex(SafetyError, "JOB_LIMIT"):
                self.refresh(job_limit=limit)

    def test_not_due_hook_uses_only_derivative_and_exposes_age_and_next_due(self):
        self.job("job-a"); self.refresh()
        original = self.cache.read_bytes()
        with patch("product_radar.operational_effort_cache._fingerprint", side_effect=AssertionError("No probe")), \
                patch("product_radar.operational_effort_cache.model_effort", side_effect=AssertionError("No body")), \
                patch("product_radar.operational_effort_cache.atomic_json", side_effect=AssertionError("No save")):
            value = refresh_model_effort_checkpoint(self.memory, self.state,
                as_of="2026-10-08T11:04:59.250000+00:00", minimum_interval_seconds=300)
        self.assertEqual(value["cache_age_seconds"], 299.25)
        self.assertEqual(value["refresh"]["skipped"], "not_due")
        self.assertEqual(value["refresh"]["next_refresh_at"], "2026-10-08T11:05:00+00:00")
        self.assertEqual(value["refresh"]["probed_jobs"], 0)
        self.assertEqual(value["refresh"]["metadata_probes"], 0)
        self.assertFalse(value["refresh"]["saved"])
        self.assertEqual(original, self.cache.read_bytes())
        loaded = load_cached_model_effort(self.memory, self.state, as_of="2026-10-08T11:20:00+00:00")
        self.assertEqual(loaded["cache_age_seconds"], 1200)

    def test_exact_due_boundary_probes_unchanged_receipts_once(self):
        self.job("job-a"); self.refresh()
        with patch("product_radar.operational_effort_cache.model_effort", side_effect=AssertionError("Reuse body")):
            value = refresh_model_effort_checkpoint(self.memory, self.state,
                as_of="2026-10-08T11:05:00+00:00", minimum_interval_seconds=300)
        self.assertIsNone(value["refresh"]["skipped"])
        self.assertEqual(value["refresh"]["probed_jobs"], 1)
        self.assertEqual(value["refresh"]["reused_jobs"], 1)
        self.assertEqual(value["cache_age_seconds"], 0)
        self.assertEqual(value["refresh"]["next_refresh_at"], "2026-10-08T11:10:00+00:00")

    def test_due_gate_never_hides_current_request_or_new_job_backlog(self):
        self.job("job-a"); self.refresh()
        self.state["jobs"]["job-a"]["request"]["model"] = "changed"
        self.job("job-b")
        with patch("product_radar.operational_effort_cache._fingerprint", side_effect=AssertionError("Not due")):
            value = refresh_model_effort_checkpoint(self.memory, self.state,
                as_of="2026-10-08T11:01:00+00:00", minimum_interval_seconds=300)
        self.assertIsNone(value["model"])
        self.assertEqual(value["state"], "unknown")
        self.assertEqual(value["coverage"]["unprocessed_jobs"], 2)
        self.assertFalse(value["coverage"]["all_current_jobs_probed"])
        self.assertEqual(value["refresh"]["probed_jobs"], 0)

    def test_missing_corrupt_and_semantically_bad_cache_bypass_due_gate(self):
        self.job("job-a"); self.job("job-b")
        missing = self.refresh(job_limit=1, minimum_interval_seconds=300)
        self.assertEqual(missing["refresh"]["probed_jobs"], 1)
        value = load_json(self.cache); value["hash"] = "0" * 64; atomic_json(self.cache, value)
        rebuilt = self.refresh(job_limit=1, minimum_interval_seconds=300)
        self.assertEqual(rebuilt["refresh"]["probed_jobs"], 1)
        self.assertEqual(rebuilt["coverage"]["unprocessed_jobs"], 1)
        self.reseal(lambda value: value["entries"]["job-a"]["rows"][0].update(duration_seconds=True))
        repaired = self.refresh(job_limit=1, minimum_interval_seconds=300)
        self.assertIsNone(repaired["refresh"]["skipped"])
        self.assertEqual(repaired["refresh"]["probed_jobs"], 1)

    def test_clock_ahead_cache_is_explicit_and_does_not_delay_refresh(self):
        self.job("job-a"); self.refresh()
        past = "2026-10-08T10:59:59+00:00"
        loaded = load_cached_model_effort(self.memory, self.state, as_of=past)
        self.assertIsNone(loaded["cache_age_seconds"])
        self.assertIn({"reason": "MODEL_EFFORT_CACHE_CLOCK_AHEAD"}, loaded["diagnostics"])
        value = refresh_model_effort_checkpoint(self.memory, self.state,
            as_of=past, minimum_interval_seconds=300)
        self.assertIsNone(value["refresh"]["skipped"])
        self.assertEqual(value["refresh"]["probed_jobs"], 1)
        self.assertEqual(value["cache_age_seconds"], 0)
        # Even a resealed extreme future observer date must not overflow the
        # ordinary interval or prevent this healthy current slice refreshing.
        self.reseal(lambda value: value.update(updated_at="9999-12-31T23:59:59+00:00"))
        value = self.refresh(minimum_interval_seconds=300)
        self.assertEqual(value["refresh"]["probed_jobs"], 1)
        self.assertEqual(value["cache_checked_at"], CHECKED)

    def test_invalid_refresh_intervals_fail_before_any_receipt_or_cache_mutation(self):
        self.job("job-a")
        for interval in (-1, True, "300", float("nan"), float("inf"), 10 ** 1000):
            with self.subTest(interval=repr(interval)[:40]):
                with patch("product_radar.operational_effort_cache._fingerprint", side_effect=AssertionError("No probe")):
                    with self.assertRaisesRegex(SafetyError, "REFRESH_INTERVAL"):
                        self.refresh(minimum_interval_seconds=interval)
        self.assertFalse(self.cache.exists())

    def test_readonly_and_due_gate_both_refuse_cache_symlink_with_safe_code(self):
        self.job("job-a"); self.cache.parent.mkdir(parents=True)
        target = self.root / "PRIVATE_LOCAL_DETAILS.json"; atomic_json(target, {"never_read": True})
        self.cache.symlink_to(target)
        for reader in (lambda: load_cached_model_effort(self.memory, self.state),
                lambda: self.refresh(minimum_interval_seconds=300)):
            with patch("product_radar.operational_effort_cache.load_json", side_effect=AssertionError("Outside read")):
                with self.assertRaises(SafetyError) as caught:
                    reader()
            self.assertEqual(str(caught.exception), "SYMLINK_PATH_REFUSED")
            self.assertNotIn("PRIVATE_LOCAL_DETAILS", str(caught.exception))
        self.assertEqual(load_json(target), {"never_read": True})

    def test_measurement_directory_symlink_is_refused_before_any_outside_read_or_write(self):
        self.job("job-a")
        outside = self.root / "outside-measurements"; outside.mkdir()
        sentinel = outside / "model_effort.json"; atomic_json(sentinel, {"leave": "alone"})
        self.cache.parent.symlink_to(outside, target_is_directory=True)
        for reader in (lambda: load_cached_model_effort(self.memory, self.state), lambda: self.refresh()):
            with patch("product_radar.operational_effort_cache.load_json", side_effect=AssertionError("Outside read")), \
                    patch("product_radar.operational_effort_cache.atomic_json", side_effect=AssertionError("Outside write")):
                with self.assertRaisesRegex(SafetyError, "^SYMLINK_PATH_REFUSED$"):
                    reader()
        self.assertEqual(load_json(sentinel), {"leave": "alone"})

    def test_retry_token_and_duration_unknowns_do_not_become_actual_zero_usage(self):
        self.job("job-a", usage=None)
        self.job("job-a", attempt="retry-1", usage=None)
        folder = self.memory.root / "ai_jobs/job-a/retry-1"
        (folder / "result.json").unlink()
        atomic_json(folder / "failure.json", {"code": "AI_LOCAL_EXECUTION_FAILED", "at": LATER})
        value = self.refresh()
        self.assertEqual(value["model"]["usage_receipts"], 0)
        self.assertEqual(value["model"]["attempts_without_usable_token_counts"], 2)
        self.assertTrue(all(row["usage"] is None for row in value["model"]["rows"]))
        self.assertEqual(value["model"]["duration_receipts"], 2)
        self.assertEqual(value["model"]["recorded_duration_sum_seconds"], 20)
        self.assertIsNone(value["model"]["incremental_model_cost_usd"])


if __name__ == "__main__":
    unittest.main()
