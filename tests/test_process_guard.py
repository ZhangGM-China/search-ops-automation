import tempfile
import unittest
from pathlib import Path

from src.runtime.process_guard import ProcessLockError, acquire_process_lock


class TestProcessGuard(unittest.TestCase):
    def test_acquire_process_lock_blocks_duplicate_holder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_dir = Path(tmpdir)
            first_lock = acquire_process_lock("resume_2026_07_03", lock_dir=lock_dir)
            try:
                with self.assertRaises(ProcessLockError) as ctx:
                    acquire_process_lock("resume_2026_07_03", lock_dir=lock_dir)
            finally:
                first_lock.release()

        self.assertIn("resume_2026_07_03", str(ctx.exception))

    def test_release_process_lock_allows_reacquire(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_dir = Path(tmpdir)
            first_lock = acquire_process_lock("scheduler_main", lock_dir=lock_dir)
            first_lock.release()
            second_lock = acquire_process_lock("scheduler_main", lock_dir=lock_dir)
            second_lock.release()

