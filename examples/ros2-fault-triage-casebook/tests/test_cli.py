"""Command line: exit codes and the shape of what is printed."""

import contextlib
import io
import json
import unittest

from triage import __main__ as cli
from triage import taxonomy as T

from .support import FIXTURES, TmpDirCase


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = cli.main(list(argv))
        except SystemExit as exc:  # argparse usage errors
            rc = exc.code
    return rc, out.getvalue(), err.getvalue()


class CliTests(TmpDirCase):
    def test_validate_ok(self):
        rc, out, _ = run("validate", "--dir", str(self.dir))
        self.assertEqual(0, rc)
        self.assertIn("0 diagnostics", out)

    def test_validate_reports_typed_diagnostics_with_exit_1(self):
        (self.dir / "case_001.json").write_text('{"a": 1, "a": 2}')
        rc, _out, err = run("validate", "--dir", str(self.dir))
        self.assertEqual(1, rc)
        self.assertIn("case_001.json: DUPLICATE_KEY", err)

    def test_fixtures_check_ok_and_drift(self):
        self.assertEqual(0, run("fixtures", "--check", "--dir", str(self.dir))[0])
        p = self.dir / "case_001.json"
        p.write_text(json.dumps(json.loads(p.read_text())))
        rc, _out, err = run("fixtures", "--check", "--dir", str(self.dir))
        self.assertEqual(1, rc)
        self.assertIn("NOT_CANONICAL", err)

    def test_fixtures_write_then_check(self):
        p = self.dir / "case_001.json"
        p.write_text(json.dumps(json.loads(p.read_text())))
        self.assertEqual(0, run("fixtures", "--write", "--dir", str(self.dir))[0])
        self.assertEqual(0, run("fixtures", "--check", "--dir", str(self.dir))[0])

    def test_fixtures_write_refused_on_invalid_input_changes_nothing(self):
        (self.dir / "case_002.json").write_text("[]")
        before = self.snapshot()
        rc, _out, err = run("fixtures", "--write", "--dir", str(self.dir))
        self.assertEqual(1, rc)
        self.assertIn("nothing was written", err)
        self.assertEqual(before, self.snapshot())

    def test_fixtures_requires_a_mode(self):
        self.assertEqual(2, run("fixtures", "--dir", str(self.dir))[0])
        self.assertEqual(2, run("fixtures", "--check", "--write", "--dir", str(self.dir))[0])

    def test_evaluate_text_json_and_split(self):
        rc, out, _ = run("evaluate", "--dir", str(self.dir))
        self.assertEqual(0, rc)
        self.assertTrue(out.startswith(T.BANNER))
        rc, out, _ = run("evaluate", "--dir", str(self.dir), "--json")
        self.assertEqual(0, rc)
        total = json.loads(out)["cases"]
        rc, out, _ = run("evaluate", "--dir", str(self.dir), "--json", "--split", "holdout")
        holdout = json.loads(out)["cases"]
        rc, out, _ = run("evaluate", "--dir", str(self.dir), "--json", "--split", "dev")
        dev = json.loads(out)["cases"]
        self.assertEqual(total, holdout + dev)
        self.assertGreater(holdout, 0)

    def test_evaluate_strict_passes_on_shipped_cases(self):
        self.assertEqual(0, run("evaluate", "--strict", "--dir", str(self.dir))[0])

    def test_evaluate_strict_fails_when_a_label_is_edited_to_disagree(self):
        p = self.dir / "case_002.json"  # hard negative, expected no_fault
        obj = json.loads(p.read_text())
        obj.update({"case_role": "positive", "looks_like": None})
        obj["expected"].update({"label": "rmw_mismatch", "safe_next_step": "compare_rmw_settings"})
        p.write_text(json.dumps(obj))
        self.assertEqual(0, run("evaluate", "--dir", str(self.dir))[0])
        rc, out, _ = run("evaluate", "--strict", "--dir", str(self.dir))
        self.assertEqual(1, rc)
        self.assertIn("case-002", out)

    def test_evaluate_refuses_invalid_cases(self):
        (self.dir / "case_003.json").write_text("{")
        rc, out, err = run("evaluate", "--dir", str(self.dir))
        self.assertEqual(1, rc)
        self.assertEqual("", out)
        self.assertIn("evaluate refused", err)

    def test_evaluate_unknown_split_is_a_usage_error(self):
        self.assertEqual(2, run("evaluate", "--split", "test", "--dir", str(self.dir))[0])

    def test_triage_a_case_file_and_a_bare_evidence_file(self):
        rc, out, err = run("triage", str(self.dir / "case_001.json"))
        self.assertEqual(0, rc)
        res = json.loads(out)
        self.assertEqual("qos_incompatible_reliability", res["label"])
        self.assertIn(T.BANNER, err)
        bare = self.dir.parent / "bare.json"
        bare.write_text(json.dumps(json.loads((self.dir / "case_001.json").read_text())["evidence"]))
        rc, out, _ = run("triage", str(bare))
        self.assertEqual("qos_incompatible_reliability", json.loads(out)["label"])

    def test_triage_invalid_evidence_abstains_with_exit_0(self):
        bad = self.dir.parent / "bad.json"
        bad.write_text(json.dumps({"pods": []}))
        rc, out, _ = run("triage", str(bad))
        self.assertEqual(0, rc)
        res = json.loads(out)
        self.assertEqual(("insufficient_evidence", "R00"), (res["label"], res["rule_id"]))

    def test_triage_unreadable_input_is_exit_2(self):
        bad = self.dir.parent / "bad.json"
        for text in ("{", "NaN", '{"a":1,"a":2}'):
            bad.write_text(text)
            self.assertEqual(2, run("triage", str(bad))[0], msg=text)
        self.assertEqual(2, run("triage", str(self.dir.parent / "missing.json"))[0])

    def test_coverage_and_taxonomy(self):
        rc, out, _ = run("coverage", "--dir", str(self.dir))
        self.assertEqual(0, rc)
        self.assertIn("resembled_by_hard_negative", out)
        rc, out, _ = run("taxonomy")
        self.assertEqual(0, rc)
        for label in T.LABELS:
            self.assertIn(label, out)

    def test_unknown_command_is_exit_2(self):
        self.assertEqual(2, run("nonsense")[0])


if __name__ == "__main__":
    unittest.main()
