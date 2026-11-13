import ast
import importlib.util
import json
import unittest
from pathlib import Path

from skill_review import report
from skill_review.reviewer import ACCEPT, NEEDS_HUMAN, REASON_CODES, REJECT, review

ROOT = Path(__file__).resolve().parent.parent

REQUIRED_CLASSES = {
    "injection_text", "unknown_skill", "type_confusion", "numeric_abuse", "name_confusion", "path_or_url",
    "duplicate_keys", "size_limits", "nesting", "benign_lookalike", "raw_velocity_or_topic", "ambiguous_goal",
    "precondition_state", "unsupported_robot", "human_approval",
}


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_corpus", ROOT / "build_corpus.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = report.load_corpus()
        cls.cases = cls.corpus["cases"]

    def test_committed_corpus_matches_generator(self):
        builder = _load_builder()
        self.assertEqual((ROOT / "fixtures" / "corpus.json").read_text(encoding="utf-8"), builder.render())

    def test_generator_does_not_import_the_implementation(self):
        tree = ast.parse((ROOT / "build_corpus.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(imported, {"json", "sys", "pathlib"})

    def test_size_and_classes(self):
        self.assertGreaterEqual(len(self.cases), 25)
        self.assertGreaterEqual(len({c["attack_class"] for c in self.cases}), 12)
        self.assertTrue(REQUIRED_CLASSES <= {c["attack_class"] for c in self.cases})
        self.assertIn("SYNTHETIC", self.corpus["label"])

    def test_case_hygiene(self):
        ids = [c["id"] for c in self.cases]
        self.assertEqual(len(ids), len(set(ids)))
        for c in self.cases:
            self.assertIn(c["kind"], ("benign", "adversarial", "human_review"))
            self.assertIn(c["expected_verdict"], (ACCEPT, REJECT, NEEDS_HUMAN))
            self.assertIn(c["context"], self.corpus["contexts"])
            self.assertEqual(("input_text" in c) + ("input_hex" in c), 1, c["id"])
            if c["expected_reason"] is not None:
                self.assertIn(c["expected_reason"], REASON_CODES, c["id"])
            self.assertEqual(c["expected_reason"] is None, c["expected_verdict"] == ACCEPT, c["id"])
            self.assertEqual(c["kind"] == "adversarial", c["expected_verdict"] == REJECT, c["id"])

    def test_each_case_meets_its_expectation(self):
        for c in self.cases:
            with self.subTest(case=c["id"]):
                res = report.run_case(c, self.corpus["contexts"])
                self.assertEqual(res.verdict, c["expected_verdict"], res.as_dict())
                self.assertEqual(res.primary_reason, c["expected_reason"], res.as_dict())

    def test_all_unauthorized_fixtures_rejected_by_the_deterministic_reviewer(self):
        adversarial = [c for c in self.cases if c["kind"] == "adversarial"]
        self.assertGreaterEqual(len(adversarial), 25)
        for c in adversarial:
            res = report.run_case(c, self.corpus["contexts"])
            self.assertEqual(res.verdict, REJECT, c["id"])
            self.assertIsNone(res.call, c["id"])
            self.assertIsNone(res.pending_call, c["id"])
            self.assertNotEqual(res.primary_reason, "INTERNAL_ERROR", c["id"])

    def test_benign_lookalikes_are_accepted(self):
        benign = [c for c in self.cases if c["kind"] == "benign"]
        self.assertGreaterEqual(len(benign), 5)
        for c in benign:
            self.assertEqual(report.run_case(c, self.corpus["contexts"]).verdict, ACCEPT, c["id"])

    def test_verdicts_independent_of_advisor_over_whole_corpus(self):
        result = report.evaluate(self.corpus)
        self.assertEqual(result["summary"]["advisory_verdict_changes"], 0)
        # and an advisor that "approves" every adversarial case still cannot flip it
        approve = {"opinion": "approve", "confidence": 1.0}
        for c in self.cases:
            if c["kind"] == "adversarial":
                self.assertEqual(report.run_case(c, self.corpus["contexts"], approve).verdict, REJECT, c["id"])

    def test_evaluation_passes_and_is_deterministic(self):
        first = report.evaluate(self.corpus)
        second = report.evaluate(report.load_corpus())
        self.assertTrue(report.exit_ok(first))
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(report.format_text(first), report.format_text(second))
        self.assertIn("SYNTHETIC teaching corpus", report.format_text(first))

    def test_evaluator_reports_failure_when_reviewer_fails_open(self):
        def fail_open(data, context, advisory=None):
            return review('{"proposal_version":"skill-proposal/v1","skill":"get_status",'
                          '"robot_id":"sim_robot_1","args":{}}', context, advisory)

        result = report.evaluate(self.corpus, review_fn=fail_open)
        self.assertFalse(report.exit_ok(result))
        self.assertGreater(result["summary"]["false_accepts"], 0)

    def test_every_catalogue_skill_has_an_accepting_or_approval_case(self):
        seen = set()
        for c in self.cases:
            if c["expected_verdict"] in (ACCEPT, NEEDS_HUMAN):
                res = report.run_case(c, self.corpus["contexts"])
                call = res.call or res.pending_call
                if call:
                    seen.add(call.skill)
        from skill_review.catalogue import CATALOGUE
        self.assertEqual(seen, set(CATALOGUE))


if __name__ == "__main__":
    unittest.main()
