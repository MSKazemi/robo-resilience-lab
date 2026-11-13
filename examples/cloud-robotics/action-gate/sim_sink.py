"""Simulated, non-actuating sink for gate decisions (issue #59). Standard library only.

SIMULATION ONLY. This object stands in for "what would be handed to a simulated
actuator". It sends nothing anywhere: it keeps an in-memory register of the last
simulated joint targets and a log with one record per decision, each carrying
``dispatched: true|false``.

Defence in depth: the sink dispatches only when ALL of these hold, otherwise it
records ``dispatched: false`` with a typed refusal:

1. the argument is a genuine ``Decision`` object with verdict ALLOW;
2. its own simulated mode is AUTONOMOUS (``sync_mode`` mirrors the gate);
3. the gate confirms it issued that exact decision, once, in the current epoch,
   while the gate is still AUTONOMOUS (single use, so replaying a decision fails);
4. the decision has not expired at dispatch time (``now < expires_tick``);
5. its targets pass an independent finite/limit check (second line of defence,
   written separately from the gate's check).

Simulated safe states (they mirror the gate mode, one to one):
AUTONOMOUS = accepting gate-approved commands; HOLD = nothing is dispatched and
the simulated target register keeps its last value; INTERVENTION = an operator
has control, nothing is dispatched; SAFE_STOP = like HOLD but latched until an
explicit reset; LOCKED = gate state lost, nothing is dispatched. These are
logical states of a simulation. They do NOT describe motor braking or any
physical stop.
"""

from __future__ import annotations

import enum
import math
from typing import Any, Optional

from gate_core import ACTION_DIM, JOINT_LIMITS, Decision, Mode, Verdict


class Refusal(str, enum.Enum):
    GATE_BLOCKED = "GATE_BLOCKED"            # the decision was HOLD or REJECT
    NOT_A_DECISION = "NOT_A_DECISION"        # forged or wrong-typed argument
    SINK_NOT_ACTIVE = "SINK_NOT_ACTIVE"      # sink mode is not AUTONOMOUS
    DECISION_NOT_PENDING = "DECISION_NOT_PENDING"  # not issued by the gate, or already used
    GATE_NOT_AUTONOMOUS = "GATE_NOT_AUTONOMOUS"    # gate left AUTONOMOUS after the ALLOW
    EPOCH_CHANGED = "EPOCH_CHANGED"
    DECISION_EXPIRED = "DECISION_EXPIRED"    # delayed between gate and sink
    SINK_BOUNDS = "SINK_BOUNDS"              # independent check failed
    SINK_ERROR = "SINK_ERROR"                # unexpected error: blocked


_CONFIRM_REFUSAL = {
    "not_a_decision": Refusal.NOT_A_DECISION,
    "decision_not_pending": Refusal.DECISION_NOT_PENDING,
    "gate_not_autonomous": Refusal.GATE_NOT_AUTONOMOUS,
    "epoch_changed": Refusal.EPOCH_CHANGED,
}


def _targets_ok(targets: Any) -> bool:
    """Independent of the gate's check: exact types, finite, inside the joint limits."""
    if type(targets) is not tuple or len(targets) != ACTION_DIM:
        return False
    for i in range(ACTION_DIM):
        v = targets[i]
        if type(v) is not float or not math.isfinite(v):
            return False
        lo, hi = JOINT_LIMITS[i]
        if not (lo <= v <= hi):
            return False
    return True


class SimActuatorSink:
    def __init__(self, gate: Any) -> None:
        self._gate = gate
        self.mode = Mode.HOLD  # fail closed until sync_mode says otherwise
        self.targets: Optional[tuple] = None  # last simulated joint targets
        self.log: list = []
        self.dispatched_count = 0
        self.blocked_count = 0

    def attach(self, gate: Any) -> None:
        """Re-bind to a restarted gate. The simulated register survives; mode resets to HOLD."""
        self._gate = gate
        self.mode = Mode.HOLD

    def sync_mode(self, mode: Any) -> None:
        self.mode = mode if type(mode) is Mode else Mode.HOLD  # unknown -> HOLD

    def _refuse(self, decision: Any, now: Any, why: Refusal) -> dict:
        self.blocked_count += 1
        rec = {
            "n": len(self.log) + 1,
            "tick": now if type(now) is int else None,
            "decision_id": getattr(decision, "decision_id", None) if type(decision) is Decision else None,
            "verdict": decision.verdict.value if type(decision) is Decision else None,
            "reason": decision.reason.value if type(decision) is Decision and decision.reason else None,
            "command": decision.command if type(decision) is Decision else None,
            "dispatched": False,
            "refusal": why.value,
            "sim_effect": None,
        }
        self.log.append(rec)
        return rec

    def apply(self, decision: Any, now: Any) -> dict:
        """Record the fate of one decision. Never raises; blocked or doubtful -> dispatched false."""
        try:
            return self._apply(decision, now)
        except Exception:
            return self._refuse(decision, now, Refusal.SINK_ERROR)

    def _apply(self, decision: Any, now: Any) -> dict:
        if type(decision) is not Decision:
            return self._refuse(decision, now, Refusal.NOT_A_DECISION)
        if decision.verdict is not Verdict.ALLOW:
            return self._refuse(decision, now, Refusal.GATE_BLOCKED)
        if self.mode is not Mode.AUTONOMOUS:
            return self._refuse(decision, now, Refusal.SINK_NOT_ACTIVE)
        ok, why = self._gate.confirm_dispatch(decision)
        if not ok:
            return self._refuse(decision, now, _CONFIRM_REFUSAL.get(why, Refusal.SINK_ERROR))
        if type(now) is not int or decision.expires_tick is None or now >= decision.expires_tick:
            return self._refuse(decision, now, Refusal.DECISION_EXPIRED)
        if decision.command == "set_joint_targets":
            if not _targets_ok(decision.targets):
                return self._refuse(decision, now, Refusal.SINK_BOUNDS)
            self.targets = decision.targets
            effect = "targets_applied"
        elif decision.command == "hold":
            effect = "hold_pose"  # register unchanged: SIMULATED hold of the last targets
        else:
            return self._refuse(decision, now, Refusal.SINK_ERROR)
        self.dispatched_count += 1
        rec = {
            "n": len(self.log) + 1, "tick": now, "decision_id": decision.decision_id,
            "verdict": decision.verdict.value, "reason": None, "command": decision.command,
            "dispatched": True, "refusal": None, "sim_effect": effect,
        }
        self.log.append(rec)
        return rec
