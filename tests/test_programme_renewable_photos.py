"""Programme photo stage invokes actual renewable controller, not fake receipts."""

from datetime import timedelta
from pathlib import Path
import unittest
from unittest.mock import patch

import product_radar.programme as programme
from product_radar.models import timestamp
from product_radar.work_turns import bind_work_turn, open_work_turn, close_work_turn
import test_renewable_product_images as fixtures


class ProgrammeRenewablePhotoTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RenewableImageTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.memory, self.cid, self.candidate = self.fixture.memory, self.fixture.cid, self.fixture.candidate
        self.record = {"programme_id": "programme-photo", "concept_ids": [self.cid], "client_id": "default",
            "source_run_id": "source-photo", "deferrals": {}, "follow_on_reviews": {}}
        self.context = {"at": self.fixture.at, "fixed_as_of": self.fixture.at, "photo_limit": 3, "retry_seconds": 300,
            "media_root": self.fixture.media, "photo_fetcher": self.fixture.fetch}
        self.pool = {"facts_by_concept": {self.cid: {"image_candidates": [self.candidate]}}}

    def stage(self, *, at=None, fetcher=None):
        at = at or self.fixture.at
        self.fixture.fetch_at = at
        context = dict(self.context, at=at, fixed_as_of=at)
        if fetcher:
            context["photo_fetcher"] = fetcher
        turn = open_work_turn(self.memory, self.record["programme_id"], at=at)
        try:
            with bind_work_turn(self.memory, turn["id"]), patch("product_radar.report_selection.build_report_pool", return_value=self.pool):
                result = programme._photos(self.memory, self.record, context)
        finally:
            close_work_turn(self.memory, turn["id"], at=at, reason="Synthetic photo stage complete")
        return result

    def hint(self, at):
        with patch("product_radar.report_selection.build_report_pool", return_value=self.pool):
            return programme._photo_turn_ready(self.memory, self.memory.snapshot(), self.record, at,
                media_root=self.fixture.media, detailed=True)

    def test_real_stage_uses_one_global_owner_and_missing_media_recovers_without_get(self):
        first = self.stage(); self.assertEqual(first["outcomes"][0]["state"], "usable")
        self.assertEqual(len(self.fixture.calls), 1)
        self.assertEqual(self.fixture.owners()[0][1]["binding"]["generation"], 1)
        image = next(row for row in self.memory.snapshot()["source_checks"].values()
            if row.get("version") == fixtures.RENEWABLE_VERSION and row.get("image"))
        Path(image["image"]["path"]).unlink()
        self.assertTrue(self.hint(self.fixture.at)["ready"])
        recovered = self.stage()
        self.assertEqual(recovered["outcomes"][0]["state"], "usable")
        self.assertEqual(len(self.fixture.calls), 1)

    def test_real_host_deadline_controls_retry_not_old_blanket_deferral(self):
        self.stage(fetcher=self.fixture.failed(429, retry_after="600"))
        waiting = self.hint(self.fixture.at)
        self.assertFalse(waiting["ready"]); self.assertTrue(waiting["waits"])
        before = (timestamp(waiting["waits"][0]) - timedelta(seconds=1)).isoformat()
        self.stage(at=before)
        self.assertEqual(len(self.fixture.calls), 1)
        # A formerly longer generic deferral must not suppress a proved due
        # retry. The concrete source owner rechecks actual launch authority.
        for row in self.record["deferrals"].values():
            row["next_retry_at"] = (timestamp(waiting["waits"][0]) + timedelta(days=1)).isoformat()
        due = waiting["waits"][0]
        self.assertTrue(self.hint(due)["ready"])
        result = self.stage(at=due)
        self.assertEqual(result["outcomes"][0]["state"], "usable")
        self.assertEqual(len(self.fixture.calls), 2)

    def test_terminal_original_does_not_create_empty_repeat_turn_hint(self):
        self.stage(fetcher=self.fixture.failed(404))
        later = self.fixture.later(86400)
        self.assertFalse(self.hint(later)["ready"])
        result = self.stage(at=later)
        self.assertEqual(result["outcomes"], [])
        self.assertEqual(len(self.fixture.calls), 1)
        self.assertEqual(self.record["deferrals"], {})


if __name__ == "__main__":
    unittest.main()
