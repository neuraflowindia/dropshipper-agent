"""One programme round's share of the signed-in browser; local records and doubles only."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar import signed_in_browser as signed
from product_radar import signed_in_round as share
from product_radar.models import SafetyError
from product_radar.native_public_browser import signed_in_budget
from product_radar.signed_in_attempts import CALM_CODES, SignedInWait, begin_signed_in_read, signed_in_hold

SETTINGS = {"signed_in_seconds": 300, "site_reads": {"indiamart": 4}}


class SignedInRoundTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="signed-in-round-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        signed.note_sign_in_window(self.root, True, opened=True, at="2026-10-14T10:00:00+00:00")
        signed.note_sign_in_window(self.root, False, at="2026-10-14T10:05:00+00:00")
        self.calls = []

    def browser(self, seconds=30, *, started=True):
        def read(url, expression, folder, **options):
            self.calls.append(url)
            self.clock[0] += seconds
            return {"state": "captured" if started else "waiting", "navigation_started": started,
                    "snapshot": {"url": url} if started else None, "reason": None if started else "SIGNED_IN_BROWSER_BUSY"}
        return read

    def read(self, site="indiamart", **options):
        return signed.signed_in_read(site, "https://www.indiamart.com/", "x", self.root, root=self.root,
                                     marker=lambda snapshot: True, **options)

    def test_outside_a_round_there_is_no_round_share(self):
        self.assertIsNone(share.round_wait("indiamart"))
        share.note_round_read("indiamart", 1000)  # nothing to charge
        self.assertIsNone(signed.signed_in_wait(self.root, "indiamart"))

    def test_indiamart_reads_are_capped_per_round_and_other_sites_continue(self):
        self.clock = [0.0]
        with patch.object(signed.time, "monotonic", side_effect=lambda: self.clock[0]), \
                share.signed_in_round(settings=SETTINGS):
            for _ in range(4):
                self.assertEqual(self.read(browser=self.browser(5))["state"], "captured")
            held = self.read(browser=self.browser(5))
            self.assertEqual((held["state"], held["reason"], held["navigation_started"]),
                             ("waiting", share.READS_USED, False))
            self.assertEqual(len(self.calls), 4)
            self.assertIsNone(signed.signed_in_wait(self.root, "google-trends"))
            hold = signed_in_hold(self.root, "indiamart")
            self.assertEqual(hold["reason"], share.READS_USED)
            self.assertIsNotNone(hold["next_retry_at"])  # looked at again, next round
            with self.assertRaises(SignedInWait) as waiting:
                begin_signed_in_read(self.root, "indiamart")
            self.assertEqual(str(waiting.exception), share.READS_USED)
        # A new round has a new share.
        with share.signed_in_round(settings=SETTINGS):
            self.assertIsNone(signed.signed_in_wait(self.root, "indiamart"))

    def test_the_round_time_must_cover_a_reads_worst_case(self):
        self.clock = [0.0]
        worst = signed_in_budget()
        with patch.object(signed.time, "monotonic", side_effect=lambda: self.clock[0]), \
                share.signed_in_round(settings=SETTINGS) as current:
            self.read("google-trends", browser=self.browser(300 - worst))
            self.assertAlmostEqual(current["seconds"], 300 - worst)
            self.assertIsNone(share.round_wait("google-trends"))  # exactly one worst case left
            # A read that never started uses no share.
            self.read("dropdash", browser=self.browser(20, started=False))
            self.assertEqual(current["reads"], {"google-trends": 1})
            self.read("dropdash", browser=self.browser(1))
            for site in ("google-trends", "dropdash", "indiamart"):
                self.assertEqual(signed.signed_in_wait(self.root, site), share.TIME_USED)
            self.assertEqual(self.read("google-trends", browser=self.browser())["reason"], share.TIME_USED)
            self.assertEqual(len(self.calls), 3)

    def test_an_interrupted_read_is_still_charged(self):
        self.clock = [0.0]
        def broken(url, expression, folder, **options):
            self.clock[0] += 40
            raise OSError("pipe closed")
        with patch.object(signed.time, "monotonic", side_effect=lambda: self.clock[0]), \
                share.signed_in_round(settings=SETTINGS) as current:
            with self.assertRaises(OSError):
                self.read(browser=broken)
            self.assertEqual((current["seconds"], current["reads"]), (40.0, {"indiamart": 1}))

    def test_round_waits_are_calm_and_settings_come_from_the_pacing_file(self):
        self.assertIn(share.TIME_USED, CALM_CODES)
        self.assertIn(share.READS_USED, CALM_CODES)
        with share.signed_in_round() as current:
            with share.signed_in_round() as nested:  # a nested round keeps the outer share
                self.assertIs(nested, current)
            self.assertIsNone(current["settings"])
            self.assertIsNone(share.round_wait("indiamart"))
            self.assertEqual(current["settings"], SETTINGS)
        with share.signed_in_round(), \
                patch("product_radar.signed_in_pacing.round_settings", side_effect=SafetyError("SIGNED_IN_PACING_SETTINGS_INVALID")):
            # Damaged settings fail the signed-in read itself (not a calm wait), never the round.
            with self.assertRaisesRegex(SafetyError, "SIGNED_IN_PACING_SETTINGS_INVALID"):
                signed.signed_in_wait(self.root, "indiamart")


if __name__ == "__main__":
    unittest.main()
