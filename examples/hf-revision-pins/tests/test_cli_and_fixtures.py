"""CLI exit codes, secret hygiene, fixture drift and README/code agreement."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from tests import support as s

import fixture_gen as g
import hf_cache as c
import hf_pins as p
import hfpins

CLI = os.path.join(s.ROOT, "hfpins.py")


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = hfpins.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hfpins-cli-")
        self.cache = os.path.join(self.tmp, "hub")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def seed(self):
        rc, out, err = run("seed-synthetic", s.VALID, "--source", s.SOURCES, "--cache-dir", self.cache)
        self.assertEqual(rc, 0, err)
        self.assertIn("SYNTHETIC", out)

    def test_validate_exit_codes(self):
        self.assertEqual(run("validate", s.VALID, s.NO_HASHES)[0], 0)
        rc, out, _ = run("validate", s.VALID, os.path.join(s.INVALID_DIR, "mutable-main.json"))
        self.assertEqual(rc, 1)
        self.assertIn("[REVISION_BRANCH] /pins/0/revision", out)
        self.assertIn("1 valid, 1 invalid", out)
        rc, out, _ = run("validate", "--release", s.VALID)
        self.assertEqual(rc, 1)
        self.assertIn("RELEASE_SYNTHETIC", out)

    def test_usage_and_io_errors_are_exit_2(self):
        self.assertEqual(run()[0], 2)
        self.assertEqual(run("bogus")[0], 2)
        self.assertEqual(run("validate", os.path.join(self.tmp, "missing.json"))[0], 2)
        self.assertEqual(run("check", s.FIX)[0], 2)

    def test_list_codes_names_every_code(self):
        rc, out, _ = run("--list-codes")
        self.assertEqual(rc, 0)
        for code in list(p.CODES) + list(c.CHECK_CODES):
            self.assertIn(code, out)

    def test_seed_then_check(self):
        self.seed()
        rc, out, _ = run("check", s.VALID, "--cache-dir", self.cache)
        self.assertEqual(rc, 0, out)
        self.assertIn("4 file(s) verified by sha256, 0 error(s)", out)
        rc, out, _ = run("check", s.VALID, "--cache-dir", self.cache, "--release")
        self.assertEqual(rc, 1)
        self.assertIn("RELEASE_SYNTHETIC", out)

    def test_check_against_empty_cache_fails(self):
        os.makedirs(self.cache)
        rc, out, _ = run("check", s.VALID, "--cache-dir", self.cache)
        self.assertEqual(rc, 1)
        self.assertEqual(out.count("CACHE_REPO_MISSING"), 2)

    def test_check_refuses_invalid_pin_without_touching_cache(self):
        rc, out, _ = run("check", os.path.join(s.INVALID_DIR, "short-sha.json"), "--cache-dir",
                         os.path.join(self.tmp, "never-created"))
        self.assertEqual(rc, 1)
        self.assertIn("cache not inspected", out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "never-created")))

    def test_seed_refusal_is_exit_2(self):
        self.seed()
        rc, _out, err = run("seed-synthetic", s.VALID, "--source", s.SOURCES, "--cache-dir", self.cache)
        self.assertEqual(rc, 2)
        self.assertIn("already exists", err)

    def test_resolve_json(self):
        rc, out, _ = run("resolve", s.VALID, "--cache-dir", self.cache, "--json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["cache_dir"], os.path.abspath(self.cache))
        self.assertEqual([x["id"] for x in data["pins"]], ["tiny-policy", "tiny-episodes"])
        self.assertTrue(data["pins"][0]["snapshot_dir"].endswith(
            os.path.join("models--rrl-synthetic--tiny-policy-fixture", "snapshots", data["pins"][0]["revision"])))
        rc, out, _ = run("resolve", s.VALID, "--cache-dir", self.cache)
        self.assertEqual(rc, 0)
        self.assertIn("snapshot:", out)

    def test_subprocess_never_prints_token_values(self):
        self.seed()
        sentinel = "hf_SENTINEL_" + hashlib.sha256(b"not a token").hexdigest()[:20]
        env = dict(os.environ, HF_TOKEN=sentinel, HUGGING_FACE_HUB_TOKEN=sentinel, HF_HUB_OFFLINE="1")
        for argv in (["check", s.VALID, "--cache-dir", self.cache, "--require-offline-env"],
                     ["check", s.VALID, "--cache-dir", os.path.join(self.tmp, "nope")],
                     ["resolve", s.VALID, "--json"],
                     ["validate", os.path.join(s.INVALID_DIR, "tag-ref.json")]):
            proc = subprocess.run([sys.executable, "-I", CLI] + argv, capture_output=True, text=True, env=env,
                                  timeout=60, check=False)
            with self.subTest(argv=argv[0]):
                self.assertNotIn(sentinel, proc.stdout + proc.stderr)
                self.assertIn(proc.returncode, (0, 1))
        self.assertEqual(subprocess.run([sys.executable, "-I", CLI] + ["check", s.VALID, "--cache-dir", self.cache,
                                         "--require-offline-env"], capture_output=True, env=env, timeout=60,
                                        check=False).returncode, 0)

    def test_require_offline_env_via_cli(self):
        self.seed()
        env = {k: v for k, v in os.environ.items() if k not in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")}
        proc = subprocess.run([sys.executable, "-I", CLI, "check", s.VALID, "--cache-dir", self.cache,
                               "--require-offline-env"], capture_output=True, text=True, env=env, timeout=60,
                              check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("OFFLINE_ENV_NOT_SET", proc.stdout)


class FixtureTests(unittest.TestCase):
    def test_fixtures_have_no_drift(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(g.main(["--check"]), 0, out.getvalue())

    def test_fixture_hashes_match_source_files(self):
        doc = s.valid()
        for pin in doc["pins"]:
            for f in pin["files"]:
                path = os.path.join(s.SOURCES, pin["id"], *f["path"].split("/"))
                with open(path, "rb") as fh:
                    data = fh.read()
                self.assertEqual(hashlib.sha256(data).hexdigest(), f["sha256"], path)
                self.assertEqual(len(data), f["size"], path)

    def test_fixtures_are_labelled_synthetic(self):
        for name in ("synthetic-valid.json", "synthetic-no-hashes.json"):
            doc = s.load(os.path.join(s.PINS, name))
            self.assertEqual(doc["data_kind"], "synthetic")
            self.assertIn("SYNTHETIC", doc["description"])
            for pin in doc["pins"]:
                self.assertTrue(pin["repo_id"].startswith("rrl-synthetic/"))
                self.assertEqual(pin["revision"], g.fake_revision(pin["id"] + " r1"))
        for root, _dirs, files in os.walk(s.SOURCES):
            for name in files:
                with open(os.path.join(root, name), encoding="utf-8") as fh:
                    self.assertIn("synthetic", fh.read().lower(), name)


class ReadmeTests(unittest.TestCase):
    def test_readme_documents_every_code(self):
        with open(os.path.join(s.ROOT, "README.md"), encoding="utf-8") as fh:
            text = fh.read()
        for code in list(p.CODES) + list(c.CHECK_CODES):
            with self.subTest(code=code):
                self.assertIn("`%s`" % code, text)


if __name__ == "__main__":
    unittest.main()
