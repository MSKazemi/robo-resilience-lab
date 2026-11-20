"""Shared helpers for the action-gate tests (standard-library unittest only)."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import gate_core as g  # noqa: E402
import harness as hx  # noqa: E402
from gate_core import Category, Mode, Reason, Verdict  # noqa: E402
from harness import Harness, default_config, make_action, make_obs, valid_args  # noqa: E402

NAN = float("nan")
INF = float("inf")


class GateCase(unittest.TestCase):
    """A harness at tick 0: gate AUTONOMOUS in epoch 1, observation obs-1 (tick 0, all joints 0.0).

    Default config: observation_ttl 5, max message ttl 20, watchdog 10.
    """

    config_overrides: dict = {}

    def setUp(self):
        self.h = Harness(default_config(**self.config_overrides))
        res = self.h.observe(make_obs("obs-1", 0), 0)
        assert res.ok, res

    @property
    def gate(self):
        return self.h.gate

    @property
    def sink(self):
        return self.h.sink

    def send(self, msg, now=1, **kw):
        return self.h.submit(msg, now, **kw)

    def assertAllowed(self, msg, now=1):
        d, rec = self.send(msg, now)
        self.assertEqual(d.verdict, Verdict.ALLOW, (d.reason, d.field, d.detail))
        self.assertTrue(rec["dispatched"])
        return d, rec

    def assertBlocked(self, msg, reason, now=1, field=None, **kw):
        """Blocked with the typed reason, nothing dispatched, gate state unchanged."""
        before = self.gate.snapshot()
        targets_before = self.sink.targets
        d, rec = self.send(msg, now, **kw)
        self.assertNotEqual(d.verdict, Verdict.ALLOW, "must be blocked")
        self.assertEqual(d.reason, reason, (d.field, d.detail))
        self.assertEqual(d.category, g.REASON_CATEGORY[reason].value)
        self.assertIs(d.verdict, g.verdict_for(reason))
        self.assertFalse(rec["dispatched"])
        self.assertEqual(self.sink.targets, targets_before)
        self.assertEqual(self.gate.snapshot(), before, "a blocked message changed gate state")
        if field is not None:
            self.assertEqual(d.field, field)
        return d, rec
