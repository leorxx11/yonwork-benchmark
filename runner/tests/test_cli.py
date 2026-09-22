from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from runner.__main__ import main, EXIT_USAGE
from runner.job_store import WorkerAlreadyRunning
from runner.db import DatabaseError
from runner.tests.test_batch import _FakeDriver


class CliLockTests(unittest.TestCase):
    def test_busy_or_unavailable_lock_prevents_any_turn(self):
        for error in (WorkerAlreadyRunning('busy'), DatabaseError('offline')):
            with self.subTest(error=type(error).__name__), TemporaryDirectory() as directory:
                driver = _FakeDriver(['ok'])
                with patch('runner.__main__.build_driver', return_value=driver), \
                     patch('runner.__main__.exclusive_worker_lock', side_effect=error), \
                     patch.object(driver, 'close') as close:
                    code = main(['--out-dir', directory, '--no-xlsx', '--no-usage'])
                self.assertEqual(EXIT_USAGE, code)
                self.assertEqual([], driver.session_keys)
                self.assertEqual([], list(Path(directory).iterdir()))
                close.assert_called_once()

    def test_lock_covers_turn_and_is_released_afterward(self):
        held = []
        @contextmanager
        def lock():
            held.append(True)
            try:
                yield 'lock'
            finally:
                held.pop()
        driver = _FakeDriver(['ok'])
        original = driver.run_turn
        def guarded(**kwargs):
            self.assertEqual([True], held)
            return original(**kwargs)
        with TemporaryDirectory() as directory, \
             patch('runner.__main__.build_driver', return_value=driver), \
             patch('runner.__main__.exclusive_worker_lock', side_effect=lock), \
             patch.object(driver, 'run_turn', side_effect=guarded):
            self.assertEqual(0, main(['--out-dir', directory, '--no-xlsx', '--no-usage']))
        self.assertEqual([], held)

    def test_dry_run_does_not_need_lock_or_database(self):
        driver = _FakeDriver([])
        with patch('runner.__main__.build_driver', return_value=driver), \
             patch('runner.__main__.exclusive_worker_lock') as lock:
            self.assertEqual(0, main(['--dry-run']))
        lock.assert_not_called()
