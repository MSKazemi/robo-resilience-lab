"""Per-check positive and negative tests: each block has a typed reason and field path."""

import copy
import unittest

from tests.support import (INF, NAN, Category, GateCase, Reason, Verdict, default_config, g, make_action,
                           make_obs, valid_args)


class PositiveTests(GateCase):
    def test_valid_move_is_allowed_and_dispatched_with_validated_targets(self):
        d, rec = self.assertAllowed(make_action())
        self.assertEqual(d.command, "set_joint_targets")
        self.assertEqual(d.targets, (0.1, 0.0, 0.0, 0.0, 0.0, 0.01))
        self.assertEqual(rec["sim_effect"], "targets_applied")
        self.assertEqual(self.sink.targets, d.targets)
        self.assertEqual(self.gate.last_sequence, 1)

    def test_hold_command_is_allowed_and_changes_no_targets(self):
        self.assertAllowed(make_action(seq=1))
        before = self.sink.targets
        d, rec = self.assertAllowed(make_action(seq=2, command="hold", args={}), now=2)
        self.assertEqual(rec["sim_effect"], "hold_pose")
        self.assertEqual(self.sink.targets, before)

    def test_integer_values_and_limit_values_are_accepted(self):
        vals = [0, 0, 0, 0, 0, 0]  # ints are numbers too (JSON gives ints for 0)
        d, _ = self.assertAllowed(make_action(args=valid_args(vals)))
        self.assertEqual(d.targets, (0.0,) * 6)
        # exactly at the joint limit and at the per-step bound
        vals = [0.5, 0.5, -0.5, 0.5, -0.5, 0.04]
        d, _ = self.assertAllowed(make_action(seq=2, args=valid_args(vals)), now=2)
        self.assertEqual(d.targets, tuple(vals))

    def test_decision_dict_is_json_friendly(self):
        d, _ = self.send(make_action(seq=1, source_id="ROGUE"))
        out = d.as_dict()
        self.assertEqual(out["reason"], "UNAUTHORIZED_SOURCE")
        self.assertEqual(out["verdict"], "REJECT")


class EnvelopeTests(GateCase):
    def test_not_an_object(self):
        for bad in (None, 5, "x", [], [make_action()], (1,), b"x", object(), True):
            with self.subTest(bad=type(bad).__name__):
                self.assertBlocked(bad, Reason.MALFORMED_MESSAGE, field="msg")

    def test_missing_each_required_field(self):
        for name, _kind in g._REQUIRED:
            with self.subTest(field=name):
                self.assertBlocked(make_action(drop=(name,)), Reason.MISSING_FIELD, field=f"msg.{name}")

    def test_unknown_field_is_rejected_not_ignored(self):
        self.assertBlocked(make_action(velocity=1.0), Reason.UNKNOWN_FIELD, field="msg.velocity")

    def test_non_string_key_and_too_many_keys(self):
        msg = make_action()
        msg[1] = "x"
        self.assertBlocked(msg, Reason.MALFORMED_MESSAGE)
        msg = make_action()
        for i in range(g.MAX_KEYS):
            msg[f"k{i}"] = i
        self.assertBlocked(msg, Reason.MALFORMED_MESSAGE)

    def test_wrong_types_per_field(self):
        cases = {
            "schema": 1, "simulated": 1, "source_id": 1, "model_id": [], "model_revision": {},
            "control_domain": None, "command": 1.5, "mode": [], "epoch": "1", "sequence_id": "1",
            "observation_id": None, "created_tick": 1.0, "expires_tick": "6", "args": [],
        }
        for name, bad in cases.items():
            with self.subTest(field=name):
                self.assertBlocked(make_action(**{name: bad}), Reason.WRONG_TYPE, field=f"msg.{name}")

    def test_integer_fields_reject_bool_float_negative_zero_and_huge(self):
        for name in ("epoch", "sequence_id"):
            for bad, reason in ((True, Reason.WRONG_TYPE), (1.0, Reason.WRONG_TYPE),
                                (0, Reason.INTEGER_OUT_OF_RANGE), (-1, Reason.INTEGER_OUT_OF_RANGE),
                                (2**53 + 1, Reason.INTEGER_OUT_OF_RANGE), (10**400, Reason.INTEGER_OUT_OF_RANGE)):
                with self.subTest(field=name, bad=bad):
                    self.assertBlocked(make_action(**{name: bad}), reason, field=f"msg.{name}")
        self.assertBlocked(make_action(created_tick=-1), Reason.INTEGER_OUT_OF_RANGE)
        self.assertBlocked(make_action(created_tick=False), Reason.WRONG_TYPE)

    def test_identifiers_use_fullmatch_ascii_only(self):
        for field in ("source_id", "model_id", "model_revision", "observation_id"):
            for bad in ("", "a b", "obs-1\n", "obs-1 ", "-x", "x" * 65, "١٢", "a\x00b", "café"):
                with self.subTest(field=field, bad=repr(bad)):
                    self.assertBlocked(make_action(**{field: bad}), Reason.BAD_IDENTIFIER, field=f"msg.{field}")

    def test_schema_version_and_simulated_flag(self):
        self.assertBlocked(make_action(schema="action-gate/v2"), Reason.UNSUPPORTED_SCHEMA)
        self.assertBlocked(make_action(schema="action-gate/v1\n"), Reason.UNSUPPORTED_SCHEMA)
        self.assertBlocked(make_action(simulated=False), Reason.NOT_SIMULATED)


class ProvenanceAndCommandTests(GateCase):
    def test_source_must_be_allow_listed(self):
        self.assertBlocked(make_action(source_id="ROGUE-agent"), Reason.UNAUTHORIZED_SOURCE, field="msg.source_id")

    def test_default_config_blocks_everything(self):
        from tests.support import Harness
        h = Harness(g.GateConfig())  # no allowed source, no approved model
        h.observe(make_obs("obs-1", 0), 0)
        d, rec = h.submit(make_action(), 1)
        self.assertEqual(d.reason, Reason.UNAUTHORIZED_SOURCE)
        self.assertFalse(rec["dispatched"])

    def test_model_and_revision_must_be_approved(self):
        self.assertBlocked(make_action(model_id="other"), Reason.MODEL_NOT_APPROVED, field="msg.model_id")
        self.assertBlocked(make_action(model_revision="r2"), Reason.MODEL_NOT_APPROVED, field="msg.model_revision")

    def test_approval_is_whole_value_not_substring_or_prefix(self):
        for rev in ("r", "r11", "R1", "r1-extra"):
            with self.subTest(rev=rev):
                self.assertBlocked(make_action(model_revision=rev), Reason.MODEL_NOT_APPROVED)
        self.assertBlocked(make_action(source_id="MOCK-policy-0"), Reason.UNAUTHORIZED_SOURCE)
        self.assertBlocked(make_action(source_id="MOCK-policy-011"), Reason.UNAUTHORIZED_SOURCE)

    def test_unknown_skills_are_not_allow_listed(self):
        for cmd in ("cmd_vel", "/cmd_vel", "shell", "rm -rf /", "", "SET_JOINT_TARGETS",
                    "set_joint_targets ", "set_joint_targets\n", "hold\x00", "x" * 100,
                    "set_joint_targets;cmd_vel"):
            with self.subTest(cmd=cmd[:20]):
                self.assertBlocked(make_action(command=cmd), Reason.UNKNOWN_SKILL, field="msg.command")

    def test_disabled_skill_is_distinct_from_unknown(self):
        from tests.support import Harness
        h = Harness(default_config(enabled_commands=["hold"]))
        h.observe(make_obs("obs-1", 0), 0)
        d, _ = h.submit(make_action(), 1)
        self.assertEqual(d.reason, Reason.COMMAND_DISABLED)
        self.assertEqual(d.category, Category.UNAVAILABLE_COMMAND.value)

    def test_control_domain_is_restricted_to_simulation(self):
        for dom in ("physical", "real", "/dev/ttyUSB0", "SIM-ARM", "sim-arm ", "", "sim-arm\n", "x" * 100):
            with self.subTest(domain=dom[:12]):
                self.assertBlocked(make_action(control_domain=dom), Reason.CONTROL_DOMAIN_DENIED)

    def test_mode_must_be_known_and_match_the_gate(self):
        for m in ("manual", "AUTONOMOUS", "autonomous ", "", "x" * 40):
            with self.subTest(mode=m):
                self.assertBlocked(make_action(mode=m), Reason.UNKNOWN_MODE, field="msg.mode")
        for m in ("hold", "intervention", "safe_stop", "locked"):
            with self.subTest(mode=m):
                self.assertBlocked(make_action(mode=m), Reason.MODE_MISMATCH, field="msg.mode")


class ArgumentTests(GateCase):
    def test_wrong_dimension(self):
        for n in (0, 1, 5, 7, 100):
            with self.subTest(n=n):
                self.assertBlocked(make_action(args=valid_args([0.0] * n)), Reason.WRONG_DIMENSION,
                                   field="msg.args.values")
        args = valid_args()
        args["units"] = ["rad"] * 5
        self.assertBlocked(make_action(args=args), Reason.WRONG_DIMENSION, field="msg.args.units")

    def test_huge_list_is_rejected_by_length_without_iterating(self):
        self.assertBlocked(make_action(args=valid_args([0.0] * 5_000_000)), Reason.WRONG_DIMENSION)

    def test_units_per_dimension(self):
        for i in range(6):
            args = valid_args()
            args["units"][i] = "deg" if i != 5 else "mm"
            with self.subTest(i=i):
                self.assertBlocked(make_action(args=args), Reason.WRONG_UNITS, field=f"msg.args.units[{i}]")
        args = valid_args()
        args["units"][2] = 7
        self.assertBlocked(make_action(args=args), Reason.BAD_ARGUMENT_TYPE)

    def test_non_numbers_in_values(self):
        for bad in ("0.1", None, True, False, [1], [[1]], {"a": 1}, b"1", (1,), object(), 1j):
            with self.subTest(bad=type(bad).__name__):
                self.assertBlocked(make_action(args=valid_args([bad, 0, 0, 0, 0, 0])),
                                   Reason.BAD_ARGUMENT_TYPE, field="msg.args.values[0]")

    def test_nan_and_infinity(self):
        for bad in (NAN, INF, -INF):
            with self.subTest(bad=bad):
                self.assertBlocked(make_action(args=valid_args([0, 0, 0, 0, 0, bad])),
                                   Reason.NON_FINITE_VALUE, field="msg.args.values[5]")

    def test_out_of_bounds_per_joint(self):
        limits = g.JOINT_LIMITS
        for i, (lo, hi) in enumerate(limits):
            for bad in (hi + 0.001, lo - 0.001, 1e308, -1e308, 10**400, -10**400):
                vals = [0.0] * 6
                vals[i] = bad
                with self.subTest(i=i, bad=bad):
                    self.assertBlocked(make_action(args=valid_args(vals)), Reason.OUT_OF_BOUNDS,
                                       field=f"msg.args.values[{i}]")

    def test_negative_gripper_and_just_over_gripper_limit(self):
        self.assertBlocked(make_action(args=valid_args([0, 0, 0, 0, 0, -0.001])), Reason.OUT_OF_BOUNDS)
        self.assertBlocked(make_action(args=valid_args([0, 0, 0, 0, 0, 0.081])), Reason.OUT_OF_BOUNDS)

    def test_trajectory_step_bound_is_relative_to_the_observed_state(self):
        # state is 0.0; per-step bound for joint 1 is 0.5 (inclusive)
        self.assertBlocked(make_action(args=valid_args([0, 0.5001, 0, 0, 0, 0])), Reason.STEP_TOO_LARGE,
                           field="msg.args.values[1]")
        self.assertAllowed(make_action(args=valid_args([0, 0.5, 0, 0, 0, 0])))
        # a different observed state moves the window
        self.h.observe(make_obs("obs-2", 1, state=[0, 1.0, 0, 0, 0, 0]), 1)
        self.assertAllowed(make_action(seq=2, created=1, expires=6, obs="obs-2",
                                       args=valid_args([0, 1.4, 0, 0, 0, 0])))
        self.assertBlocked(make_action(seq=3, created=1, expires=6, obs="obs-2",
                                       args=valid_args([0, 0.4, 0, 0, 0, 0])), Reason.STEP_TOO_LARGE, now=1)

    def test_gripper_step_bound(self):
        self.assertBlocked(make_action(args=valid_args([0, 0, 0, 0, 0, 0.05])), Reason.STEP_TOO_LARGE)

    def test_argument_object_shape(self):
        args = valid_args()
        args["gain"] = 1
        self.assertBlocked(make_action(args=args), Reason.BAD_ARGUMENT_FIELDS, field="msg.args.gain")
        args = valid_args()
        del args["units"]
        self.assertBlocked(make_action(args=args), Reason.BAD_ARGUMENT_FIELDS, field="msg.args.units")
        args = valid_args()
        args[3] = "x"
        self.assertBlocked(make_action(args=args), Reason.BAD_ARGUMENT_FIELDS)
        self.assertBlocked(make_action(command="hold", args={"x": 1}), Reason.BAD_ARGUMENT_FIELDS)

    def test_action_space(self):
        self.assertBlocked(make_action(args=valid_args() | {"action_space": "cartesian_velocity"}),
                           Reason.WRONG_ACTION_SPACE)
        self.assertBlocked(make_action(args=valid_args() | {"action_space": ["joint_position_target"]}),
                           Reason.BAD_ARGUMENT_TYPE)
        self.assertBlocked(make_action(args=valid_args() | {"action_space": "joint_position_target\n"}),
                           Reason.WRONG_ACTION_SPACE)


class TimelineTests(GateCase):
    def test_equal_timestamp_semantics_are_explicit(self):
        # created == now is valid; created == now + 1 is the future
        self.assertAllowed(make_action(seq=1, created=3, expires=8), now=3)
        self.assertBlocked(make_action(seq=2, created=4, expires=8), Reason.FUTURE_TIMESTAMP, now=3)
        # expires == now is expired (exclusive); expires == now + 1 is valid
        self.assertBlocked(make_action(seq=2, created=1, expires=4), Reason.EXPIRED, now=4)
        self.assertAllowed(make_action(seq=2, created=1, expires=5), now=4)

    def test_observation_age_is_half_open(self):
        # obs tick 0, ttl 5: age 4 valid, age 5 stale
        self.assertAllowed(make_action(seq=1, created=4, expires=9), now=4)
        self.assertBlocked(make_action(seq=2, created=5, expires=9), Reason.STALE_OBSERVATION, now=5,
                           field="msg.observation_id")

    def test_expiry_must_follow_creation_and_be_bounded(self):
        self.assertBlocked(make_action(created=2, expires=2), Reason.INVALID_EXPIRY, now=2)
        self.assertBlocked(make_action(created=2, expires=1), Reason.INVALID_EXPIRY, now=2)
        self.assertBlocked(make_action(created=2, expires=2 + 21), Reason.INVALID_EXPIRY, now=2)
        self.assertAllowed(make_action(created=2, expires=2 + 20), now=2)

    def test_unknown_observation_and_observation_after_action(self):
        self.assertBlocked(make_action(obs="obs-zzz"), Reason.UNKNOWN_OBSERVATION)
        self.h.observe(make_obs("obs-2", 3), 3)
        self.assertBlocked(make_action(obs="obs-2", created=2, expires=6), Reason.INVALID_TIMELINE, now=3)

    def test_registry_eviction_fails_closed(self):
        from tests.support import Harness
        h = Harness(default_config(observation_registry_size=2, observation_ttl_ticks=50))
        for i, name in enumerate(("a", "b", "c")):
            self.assertTrue(h.observe(make_obs(name, 0), 0).ok)
        d, rec = h.submit(make_action(obs="a"), 1)
        self.assertEqual(d.reason, Reason.UNKNOWN_OBSERVATION)
        self.assertFalse(rec["dispatched"])
        d, rec = h.submit(make_action(obs="c"), 1)
        self.assertTrue(d.allowed)
        self.assertEqual(h.gate.observations_evicted, 1)  # the eviction was counted, not silent


class ObservationRegistrationTests(GateCase):
    def test_bad_observations_are_rejected_without_exceptions(self):
        bad = [
            None, [], "x", {}, {"observation_id": "a"}, {"observation_id": "a", "tick": 0, "state": [0] * 6, "x": 1},
            make_obs("a b"), make_obs("a", -1), make_obs("a", True), make_obs("a", 99),
            make_obs("a", 0, [0] * 5), make_obs("a", 0, [NAN] + [0] * 5), make_obs("a", 0, [9.0] + [0] * 5),
            make_obs("a", 0, ["0"] + [0] * 5), make_obs("a", 0, [True] + [0] * 5), make_obs("a", 0, "000000"),
            make_obs("a", 0, [10**400] + [0] * 5), {1: 2},
        ]
        for obs in bad:
            with self.subTest(obs=repr(obs)[:40]):
                before = self.gate.snapshot()
                res = self.h.observe(obs, 1)
                self.assertFalse(res.ok)
                self.assertIsInstance(res.reason, Reason)
                self.assertEqual(self.gate.snapshot(), before)

    def test_duplicate_observation_id_is_rejected_not_replaced(self):
        res = self.h.observe(make_obs("obs-1", 1, [0, 0.4, 0, 0, 0, 0]), 1)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, Reason.DUPLICATE_OBSERVATION)
        self.assertEqual(self.gate.lookup_observation("obs-1").state, (0.0,) * 6)


class ConfigTests(unittest.TestCase):
    def test_invalid_config_raises_at_construction(self):
        for kw in (dict(max_step=(1,)), dict(max_step=(0,) * 6), dict(watchdog_ticks=0),
                   dict(watchdog_ticks=True), dict(enabled_commands=["cmd_vel"]), dict(allowed_sources="abc"),
                   dict(approved_models=[1]), dict(observation_ttl_ticks=-1), dict(audit_capacity=0)):
            with self.subTest(kw=kw):
                with self.assertRaises(ValueError):
                    g.GateConfig(**kw)

    def test_config_is_copied_and_frozen(self):
        models = {"m": {"r"}}
        cfg = g.GateConfig(approved_models=models)
        models["m"].add("evil")
        self.assertEqual(cfg.approved_models["m"], frozenset({"r"}))
        with self.assertRaises(TypeError):
            cfg.approved_models["x"] = frozenset()

    def test_every_reason_has_a_category_and_a_verdict(self):
        for reason in Reason:
            self.assertIn(reason, g.REASON_CATEGORY)
            self.assertIn(g.verdict_for(reason), (Verdict.HOLD, Verdict.REJECT))
        self.assertEqual(g.verdict_for(Reason.INTERVENTION_ACTIVE), Verdict.HOLD)
        self.assertEqual(g.verdict_for(Reason.GATE_HOLD), Verdict.HOLD)
        self.assertEqual(g.verdict_for(Reason.EXPIRED), Verdict.REJECT)

    def test_message_is_not_mutated_by_evaluation(self):
        from tests.support import Harness
        hh = Harness(default_config())
        hh.observe(make_obs("obs-1", 0), 0)
        msg = make_action()
        snapshot = copy.deepcopy(msg)
        hh.submit(msg, 1)
        self.assertEqual(msg, snapshot)


if __name__ == "__main__":
    unittest.main()
