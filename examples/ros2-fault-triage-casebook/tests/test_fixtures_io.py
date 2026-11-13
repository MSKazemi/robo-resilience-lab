"""Canonical text, manifest, and the --check / --write behaviour (byte-identical regeneration)."""

import json
import unittest

from triage import fixtures_io as F
from triage import validator as V

from .support import FIXTURES, TmpDirCase, all_cases, load_case


class CanonicalFormTests(unittest.TestCase):
    def test_shipped_files_are_byte_identical_to_canonical_form(self):
        for p in sorted(FIXTURES.glob("case_*.json")):
            obj = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(F.canonical_dumps(obj), p.read_text(encoding="utf-8"), msg=p.name)

    def test_round_trip_is_stable(self):
        for c in all_cases():
            once = F.canonical_dumps(c)
            self.assertEqual(c, json.loads(once))
            self.assertEqual(once, F.canonical_dumps(json.loads(once)))

    def test_key_order_does_not_matter(self):
        c = load_case(1)
        shuffled = dict(reversed(list(c.items())))
        self.assertEqual(F.canonical_dumps(c), F.canonical_dumps(shuffled))

    def test_ends_with_exactly_one_newline_and_is_ascii(self):
        for c in all_cases():
            t = F.canonical_dumps(c)
            self.assertTrue(t.endswith("}\n") and not t.endswith("\n\n"))
            t.encode("ascii")

    def test_unsupported_types_raise_rather_than_write_something_odd(self):
        with self.assertRaises(TypeError):
            F.canonical_dumps({"a": 1.5})


class CheckAndWriteTests(TmpDirCase):
    def test_check_passes_on_shipped_fixtures(self):
        self.assertEqual([], F.check(FIXTURES))

    def test_manifest_lists_every_case_file_with_its_sha256(self):
        m = json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))
        self.assertTrue(m["synthetic"])
        self.assertEqual(len(all_cases()), m["case_count"])
        for name, digest in m["files"].items():
            self.assertEqual(F.sha256_hex((FIXTURES / name).read_text(encoding="utf-8")), digest)

    def test_check_detects_a_reformatted_file(self):
        p = self.dir / "case_001.json"
        p.write_text(json.dumps(json.loads(p.read_text()), indent=4) + "\n")
        problems = F.check(self.dir)
        self.assertTrue(any("case_001.json: NOT_CANONICAL" in x for x in problems), problems)

    def test_check_detects_a_missing_trailing_newline(self):
        p = self.dir / "case_002.json"
        p.write_text(p.read_text().rstrip("\n"))
        self.assertTrue(F.check(self.dir))

    def test_check_detects_a_changed_value_through_the_manifest(self):
        p = self.dir / "case_003.json"
        obj = json.loads(p.read_text())
        obj["symptom"] = obj["symptom"] + " Edited."
        p.write_text(F.canonical_dumps(obj))  # canonical, valid, but the manifest hash is now stale
        problems = F.check(self.dir)
        self.assertTrue(any(x.startswith("MANIFEST.json: STALE") for x in problems), problems)

    def test_check_detects_missing_manifest(self):
        (self.dir / "MANIFEST.json").unlink()
        self.assertTrue(any("MISSING" in x for x in F.check(self.dir)))

    def test_check_reports_invalid_cases_instead_of_comparing(self):
        (self.dir / "case_004.json").write_text("{")
        self.assertTrue(any("case_004.json" in x and "UNREADABLE" in x for x in F.check(self.dir)))

    def test_write_is_idempotent_and_reaches_a_fixed_point(self):
        before = self.snapshot()
        changed, problems = F.write(self.dir)
        self.assertEqual(([], []), (changed, problems))
        self.assertEqual(before, self.snapshot())

    def test_write_repairs_formatting_and_manifest_then_check_passes(self):
        p = self.dir / "case_001.json"
        p.write_text(json.dumps(json.loads(p.read_text()), indent=4))
        (self.dir / "MANIFEST.json").write_text("{}")
        changed, problems = F.write(self.dir)
        self.assertEqual([], problems)
        self.assertEqual(["case_001.json", "MANIFEST.json"], changed)
        self.assertEqual([], F.check(self.dir))
        self.assertEqual((FIXTURES / "case_001.json").read_bytes(), p.read_bytes())

    def test_write_refuses_when_any_case_is_invalid_and_changes_nothing(self):
        p = self.dir / "case_001.json"
        p.write_text(json.dumps(json.loads(p.read_text()), indent=4))  # would be rewritten...
        bad = json.loads((self.dir / "case_002.json").read_text())
        bad["expected"]["label"] = "not_a_label"  # ...but this file is invalid
        (self.dir / "case_002.json").write_text(json.dumps(bad))
        before = self.snapshot()
        changed, problems = F.write(self.dir)
        self.assertEqual([], changed)
        self.assertTrue(problems)
        self.assertEqual(before, self.snapshot())

    def test_write_leaves_no_temp_files(self):
        p = self.dir / "case_001.json"
        p.write_text(json.dumps(json.loads(p.read_text()), indent=4))
        F.write(self.dir)
        self.assertEqual([], [x.name for x in self.dir.iterdir() if x.name.startswith(".tmp-")])

    def test_regeneration_from_scrambled_key_order_is_byte_identical(self):
        for p in sorted(self.dir.glob("case_*.json")):
            obj = json.loads(p.read_text())
            p.write_text(json.dumps(dict(reversed(list(obj.items()))), separators=(",", ":")))
        changed, problems = F.write(self.dir)
        self.assertEqual([], problems)
        for p in sorted(FIXTURES.iterdir()):
            self.assertEqual(p.read_bytes(), (self.dir / p.name).read_bytes(), msg=p.name)


if __name__ == "__main__":
    unittest.main()
