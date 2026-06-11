import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.ob_cache_sync import (
    default_ob_cache_dir,
    default_pts_clone_dir,
    ensure_pts_clone,
    project_root,
    sync_ob_cache,
)


class ObCachePathTest(unittest.TestCase):
    def test_default_pts_clone_under_instance(self):
        root = project_root()
        self.assertEqual(default_pts_clone_dir(root), root / "instance" / "phoronix-test-suite")

    def test_env_override_pts_clone_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["BENCHVIZ_PTS_CLONE_DIR"] = tmp
            try:
                self.assertEqual(default_pts_clone_dir(), Path(tmp))
            finally:
                del os.environ["BENCHVIZ_PTS_CLONE_DIR"]

    def test_default_ob_cache_dir(self):
        root = project_root()
        self.assertEqual(default_ob_cache_dir(root), root / "instance" / "ob-cache")


class EnsurePtsCloneTest(unittest.TestCase):
    @mock.patch("app.ob_cache_sync._run")
    def test_updates_existing_clone(self, mock_run):
        with tempfile.TemporaryDirectory() as tmp:
            clone = Path(tmp) / "pts"
            clone.mkdir()
            (clone / ".git").mkdir()
            meta = ensure_pts_clone(clone, branch="master")
            self.assertEqual(meta["action"], "updated")
            self.assertEqual(mock_run.call_count, 2)

    @mock.patch("app.ob_cache_sync._run")
    def test_clones_when_missing(self, mock_run):
        with tempfile.TemporaryDirectory() as tmp:
            clone = Path(tmp) / "pts"
            meta = ensure_pts_clone(clone, branch="master")
            self.assertEqual(meta["action"], "cloned")
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            self.assertEqual(args[0], "git")
            self.assertEqual(args[1], "clone")


class SyncObCacheLocalTest(unittest.TestCase):
    def test_copies_generated_json_from_local_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clone = root / "pts"
            profile = clone / "ob-cache" / "test-profiles" / "pts/example-1.0"
            profile.mkdir(parents=True)
            (profile / "generated.json").write_text(
                '{"overview": {"abc123": {"description": "Test", "hib": 1, "samples": 1, "percentiles": [0]*51}}}',
                encoding="utf-8",
            )
            dest = root / "instance" / "ob-cache"
            meta = sync_ob_cache(
                dest_dir=dest,
                source="local",
                local_path=clone,
                ensure_clone=False,
                run_pts_update=False,
            )
            self.assertEqual(meta["files_copied"], 1)
            self.assertTrue((dest / "test-profiles" / "pts/example-1.0" / "generated.json").is_file())


if __name__ == "__main__":
    unittest.main()
