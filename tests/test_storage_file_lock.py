"""The one shared local record lock (storage.file_lock).

Temporary folders only; nothing outside the test folder is locked.
"""

from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.models import SafetyError
from product_radar.storage import file_lock


class FileLockTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="radar-file-lock-"); self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def test_one_holder_at_a_time_with_the_callers_own_busy_code(self):
        path = self.root / "records" / "named.lock"
        with file_lock(path, 0, "NAMED_RECORD_BUSY"):
            self.assertTrue(path.is_file())  # its folder is made on first use
            with self.assertRaisesRegex(SafetyError, "^NAMED_RECORD_BUSY$"):
                with file_lock(path, 0, "NAMED_RECORD_BUSY"):
                    pass
            started = time.monotonic()
            with self.assertRaisesRegex(SafetyError, "^OTHER_CODE$"):
                with file_lock(path, .2, "OTHER_CODE"):
                    pass
            self.assertGreaterEqual(time.monotonic() - started, .2)  # waited its allowance, no longer
        with file_lock(path, 0, "NAMED_RECORD_BUSY"):
            pass  # released when the holder finished

    def test_released_when_the_holder_fails(self):
        path = self.root / "named.lock"
        with self.assertRaisesRegex(RuntimeError, "inside"):
            with file_lock(path, 0, "NAMED_RECORD_BUSY"):
                raise RuntimeError("inside")
        with file_lock(path, 0, "NAMED_RECORD_BUSY"):
            pass

    def test_linked_lock_files_and_folders_are_refused(self):
        target = self.root / "elsewhere.lock"; target.write_text("")
        (self.root / "linked.lock").symlink_to(target)
        with self.assertRaisesRegex(SafetyError, "SYMLINK_PATH_REFUSED"):
            with file_lock(self.root / "linked.lock", 0, "NAMED_RECORD_BUSY"):
                pass
        (self.root / "real").mkdir(); (self.root / "linked").symlink_to(self.root / "real")
        with self.assertRaisesRegex(SafetyError, "SYMLINK_PATH_REFUSED"):
            with file_lock(self.root / "linked" / "named.lock", 0, "NAMED_RECORD_BUSY"):
                pass
        self.assertEqual(list((self.root / "real").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
