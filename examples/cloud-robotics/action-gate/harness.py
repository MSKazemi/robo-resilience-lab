"""Deterministic simulation harness for the action gate (issue #59). Standard library only.

SIMULATION. Logical ticks only: no wall clock, no threads, no sockets, no random
numbers. The same scenario always gives the same report, byte for byte.

``Harness`` wires an ``ActionGate`` to a ``SimActuatorSink`` and an ``EpochStore``
and can "crash" the gate (drop the object, keep the store and the sink register).
``run_scenario`` plays a scripted event list; ``run_suite`` runs many and sums up.

Order inside one tick (fixed and documented): (1) scripted events in list order,
(2) message deliveries due this tick in send order, (3) the gate watchdog tick.

Time-to-block = first blocking event (a non-ALLOW decision, or a watchdog HOLD)
at or after ``fault_tick``, minus ``fault_tick``, in logical ticks. The gate
decides synchronously on arrival, so arrival-to-decision is 0 ticks by
construction; the interesting number is fault onset to first block (it includes
the scripted network delay or silence).
"""

from __future__ import annotations

import json
from typing import Any, Optional

from gate_core import (
    ActionGate, EpochStore, GateConfig, Reason, REASON_CATEGORY, Verdict, verdict_for,
)
from sim_sink import SimActuatorSink

SOURCE = "MOCK-policy-01"
MODEL = "mock-policy"
REVISION = "r1"
MAX_END_TICK = 10_000
MAX_EVENTS = 1_000
GATE_CALLS = frozenset({
    "grant_epoch", "resume", "hold", "intervene", "release_intervention", "safe_stop",
    "reset_safe_stop", "operator_reset",
})


# --------------------------------------------------------------------------
# Builders (used by tests and scenarios). Every call returns fresh objects.
# --------------------------------------------------------------------------
def default_config(**overrides: Any) -> GateConfig:
    base = dict(allowed_sources=frozenset({SOURCE}), approved_models={MODEL: {REVISION}})
    base.update(overrides)
    return GateConfig(**base)


def valid_values() -> list:
    return [0.1, 0.0, 0.0, 0.0, 0.0, 0.01]


def valid_args(values: Optional[list] = None) -> dict:
    return {"action_space": "joint_position_target", "units": ["rad", "rad", "rad", "rad", "rad", "m"],
            "values": valid_values() if values is None else values}


def make_action(seq: int = 1, created: int = 1, expires: int = 6, epoch: int = 1,
                obs: str = "obs-1", drop: tuple = (), **over: Any) -> dict:
    msg: dict = {
        "schema": "action-gate/v1", "simulated": True, "source_id": SOURCE, "model_id": MODEL,
        "model_revision": REVISION, "control_domain": "sim-arm", "command": "set_joint_targets",
        "mode": "autonomous", "epoch": epoch, "sequence_id": seq, "observation_id": obs,
        "created_tick": created, "expires_tick": expires, "args": valid_args(),
    }
    msg.update(over)
    for k in drop:
        msg.pop(k, None)
    return msg


def make_obs(oid: str = "obs-1", tick: int = 0, state: Optional[list] = None) -> dict:
    return {"observation_id": oid, "tick": tick, "state": [0.0] * 6 if state is None else state}


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------
class Harness:
    def __init__(self, config: Optional[GateConfig] = None, store: Optional[EpochStore] = None,
                 bootstrap: bool = True, pre_checks: tuple = (), extra_checks: tuple = ()) -> None:
        self.pre_checks, self.extra_checks = pre_checks, extra_checks
        self.config = config if config is not None else default_config()
        self.store = store if store is not None else EpochStore.provisioned()
        self.audit_all: list = []  # survives gate restarts (the gate's own log does not)
        self.restarts = 0
        self.gate = self._new_gate()
        self.sink = SimActuatorSink(self.gate)
        self.sink.sync_mode(self.gate.mode)
        if bootstrap:
            self.op("resume", 0)  # the gate boots in HOLD with epoch 1; operator resumes

    def _new_gate(self) -> ActionGate:
        return ActionGate(self.config, self.store, audit_hook=self.audit_all.append,
                          pre_checks=self.pre_checks, extra_checks=self.extra_checks)

    def op(self, call: str, now: Any, *args: Any) -> Any:
        if call not in GATE_CALLS and call not in ("tick", "register_observation"):
            raise ValueError(f"unknown gate call {call!r}")
        res = getattr(self.gate, call)(now, *args) if call != "register_observation" else \
            self.gate.register_observation(args[0], now)
        self.sink.sync_mode(self.gate.mode)
        return res

    def observe(self, obs: Any, now: int) -> Any:
        return self.op("register_observation", now, obs)

    def submit(self, msg: Any, now: Any, *, as_text: bool = False) -> tuple:
        """Gate -> sink. Returns (decision, sink_record)."""
        if as_text:
            decision = self.gate.evaluate_text(msg, now)
        else:
            decision = self.gate.evaluate(msg, now)
        self.sink.sync_mode(self.gate.mode)
        rec = self.sink.apply(decision, now)
        self.sink.sync_mode(self.gate.mode)
        return decision, rec

    def crash_restart(self, store_fault: Optional[str] = None) -> None:
        """Process crash: the gate object (epoch in RAM, sequence, observations, pending
        decisions, audit ring) is gone; the store and the simulated actuator register stay."""
        if store_fault == "lost":
            self.store.lose()
        elif store_fault == "corrupt":
            self.store.corrupt()
        elif store_fault is not None:
            raise ValueError("store_fault must be None, 'lost' or 'corrupt'")
        self.gate = self._new_gate()
        self.sink.attach(self.gate)
        self.sink.sync_mode(self.gate.mode)
        self.restarts += 1


# --------------------------------------------------------------------------
# Scenario runner
# --------------------------------------------------------------------------
def _expected(entry: Any) -> dict:
    """Normalise an expectation: 'ALLOW', 'DROPPED', or a Reason name (verdict and
    category are then derived from the typed reason, so all three are checked)."""
    if entry == "ALLOW":
        return {"verdict": "ALLOW", "reason": None, "category": None}
    if entry == "DROPPED":
        return {"verdict": "DROPPED", "reason": None, "category": None}
    reason = Reason(entry)  # ValueError on a typo: a scenario bug must be loud
    return {"verdict": verdict_for(reason).value, "reason": reason.value,
            "category": REASON_CATEGORY[reason].value}


def run_scenario(sc: dict) -> dict:
    name, end = sc["name"], sc["end_tick"]
    if not isinstance(end, int) or isinstance(end, bool) or not 0 <= end <= MAX_END_TICK:
        raise ValueError(f"{name}: end_tick out of range")
    events = sc["events"]
    if len(events) > MAX_EVENTS:
        raise ValueError(f"{name}: too many events")
    fault_tick = sc.get("fault_tick")
    h = Harness(default_config(**sc.get("config", {})))

    by_tick: dict = {}
    deliveries: dict = {}
    sends = []
    for idx, ev in enumerate(events):
        if not 0 <= ev["at"] <= end:
            raise ValueError(f"{name}: event {idx} outside 0..end_tick")
        by_tick.setdefault(ev["at"], []).append(ev)
        if ev["op"] == "send":
            if len(ev["deliver"]) != len(ev["expect"]):
                raise ValueError(f"{name}: send {ev['label']} needs one expectation per delivery")
            sends.append(ev)
            for j, d in enumerate(ev["deliver"]):
                if d < ev["at"]:
                    raise ValueError(f"{name}: delivery before send for {ev['label']}")
                deliveries.setdefault(d, []).append((len(sends) - 1, j, ev))

    link_up = True
    mismatches: list = []
    gate_ops: list = []
    out_deliveries: list = []
    counts = {"sent": len(sends), "scheduled_deliveries": sum(len(e["deliver"]) for e in sends),
              "delivered": 0, "dropped_by_link": 0, "allowed": 0, "held": 0, "rejected": 0,
              "dispatched": 0, "fail_open": 0, "false_blocks": 0, "undelivered": 0}
    by_category: dict = {}
    first_block_tick = None
    first_block_by = None

    for t in range(end + 1):
        for ev in by_tick.get(t, []):
            op = ev["op"]
            if op == "observe":
                res = h.observe(ev["obs"], t)
                gate_ops.append({"tick": t, "call": "register_observation", "ok": res.ok,
                                 "reason": res.reason.value if res.reason else None})
                if res.ok != ev.get("expect_ok", True):
                    mismatches.append(f"t={t} register_observation ok={res.ok} unexpected")
            elif op == "gate":
                res = h.op(ev["call"], t, *ev.get("args", []))
                gate_ops.append({"tick": t, "call": ev["call"], "ok": res.ok,
                                 "reason": res.reason.value if res.reason else None})
                if res.ok != ev.get("expect_ok", True):
                    mismatches.append(f"t={t} {ev['call']} ok={res.ok} unexpected")
            elif op == "link":
                link_up = ev["state"] == "up"
            elif op == "crash":
                h.crash_restart(ev.get("store_fault"))
                gate_ops.append({"tick": t, "call": "crash_restart", "ok": True,
                                 "reason": ev.get("store_fault")})
            elif op == "send":
                pass  # delivery handled below
            else:
                raise ValueError(f"{name}: unknown op {op!r}")
        for _i, j, ev in sorted(deliveries.get(t, []), key=lambda x: (x[0], x[1])):
            exp = _expected(ev["expect"][j])
            counts["delivered"] += 1
            rec = {"label": ev["label"], "send_tick": ev["at"], "delivery_tick": t, "expected": exp}
            if not link_up:
                counts["dropped_by_link"] += 1
                rec.update(verdict="DROPPED", reason=None, category=None, dispatched=False)
                if exp["verdict"] != "DROPPED":
                    mismatches.append(f"t={t} {ev['label']}: dropped by link, expected {exp['verdict']}")
            else:
                if "text" in ev:
                    decision, sink = h.submit(ev["text"], t, as_text=True)
                else:
                    decision, sink = h.submit(ev["msg"], t)
                rec.update(verdict=decision.verdict.value,
                           reason=decision.reason.value if decision.reason else None,
                           category=decision.category, field=decision.field,
                           dispatched=sink["dispatched"])
                key = {Verdict.ALLOW: "allowed", Verdict.HOLD: "held", Verdict.REJECT: "rejected"}
                counts[key[decision.verdict]] += 1
                if decision.category:
                    by_category[decision.category] = by_category.get(decision.category, 0) + 1
                if sink["dispatched"]:
                    counts["dispatched"] += 1
                    if exp["verdict"] != "ALLOW":
                        counts["fail_open"] += 1
                if decision.verdict is not Verdict.ALLOW:
                    if fault_tick is not None and t >= fault_tick and first_block_tick is None:
                        first_block_tick, first_block_by = t, "decision"
                    if exp["verdict"] == "ALLOW":
                        counts["false_blocks"] += 1
                got = (rec["verdict"], rec["reason"], rec["category"])
                want = (exp["verdict"], exp["reason"], exp["category"])
                if got != want:
                    mismatches.append(f"t={t} {ev['label']}: got {got} expected {want}")
            out_deliveries.append(rec)
        h.op("tick", t)

    for t, items in sorted(deliveries.items()):
        if t > end:
            counts["undelivered"] += len(items)
            mismatches.append(f"{len(items)} delivery(ies) scheduled after end_tick at {t} were never run")
    if fault_tick is not None:  # watchdog HOLD counts as a block of the silent stream
        for rec in h.audit_all:
            if rec.get("kind") == "transition" and rec.get("detail") == "watchdog" and rec["tick"] >= fault_tick:
                if first_block_tick is None or rec["tick"] < first_block_tick:
                    first_block_tick, first_block_by = rec["tick"], "watchdog"
                break
    ttb = None if first_block_tick is None or fault_tick is None else first_block_tick - fault_tick
    return {
        "name": name, "description": sc["description"], "fault_tick": fault_tick, "end_tick": end,
        "deliveries": out_deliveries, "gate_ops": gate_ops, "counts": counts,
        "by_category": dict(sorted(by_category.items())),
        "first_block_tick": first_block_tick, "first_block_by": first_block_by,
        "time_to_block_ticks": ttb,
        "final_gate_mode": h.gate.mode.value, "final_epoch": h.gate.epoch, "restarts": h.restarts,
        "audit_records": len(h.audit_all), "failure_count": len(mismatches), "mismatches": mismatches,
    }


def run_suite(scenarios: list) -> dict:
    results = [run_scenario(sc) for sc in scenarios]
    names = [r["name"] for r in results]
    if len(set(names)) != len(names):
        raise ValueError("duplicate scenario names")
    cats: dict = {}
    for r in results:
        for k, v in r["by_category"].items():
            cats[k] = cats.get(k, 0) + v
    return {
        "label": "SIMULATION: logical ticks, no hardware, no wall clock, no network, no ROS 2",
        "scenarios": results,
        "totals": {
            "scenarios": len(results),
            "deliveries": sum(r["counts"]["delivered"] for r in results),
            "dispatched": sum(r["counts"]["dispatched"] for r in results),
            "fail_open": sum(r["counts"]["fail_open"] for r in results),
            "false_blocks": sum(r["counts"]["false_blocks"] for r in results),
            "failure_count": sum(r["failure_count"] for r in results),
            "blocked_by_category": dict(sorted(cats.items())),
        },
    }


def render(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
