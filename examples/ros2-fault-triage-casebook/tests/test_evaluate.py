"""Evaluator: per-class results, confusion, abstention/coverage, misclassified list, determinism."""

import json
import random
import unittest
from collections import namedtuple

from triage import evaluate as E
from triage import taxonomy as T

from .support import all_cases

Fake = namedtuple("Fake", "label confidence rule_id")


def always(label, tier="high"):
    return lambda evidence: Fake(label, tier, "RX")


class RealBaselineTests(unittest.TestCase):
    def test_shipped_cases_reproduce_their_authored_labels(self):
        r = E.evaluate(all_cases())
        self.assertEqual([], r["misclassified"])
        self.assertEqual(r["cases"], r["exact_match"]["correct"])
        self.assertEqual(T.BANNER, r["banner"])

    def test_counts_are_consistent(self):
        cases = all_cases()
        r = E.evaluate(cases)
        self.assertEqual(len(cases), sum(v["support"] for v in r["per_class"].values()))
        self.assertEqual(len(cases), sum(v["predicted"] for v in r["per_class"].values()))
        self.assertEqual(len(cases), r["coverage"]["answered"] + r["coverage"]["abstained"])
        self.assertEqual(len(cases), sum(sum(row.values()) for row in r["confusion"].values()))
        self.assertEqual(len(cases), sum(t["n"] for t in r["by_confidence_tier"].values()))

    def test_deterministic_and_order_independent(self):
        cases = all_cases()
        shuffled = list(cases)
        random.Random(7).shuffle(shuffled)
        a, b = E.render_json(E.evaluate(cases)), E.render_json(E.evaluate(shuffled))
        self.assertEqual(a, b)
        self.assertEqual(E.render_text(E.evaluate(cases)), E.render_text(E.evaluate(shuffled)))

    def test_text_report_carries_the_banner_and_every_label(self):
        text = E.render_text(E.evaluate(all_cases()))
        self.assertTrue(text.startswith(T.BANNER))
        for label in T.LABELS:
            self.assertIn(label, text)


class BrokenTriagerTests(unittest.TestCase):
    def test_always_abstain(self):
        cases = all_cases()
        r = E.evaluate(cases, always(T.ABSTAIN, "abstain"))
        ambiguous = sum(1 for c in cases if c["expected"]["label"] == T.ABSTAIN)
        self.assertEqual(0, r["coverage"]["answered"])
        self.assertEqual(0.0, r["coverage"]["coverage"])
        self.assertIsNone(r["accuracy_on_answered"]["ratio"])
        self.assertEqual(ambiguous, r["abstention"]["correct_abstentions"])
        self.assertEqual(len(cases) - ambiguous, r["abstention"]["abstained_on_answerable"])
        self.assertEqual(0, r["abstention"]["answered_when_should_abstain"])
        self.assertEqual(len(cases) - ambiguous, len(r["misclassified"]))

    def test_always_no_fault_is_overconfident_on_abstain_cases(self):
        cases = all_cases()
        r = E.evaluate(cases, always("no_fault"))
        should_abstain = sum(1 for c in cases if c["expected"]["label"] == T.ABSTAIN)
        self.assertEqual(should_abstain, r["abstention"]["answered_when_should_abstain"])
        self.assertEqual(1.0, r["coverage"]["coverage"])
        self.assertEqual(8, r["per_class"]["no_fault"]["correct"])
        self.assertEqual(1.0, r["per_class"]["no_fault"]["recall"])
        self.assertLess(r["per_class"]["no_fault"]["precision"], 1.0)
        self.assertIsNone(r["per_class"]["publisher_restart"]["precision"])  # small n: n/a, not 0 or a crash
        self.assertIsNone(r["per_class"]["publisher_restart"]["recall"])
        self.assertEqual(0, r["per_class"]["publisher_restart"]["correct"])  # raw counts stay visible

    def test_misclassified_entries_are_sorted_and_complete(self):
        r = E.evaluate(all_cases(), always("no_fault"))
        ids = [m["case_id"] for m in r["misclassified"]]
        self.assertEqual(sorted(ids), ids)
        for m in r["misclassified"]:
            self.assertEqual({"case_id", "family", "truth", "predicted", "confidence", "rule_id"}, set(m))
            self.assertNotEqual(m["truth"], m["predicted"])

    def test_label_outside_taxonomy_is_counted_not_crashed(self):
        for bad in ("made_up", None, ["no_fault"], 3):
            r = E.evaluate(all_cases(), always(bad))
            self.assertEqual(len(all_cases()), r["per_class"]["<invalid>"]["predicted"])
            self.assertEqual(len(all_cases()), len(r["misclassified"]))
            E.render_text(r)  # must render too

    def test_confusion_cells(self):
        r = E.evaluate(all_cases(), always("no_fault"))
        self.assertEqual({"no_fault": 2}, r["confusion"]["pod_not_running"])
        self.assertEqual({"no_fault": 8}, r["confusion"]["no_fault"])

    def test_confidence_tier_table_shows_wrong_high_confidence_answers(self):
        r = E.evaluate(all_cases(), always("no_fault", "high"))
        tier = r["by_confidence_tier"]["high"]
        self.assertEqual(len(all_cases()), tier["n"])
        self.assertLess(tier["correct"], tier["n"])


class SmallNTests(unittest.TestCase):
    def test_support_below_three_never_gets_numeric_precision_or_recall(self):
        r = E.evaluate(all_cases())
        small = [l for l, v in r["per_class"].items() if 0 < v["support"] < 3]
        self.assertIn("qos_incompatible_reliability", small)  # support 2
        self.assertIn("rmw_mismatch", small)                  # support 1
        for l, v in r["per_class"].items():
            self.assertEqual(v["support"] < 3, v["small_n"], l)
            if v["small_n"]:
                self.assertIsNone(v["precision"], l)
                self.assertIsNone(v["recall"], l)
            else:
                self.assertIsNotNone(v["precision"], l)

    def test_text_prints_na_for_small_classes_and_no_number(self):
        r = E.evaluate(all_cases())
        text = E.render_text(r)
        table = text.split("confusion (")[0].splitlines()  # per-class section only
        checked = 0
        for line in table:
            for l, v in r["per_class"].items():
                if line.startswith(f"  {l} ") and v["support"] < 3 and v["support"] > 0:
                    self.assertIn("n/a (n<3)", line)
                    self.assertIn("n<3: not meaningful", line)
                    self.assertNotIn("1.000", line)
                    self.assertNotIn("0.000", line)
                    checked += 1
        self.assertGreaterEqual(checked, 10)

    def test_class_with_support_exactly_three_gets_numbers(self):
        cases = [c for c in all_cases() if c["expected"]["label"] == "pod_not_running"]
        c = dict(cases[0])
        extra = [dict(c, case_id="case-901"), dict(c, case_id="case-902")]
        r = E.evaluate(cases + extra)  # 2 + 2 = 4 >= 3
        self.assertFalse(r["per_class"]["pod_not_running"]["small_n"])
        self.assertEqual(1.0, r["per_class"]["pod_not_running"]["precision"])

    def test_circular_note_is_right_under_the_overall_lines_and_in_json(self):
        text = E.render_text(E.evaluate(all_cases()))
        self.assertIn(E.CIRCULAR_NOTE, text)
        lines = text.splitlines()
        self.assertTrue(lines[lines.index(E.CIRCULAR_NOTE) - 1].startswith("abstention:"))
        self.assertEqual(E.CIRCULAR_NOTE, json.loads(E.render_json(E.evaluate(all_cases())))["circular_note"])

    def test_overall_lines_are_kept(self):
        text = E.render_text(E.evaluate(all_cases()))
        for key in ("exact match", "coverage (answered", "accuracy on answered", "abstention:"):
            self.assertIn(key, text)


class EdgeTests(unittest.TestCase):
    def test_no_cases(self):
        r = E.evaluate([])
        self.assertEqual(0, r["cases"])
        self.assertIsNone(r["exact_match"]["ratio"])
        self.assertIsNone(r["coverage"]["coverage"])
        self.assertEqual([], r["misclassified"])
        E.render_text(r)

    def test_json_output_parses(self):
        json.loads(E.render_json(E.evaluate(all_cases())))


class CoverageTests(unittest.TestCase):
    def test_every_fault_label_has_a_positive_and_a_resembling_hard_negative(self):
        cov = E.coverage_table(all_cases())
        for label in T.LABELS:
            if label in (T.ABSTAIN, "no_fault"):
                continue
            self.assertGreaterEqual(cov["by_label_and_role"][label]["positive"], 1, label)
            self.assertGreaterEqual(cov["resembled_by_hard_negatives"][label], 1, label)

    def test_abstain_and_no_fault_have_cases_of_the_required_kinds(self):
        cov = E.coverage_table(all_cases())["by_label_and_role"]
        self.assertGreaterEqual(cov["no_fault"]["positive"], 1)
        self.assertGreaterEqual(cov["no_fault"]["hard_negative"], 1)
        self.assertGreaterEqual(cov[T.ABSTAIN]["ambiguous"], 1)
        self.assertGreaterEqual(cov[T.ABSTAIN]["hard_negative"], 1)

    def test_at_least_twelve_cases_and_required_hard_negatives(self):
        cases = all_cases()
        self.assertGreaterEqual(len(cases), 12)
        labels = {c["expected"]["label"] for c in cases}
        self.assertIn("clock_skew_misleading_latency", labels)
        self.assertIn(T.ABSTAIN, labels)
        self.assertIn("no_fault", labels)

    def test_every_case_is_labelled_synthetic(self):
        for c in all_cases():
            self.assertIs(True, c["synthetic"])
            self.assertTrue(c["source"].startswith("SYNTHETIC: "))


if __name__ == "__main__":
    unittest.main()
