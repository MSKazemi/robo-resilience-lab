"""Fail-open and bypass regression tests: the gate must block, with a typed reason, whenever
it cannot prove an action valid. Each test names the bug class it guards against."""

import dataclasses
import json
import unittest
from unittest import mock

from tests.support import (INF, NAN, GateCase, Harness, Reason, Verdict, default_config, g, make_action,
                           make_obs, valid_args)


def _harness(**kw):
    h = Harness(default_config(), **kw)
    h.observe(make_obs("obs-1", 0), 0)
    return h


class CheckFailureTests(unittest.TestCase):
    """An exception inside a check, or a check that returns the wrong thing, must block."""

    def _run(self, check, where="extra"):
        kw = {"extra_checks": (("custom", check),)} if where == "extra" else {"pre_checks": (("custom", check),)}
        h = _harness(**kw)
        before = h.gate.snapshot()
        d, rec = h.submit(make_action(), 1)
        return h, before, d, rec

    def test_exception_in_a_check_blocks(self):
        for exc in (ValueError("x"), KeyError("k"), ZeroDivisionError(), RecursionError(), MemoryError(),
                    AttributeError("a"), TypeError("t")):
            for where in ("extra", "pre"):
                with self.subTest(exc=type(exc).__name__, where=where):
                    def boom(ctx, exc=exc):
                        raise exc
                    h, before, d, rec = self._run(boom, where)
                    self.assertIs(d.verdict, Verdict.REJECT)
                    self.assertEqual(d.reason, Reason.INTERNAL_ERROR)
                    self.assertEqual(d.field, "check.custom")
                    self.assertFalse(rec["dispatched"])
                    self.assertIsNone(h.sink.targets)
                    self.assertEqual(h.gate.snapshot(), before)

    def test_a_check_that_returns_none_or_anything_but_pass_blocks(self):
        bad_returns = [None, True, False, 0, 1, "PASS", "", {}, [], (), Verdict.ALLOW, Reason.EXPIRED,
                       g.Failure, object(), g.Failure("EXPIRED", "x")]
        for value in bad_returns:
            with self.subTest(value=repr(value)[:30]):
                h, before, d, rec = self._run(lambda ctx, v=value: v)
                self.assertIs(d.verdict, Verdict.REJECT)
                self.assertEqual(d.reason, Reason.CHECK_RETURNED_INVALID)
                self.assertFalse(rec["dispatched"])
                self.assertEqual(h.gate.snapshot(), before)

    def test_a_check_that_forgets_to_return_blocks(self):
        def forgetful(ctx):
            pass
        h, before, d, rec = self._run(forgetful)
        self.assertEqual(d.reason, Reason.CHECK_RETURNED_INVALID)

    def test_a_failure_returned_by_an_extra_check_is_honoured(self):
        h, before, d, rec = self._run(lambda ctx: g.Failure(Reason.OUT_OF_BOUNDS, "msg.x", "custom"))
        self.assertEqual(d.reason, Reason.OUT_OF_BOUNDS)
        self.assertFalse(rec["dispatched"])

    def test_pass_from_every_check_is_required(self):
        # the only way through is PASS from every check, including the custom one
        h, before, d, rec = self._run(lambda ctx: g.PASS)
        self.assertTrue(d.allowed)
        self.assertTrue(rec["dispatched"])

    def test_default_checks_cannot_be_removed_through_the_api(self):
        with self.assertRaises(TypeError):
            g.ActionGate(default_config(), g.EpochStore(), checks=())  # no such parameter
        with self.assertRaises(ValueError):
            g.ActionGate(default_config(), g.EpochStore(), extra_checks=(("n", None),))

    def test_allow_without_validated_targets_is_blocked(self):
        def erase(ctx):
            ctx.out.pop("targets", None)
            return g.PASS
        h, before, d, rec = self._run(erase)
        self.assertEqual(d.reason, Reason.INTERNAL_ERROR)
        self.assertFalse(rec["dispatched"])
        self.assertEqual(h.gate.snapshot(), before)

    def test_internal_bookkeeping_error_gives_constant_reject(self):
        h = _harness()
        before = h.gate.snapshot()
        with mock.patch.object(h.gate, "_record", side_effect=RuntimeError("boom")):
            d, rec = h.submit(make_action(), 1)
        self.assertIs(d.verdict, Verdict.REJECT)
        self.assertEqual(d.reason, Reason.INTERNAL_ERROR)
        self.assertEqual(d.decision_id, -1)
        self.assertFalse(rec["dispatched"])
        self.assertEqual(h.gate.snapshot(), before)

    def test_audit_failure_blocks_an_allow_and_changes_no_state(self):
        calls = []

        def hook(rec):
            calls.append(rec)
            if rec.get("kind") == "decision":
                raise OSError("audit disk full")

        h = Harness(default_config(), bootstrap=False)
        h.gate._audit_hook = hook
        h.op("resume", 0)
        h.observe(make_obs("obs-1", 0), 0)
        before = h.gate.snapshot()
        d, rec = h.submit(make_action(), 1)
        self.assertIs(d.verdict, Verdict.REJECT)
        self.assertEqual(d.reason, Reason.AUDIT_FAILURE)
        self.assertFalse(rec["dispatched"])
        self.assertEqual(h.gate.snapshot(), before)
        self.assertEqual(h.gate.last_sequence, 0)
        # the same message is still allowed once audit works again (it was not burned)
        h.gate._audit_hook = None
        d, rec = h.submit(make_action(), 1)
        self.assertTrue(d.allowed)

    def test_audit_hook_failure_on_a_blocked_decision_stays_blocked(self):
        h = _harness()
        h.gate._audit_hook = mock.Mock(side_effect=OSError("down"))
        d, rec = h.submit(make_action(source_id="ROGUE"), 1)
        self.assertEqual(d.reason, Reason.UNAUTHORIZED_SOURCE)
        self.assertFalse(rec["dispatched"])
        self.assertGreaterEqual(h.gate.audit_hook_failures, 1)


TYPE_CONFUSERS = [
    None, True, False, 0, 1, -1, 2, 1.5, NAN, INF, -INF, 10**400, -10**400, "", "x", "1", "autonomous", [],
    [1], [[1]], {}, {"a": 1}, (1,), b"x", frozenset({1}), object(), 1j, set(),
]


class TypeConfusionTests(GateCase):
    """Each case uses a fresh gate so one allowed message cannot make the next a duplicate."""

    def _try(self, msg):
        h = _harness()
        d, rec = h.submit(msg, 1)
        self.assertNotEqual(d.reason, Reason.INTERNAL_ERROR, "type confusion collapsed to INTERNAL_ERROR")
        self.assertIsInstance(d.field, str)
        if not d.allowed:
            self.assertIsInstance(d.reason, Reason)
            self.assertFalse(rec["dispatched"])
        return d, rec

    def test_every_top_level_field_with_every_confuser_never_raises_and_is_typed(self):
        base = make_action()
        for name in base:
            for bad in TYPE_CONFUSERS:
                msg = make_action()
                msg[name] = bad
                with self.subTest(field=name, bad=repr(bad)[:25]):
                    d, _ = self._try(msg)
                    if d.allowed:  # only a value of the exact same type (never bool for int) may pass
                        self.assertIs(type(bad), type(base[name]))
                        if name not in ("sequence_id", "created_tick", "expires_tick"):
                            self.assertEqual(bad, base[name])
                        # (for those three fields other integers, e.g. 2, are legitimately valid)

    def test_argument_fields_with_every_confuser(self):
        for key in ("action_space", "units", "values"):
            for bad in TYPE_CONFUSERS:
                args = valid_args()
                args[key] = bad
                with self.subTest(arg=key, bad=repr(bad)[:25]):
                    d, _ = self._try(make_action(args=args))
                    self.assertFalse(d.allowed)

    def test_every_element_of_values_and_units_with_every_confuser(self):
        for key in ("units", "values"):
            for i in range(6):
                for bad in TYPE_CONFUSERS:
                    args = valid_args()
                    args[key][i] = bad
                    with self.subTest(arg=key, i=i, bad=repr(bad)[:25]):
                        d, _ = self._try(make_action(args=args))
                        if d.allowed:  # only a legitimately valid number may pass
                            self.assertEqual(key, "values")
                            self.assertIn(type(bad), (int, float))
                            self.assertTrue(abs(bad) <= 0.04)

    def test_bool_is_not_an_int_anywhere(self):
        self.assertBlocked(make_action(epoch=True), Reason.WRONG_TYPE)
        self.assertBlocked(make_action(sequence_id=True), Reason.WRONG_TYPE)
        self.assertBlocked(make_action(created_tick=True), Reason.WRONG_TYPE)
        self.assertBlocked(make_action(expires_tick=True), Reason.WRONG_TYPE)
        self.assertBlocked(make_action(args=valid_args([True, 0, 0, 0, 0, 0])), Reason.BAD_ARGUMENT_TYPE)
        self.assertBlocked(make_action(args=valid_args([0, 0, 0, 0, 0, False])), Reason.BAD_ARGUMENT_TYPE)
        # "simulated" must be the boolean True: the integer 1 is not accepted
        self.assertBlocked(make_action(simulated=1), Reason.WRONG_TYPE)
        res = self.h.observe(make_obs("o2", True), 1)
        self.assertFalse(res.ok)

    def test_unhashable_values_where_a_string_is_looked_up(self):
        for field in ("source_id", "model_id", "model_revision", "command", "control_domain", "mode",
                      "observation_id", "schema"):
            for bad in ([], {}, [1], {"a": 1}, set(), ([],)):
                with self.subTest(field=field, bad=type(bad).__name__):
                    self.assertBlocked(make_action(**{field: bad}), Reason.WRONG_TYPE, field=f"msg.{field}")


class HostileObjectTests(GateCase):
    """Objects that would misbehave if the gate hashed, compared, sized or iterated them."""

    def test_str_subclass_with_hostile_methods_is_rejected_by_exact_type(self):
        touched = []

        class EvilStr(str):
            def __eq__(self, other):
                touched.append("eq")
                return True

            def __hash__(self):
                touched.append("hash")
                return 1

        for field in ("source_id", "model_id", "model_revision", "command", "control_domain", "mode", "schema"):
            with self.subTest(field=field):
                d, rec = self.h.submit(make_action(**{field: EvilStr("MOCK-policy-01")}), 1)
                self.assertEqual(d.reason, Reason.WRONG_TYPE)
                self.assertFalse(rec["dispatched"])
        self.assertEqual(touched, [], "gate called a method of an untrusted object")

    def test_int_and_float_subclasses_are_rejected(self):
        class MyInt(int):
            pass

        class MyFloat(float):
            pass

        self.assertBlocked(make_action(epoch=MyInt(1)), Reason.WRONG_TYPE)
        self.assertBlocked(make_action(sequence_id=MyInt(1)), Reason.WRONG_TYPE)
        self.assertBlocked(make_action(args=valid_args([MyFloat(0.1), 0, 0, 0, 0, 0])), Reason.BAD_ARGUMENT_TYPE)
        self.assertBlocked(make_action(args=valid_args([MyInt(0), 0, 0, 0, 0, 0])), Reason.BAD_ARGUMENT_TYPE)

    def test_dict_and_list_subclasses_are_rejected(self):
        class D(dict):
            def __getitem__(self, k):
                raise AssertionError("must not be called")

        class L(list):
            def __len__(self):
                raise AssertionError("must not be called")

        self.assertBlocked(D(make_action()), Reason.MALFORMED_MESSAGE)
        self.assertBlocked(make_action(args=D(valid_args())), Reason.WRONG_TYPE)
        self.assertBlocked(make_action(args=valid_args(L([0.0] * 6))), Reason.BAD_ARGUMENT_TYPE)

    def test_huge_deep_and_cyclic_values_do_not_hang_or_raise(self):
        deep = cur = []
        for _ in range(50_000):
            nxt = []
            cur.append(nxt)
            cur = nxt
        cyclic = {}
        cyclic["self"] = cyclic
        cyc_list = []
        cyc_list.append(cyc_list)
        cases = [
            make_action(args=valid_args([deep, 0, 0, 0, 0, 0])),
            make_action(args=valid_args([cyclic, 0, 0, 0, 0, 0])),
            make_action(args=valid_args(cyc_list)),
            make_action(source_id="x" * 5_000_000),
            make_action(command="x" * 5_000_000),
            make_action(control_domain="x" * 5_000_000),
            make_action(mode="x" * 5_000_000),
            make_action(args={"k" * 1000: 1}),
            make_action(**{"x" * 1_000_000: 1}),
        ]
        for msg in cases:
            d, rec = self.h.submit(msg, 1)
            self.assertFalse(d.allowed)
            self.assertNotEqual(d.reason, Reason.INTERNAL_ERROR)
            self.assertFalse(rec["dispatched"])

    def test_audit_text_is_sanitised_and_bounded(self):
        evil = "set\n\x1b[31mjoint\r\nFORGED audit_id=1 ALLOW" + "z" * 500
        d, _ = self.h.submit(make_action(command=evil, source_id="a\nb"), 1)
        rec = self.gate.audit_log[-1]
        self.assertEqual(rec["audit_id"], d.audit_id)
        for key in ("command", "source_id", "field", "detail"):
            value = rec[key]
            if value is not None:
                self.assertNotRegex(value, r"[\x00-\x1f\x7f]")
                self.assertLessEqual(len(value), 130)
        self.assertEqual(rec["verdict"], "REJECT")


class ClockAndTextTests(GateCase):
    def test_invalid_now_is_a_typed_block_not_an_exception(self):
        for bad in (None, "5", 5.0, True, -1, 10**400, NAN, [], {}, object()):
            with self.subTest(now=repr(bad)[:20]):
                before = self.gate.snapshot()
                d, rec = self.h.submit(make_action(), bad)
                self.assertEqual(d.reason, Reason.INVALID_CLOCK)
                self.assertFalse(rec["dispatched"])
                self.assertEqual(self.gate.snapshot(), before)

    def test_clock_regression_blocks_and_changes_nothing(self):
        self.assertAllowed(make_action(seq=1, created=3, expires=8), now=3)
        self.assertBlocked(make_action(seq=2, created=2, expires=8), Reason.CLOCK_REGRESSION, now=2)
        self.assertAllowed(make_action(seq=2, created=3, expires=8), now=3)  # equal clock is fine

    def test_text_input_is_parsed_strictly(self):
        good = json.dumps(make_action())
        d, rec = self.h.submit(good, 1, as_text=True)
        self.assertTrue(d.allowed)
        cases = {
            "not json": "{not json",
            "empty": "",
            "duplicate keys": '{"a": 1, "a": 2}',
            "nan literal": good.replace("0.1", "NaN", 1),
            "infinity literal": good.replace("0.1", "Infinity", 1),
            "minus infinity": good.replace("0.1", "-Infinity", 1),
            "list": "[1]",
            "scalar": "5",
            "null": "null",
            "deep nesting": "[" * 60_000,
            "oversize": " " * (g.MAX_TEXT + 1),
            "bytes": b"{}",
            "none": None,
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                self.assertBlocked(text, Reason.MALFORMED_MESSAGE, now=1, as_text=True)

    def test_text_with_duplicate_nested_keys_in_args_is_rejected(self):
        text = good = json.dumps(make_action())
        text = text.replace('"gain"', '"gain"')
        dup = good[:-1] + ', "epoch": 1}'
        self.assertBlocked(dup, Reason.MALFORMED_MESSAGE, now=2, as_text=True)

    def test_huge_integer_text_is_a_typed_block(self):
        text = json.dumps(make_action()).replace('"sequence_id": 1', '"sequence_id": ' + "9" * 400)
        d, rec = self.h.submit(text, 1, as_text=True)
        self.assertEqual(d.reason, Reason.INTEGER_OUT_OF_RANGE)
        text = json.dumps(make_action()).replace('"sequence_id": 1', '"sequence_id": ' + "9" * 5000)
        d, rec = self.h.submit(text, 1, as_text=True)
        self.assertEqual(d.reason, Reason.MALFORMED_MESSAGE)  # CPython int-digit limit -> unparseable
        self.assertFalse(rec["dispatched"])


class DecisionIntegrityTests(GateCase):
    def test_a_blocked_decision_never_carries_targets_and_is_never_allowed(self):
        for msg in (make_action(source_id="x y"), make_action(command="cmd_vel"), None, 5):
            d, _ = self.h.submit(msg, 1)
            self.assertIsNone(d.targets)
            self.assertFalse(d.allowed)
            self.assertIn(d.verdict, (Verdict.HOLD, Verdict.REJECT))

    def test_decision_is_immutable(self):
        d, _ = self.assertAllowed(make_action())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            d.verdict = Verdict.REJECT
        self.assertIsInstance(d.targets, tuple)


if __name__ == "__main__":
    unittest.main()
