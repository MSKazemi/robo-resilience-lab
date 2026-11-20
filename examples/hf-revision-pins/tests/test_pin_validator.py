"""Pin-file validator: positive, negative, release-mode and hostile-input tests."""

import copy
import os
import unittest
from unittest import mock

from tests import support as s
from tests.support import codes, valid

import hf_pins as p

COMMIT = "0123456789abcdef0123456789abcdef01234567"


class PositiveTests(unittest.TestCase):
    def test_committed_valid_fixtures_validate(self):
        for path in (s.VALID, s.NO_HASHES):
            with self.subTest(path=path):
                r = p.validate_file(path)
                self.assertTrue(r.ok, [d.render() for d in r.diagnostics])

    def test_full_lowercase_commit_is_immutable(self):
        self.assertIsNone(p.classify_revision(COMMIT))

    def test_repo_id_forms_accepted(self):
        for rid in ("tiny", "org/tiny", "Org_1/Model.v2-small", "a/b", "x" * 96, "ns/" + "y" * 96):
            doc = valid()
            doc["pins"][0]["repo_id"] = rid
            with self.subTest(rid=rid):
                self.assertNotIn("REPO_ID", codes(doc))

    def test_optional_fields_may_be_omitted(self):
        doc = valid()
        for pin in doc["pins"]:
            pin.pop("gated", None)
            for f in pin["files"]:
                f.pop("size")
                f.pop("role")
        doc.pop("description")
        self.assertEqual(codes(doc), [])

    def test_real_pin_file_with_hashes_passes_release(self):
        self.assertEqual(codes(s.as_real(valid()), release=True), [])


class RevisionTests(unittest.TestCase):
    CASES = {
        "main": "REVISION_BRANCH",
        "master": "REVISION_BRANCH",
        "Main": "REVISION_BRANCH",
        "refs/heads/main": "REVISION_BRANCH",
        "refs/heads/feature-x": "REVISION_BRANCH",
        "refs/pr/1": "REVISION_PR_REF",
        "v1.0": "REVISION_NOT_COMMIT",
        "release-2024-01": "REVISION_NOT_COMMIT",
        "refs/tags/v1.0": "REVISION_NOT_COMMIT",
        COMMIT[:7]: "REVISION_SHORT_SHA",
        COMMIT[:12]: "REVISION_SHORT_SHA",
        COMMIT[:39]: "REVISION_SHORT_SHA",
        COMMIT.upper(): "REVISION_UPPERCASE",
        COMMIT + "8": "REVISION_NOT_COMMIT",
        "a" * 64: "REVISION_NOT_COMMIT",
        " " + COMMIT: "REVISION_NOT_COMMIT",
        COMMIT + "\n": "REVISION_NOT_COMMIT",
        COMMIT[:39] + "g": "REVISION_NOT_COMMIT",
        "": "REVISION_MISSING",
    }

    def test_every_mutable_or_malformed_ref_is_rejected(self):
        for rev, code in self.CASES.items():
            doc = valid()
            doc["pins"][0]["revision"] = rev
            with self.subTest(rev=rev):
                self.assertEqual(codes(doc), [code])

    def test_non_string_revisions_are_rejected(self):
        for rev in (None, 123, True, [COMMIT], {"sha": COMMIT}):
            doc = valid()
            doc["pins"][0]["revision"] = rev
            with self.subTest(rev=rev):
                self.assertEqual(codes(doc), ["REVISION_MISSING"])

    def test_absent_revision_is_rejected(self):
        doc = valid()
        del doc["pins"][1]["revision"]
        self.assertEqual(codes(doc), ["REVISION_MISSING"])

    def test_committed_invalid_fixtures_give_expected_codes(self):
        expected = s.load(os.path.join(s.INVALID_DIR, "expected_codes.json"))
        self.assertGreaterEqual(len(expected), 5)
        for rel, want in expected.items():
            with self.subTest(fixture=rel):
                self.assertEqual(p.validate_file(os.path.join(s.PINS, rel)).codes, want)


class FieldTests(unittest.TestCase):
    def _codes_with(self, mutate):
        doc = valid()
        mutate(doc)
        return codes(doc)

    @staticmethod
    def _set(keys, value):
        doc = valid()
        cur = doc
        for k in keys[:-1]:
            cur = cur[k]
        cur[keys[-1]] = value
        return codes(doc)

    def test_bad_repo_ids(self):
        for rid in ("a/b/c", "datasets/foo/bar", "foo--bar", "foo..bar", ".foo", "foo.", "-foo", "foo-",
                    "foo.git", "caf\u00e9/x", "", "x" * 97, "org/", "/name", "org name/x", "org/na me"):
            with self.subTest(rid=rid):
                self.assertEqual(self._set(("pins", 0, "repo_id"), rid), ["REPO_ID"])

    def test_repo_id_type(self):
        self.assertEqual(self._set(("pins", 0, "repo_id"), 5), ["TYPE_ERROR"])

    def test_bad_kind(self):
        for kind in ("space", "Model", "models", "", None, ["model"]):
            with self.subTest(kind=kind):
                self.assertEqual(self._set(("pins", 0, "kind"), kind), ["KIND"])

    def test_bad_license(self):
        for lic in ("unknown", "UNKNOWN", "", "TBD", 123, None, "apache 2.0", "-mit"):
            with self.subTest(lic=lic):
                self.assertEqual(self._set(("pins", 0, "license"), lic), ["LICENSE"])

    def test_missing_required_fields(self):
        for key in ("id", "repo_id", "kind", "license"):
            doc = valid()
            del doc["pins"][0][key]
            with self.subTest(key=key):
                self.assertEqual(codes(doc), ["MISSING_FIELD"])

    def test_typo_is_unknown_field_and_missing_revision(self):
        doc = valid()
        doc["pins"][0]["revison"] = doc["pins"][0].pop("revision")
        self.assertEqual(sorted(codes(doc)), ["REVISION_MISSING", "UNKNOWN_FIELD"])

    def test_bad_file_paths(self):
        for path in ("/etc/passwd", "../x", "a/../b", "a//b", "a/./b", "a\\b", "C:/x", "", "a/", "a\x00b",
                     "a\nb", 7):
            with self.subTest(path=path):
                got = self._set(("pins", 0, "files", 0, "path"), path)
                self.assertEqual(got, ["FILE_PATH"])

    def test_bad_sha256(self):
        for sha in ("abc", "A" * 64, "g" * 64, "a" * 63, "a" * 65, None, 1):
            with self.subTest(sha=sha):
                got = self._set(("pins", 0, "files", 0, "sha256"), sha)
                self.assertEqual(got, ["SHA256"])

    def test_bad_size_and_role(self):
        for size in (-1, True, 1.5, "10", None):
            with self.subTest(size=size):
                got = self._set(("pins", 0, "files", 0, "size"), size)
                self.assertEqual(got, ["SIZE"])
        self.assertEqual(self._set(("pins", 0, "files", 0, "role"), "model"), ["ROLE"])

    def test_missing_file_fields(self):
        for key in ("path", "sha256"):
            doc = valid()
            del doc["pins"][0]["files"][0][key]
            with self.subTest(key=key):
                self.assertEqual(codes(doc), ["MISSING_FIELD"])

    def test_duplicates(self):
        doc = valid()
        doc["pins"][0]["files"].append(copy.deepcopy(doc["pins"][0]["files"][0]))
        self.assertEqual(codes(doc), ["DUPLICATE_FILE"])
        doc = valid()
        doc["pins"][1]["id"] = doc["pins"][0]["id"]
        self.assertEqual(codes(doc), ["DUPLICATE_PIN_ID"])
        doc = valid()
        extra = copy.deepcopy(doc["pins"][0])
        extra["id"] = "other"
        extra["revision"] = COMMIT
        doc["pins"].append(extra)
        self.assertEqual(codes(doc), ["DUPLICATE_REPO"])

    def test_same_repo_id_as_model_and_dataset_is_allowed(self):
        doc = valid()
        doc["pins"][1]["repo_id"] = doc["pins"][0]["repo_id"]
        self.assertEqual(codes(doc), [])

    def test_bad_ids_and_flags(self):
        for pid in ("Policy", "", "a b", "-x", "x" * 65, 3):
            with self.subTest(pid=pid):
                self.assertEqual(self._set(("pins", 0, "id"), pid), ["PIN_ID"])
        self.assertEqual(self._set(("pins", 0, "gated"), "yes"), ["TYPE_ERROR"])

    def test_top_level_errors(self):
        self.assertEqual(self._codes_with(lambda d: d.__setitem__("schema_version", "v2")), ["SCHEMA_VERSION"])
        self.assertEqual(self._codes_with(lambda d: d.__setitem__("data_kind", "measured")), ["DATA_KIND"])
        self.assertEqual(self._codes_with(lambda d: d.__setitem__("pins", [])), ["NO_PINS"])
        self.assertEqual(self._codes_with(lambda d: d.__setitem__("pins", {})), ["TYPE_ERROR"])
        self.assertEqual(self._codes_with(lambda d: d.__setitem__("extra", 1)), ["UNKNOWN_FIELD"])
        self.assertEqual(self._set(("pins", 0, "files"), []), ["FILES_EMPTY"])
        self.assertEqual(self._set(("pins", 0, "files"), "x"), ["TYPE_ERROR"])
        self.assertEqual(self._codes_with(lambda d: d.__setitem__("description", 1)), ["TYPE_ERROR"])
        self.assertEqual(self._codes_with(lambda d: d.__delitem__("pins")), ["MISSING_FIELD"])

    def test_limits(self):
        doc = valid()
        doc["pins"] = [copy.deepcopy(doc["pins"][0]) for _ in range(p.MAX_PINS + 1)]
        self.assertEqual(codes(doc), ["TOO_MANY"])
        doc = valid()
        doc["pins"][0]["files"] = [{"path": "f%d" % i, "sha256": "a" * 64} for i in range(p.MAX_FILES_PER_PIN + 1)]
        self.assertEqual(codes(doc), ["TOO_MANY"])

    def test_diagnostics_are_truncated(self):
        doc = valid()
        doc["pins"][0]["files"] = [{"path": "/bad", "sha256": "x"} for _ in range(150)]
        r = p.validate_document(doc)
        self.assertEqual(r.codes[-1], "TRUNCATED")
        self.assertEqual(len(r.codes), p.MAX_DIAGNOSTICS + 1)


class ReleaseModeTests(unittest.TestCase):
    def test_synthetic_fixture_cannot_pass_release(self):
        self.assertEqual(p.validate_file(s.VALID, release=True).codes, ["RELEASE_SYNTHETIC"])

    def test_release_requires_file_hashes_on_every_pin(self):
        doc = s.as_real(s.load(s.NO_HASHES))
        self.assertEqual(codes(doc), [])
        self.assertEqual(codes(doc, release=True), ["RELEASE_NO_FILE_HASHES"] * 2)

    def test_release_still_rejects_mutable_refs(self):
        doc = s.as_real(valid())
        doc["pins"][0]["revision"] = "main"
        self.assertEqual(codes(doc, release=True), ["REVISION_BRANCH"])


class HostileInputTests(unittest.TestCase):
    def test_non_object_roots(self):
        for root in ([], "x", None, 1, True, 1.5):
            with self.subTest(root=root):
                self.assertEqual(codes(root), ["TYPE_ERROR"])

    def test_non_object_pin_and_file(self):
        for bad in (None, 1, "pin", []):
            doc = valid()
            doc["pins"][0] = bad
            self.assertEqual(codes(doc), ["TYPE_ERROR"])
            doc = valid()
            doc["pins"][0]["files"][0] = bad
            self.assertEqual(codes(doc), ["TYPE_ERROR"])

    def test_strict_json(self):
        with open(s.VALID, encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(p.validate_text(text).ok)
        dup = text.replace('"data_kind": "synthetic",', '"data_kind": "synthetic", "data_kind": "real",', 1)
        self.assertEqual(p.validate_text(dup).codes, ["PARSE_ERROR"])
        for bad in ('{"pins": NaN}', '{"pins": Infinity}', "{", "", "[1,]", b"\xff\xfe{}"):
            with self.subTest(bad=bad):
                self.assertEqual(p.validate_text(bad).codes, ["PARSE_ERROR"])

    def test_deep_nesting_does_not_crash(self):
        self.assertEqual(p.validate_text("[" * 100000 + "]" * 100000).codes, ["PARSE_ERROR"])

    def test_oversized_and_unreadable_files(self):
        self.assertEqual(p.validate_text(b" " * (p.MAX_PIN_FILE_BYTES + 1)).codes, ["PARSE_ERROR"])
        self.assertEqual(p.validate_file(os.path.join(s.FIX, "does-not-exist.json")).codes, ["PARSE_ERROR"])
        self.assertEqual(p.validate_file(s.FIX).codes, ["PARSE_ERROR"])

    def test_type_substitution_never_raises_or_hits_internal_error(self):
        replacements = (None, True, 0, -1, 1.5, "", "x", [], {}, [None], {"k": None})
        base = valid()

        def walk(node, path=()):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield path + (k,)
                    yield from walk(v, path + (k,))
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    yield path + (i,)
                    yield from walk(v, path + (i,))

        n = 0
        for path in list(walk(base)):
            for rep in replacements:
                doc = copy.deepcopy(base)
                cur = doc
                for k in path[:-1]:
                    cur = cur[k]
                cur[path[-1]] = rep
                r = p.validate_document(doc)
                n += 1
                self.assertNotIn("INTERNAL_ERROR", r.codes, (path, rep))
        self.assertGreater(n, 300)

    def test_internal_error_fails_closed(self):
        with mock.patch.object(p, "classify_revision", side_effect=RuntimeError("boom")):
            r = p.validate_document(valid())
        self.assertFalse(r.ok)
        self.assertIn("INTERNAL_ERROR", r.codes)

    def test_every_code_is_documented(self):
        self.assertTrue(all(isinstance(v, str) and v for v in p.CODES.values()))


if __name__ == "__main__":
    unittest.main()
