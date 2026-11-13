"""Sequence, epoch, restart, watchdog, mode transitions, audit: state handling tests."""

import unittest

from tests.support import (GateCase, Harness, Mode, Reason, Verdict, default_config, g, make_action, make_obs,
                           valid_args)


class SequenceTests(GateCase):
    def test_duplicate_and_older_sequences_are_typed_differently(self):
        self.assertAllowed(make_action(seq=5, created=1, expires=6))
        self.assertBlocked(make_action(seq=5, created=1, expires=6), Reason.DUPLICATE_SEQUENCE, field="msg.sequence_id")
        self.assertBlocked(make_action(seq=4, created=1, expires=6), Reason.STALE_SEQUENCE)
        self.assertBlocked(make_action(seq=1, created=1, expires=6), Reason.STALE_SEQUENCE)

    def test_gaps_are_allowed_so_a_lost_message_cannot_wedge_the_gate(self):
        self.assertAllowed(make_action(seq=1))
        self.assertAllowed(make_action(seq=10, created=2, expires=7), now=2)
        self.assertEqual(self.gate.last_sequence, 10)

    def test_reordered_packets_older_one_is_never_dispatched(self):
        self.assertAllowed(make_action(seq=2, created=1, expires=6))
        before = self.sink.targets
        self.assertBlocked(make_action(seq=1, created=1, expires=6, args=valid_args([0.3, 0, 0, 0, 0, 0])),
                           Reason.STALE_SEQUENCE)
        self.assertEqual(self.sink.targets, before)

    def test_a_rejected_action_does_not_advance_the_accepted_sequence(self):
        rejects = [
            make_action(seq=50, source_id="ROGUE"),
            make_action(seq=51, command="cmd_vel"),
            make_action(seq=52, args=valid_args([NaN_(), 0, 0, 0, 0, 0])),
            make_action(seq=53, created=1, expires=1),
            make_action(seq=54, obs="obs-zzz"),
            make_action(seq=55, model_revision="r9"),
            make_action(seq=56, args=valid_args([0, 1.5, 0, 0, 0, 0])),
            make_action(seq=57, mode="hold"),
            make_action(seq=58, epoch=9),
            make_action(seq=59, simulated=False),
        ]
        for msg in rejects:
            with self.subTest(seq=msg["sequence_id"]):
                d, _ = self.send(msg, 1)
                self.assertFalse(d.allowed)
                self.assertEqual(self.gate.last_sequence, 0)
        # low sequence numbers are still accepted afterwards: nothing was poisoned
        self.assertAllowed(make_action(seq=1))

    def test_expired_high_sequence_does_not_poison_later_valid_messages(self):
        self.assertBlocked(make_action(seq=1000, created=1, expires=3), Reason.EXPIRED, now=3)
        self.assertAllowed(make_action(seq=2, created=3, expires=8), now=3)

    def test_sequence_bounds(self):
        self.assertAllowed(make_action(seq=2**53))
        self.assertBlocked(make_action(seq=2**53 + 1), Reason.INTEGER_OUT_OF_RANGE)
        self.assertBlocked(make_action(seq=2**53), Reason.DUPLICATE_SEQUENCE)  # wedged until a new epoch (documented)
        self.h.op("grant_epoch", 1)
        self.h.op("resume", 1)
        self.assertAllowed(make_action(seq=1, epoch=2), now=1)


def NaN_():
    return float("nan")


class EpochAndRestartTests(GateCase):
    def test_stale_and_ungranted_epochs(self):
        self.assertBlocked(make_action(epoch=2), Reason.EPOCH_NOT_GRANTED, field="msg.epoch")
        self.h.op("grant_epoch", 1)
        self.h.op("resume", 1)
        self.assertEqual(self.gate.epoch, 2)
        self.assertBlocked(make_action(epoch=1), Reason.STALE_EPOCH, field="msg.epoch")
        self.assertAllowed(make_action(epoch=2, seq=1), now=1)

    def test_grant_epoch_resets_sequence_and_forces_hold(self):
        self.assertAllowed(make_action(seq=7))
        res = self.h.op("grant_epoch", 2)
        self.assertTrue(res.ok)
        self.assertEqual(res.epoch, 2)
        self.assertIs(self.gate.mode, Mode.HOLD)
        self.assertEqual(self.gate.last_sequence, 0)
        d, rec = self.send(make_action(epoch=2, seq=1, created=2, expires=7), now=2)
        self.assertEqual(d.reason, Reason.GATE_HOLD)
        self.assertFalse(rec["dispatched"])

    def test_failed_epoch_persist_changes_nothing(self):
        self.h.store.fail_saves = True
        before = self.gate.snapshot()
        res = self.h.op("grant_epoch", 1)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, Reason.EPOCH_PERSIST_FAILED)
        self.assertEqual(self.gate.snapshot(), before)
        self.assertEqual(self.h.store.load(), 1)

    def test_crash_restart_takes_a_new_epoch_and_blocks_replay(self):
        self.assertAllowed(make_action(seq=1))
        self.assertAllowed(make_action(seq=2, created=2, expires=7), now=2)
        self.h.crash_restart()
        self.assertEqual(self.gate.epoch, 2)
        self.assertIs(self.gate.mode, Mode.HOLD)
        self.assertEqual(self.h.store.load(), 2)
        self.h.observe(make_obs("obs-1", 3), 3)
        self.h.op("resume", 3)
        for seq in (1, 2, 3, 99):  # replayed and never-seen pre-crash sequence numbers
            with self.subTest(seq=seq):
                self.assertBlocked(make_action(epoch=1, seq=seq, created=3, expires=8), Reason.STALE_EPOCH, now=3)
        self.assertAllowed(make_action(epoch=2, seq=1, created=3, expires=8), now=3)

    def test_negative_control_amnesiac_restart_would_accept_the_replay(self):
        """Control (documents WHY the durable epoch exists): a gate restarted from a FRESH store
        re-uses epoch 1, so a pre-crash message is accepted again. The real restart path never does this."""
        self.assertAllowed(make_action(seq=3, created=1, expires=6))
        amnesiac = Harness(default_config())  # brand-new store: counter back at 0
        amnesiac.observe(make_obs("obs-1", 0), 0)
        d, _ = amnesiac.submit(make_action(seq=3, created=1, expires=6), 1)
        self.assertTrue(d.allowed, "control expected the replay to pass on an amnesiac gate")

    def test_pre_crash_decisions_cannot_be_dispatched_after_restart(self):
        d = self.gate.evaluate(make_action(), 1)  # ALLOW issued, not yet dispatched
        self.assertTrue(d.allowed)
        self.h.crash_restart()
        rec = self.sink.apply(d, 1)
        self.assertFalse(rec["dispatched"])
        self.assertIsNone(self.sink.targets)

    def test_observations_do_not_survive_a_restart(self):
        self.h.crash_restart()
        self.h.op("resume", 1)
        d, _ = self.send(make_action(epoch=2), 1)
        self.assertEqual(d.reason, Reason.UNKNOWN_OBSERVATION)

    def test_unreadable_store_locks_the_gate(self):
        for bad in (None, "corrupt", True, False, -1, 1.5, [], 2**53, 10**400):
            with self.subTest(bad=repr(bad)):
                h = Harness(default_config(), store=g.EpochStore(bad), bootstrap=False)
                self.assertIs(h.gate.mode, Mode.LOCKED)
                self.assertEqual(h.gate.epoch, 0)
                self.assertFalse(h.op("resume", 0).ok)
                self.assertFalse(h.op("grant_epoch", 0).ok)
                h.observe(make_obs("obs-1", 0), 0)
                d, rec = h.submit(make_action(epoch=1), 1)
                self.assertEqual(d.reason, Reason.GATE_LOCKED)
                self.assertIs(d.verdict, Verdict.HOLD)
                self.assertFalse(rec["dispatched"])

    def test_lost_and_corrupt_store_after_a_crash_lock_the_gate(self):
        for fault in ("lost", "corrupt"):
            with self.subTest(fault=fault):
                h = Harness(default_config())
                h.crash_restart(fault)
                self.assertIs(h.gate.mode, Mode.LOCKED)
                self.assertEqual(h.sink.mode, Mode.LOCKED)

    def test_unwritable_store_at_boot_locks_the_gate(self):
        store = g.EpochStore(0)
        store.fail_saves = True
        h = Harness(default_config(), store=store, bootstrap=False)
        self.assertIs(h.gate.mode, Mode.LOCKED)

    def test_operator_reset_recovers_only_with_a_valid_floor(self):
        h = Harness(default_config())
        h.crash_restart("lost")
        before = h.gate.snapshot()
        for bad in (None, True, -1, 1.5, "10", 2**53, 10**400):
            with self.subTest(bad=repr(bad)):
                res = h.op("operator_reset", 1, bad)
                self.assertFalse(res.ok)
                self.assertEqual(res.reason, Reason.INVALID_ARGUMENT)
                self.assertEqual(h.gate.snapshot(), before)
        res = h.op("operator_reset", 2, 40)
        self.assertTrue(res.ok)
        self.assertEqual(h.gate.epoch, 41)
        self.assertIs(h.gate.mode, Mode.HOLD)  # never straight to autonomous
        self.assertFalse(h.op("operator_reset", 3, 40).ok)  # only valid from LOCKED

    def test_operator_reset_persist_failure_stays_locked(self):
        h = Harness(default_config())
        h.crash_restart("lost")
        h.store.fail_saves = True
        res = h.op("operator_reset", 1, 5)
        self.assertEqual(res.reason, Reason.EPOCH_PERSIST_FAILED)
        self.assertIs(h.gate.mode, Mode.LOCKED)


class ModeTransitionTests(GateCase):
    def test_transition_table(self):
        # (setup calls, call under test, expected ok, expected mode afterwards)
        table = [
            ([], "hold", True, Mode.HOLD),
            ([], "intervene", True, Mode.INTERVENTION),
            ([], "safe_stop", True, Mode.SAFE_STOP),
            ([], "resume", False, Mode.AUTONOMOUS),
            ([], "release_intervention", False, Mode.AUTONOMOUS),
            ([], "reset_safe_stop", False, Mode.AUTONOMOUS),
            (["hold"], "resume", True, Mode.AUTONOMOUS),
            (["hold"], "intervene", True, Mode.INTERVENTION),
            (["hold"], "hold", True, Mode.HOLD),
            (["intervene"], "resume", False, Mode.INTERVENTION),
            (["intervene"], "hold", False, Mode.INTERVENTION),
            (["intervene"], "release_intervention", True, Mode.HOLD),
            (["intervene"], "safe_stop", True, Mode.SAFE_STOP),
            (["safe_stop"], "resume", False, Mode.SAFE_STOP),
            (["safe_stop"], "hold", False, Mode.SAFE_STOP),
            (["safe_stop"], "intervene", False, Mode.SAFE_STOP),
            (["safe_stop"], "reset_safe_stop", True, Mode.HOLD),
        ]
        for setup, call, ok, mode in table:
            with self.subTest(setup=setup, call=call):
                h = Harness(default_config())
                for s in setup:
                    self.assertTrue(h.op(s, 1).ok)
                res = h.op(call, 2)
                self.assertEqual(res.ok, ok, res)
                if not ok:
                    self.assertEqual(res.reason, Reason.INVALID_TRANSITION)
                self.assertIs(h.gate.mode, mode)
                self.assertIs(h.sink.mode, mode)

    def test_resume_requires_hold_and_never_skips_it(self):
        self.h.op("intervene", 1)
        self.h.op("release_intervention", 2)
        self.assertIs(self.gate.mode, Mode.HOLD)
        self.assertFalse(self.send(make_action(seq=1, created=3, expires=8), 3)[1]["dispatched"])
        self.assertTrue(self.h.op("resume", 3).ok)

    def test_locked_allows_no_operator_shortcut(self):
        h = Harness(default_config())
        h.crash_restart("lost")
        for call in ("hold", "intervene", "safe_stop", "resume", "release_intervention", "reset_safe_stop",
                     "grant_epoch"):
            with self.subTest(call=call):
                self.assertFalse(h.op(call, 1).ok)
                self.assertIs(h.gate.mode, Mode.LOCKED)

    def test_each_blocking_mode_has_its_own_reason_category_and_verdict(self):
        expect = {
            "hold": (Reason.GATE_HOLD, "hold"),
            "intervene": (Reason.INTERVENTION_ACTIVE, "intervention"),
            "safe_stop": (Reason.SAFE_STOP_ACTIVE, "hold"),
        }
        for call, (reason, category) in expect.items():
            with self.subTest(call=call):
                h = Harness(default_config())
                h.observe(make_obs("obs-1", 0), 0)
                h.op(call, 1)
                d, rec = h.submit(make_action(created=1, expires=6), 1)
                self.assertEqual(d.reason, reason)
                self.assertEqual(d.category, category)
                self.assertIs(d.verdict, Verdict.HOLD)
                self.assertFalse(rec["dispatched"])


class WatchdogTests(GateCase):
    def test_silence_puts_the_gate_in_hold_at_the_boundary(self):
        # resumed at tick 0, watchdog 10: tick 9 still fine, tick 10 holds (half-open)
        self.h.op("tick", 9)
        self.assertIs(self.gate.mode, Mode.AUTONOMOUS)
        self.h.op("tick", 10)
        self.assertIs(self.gate.mode, Mode.HOLD)
        self.assertEqual(self.gate.hold_cause, "watchdog")
        self.assertIs(self.sink.mode, Mode.HOLD)

    def test_a_late_message_cannot_slip_past_a_missed_tick(self):
        # tick() is never called; the gate itself notices the silence on evaluate
        d, rec = self.send(make_action(seq=1, created=10, expires=15, obs="obs-1"), 10)
        self.assertEqual(d.reason, Reason.GATE_HOLD)
        self.assertIs(d.verdict, Verdict.HOLD)
        self.assertFalse(rec["dispatched"])
        self.assertEqual(self.gate.hold_cause, "watchdog")

    def test_allowed_commands_refresh_the_watchdog(self):
        self.h.observe(make_obs("obs-2", 8), 8)
        self.assertAllowed(make_action(seq=1, created=8, expires=13, obs="obs-2"), now=8)
        self.h.op("tick", 17)
        self.assertIs(self.gate.mode, Mode.AUTONOMOUS)
        self.h.op("tick", 18)
        self.assertIs(self.gate.mode, Mode.HOLD)

    def test_rejected_messages_do_not_refresh_the_watchdog(self):
        for t in range(1, 10):
            self.send(make_action(seq=1, created=t, expires=t + 5, source_id="ROGUE"), t)
        self.h.op("tick", 10)
        self.assertIs(self.gate.mode, Mode.HOLD)

    def test_hold_from_watchdog_persists_until_operator_resumes(self):
        self.h.op("tick", 10)
        self.h.observe(make_obs("obs-2", 11), 11)
        for t in (11, 12):
            d, _ = self.send(make_action(seq=1, created=t, expires=t + 5, obs="obs-2"), t)
            self.assertEqual(d.reason, Reason.GATE_HOLD)
        self.h.op("resume", 13)
        self.assertAllowed(make_action(seq=1, created=13, expires=18, obs="obs-2"), now=13)


class ClockOperationTests(GateCase):
    def test_operator_calls_validate_the_clock_too(self):
        self.h.op("tick", 5)
        for call in ("hold", "intervene", "safe_stop", "grant_epoch", "resume"):
            with self.subTest(call=call):
                res = self.h.op(call, 4)
                self.assertFalse(res.ok)
                self.assertEqual(res.reason, Reason.CLOCK_REGRESSION)
                self.assertIs(self.gate.mode, Mode.AUTONOMOUS)
        for bad in (None, True, 1.0, -1, "3"):
            self.assertEqual(self.h.op("tick", bad).reason, Reason.INVALID_CLOCK)


class AuditTests(GateCase):
    def test_every_decision_leaves_exactly_one_decision_record_with_a_typed_reason(self):
        msgs = [make_action(seq=1), make_action(seq=1, source_id="ROGUE"), make_action(command="shell"), None]
        for i, m in enumerate(msgs):
            before = [r for r in self.gate.audit_log if r["kind"] == "decision"]
            d, _ = self.send(m, 1)
            after = [r for r in self.gate.audit_log if r["kind"] == "decision"]
            self.assertEqual(len(after), len(before) + 1)
            rec = after[-1]
            self.assertEqual(rec["audit_id"], d.audit_id)
            self.assertEqual(rec["verdict"], d.verdict.value)
            self.assertEqual(rec["reason"], d.reason.value if d.reason else None)
            self.assertEqual(rec["category"], d.category)
            self.assertFalse(rec["dispatched"])

    def test_audit_ids_are_unique_and_increasing_and_hook_gets_copies(self):
        for t in range(1, 6):
            self.send(make_action(seq=t, created=t, expires=t + 5), t)
        ids = [r["audit_id"] for r in self.h.audit_all]
        self.assertEqual(ids, sorted(set(ids)))
        self.h.audit_all[-1]["reason"] = "TAMPERED"
        self.assertNotEqual(self.gate.audit_log[-1]["reason"], "TAMPERED")

    def test_mode_changes_are_audited(self):
        self.h.op("intervene", 1)
        kinds = [(r["kind"], r.get("what")) for r in self.gate.audit_log]
        self.assertIn(("transition", "autonomous->intervention"), kinds)

    def test_audit_ring_is_bounded_and_counts_what_it_drops(self):
        h = Harness(default_config(audit_capacity=5))
        for t in range(1, 21):
            h.submit(make_action(seq=1, created=t, expires=t + 5, source_id="ROGUE"), t)
        self.assertEqual(len(h.gate.audit_log), 5)
        self.assertGreater(h.gate.audit_dropped, 0)
        # the external hook (here: harness.audit_all) still has everything
        self.assertGreaterEqual(len(h.audit_all), 20)


if __name__ == "__main__":
    unittest.main()
