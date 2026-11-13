"""The deterministic fault-case suite: results, categories, time-to-block, harness self-checks."""

import contextlib
import io
import os
import unittest
from pathlib import Path

import harness as hx
import run_fault_suite
import scenarios
from tests.support import Category, Reason, g

REPORT = Path(run_fault_suite.REPORT)


def _suite():
    return hx.run_suite(scenarios.build_scenarios())


class SuiteResultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = _suite()
        cls.by_name = {s["name"]: s for s in cls.report["scenarios"]}

    def test_all_expectations_met_and_nothing_failed_open(self):
        t = self.report["totals"]
        self.assertEqual(t["failure_count"], 0, [s["mismatches"] for s in self.report["scenarios"] if s["mismatches"]])
        self.assertEqual(t["fail_open"], 0)
        self.assertEqual(t["false_blocks"], 0)
        self.assertGreater(t["deliveries"], 100)

    def test_required_fault_cases_exist(self):
        for name in ("delayed_reply", "reordered_packet", "duplicate_packet", "disconnection",
                     "process_crash_replay", "process_crash_state_lost", "unknown_skill", "llm_invalid_args",
                     "expired_observation", "operator_intervention", "model_failure", "unavailable_command"):
            self.assertIn(name, self.by_name)

    def test_blocked_commands_are_never_dispatched(self):
        for s in self.report["scenarios"]:
            for d in s["deliveries"]:
                if d["verdict"] != "ALLOW":
                    self.assertFalse(d["dispatched"], (s["name"], d["label"]))
                    if d["verdict"] != "DROPPED":
                        self.assertIsNotNone(d["reason"])
                        self.assertIsNotNone(d["category"])
                        self.assertTrue(d["field"])
                else:
                    self.assertTrue(d["dispatched"])

    def test_tests_distinguish_unavailable_stale_intervention_and_model_failure(self):
        def cats(name, prefix=None):
            return {d["category"] for d in self.by_name[name]["deliveries"]
                    if d["verdict"] not in ("ALLOW", "DROPPED") and (prefix is None or d["label"].startswith(prefix))}

        self.assertEqual(cats("unknown_skill"), {"unavailable_command"})
        self.assertEqual(cats("unavailable_command"), {"unavailable_command"})
        self.assertEqual(cats("expired_observation"), {"stale_response"})
        self.assertEqual(cats("reordered_packet"), {"stale_response"})
        self.assertEqual(cats("duplicate_packet"), {"stale_response"})
        self.assertEqual(cats("delayed_reply", "too_late"), {"stale_response"})
        self.assertEqual(cats("operator_intervention", "during_1"), {"intervention"})
        self.assertEqual(cats("model_failure", "unapproved"), {"model_failure"})
        self.assertEqual(cats("model_failure", "step"), {"model_failure"})
        self.assertEqual(cats("model_failure", "rogue"), {"provenance"})
        self.assertEqual(self.report["totals"]["blocked_by_category"].keys() >= {
            "unavailable_command", "stale_response", "intervention", "model_failure"}, True)

    def test_reason_sets_per_category_are_disjoint(self):
        seen = {}
        for reason, cat in g.REASON_CATEGORY.items():
            seen.setdefault(cat, set()).add(reason)
        cats = list(seen)
        for i, a in enumerate(cats):
            for b in cats[i + 1:]:
                self.assertFalse(seen[a] & seen[b])

    def test_time_to_block_in_logical_ticks(self):
        ttb = {n: s["time_to_block_ticks"] for n, s in self.by_name.items()}
        self.assertEqual(ttb["delayed_reply"], 7)        # fault at 2, rejected on arrival at 9
        self.assertEqual(ttb["disconnection"], 7)        # link down at 4, watchdog HOLD at 11
        self.assertEqual(ttb["model_silent_watchdog"], 8)  # last good command 2, HOLD at 10 (= watchdog_ticks)
        self.assertEqual(ttb["duplicate_packet"], 0)
        self.assertEqual(ttb["operator_intervention"], 0)
        self.assertIsNone(ttb["baseline_valid"])
        self.assertEqual(self.by_name["disconnection"]["first_block_by"], "watchdog")
        self.assertEqual(self.by_name["delayed_reply"]["first_block_by"], "decision")

    def test_duplicate_and_reorder_dispatch_counts(self):
        self.assertEqual(self.by_name["duplicate_packet"]["counts"]["dispatched"], 1)
        self.assertEqual(self.by_name["duplicate_packet"]["counts"]["delivered"], 3)
        r = self.by_name["reordered_packet"]["counts"]
        self.assertEqual((r["delivered"], r["dispatched"]), (3, 2))

    def test_disconnection_drops_are_counted_not_skipped(self):
        c = self.by_name["disconnection"]["counts"]
        self.assertEqual(c["dropped_by_link"], 1)
        self.assertEqual(c["delivered"], c["dropped_by_link"] + c["allowed"] + c["held"] + c["rejected"])

    def test_crash_scenarios(self):
        s = self.by_name["process_crash_replay"]
        self.assertEqual((s["restarts"], s["final_epoch"]), (1, 2))
        reasons = {d["label"]: d["reason"] for d in s["deliveries"]}
        self.assertEqual(reasons["replay_s2"], "STALE_EPOCH")
        self.assertEqual(reasons["replay_unseen_seq"], "STALE_EPOCH")
        self.assertEqual(reasons["replay_before_resume"], "GATE_HOLD")
        s = self.by_name["process_crash_state_lost"]
        self.assertEqual(s["final_epoch"], 11)
        self.assertEqual([o["ok"] for o in s["gate_ops"] if o["call"] in ("resume", "grant_epoch")][:2], [False, False])

    def test_every_delivery_is_accounted_for(self):
        for s in self.report["scenarios"]:
            c = s["counts"]
            self.assertEqual(c["delivered"] + c["undelivered"], c["scheduled_deliveries"], s["name"])
            self.assertEqual(c["undelivered"], 0, s["name"])

    def test_invalid_argument_scenario_covers_the_documented_shapes(self):
        labels = {d["label"] for d in self.by_name["llm_invalid_args"]["deliveries"]}
        for needed in ("args_nan", "args_inf", "args_huge_int", "args_bool_value", "args_nested_dict_value",
                       "args_extra_args_key", "env_extra_top_level_key", "env_epoch_is_object",
                       "text_duplicate_keys", "text_nan_literal", "text_deeply_nested", "valid_after_garbage"):
            self.assertIn(needed, labels)


class DeterminismTests(unittest.TestCase):
    def test_two_runs_are_byte_identical(self):
        self.assertEqual(hx.render(_suite()), hx.render(_suite()))

    def test_report_has_no_wall_clock_or_random_content(self):
        text = hx.render(_suite())
        for needle in ("time.time", "datetime", "random", "uuid"):
            self.assertNotIn(needle, text)
        self.assertIn("SIMULATION", text)

    @unittest.skipIf(os.environ.get("ACTION_GATE_MUTATION_RUN") == "1", "mutation run: drift tests skipped")
    def test_committed_report_is_current(self):
        self.assertTrue(REPORT.exists(), "run run_fault_suite.py --write")
        self.assertEqual(REPORT.read_text(encoding="utf-8"), hx.render(_suite()))

    @unittest.skipIf(os.environ.get("ACTION_GATE_MUTATION_RUN") == "1", "mutation run: drift tests skipped")
    def test_cli_check_mode_passes(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = run_fault_suite.main(["--check"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("failure_count=0", err.getvalue())


class HarnessSelfCheckTests(unittest.TestCase):
    """The runner must itself notice wrong expectations (so a green suite means something)."""

    def _sc(self, expect, deliver=(1,), config=None, end=3, msg=None, extra=()):
        return {"name": "t", "description": "d", "fault_tick": 1, "end_tick": end,
                "config": config or {}, "events": [
                    {"at": 0, "op": "observe", "obs": hx.make_obs("obs-1", 0)},
                    *extra,
                    {"at": 1, "op": "send", "label": "m", "deliver": list(deliver), "expect": list(expect),
                     "msg": msg or hx.make_action()}]}

    def test_matching_expectation_passes(self):
        r = hx.run_scenario(self._sc(["ALLOW"]))
        self.assertEqual(r["failure_count"], 0)

    def test_wrong_expectations_are_counted_as_failures(self):
        r = hx.run_scenario(self._sc(["UNKNOWN_SKILL"]))
        self.assertEqual(r["failure_count"], 1)
        self.assertEqual(r["counts"]["fail_open"], 1)  # dispatched although a block was expected
        r = hx.run_scenario(self._sc(["ALLOW"], msg=hx.make_action(source_id="ROGUE")))
        self.assertEqual((r["failure_count"], r["counts"]["false_blocks"]), (1, 1))
        r = hx.run_scenario(self._sc(["DROPPED"]))
        self.assertEqual(r["failure_count"], 1)

    def test_right_verdict_wrong_reason_is_a_failure(self):
        r = hx.run_scenario(self._sc(["STALE_SEQUENCE"], msg=hx.make_action(source_id="ROGUE")))
        self.assertEqual(r["failure_count"], 1)

    def test_delivery_after_end_tick_is_reported_not_skipped(self):
        r = hx.run_scenario(self._sc(["ALLOW", "DUPLICATE_SEQUENCE"], deliver=(1, 9), end=3))
        self.assertEqual(r["counts"]["undelivered"], 1)
        self.assertGreaterEqual(r["failure_count"], 1)

    def test_unexpected_gate_operation_result_is_a_failure(self):
        extra = [{"at": 0, "op": "gate", "call": "resume"}]  # already autonomous: invalid transition
        r = hx.run_scenario(self._sc(["ALLOW"], extra=extra))
        self.assertEqual(r["failure_count"], 1)

    def test_scenario_authoring_errors_are_loud(self):
        with self.assertRaises(ValueError):
            hx.run_scenario(self._sc(["NOT_A_REASON"]))
        with self.assertRaises(ValueError):
            hx.run_scenario(self._sc(["ALLOW"], end=hx.MAX_END_TICK + 1))
        with self.assertRaises(ValueError):
            hx.run_scenario(self._sc(["ALLOW", "ALLOW"], deliver=(1,)))
        bad = self._sc(["ALLOW"], deliver=(0,))
        with self.assertRaises(ValueError):
            hx.run_scenario(bad)
        with self.assertRaises(ValueError):
            hx.run_suite([self._sc(["ALLOW"]), self._sc(["ALLOW"])])  # duplicate names
        with self.assertRaises(ValueError):
            hx.run_scenario(self._sc(["ALLOW"], extra=[{"at": 0, "op": "teleport"}]))

    def test_harness_refuses_unknown_gate_calls(self):
        with self.assertRaises(ValueError):
            hx.Harness().op("evaluate", 0)
        with self.assertRaises(ValueError):
            hx.Harness().crash_restart("melted")


if __name__ == "__main__":
    unittest.main()
