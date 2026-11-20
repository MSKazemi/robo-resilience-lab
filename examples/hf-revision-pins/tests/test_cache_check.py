"""Offline cache resolver and checker: layout, cache hit, absent objects, tampering, fail-closed."""

import os
import shutil
import socket
import unittest
from unittest import mock

from tests import support as s
from tests.support import TempCache, valid

import hf_cache as c

OTHER_REV = "f" * 40


class LayoutTests(unittest.TestCase):
    def test_repo_folder_names_match_huggingface_hub_convention(self):
        self.assertEqual(c.repo_folder_name("org/name", "model"), "models--org--name")
        self.assertEqual(c.repo_folder_name("org/name", "dataset"), "datasets--org--name")
        self.assertEqual(c.repo_folder_name("gpt2", "model"), "models--gpt2")

    def test_resolve_pin_paths(self):
        doc = valid()
        r = c.resolve_pin(doc["pins"][1], "/cache")
        rev = doc["pins"][1]["revision"]
        self.assertEqual(r.repo_dir, os.path.join("/cache", "datasets--rrl-synthetic--tiny-episodes-fixture"))
        self.assertEqual(r.snapshot_dir, os.path.join(r.repo_dir, "snapshots", rev))
        self.assertEqual(dict(r.files)["data/episodes.jsonl"], os.path.join(r.snapshot_dir, "data", "episodes.jsonl"))

    def test_cache_dir_precedence(self):
        home = os.path.expanduser("~")
        cases = [
            ({}, os.path.join(home, ".cache", "huggingface", "hub")),
            ({"XDG_CACHE_HOME": "/x"}, "/x/huggingface/hub"),
            ({"XDG_CACHE_HOME": "/x", "HF_HOME": "/h"}, "/h/hub"),
            ({"HF_HOME": "/h", "HUGGINGFACE_HUB_CACHE": "/legacy"}, "/legacy"),
            ({"HF_HOME": "/h", "HUGGINGFACE_HUB_CACHE": "/legacy", "HF_HUB_CACHE": "/hub"}, "/hub"),
        ]
        for env, want in cases:
            with self.subTest(env=env):
                self.assertEqual(c.resolve_cache_dir(env=env), os.path.abspath(want))
        self.assertEqual(c.resolve_cache_dir("/explicit", env={"HF_HUB_CACHE": "/hub"}), os.path.abspath("/explicit"))

    def test_offline_env_values(self):
        for v in ("1", "true", "TRUE", "yes", "on"):
            self.assertTrue(c.offline_env_enabled({"HF_HUB_OFFLINE": v}), v)
        for v in ("0", "false", "", "off", "2"):
            self.assertFalse(c.offline_env_enabled({"HF_HUB_OFFLINE": v}), v)
        self.assertFalse(c.offline_env_enabled({}))
        self.assertTrue(c.offline_env_enabled({"TRANSFORMERS_OFFLINE": "1"}))


class CacheTestCase(unittest.TestCase):
    copy_files = False

    def setUp(self):
        self.tc = TempCache(copy_files=self.copy_files)
        self.addCleanup(self.tc.close)

    def assertOnly(self, report, codes):
        self.assertEqual(sorted(report.codes), sorted(codes), [f.render() for f in report.findings])
        self.assertFalse(report.ok)


class CacheHitTests(CacheTestCase):
    def test_seeded_cache_verifies(self):
        r = self.tc.check()
        self.assertTrue(r.ok, [f.render() for f in r.findings])
        self.assertEqual(r.findings, [])
        self.assertEqual(r.verified_files, 4)
        self.assertEqual(r.pins_checked, 2)

    def test_seeded_layout_uses_blobs_and_relative_symlinks(self):
        if not s.symlinks_supported():
            self.skipTest("symlinks unavailable")
        r = self.tc.resolved(0)
        _rel, path = r.files[0]
        self.assertTrue(os.path.islink(path))
        self.assertFalse(os.path.isabs(os.readlink(path)))
        self.assertEqual(os.path.dirname(os.path.realpath(path)), os.path.realpath(os.path.join(r.repo_dir, "blobs")))
        with open(os.path.join(r.repo_dir, "refs", "main"), encoding="ascii") as fh:
            self.assertEqual(fh.read(), r.revision)

    def test_refs_are_ignored(self):
        r = self.tc.resolved(0)
        with open(os.path.join(r.repo_dir, "refs", "main"), "w", encoding="ascii") as fh:
            fh.write(OTHER_REV)
        self.assertTrue(self.tc.check().ok)

    def test_pin_without_files_is_a_warning_only(self):
        doc = s.load(s.NO_HASHES)
        r = self.tc.check(doc)
        self.assertTrue(r.ok)
        self.assertEqual(r.codes, ["UNVERIFIED_NO_FILES"] * 2)
        self.assertTrue(all(f.warning for f in r.findings))

    def test_offline_env_gate(self):
        self.assertOnly(self.tc.check(require_offline_env=True, env={}), ["OFFLINE_ENV_NOT_SET"])
        self.assertTrue(self.tc.check(require_offline_env=True, env={"HF_HUB_OFFLINE": "1"}).ok)


class CopyModeTests(CacheHitTests):
    copy_files = True

    def test_seeded_layout_uses_blobs_and_relative_symlinks(self):
        r = self.tc.resolved(0)
        self.assertFalse(os.path.islink(r.files[0][1]))


class AbsentObjectTests(CacheTestCase):
    def test_missing_cache_dir(self):
        r = c.check_document(valid(), os.path.join(self.tc.tmp, "nope"))
        self.assertOnly(r, ["CACHE_DIR_MISSING"])
        self.assertEqual(r.pins_checked, 0)

    def test_empty_cache(self):
        empty = os.path.join(self.tc.tmp, "empty")
        os.makedirs(empty)
        self.assertOnly(c.check_document(valid(), empty), ["CACHE_REPO_MISSING"] * 2)

    def test_wrong_kind_is_a_missing_repo(self):
        doc = valid()
        doc["pins"][0]["kind"] = "dataset"
        self.assertOnly(self.tc.check(doc), ["CACHE_REPO_MISSING"])

    def test_revision_mismatch(self):
        r = self.tc.resolved(0)
        os.rename(r.snapshot_dir, os.path.join(r.repo_dir, "snapshots", OTHER_REV))
        rep = self.tc.check()
        self.assertOnly(rep, ["CACHE_REVISION_MISMATCH"])
        self.assertIn(OTHER_REV[:12], rep.findings[0].message)

    def test_revision_missing(self):
        r = self.tc.resolved(1)
        shutil.rmtree(os.path.join(r.repo_dir, "snapshots"))
        self.assertOnly(self.tc.check(), ["CACHE_REVISION_MISSING"])

    def test_refs_main_cannot_stand_in_for_a_missing_snapshot(self):
        r = self.tc.resolved(0)
        os.rename(r.snapshot_dir, os.path.join(r.repo_dir, "snapshots", OTHER_REV))
        with open(os.path.join(r.repo_dir, "refs", "main"), "w", encoding="ascii") as fh:
            fh.write(r.revision)
        self.assertOnly(self.tc.check(), ["CACHE_REVISION_MISMATCH"])

    def test_missing_file(self):
        rel, path = self.tc.resolved(1).files[0]
        os.remove(path)
        rep = self.tc.check()
        self.assertOnly(rep, ["CACHE_FILE_MISSING"])
        self.assertEqual(rep.findings[0].path, rel)
        self.assertEqual(rep.verified_files, 3)

    def test_dangling_symlink_is_missing(self):
        if not s.symlinks_supported():
            self.skipTest("symlinks unavailable")
        _rel, path = self.tc.resolved(0).files[0]
        os.remove(os.path.realpath(path))
        self.assertOnly(self.tc.check(), ["CACHE_FILE_MISSING"])

    def test_directory_instead_of_file(self):
        _rel, path = self.tc.resolved(0).files[1]
        os.remove(path)
        os.makedirs(path)
        self.assertOnly(self.tc.check(), ["CACHE_NOT_REGULAR_FILE"])


class TamperTests(CacheTestCase):
    def _blob(self, index=0, file_index=0):
        return os.path.realpath(self.tc.resolved(index).files[file_index][1])

    def test_appended_byte_is_a_size_mismatch(self):
        with open(self._blob(), "ab") as fh:
            fh.write(b"x")
        self.assertOnly(self.tc.check(), ["CACHE_SIZE_MISMATCH"])

    def test_same_size_tamper_is_a_sha256_mismatch(self):
        path = self._blob(1, 1)
        with open(path, "rb") as fh:
            data = bytearray(fh.read())
        data[0] ^= 0x01
        with open(path, "wb") as fh:
            fh.write(bytes(data))
        rep = self.tc.check()
        self.assertOnly(rep, ["CACHE_SHA256_MISMATCH"])
        self.assertEqual(rep.findings[0].pin_id, "tiny-episodes")

    def test_tamper_without_pinned_size_is_a_sha256_mismatch(self):
        doc = valid()
        for pin in doc["pins"]:
            for f in pin["files"]:
                del f["size"]
        with open(self._blob(), "ab") as fh:
            fh.write(b"x")
        self.assertOnly(self.tc.check(doc), ["CACHE_SHA256_MISMATCH"])

    def test_wrong_pinned_hash_fails(self):
        doc = valid()
        doc["pins"][0]["files"][0]["sha256"] = "0" * 64
        self.assertOnly(self.tc.check(doc), ["CACHE_SHA256_MISMATCH"])

    def test_symlink_escaping_the_repo_folder_is_refused(self):
        if not s.symlinks_supported():
            self.skipTest("symlinks unavailable")
        _rel, path = self.tc.resolved(0).files[0]
        outside = os.path.join(self.tc.tmp, "outside-copy")
        shutil.copyfile(os.path.realpath(path), outside)  # identical bytes: only the location is wrong
        os.remove(path)
        os.symlink(outside, path)
        self.assertOnly(self.tc.check(), ["CACHE_PATH_ESCAPE"])

    def test_unreadable_file_fails_closed(self):
        with mock.patch.object(c, "sha256_file", side_effect=PermissionError(13, "Permission denied")):
            rep = self.tc.check()
        self.assertOnly(rep, ["CACHE_READ_ERROR"] * 4)


class FailClosedTests(CacheTestCase):
    def test_invalid_pin_file_never_inspects_the_cache(self):
        doc = valid()
        doc["pins"][0]["revision"] = "main"
        rep = self.tc.check(doc)
        self.assertOnly(rep, ["PIN_INVALID"])
        self.assertEqual(rep.pins_checked, 0)
        self.assertIn("REVISION_BRANCH", rep.findings[0].message)

    def test_release_mode_rejects_synthetic(self):
        self.assertOnly(self.tc.check(release=True), ["PIN_INVALID"])

    def test_internal_error_fails_closed(self):
        with mock.patch.object(c, "_check_pin", side_effect=RuntimeError("boom")):
            rep = self.tc.check()
        self.assertFalse(rep.ok)

    def test_no_network_is_attempted(self):
        def refuse(*_a, **_k):
            raise AssertionError("network access attempted")

        with mock.patch.object(socket, "socket", side_effect=refuse), \
                mock.patch.object(socket, "create_connection", side_effect=refuse), \
                mock.patch.object(socket, "getaddrinfo", side_effect=refuse):
            other = TempCache()
            try:
                self.assertTrue(other.check().ok)
            finally:
                other.close()

    def test_sources_do_not_import_network_or_hub_modules(self):
        banned = ("import socket", "import urllib", "from urllib", "import http", "from http", "import requests",
                  "import httpx", "huggingface_hub import", "import huggingface_hub", "import ssl")
        for name in ("hf_pins.py", "hf_cache.py", "hfpins.py", "fixture_gen.py"):
            with open(os.path.join(s.ROOT, name), encoding="utf-8") as fh:
                text = fh.read()
            for b in banned:
                with self.subTest(file=name, pattern=b):
                    self.assertNotIn(b, text)


class SeedTests(unittest.TestCase):
    def setUp(self):
        self.tc = TempCache(seed=False)
        self.addCleanup(self.tc.close)

    def test_refuses_real_pin_files(self):
        with self.assertRaisesRegex(c.SeedError, "synthetic"):
            c.seed_synthetic_cache(s.as_real(valid()), s.SOURCES, self.tc.cache)
        self.assertEqual(os.listdir(self.tc.cache), [])

    def test_refuses_invalid_pin_files(self):
        doc = valid()
        doc["pins"][0]["revision"] = "main"
        with self.assertRaisesRegex(c.SeedError, "invalid"):
            c.seed_synthetic_cache(doc, s.SOURCES, self.tc.cache)

    def test_refuses_to_overwrite(self):
        c.seed_synthetic_cache(valid(), s.SOURCES, self.tc.cache)
        with self.assertRaisesRegex(c.SeedError, "already exists"):
            c.seed_synthetic_cache(valid(), s.SOURCES, self.tc.cache)

    def test_refuses_tampered_or_missing_sources(self):
        src = os.path.join(self.tc.tmp, "src")
        shutil.copytree(s.SOURCES, src)
        with open(os.path.join(src, "tiny-episodes", "meta.json"), "ab") as fh:
            fh.write(b" ")
        with self.assertRaisesRegex(c.SeedError, "does not match"):
            c.seed_synthetic_cache(valid(), src, self.tc.cache)
        os.remove(os.path.join(src, "tiny-episodes", "meta.json"))
        with self.assertRaisesRegex(c.SeedError, "missing fixture source"):
            c.seed_synthetic_cache(valid(), src, self.tc.cache)
        self.assertEqual(os.listdir(self.tc.cache), [], "nothing is written when validation of sources fails")


if __name__ == "__main__":
    unittest.main()
