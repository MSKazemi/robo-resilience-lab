"""The simulated sink: a blocked command is never dispatched; dispatch bypasses are refused."""

import dataclasses
import unittest

from sim_sink import Refusal, SimActuatorSink, _targets_ok
from tests.support import NAN, GateCase, Harness, Mode, Reason, Verdict, default_config, g, make_action, make_obs, valid_args


class BlockedNeverDispatchedTests(GateCase):
    def test_every_blocked_decision_is_logged_with_dispatched_false(self):
        blocked = [
            make_action(source_id="ROGUE"), make_action(command="cmd_vel"), make_action(epoch=9),
            make_action(args=valid_args([NAN, 0, 0, 0, 0, 0])), make_action(created=1, expires=1), None, 5,
            make_action(model_revision="r9"), make_action(control_domain="physical"),
        ]
        for msg in blocked:
            d, rec = self.send(msg, 1)
            self.assertFalse(d.allowed)
            self.assertFalse(rec["dispatched"])
            self.assertEqual(rec["refusal"], Refusal.GATE_BLOCKED.value)
            self.assertIsNone(rec["sim_effect"])
        self.assertEqual(self.sink.dispatched_count, 0)
        self.assertEqual(self.sink.blocked_count, len(blocked))
        self.assertEqual(len(self.sink.log), len(blocked))
        self.assertIsNone(self.sink.targets)

    def test_hold_verdicts_are_not_dispatched_either(self):
        self.h.op("intervene", 1)
        d, rec = self.send(make_action(), 1)
        self.assertIs(d.verdict, Verdict.HOLD)
        self.assertFalse(rec["dispatched"])

    def test_allowed_decision_is_dispatched_once_with_the_validated_targets(self):
        d, rec = self.assertAllowed(make_action())
        self.assertEqual(rec["sim_effect"], "targets_applied")
        self.assertEqual(self.sink.dispatched_count, 1)
        self.assertEqual(self.sink.targets, d.targets)


class BypassTests(GateCase):
    def _allow_without_dispatch(self, **kw):
        d = self.gate.evaluate(make_action(**kw), 1)
        self.assertTrue(d.allowed)
        return d

    def test_forged_allow_decision_is_refused(self):
        forged = g.Decision(Verdict.ALLOW, None, None, "", "", 999, 1, 1, "set_joint_targets",
                            (0.1, 0, 0, 0, 0, 0.01), 1, 1, 6)
        rec = self.sink.apply(forged, 1)
        self.assertFalse(rec["dispatched"])
        self.assertEqual(rec["refusal"], Refusal.DECISION_NOT_PENDING.value)

    def test_a_copy_of_a_real_decision_is_refused_and_does_not_consume_the_real_one(self):
        d = self._allow_without_dispatch()
        copy = dataclasses.replace(d)
        self.assertEqual(copy, d)
        rec = self.sink.apply(copy, 1)
        self.assertFalse(rec["dispatched"])
        self.assertEqual(rec["refusal"], Refusal.DECISION_NOT_PENDING.value)
        self.assertTrue(self.sink.apply(d, 1)["dispatched"])  # the genuine one still works

    def test_replaying_the_same_decision_is_refused(self):
        d = self._allow_without_dispatch()
        self.assertTrue(self.sink.apply(d, 1)["dispatched"])
        again = self.sink.apply(d, 1)
        self.assertFalse(again["dispatched"])
        self.assertEqual(again["refusal"], Refusal.DECISION_NOT_PENDING.value)
        self.assertEqual(self.sink.dispatched_count, 1)

    def test_decision_from_another_gate_is_refused(self):
        other = Harness(default_config())
        other.observe(make_obs("obs-1", 0), 0)
        d = other.gate.evaluate(make_action(), 1)
        self.assertTrue(d.allowed)
        rec = self.sink.apply(d, 1)
        self.assertFalse(rec["dispatched"])

    def test_message_mutation_after_the_check_changes_nothing(self):
        msg = make_action()
        d = self.gate.evaluate(msg, 1)
        msg["args"]["values"][0] = 99.0   # TOCTOU attempt
        msg["command"] = "cmd_vel"
        rec = self.sink.apply(d, 1)
        self.assertTrue(rec["dispatched"])
        self.assertEqual(self.sink.targets, (0.1, 0.0, 0.0, 0.0, 0.0, 0.01))

    def test_decision_issued_before_a_hold_is_void(self):
        d = self._allow_without_dispatch()
        self.h.op("hold", 2)
        self.h.op("resume", 2)  # back to autonomous: the old decision must still be dead
        rec = self.sink.apply(d, 2)
        self.assertFalse(rec["dispatched"])
        self.assertIsNone(self.sink.targets)

    def test_decision_issued_before_a_new_epoch_is_void(self):
        d = self._allow_without_dispatch()
        self.h.op("grant_epoch", 2)
        self.h.op("resume", 2)
        rec = self.sink.apply(d, 2)
        self.assertFalse(rec["dispatched"])

    def test_expired_decision_is_refused_at_dispatch(self):
        d = self._allow_without_dispatch()  # expires_tick 6
        rec = self.sink.apply(d, 6)
        self.assertFalse(rec["dispatched"])
        self.assertEqual(rec["refusal"], Refusal.DECISION_EXPIRED.value)
        d = self.gate.evaluate(make_action(seq=2), 1)
        self.assertTrue(self.sink.apply(d, 5)["dispatched"])  # last valid tick

    def test_sink_in_a_blocking_mode_refuses_a_genuine_allow(self):
        d = self._allow_without_dispatch()
        for mode in (Mode.HOLD, Mode.INTERVENTION, Mode.SAFE_STOP, Mode.LOCKED):
            with self.subTest(mode=mode):
                self.sink.sync_mode(mode)
                rec = self.sink.apply(d, 1)
                self.assertFalse(rec["dispatched"])
                self.assertEqual(rec["refusal"], Refusal.SINK_NOT_ACTIVE.value)

    def test_sink_starts_closed_and_garbage_mode_means_hold(self):
        fresh = SimActuatorSink(self.gate)
        self.assertIs(fresh.mode, Mode.HOLD)
        fresh.sync_mode(Mode.AUTONOMOUS)
        for junk in ("autonomous", None, 1, object()):
            fresh.sync_mode(junk)
            self.assertIs(fresh.mode, Mode.HOLD)

    def test_sink_never_raises_on_garbage(self):
        for junk in (None, 5, "ALLOW", [], {}, object(), Verdict.ALLOW):
            for now in (1, None, "x", 1.5, True, 10**400):
                rec = self.sink.apply(junk, now)
                self.assertFalse(rec["dispatched"])
        self.assertEqual(self.sink.dispatched_count, 0)

    def test_second_line_of_defence_rejects_bad_targets_even_if_the_gate_were_fooled(self):
        class YesGate:
            def confirm_dispatch(self, decision):
                return (True, "ok")

        sink = SimActuatorSink(YesGate())
        sink.sync_mode(Mode.AUTONOMOUS)
        good = (0.1, 0.0, 0.0, 0.0, 0.0, 0.01)
        bad_targets = [
            (NAN,) + good[1:], (9.0,) + good[1:], good[:-1], list(good), (1,) + good[1:], (True,) + good[1:],
            None, (0.0, 0.0, 0.0, 0.0, 0.0, 0.5),
        ]
        for t in bad_targets:
            with self.subTest(t=repr(t)[:30]):
                d = g.Decision(Verdict.ALLOW, None, None, "", "", 1, 1, 1, "set_joint_targets", t, 1, 1, 9)
                rec = sink.apply(d, 1)
                self.assertFalse(rec["dispatched"])
                self.assertEqual(rec["refusal"], Refusal.SINK_BOUNDS.value)
        d = g.Decision(Verdict.ALLOW, None, None, "", "", 2, 1, 1, "cmd_vel", good, 1, 1, 9)
        self.assertFalse(sink.apply(d, 1)["dispatched"])
        d = g.Decision(Verdict.ALLOW, None, None, "", "", 3, 1, 1, "set_joint_targets", good, 1, 1, 9)
        self.assertTrue(sink.apply(d, 1)["dispatched"])
        self.assertTrue(_targets_ok(good))

    def test_pending_decisions_are_bounded_and_evictions_are_counted(self):
        h = Harness(default_config(pending_capacity=2, watchdog_ticks=100))
        h.observe(make_obs("obs-1", 0), 0)
        ds = [h.gate.evaluate(make_action(seq=i, created=1, expires=6), 1) for i in (1, 2, 3)]
        self.assertTrue(all(d.allowed for d in ds))
        self.assertEqual(h.gate.pending_evicted, 1)
        self.assertFalse(h.sink.apply(ds[0], 1)["dispatched"])  # evicted: fails closed
        self.assertTrue(h.sink.apply(ds[2], 1)["dispatched"])

    def test_sim_register_survives_a_gate_crash_but_mode_resets_to_hold(self):
        self.assertAllowed(make_action())
        targets = self.sink.targets
        self.h.crash_restart()
        self.assertEqual(self.sink.targets, targets)
        self.assertIs(self.sink.mode, Mode.HOLD)


if __name__ == "__main__":
    unittest.main()
