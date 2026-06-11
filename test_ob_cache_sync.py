import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.ob_cache_sync import (
    DEFAULT_OB_CACHE_TTL_HOURS,
    default_ob_cache_dir,
    default_pts_clone_dir,
    ensure_fallback_buckets,
    ensure_pts_clone,
    load_ob_cache_index,
    lookup_ob_entry_with_fallback,
    ob_cache_ttl_seconds,
    project_root,
    sync_ob_cache,
    _is_cache_fresh,
    _pick_version_fallback_entry,
    _try_live_ob_lookup,
)
from app.pts_comparison import generate_comparison_hash, strip_test_profile_identifier, test_profile_family


class ObCachePathTest(unittest.TestCase):
    def test_pts_user_path_override_has_trailing_slash(self):
        from app.ob_cache_sync import pts_user_path_override_value

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["BENCHVIZ_PTS_USER_PATH"] = tmp
            try:
                val = pts_user_path_override_value()
                self.assertTrue(val.endswith("/"))
                self.assertTrue(os.path.isdir(val.rstrip("/")))
            finally:
                del os.environ["BENCHVIZ_PTS_USER_PATH"]

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
            fetch_cmd = mock_run.call_args_list[0][0][0]
            self.assertEqual(fetch_cmd[0], "git")
            self.assertEqual(fetch_cmd[1], "-c")
            self.assertIn("safe.directory=", fetch_cmd[2])
            self.assertEqual(fetch_cmd[3], "fetch")
            reset_cmd = mock_run.call_args_list[1][0][0]
            self.assertEqual(reset_cmd[3], "reset")

    @mock.patch("app.ob_cache_sync._fresh_pts_clone")
    @mock.patch("app.ob_cache_sync._run")
    def test_reclones_when_fetch_fails(self, mock_run, mock_fresh):
        with tempfile.TemporaryDirectory() as tmp:
            clone = Path(tmp) / "pts"
            clone.mkdir()
            (clone / ".git").mkdir()
            mock_run.side_effect = RuntimeError("fetch failed")
            meta = ensure_pts_clone(clone, branch="master")
            self.assertEqual(meta["action"], "recloned")
            mock_fresh.assert_called_once_with(clone, "master")

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


class ObCacheVersionFallbackTest(unittest.TestCase):
    def setUp(self):
        self._live_fetch_prev = os.environ.get("BENCHVIZ_OB_LIVE_FETCH")
        os.environ["BENCHVIZ_OB_LIVE_FETCH"] = "0"

    def tearDown(self):
        if self._live_fetch_prev is None:
            os.environ.pop("BENCHVIZ_OB_LIVE_FETCH", None)
        else:
            os.environ["BENCHVIZ_OB_LIVE_FETCH"] = self._live_fetch_prev

    def test_strip_keeps_two_part_profile_version(self):
        self.assertEqual(
            strip_test_profile_identifier("pts/build-linux-kernel-1.17"),
            "pts/build-linux-kernel-1.17",
        )
        self.assertEqual(
            strip_test_profile_identifier("pts/build-linux-kernel-1.17.1"),
            "pts/build-linux-kernel-1.17",
        )

    def test_test_profile_family_strips_version(self):
        self.assertEqual(test_profile_family("pts/compress-7zip-1.10.0"), "pts/compress-7zip")

    def test_fallback_to_older_app_version_when_exact_hash_missing(self):
        idx_path = project_root() / "instance" / "ob_cache_index.json"
        if not idx_path.is_file():
            self.skipTest("ob_cache_index.json not present")
        index = load_ob_cache_index(idx_path)
        self.assertIsNotNone(index)

        known_hash = generate_comparison_hash(
            "pts/compress-7zip-1.10",
            "",
            "Test: Compression Rating",
            "22.01",
            "MIPS",
        )
        self.assertIn(known_hash, (index or {}).get("entries", {}))

        missing_hash = generate_comparison_hash(
            "pts/compress-7zip-1.10",
            "",
            "Test: Compression Rating",
            "24.0",
            "MIPS",
        )
        self.assertNotIn(missing_hash, (index or {}).get("entries", {}))

        entry, source = lookup_ob_entry_with_fallback(
            missing_hash,
            index,
            identifier="pts/compress-7zip-1.10.0",
            title="7-Zip Compression",
            arguments="",
            description="Test: Compression Rating",
            app_version="24.0",
            scale="MIPS",
        )
        self.assertEqual(source, "fallback")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.get("app_version"), "22.01")
        self.assertEqual(entry.get("unit"), "MIPS")

    def test_build_linux_kernel_1_17_falls_back_to_1_16(self):
        idx_path = project_root() / "instance" / "ob_cache_index.json"
        if not idx_path.is_file():
            self.skipTest("ob_cache_index.json not present")

        index = load_ob_cache_index(idx_path)
        self.assertIsNotNone(index)

        missing_hash = generate_comparison_hash(
            "pts/build-linux-kernel-1.17",
            "defconfig",
            "Build: defconfig",
            "6.11",
            "Seconds",
        )
        entry, source = lookup_ob_entry_with_fallback(
            missing_hash,
            index,
            identifier="pts/build-linux-kernel-1.17",
            title="Timed Linux Kernel Compilation",
            arguments="defconfig",
            description="Build: defconfig",
            app_version="6.11",
            scale="Seconds",
        )
        self.assertEqual(source, "fallback")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.get("test_profile"), "pts/build-linux-kernel-1.16.0")
        self.assertEqual(entry.get("app_version"), "6.8")

    def test_no_fallback_when_description_differs(self):
        index = {
            "entries": {
                "abc": {
                    "test_profile": "pts/example-1.0",
                    "description": "Other",
                    "unit": "MIPS",
                    "app_version": "1.0",
                    "samples": 10,
                    "percentiles": [0] * 51,
                    "ob_median": 100.0,
                },
            },
        }
        ensure_fallback_buckets(index)
        entry, source = lookup_ob_entry_with_fallback(
            "missing",
            index,
            identifier="pts/example-1.0",
            arguments="",
            description="Test: Compression Rating",
            app_version="2.0",
            scale="MIPS",
        )
        self.assertEqual(source, "")
        self.assertIsNone(entry)


class ObCacheLookupOrderTest(unittest.TestCase):
    @mock.patch("app.ob_cache_sync._try_live_ob_lookup")
    @mock.patch("app.ob_cache_sync._try_local_disk_exact")
    @mock.patch("app.ob_cache_sync.lookup_ob_entry")
    def test_disk_before_live_when_live_allowed(self, mock_lookup, mock_disk, mock_live):
        index = {"entries": {}, "fallback_buckets": {}}
        mock_lookup.return_value = None
        mock_disk.return_value = {"app_version": "6.8", "samples": 1}
        mock_live.return_value = (None, "")

        with mock.patch("app.ob_cache_sync.live_ob_fetch_enabled", return_value=True):
            entry, source = lookup_ob_entry_with_fallback(
                "abc",
                index,
                identifier="pts/build-linux-kernel-1.17",
                description="Build: defconfig",
                scale="Seconds",
                allow_live=True,
            )
        self.assertEqual(source, "local")
        self.assertEqual(entry["app_version"], "6.8")
        mock_live.assert_not_called()

    @mock.patch("app.ob_cache_sync._try_live_ob_lookup")
    @mock.patch("app.ob_cache_sync.lookup_ob_entry")
    def test_index_hit_skips_live_on_compare(self, mock_lookup, mock_live):
        index = {"entries": {"abc": {"app_version": "1.0", "test_profile": "pts/foo-1.0"}}, "fallback_buckets": {}}
        mock_lookup.return_value = {"app_version": "1.0"}

        entry, source = lookup_ob_entry_with_fallback(
            "abc", index, identifier="pts/foo-1.0", allow_live=False,
        )
        self.assertEqual(source, "local")
        mock_live.assert_not_called()


class ObCacheFallbackPickerTest(unittest.TestCase):
    def test_prefers_newer_cached_test_profile(self):
        older = ({
            "test_profile": "pts/build-linux-kernel-1.16.0",
            "app_version": "6.8",
            "samples": 9000,
        }, "hash_old")
        newer = ({
            "test_profile": "pts/build-linux-kernel-1.17.1",
            "app_version": "6.9",
            "samples": 100,
        }, "hash_new")
        picked = _pick_version_fallback_entry([older, newer], "6.11")
        self.assertEqual(picked[0]["test_profile"], "pts/build-linux-kernel-1.17.1")


class ObCacheLiveCacheTest(unittest.TestCase):
    @mock.patch("app.ob_cache_sync.save_ob_cache_index")
    @mock.patch("app.ob_cache_sync.run_pts_fetch_test_profile")
    @mock.patch("app.ob_cache_sync.list_test_profiles_for_identifier")
    def test_downloads_are_cached_even_without_exact_hash(self, mock_profiles, mock_fetch, mock_save):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "ob-cache"
            profiles = ["pts/example-1.0"]
            mock_profiles.return_value = profiles
            gen_src = Path(tmp) / "pts-user" / "test-profiles" / "pts/example-1.0" / "generated.json"
            gen_src.parent.mkdir(parents=True)
            gen_src.write_text(
                json.dumps({
                    "overview": {
                        "otherhash": {
                            "description": "Build: defconfig",
                            "unit": "Seconds",
                            "hib": 1,
                            "samples": 10,
                            "percentiles": [0] * 51,
                            "app_version": "1.0",
                        },
                    },
                }),
                encoding="utf-8",
            )
            mock_fetch.return_value = {"ok": True, "generated_json": str(gen_src)}

            index = {"entries": {}, "fallback_buckets": {}}
            with mock.patch("app.ob_cache_sync.default_ob_cache_dir", return_value=cache):
                ent, source = _try_live_ob_lookup(
                    "missing-hash",
                    index,
                    identifier="pts/example-1.0",
                )

            self.assertIsNone(ent)
            self.assertEqual(source, "")
            self.assertTrue((cache / "test-profiles" / "pts/example-1.0" / "generated.json").is_file())
            mock_save.assert_called()
            self.assertIn("otherhash", index.get("entries", {}))


class ObCacheStaleRefreshTest(unittest.TestCase):
    def test_default_ttl_is_one_week(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("BENCHVIZ_OB_CACHE_TTL_HOURS", None)
            self.assertEqual(ob_cache_ttl_seconds(), DEFAULT_OB_CACHE_TTL_HOURS * 3600)

    def test_stale_cache_triggers_live_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "ob-cache"
            gen_cached = cache / "test-profiles" / "pts/example-1.0" / "generated.json"
            gen_cached.parent.mkdir(parents=True)
            gen_cached.write_text('{"overview": {}}', encoding="utf-8")
            old = time.time() - (DEFAULT_OB_CACHE_TTL_HOURS + 1) * 3600
            os.utime(gen_cached, (old, old))
            self.assertFalse(_is_cache_fresh(gen_cached))

            with mock.patch("app.ob_cache_sync.save_ob_cache_index"):
                with mock.patch("app.ob_cache_sync.run_pts_fetch_test_profile") as mock_fetch:
                    mock_fetch.return_value = {"ok": False}
                    with mock.patch("app.ob_cache_sync.default_ob_cache_dir", return_value=cache):
                        with mock.patch(
                            "app.ob_cache_sync.list_test_profiles_for_identifier",
                            return_value=["pts/example-1.0"],
                        ):
                            index = {"entries": {}, "fallback_buckets": {}}
                            _try_live_ob_lookup("hash", index, identifier="pts/example-1.0")
                    mock_fetch.assert_called_once_with("pts/example-1.0")

    @mock.patch("app.ob_cache_sync._try_live_ob_lookup")
    @mock.patch("app.ob_cache_sync._try_local_disk_exact", return_value=None)
    @mock.patch("app.ob_cache_sync._ingest_cached_profiles_for_identifier", return_value=0)
    @mock.patch("app.ob_cache_sync._index_entry_cache_fresh", return_value=False)
    @mock.patch("app.ob_cache_sync.lookup_ob_entry")
    def test_stale_index_entry_triggers_live(
        self, mock_lookup, _mock_fresh, _mock_ingest, _mock_disk, mock_live,
    ):
        index = {"entries": {"abc": {"test_profile": "pts/foo-1.0"}}, "fallback_buckets": {}}
        mock_lookup.return_value = {"app_version": "1.0", "test_profile": "pts/foo-1.0"}
        mock_live.return_value = (None, "")

        with mock.patch("app.ob_cache_sync.live_ob_fetch_enabled", return_value=True):
            lookup_ob_entry_with_fallback(
                "abc",
                index,
                identifier="pts/foo-1.0",
                description="Test",
                scale="Score",
                allow_live=True,
            )
        mock_live.assert_called_once()


if __name__ == "__main__":
    unittest.main()
