"""Exact-path filesystem retirement contracts."""
import tempfile
from pathlib import Path
import tarfile
import unittest
from app.retired_files import retire_files


class RetiredFilesTests(unittest.TestCase):
    def test_archive_then_remove_only_approved_worker_subtree(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / 'data'
            old = root / 'control-plane/snapshots/old'
            current = root / 'control-plane/shared-history/current'
            old.mkdir(parents=True); current.mkdir(parents=True)
            (old / 'settings').write_text('private')
            (current / 'event').write_text('retain')
            archive = base / 'retired.tar.gz'
            retire_files(root, [old], {'old'}, [], archive)
            self.assertFalse(old.exists())
            self.assertEqual((current / 'event').read_text(), 'retain')
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
            with tarfile.open(archive) as tar:
                self.assertEqual(tar.extractfile('control-plane/snapshots/old/settings').read(), b'private')

    def test_unknown_path_and_live_mount_are_refused(self):
        for mounted in (False, True):
            with self.subTest(mounted=mounted), tempfile.TemporaryDirectory() as directory:
                base = Path(directory).resolve(); root = base / 'data'
                path = root / 'control-plane/snapshots/old'
                path.mkdir(parents=True)
                with self.assertRaises(ValueError):
                    retire_files(root, [path], {'old'} if mounted else set(),
                                 [path] if mounted else [], base / 'backup.tar.gz')
                self.assertTrue(path.exists())

    def test_symlink_inside_retired_tree_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve(); root = base / 'data'
            path = root / 'control-plane/snapshots/old'; path.mkdir(parents=True)
            (path / 'escape').symlink_to(base)
            with self.assertRaises(ValueError):
                retire_files(root, [path], {'old'}, [], base / 'backup.tar.gz')
            self.assertTrue(path.exists())
