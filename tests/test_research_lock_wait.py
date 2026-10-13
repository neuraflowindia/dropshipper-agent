"""Short local research commits may wait without extending provider authority."""

from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from product_radar.models import SafetyError
from product_radar.research_memory import ResearchMemory, change
from product_radar.storage import Journal


class ResearchLockWaitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.journal = Journal(self.root / "financial")

    @contextmanager
    def contention(self, *, auto_release=None):
        """Hold a separate descriptor; release even when the assertion fails."""
        acquired, release = threading.Event(), threading.Event()
        problems = []
        def holder():
            try:
                with Journal(self.root / "financial").locked():
                    acquired.set()
                    release.wait(auto_release if auto_release is not None else 2)
            except Exception as exc:
                problems.append(exc)
                acquired.set()
        thread = threading.Thread(target=holder)
        thread.start()
        try:
            self.assertTrue(acquired.wait(1), "The fixture holder did not acquire its lock.")
            self.assertEqual(problems, [])
            yield release
        finally:
            release.set()
            thread.join(1)
            self.assertFalse(thread.is_alive(), "The fixture holder did not shut down.")
            self.assertEqual(problems, [])

    def test_explicit_wait_acquires_after_short_commit_releases(self):
        with self.contention(auto_release=.03):
            with self.journal.locked(timeout=.5):
                self.assertTrue(self.journal._locked)
                self.assertEqual(self.journal.events(), [])
        self.assertFalse(self.journal._locked)

    def test_bounded_wait_expires_with_clear_existing_contention_error(self):
        with self.contention():
            started = time.monotonic()
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                with self.journal.locked(timeout=.03):
                    self.fail("A concurrently held lock was entered.")
            self.assertLess(time.monotonic() - started, .3)
            self.assertFalse(self.journal._locked)
        with self.journal.locked():
            self.assertEqual(self.journal.events(), [])

    def test_default_is_still_immediate_for_existing_provider_workflows(self):
        with self.contention():
            started = time.monotonic()
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                with self.journal.locked():
                    self.fail("The default lock silently waited for another task.")
            self.assertLess(time.monotonic() - started, .2)

    def test_nested_same_instance_remains_refused_even_with_wait(self):
        with self.journal.locked():
            with self.assertRaisesRegex(SafetyError, "NESTED_LOCK_REFUSED"):
                with self.journal.locked(timeout=.5):
                    self.fail("A nested lock was acquired.")
            self.assertTrue(self.journal._locked)
        self.assertFalse(self.journal._locked)

    def test_invalid_timeout_never_enters_lock(self):
        for value in (True, False, -.01, 5.01, "0.1", None, [], float("nan"), float("inf")):
            with self.subTest(timeout=value), self.assertRaises(SafetyError):
                with self.journal.locked(timeout=value):
                    self.fail("An invalid timeout was accepted.")
            self.assertFalse(self.journal._locked)

    def test_research_snapshot_recovers_short_financial_contention(self):
        memory = ResearchMemory(self.root / "research", self.journal, evidence_type="synthetic")
        with self.contention(auto_release=.03):
            state = memory.snapshot()
        self.assertEqual(state["concepts"], {})
        self.assertFalse(self.journal._locked)

    def test_research_commit_recovers_short_contention_and_stays_idempotent(self):
        memory = ResearchMemory(self.root / "research", self.journal, evidence_type="synthetic")
        changes = [change("concepts", "candidate", {"label": "Synthetic fixture"})]
        with self.contention(auto_release=.03):
            self.assertTrue(memory.commit("fixture-commit", changes))
        self.assertEqual(memory.snapshot()["concepts"]["candidate"], {"label": "Synthetic fixture"})
        self.assertFalse(memory.commit("fixture-commit", changes))
        self.assertEqual(len(list((self.root / "research" / "events").glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
