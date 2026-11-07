"""Local cleanup regression checks; fixtures stay under repository data/temp/.

Run: PYTHONDONTWRITEBYTECODE=1 python3.11 -B -m unittest discover -s tests \
    -p 'test_data_cleanup_migration.py' -v
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import data_cleanup as cleanup


class DataCleanupTests(unittest.TestCase):
    def setUp(self):
        temp_root = ROOT / 'data/temp'
        temp_root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='data-cleanup-', dir=temp_root)
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.data = self.workspace / 'data'
        self.data.mkdir()
        patched = patch.multiple(
            cleanup,
            DATA_DIR=self.data,
            EVENTS_FILE=self.data / 'events.jsonl',
            SIGNALS_FILE=self.data / 'signals.jsonl',
            LOG_FILE=self.data / 'logs/cleanup.log',
        )
        patched.start()
        self.addCleanup(patched.stop)

    def write(self, relative, content=b'fixture', *, modified=1_000_000):
        path = self.data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        os.utime(path, (modified, modified))
        return path

    def test_default_limits_and_retired_ownership(self):
        self.assertEqual(cleanup.TEMP_MAX_BYTES, 1024 * 1024 * 1024)
        self.assertEqual(cleanup.TEMP_MAX_AGE, 24 * 3600)
        self.assertEqual(cleanup.ARTIFACT_MAX_AGE, 24 * 3600)
        self.assertEqual(cleanup.ATTACH_MAX_AGE, 24 * 3600)
        self.assertEqual(cleanup.EVENTS_BACKUPS, 3)
        self.assertEqual(cleanup.SIGNALS_BACKUPS, 2)
        for name in ('purge_expired_child_sessions', 'CHILD_SESSION_MAX_AGE',
                     'NODE_CONTEXTS_MAX_AGE', 'prune_cloud_logs', 'vacuum_zoaholic_stats_db'):
            self.assertFalse(hasattr(cleanup, name), name)

    def test_exact_cap_keeps_all_files(self):
        first = self.write('temp/first', b'1234')
        second = self.write('temp/second', b'123456')
        cleanup.enforce_dir_size_cap(self.data / 'temp', 10, 'temp')
        self.assertEqual(first.read_bytes(), b'1234')
        self.assertEqual(second.read_bytes(), b'123456')

    def test_over_cap_removes_oldest_and_empty_subdirectories(self):
        oldest = self.write('temp/nested/oldest', b'1234', modified=100)
        middle = self.write('temp/middle', b'123', modified=200)
        newest = self.write('temp/newest', b'1234', modified=300)
        cleanup.enforce_dir_size_cap(self.data / 'temp', 7, 'temp')
        self.assertFalse(oldest.exists())
        self.assertFalse(oldest.parent.exists())
        self.assertTrue((self.data / 'temp').is_dir())
        self.assertTrue(middle.is_file())
        self.assertTrue(newest.is_file())

    def test_missing_directory_is_not_created(self):
        missing = self.data / 'missing'
        cleanup.enforce_dir_size_cap(missing, 0, 'temp')
        self.assertFalse(missing.exists())

    def test_negative_cap_does_not_delete_files(self):
        path = self.write('temp/file', b'123')
        with self.assertRaisesRegex(ValueError, 'non-negative'):
            cleanup.enforce_dir_size_cap(path.parent, -1, 'temp')
        self.assertEqual(path.read_bytes(), b'123')

    def test_capacity_scan_does_not_follow_file_or_directory_links(self):
        internal = self.write('temp/internal', b'123')
        outside = self.workspace / 'outside'
        outside.mkdir()
        sentinel = outside / 'sentinel'
        sentinel.write_bytes(b'outside fixture')
        file_link = internal.parent / 'file-link'
        dir_link = internal.parent / 'directory-link'
        file_link.symlink_to(sentinel)
        dir_link.symlink_to(outside, target_is_directory=True)
        cleanup.enforce_dir_size_cap(internal.parent, 0, 'temp')
        self.assertFalse(internal.exists())
        self.assertEqual(sentinel.read_bytes(), b'outside fixture')
        self.assertTrue(file_link.is_symlink())
        self.assertTrue(dir_link.is_symlink())

    def test_symlink_root_and_symlink_parent_are_not_followed(self):
        outside = self.workspace / 'outside/nested'
        outside.mkdir(parents=True)
        sentinel = outside / 'sentinel'
        sentinel.write_bytes(b'outside fixture')
        link = self.data / 'temp'
        link.symlink_to(outside.parent, target_is_directory=True)
        cleanup.enforce_dir_size_cap(link, 0, 'temp')
        with self.assertLogs(level=logging.WARNING):
            cleanup.enforce_dir_size_cap(link / 'nested', 0, 'temp')
        self.assertEqual(sentinel.read_bytes(), b'outside fixture')

    def test_failed_unlink_is_not_counted_as_freed_capacity(self):
        blocked = self.write('temp/blocked', b'1234', modified=100)
        removable = self.write('temp/removable', b'123', modified=200)
        unlink = Path.unlink

        def remove(path, *args, **kwargs):
            if path == blocked:
                raise PermissionError('fixture denial')
            return unlink(path, *args, **kwargs)

        with patch.object(Path, 'unlink', remove), self.assertLogs(level=logging.INFO) as captured:
            cleanup.enforce_dir_size_cap(blocked.parent, 0, 'temp')
        self.assertEqual(blocked.read_bytes(), b'1234')
        self.assertFalse(removable.exists())
        self.assertIn('removed 1 files (3 bytes freed), remaining 4 bytes (cap 0)', captured.output[-1])
        self.assertEqual(captured.records[-1].levelno, logging.WARNING)

    def test_empty_directory_removal_failure_does_not_abort_capacity_cleanup(self):
        old = self.write('temp/nested/file', b'123')
        rmdir = Path.rmdir

        def remove(path):
            if path == old.parent:
                raise PermissionError('fixture denial')
            return rmdir(path)

        with patch.object(Path, 'rmdir', remove):
            cleanup.enforce_dir_size_cap(self.data / 'temp', 0, 'temp')
        self.assertFalse(old.exists())
        self.assertTrue(old.parent.is_dir())

    def test_local_rotation_preserves_bounded_backup_counts(self):
        for prefix, limit_name, rotate, backups in (
            ('events', 'EVENTS_MAX_BYTES', cleanup.rotate_events, 3),
            ('signals', 'SIGNALS_MAX_BYTES', cleanup.rotate_signals, 2),
        ):
            with self.subTest(prefix=prefix):
                current = self.write(f'{prefix}.jsonl', b'new!')
                for index in range(1, backups + 1):
                    self.write(f'{prefix}.jsonl.{index}', f'old{index}'.encode())
                with patch.object(cleanup, limit_name, 4):
                    rotate()
                self.assertFalse(current.exists())
                self.assertEqual((self.data / f'{prefix}.jsonl.1').read_bytes(), b'new!')
                for index in range(2, backups + 1):
                    self.assertEqual((self.data / f'{prefix}.jsonl.{index}').read_bytes(),
                                     f'old{index - 1}'.encode())
                self.assertEqual(len(list(self.data.glob(f'{prefix}.jsonl.*'))), backups)

    def test_rotation_below_limit_keeps_current_file(self):
        current = self.write('events.jsonl', b'123')
        with patch.object(cleanup, 'EVENTS_MAX_BYTES', 4):
            cleanup.rotate_events()
        self.assertEqual(current.read_bytes(), b'123')
        self.assertFalse((self.data / 'events.jsonl.1').exists())

    def test_rotation_failure_does_not_report_success(self):
        current = self.write('events.jsonl', b'1234')
        with patch.object(cleanup, 'EVENTS_MAX_BYTES', 4), \
                patch.object(Path, 'rename', side_effect=PermissionError('fixture denial')), \
                self.assertLogs(level=logging.INFO) as captured:
            with self.assertRaises(PermissionError):
                cleanup.rotate_events()
        self.assertEqual(current.read_bytes(), b'1234')
        self.assertFalse(any('[events] done' in message for message in captured.output))

    def test_main_preserves_sessions_and_snapshots_but_purges_old_artifacts(self):
        now = 1_000_000
        old = now - 72 * 3600
        protected = [
            self.write('conversations/child_active.jsonl', modified=old),
            self.write('conversations/child_accumulate.jsonl', modified=old),
            self.write('node_contexts/session/snapshot.json', modified=old),
        ]
        expired = [
            self.write('chat_dump_old.txt', modified=old),
            self.write('temp/nested/old', modified=old),
            self.write('temp_summary/old', modified=old),
            self.write('artifacts/old', modified=old),
            self.write('artifacts/tool_results/task/old', modified=old),
            self.write('attachments/session/old', modified=old),
        ]
        recent = self.write('attachments/session/recent', modified=now)
        with patch.object(cleanup, '_log_init'), patch.object(cleanup.time, 'time', return_value=now):
            cleanup.main()
        self.assertTrue(all(path.is_file() for path in protected))
        self.assertTrue(all(not path.exists() for path in expired))
        self.assertTrue(recent.is_file())

    def test_main_applies_size_cap_to_recent_temporary_files(self):
        oldest = self.write('temp/oldest', b'1234', modified=999_998)
        newest = self.write('temp/newest', b'1234', modified=999_999)
        with patch.object(cleanup, '_log_init'), patch.object(cleanup.time, 'time', return_value=1_000_000), \
                patch.object(cleanup, 'TEMP_MAX_BYTES', 4):
            cleanup.main()
        self.assertFalse(oldest.exists())
        self.assertTrue(newest.is_file())

    def test_main_does_not_purge_a_symlinked_temp_root(self):
        outside = self.workspace / 'outside'
        outside.mkdir()
        sentinel = outside / 'old'
        sentinel.write_bytes(b'outside fixture')
        os.utime(sentinel, (0, 0))
        (self.data / 'temp').symlink_to(outside, target_is_directory=True)
        with patch.object(cleanup, '_log_init'), patch.object(cleanup, 'TEMP_MAX_BYTES', 0):
            cleanup.main()
        self.assertEqual(sentinel.read_bytes(), b'outside fixture')


if __name__ == '__main__':
    unittest.main()
