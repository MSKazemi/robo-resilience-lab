"""Scripted fault scenarios for the simulated action gate (issue #59). SIMULATION ONLY.

Each scenario is plain data: a name, a description, an optional ``fault_tick`` (when the
fault begins, for time-to-block), optional gate config overrides, and an event list.
A ``send`` event lists the ticks at which copies are delivered (``[]`` = lost,
two entries = duplicated or delayed) and one expectation per delivery:
``"ALLOW"``, ``"DROPPED"`` (link down) or a typed Reason name such as
``"STALE_SEQUENCE"``. The runner derives and checks verdict, reason and category.

The gate boots in HOLD with epoch 1 and the harness resumes it at tick 0
(AUTONOMOUS, epoch 1), so messages use ``epoch=1`` until a scenario changes it.

Nothing here is a measurement of a real system. Values are synthetic.
"""

from __future__ import annotations

import json

from harness import make_action, make_obs, valid_args, valid_values

NAN = float("nan")
INF = float("inf")


def _send(label, at, deliver, expect, msg=None, text=None):
    ev = {"at": at, "op": "send", "label": label, "deliver": list(deliver), "expect": list(expect)}
    if text is not None:
        ev["text"] = text
    else:
        ev["msg"] = msg
    return ev


def _obs(at, oid="obs-1", tick=None, state=None):
    return {"at": at, "op": "observe", "obs": make_obs(oid, at if tick is None else tick, state)}


def _gate(at, call, *args, expect_ok=True):
    return {"at": at, "op": "gate", "call": call, "args": list(args), "expect_ok": expect_ok}


def _link(at, state):
    return {"at": at, "op": "link", "state": state}


def _crash(at, store_fault=None):
    return {"at": at, "op": "crash", "store_fault": store_fault}


def _one(label, tick, expect, **kw):
    """One message sent and delivered at ``tick`` with seq 1, created now, expiring in 5."""
    over = dict(kw)
    msg = make_action(seq=over.pop("seq", 1), created=tick, expires=tick + 5, **over)
    return _send(label, tick, [tick], [expect], msg=msg)


def _hold_msg(**kw):
    return dict(command="hold", args={}, **kw)


# ---------------------------------------------------------------------------
def _baseline():
    return {
        "name": "baseline_valid", "description": "valid commands pass every check and are dispatched",
        "fault_tick": None, "end_tick": 5, "events": [
            _obs(0),
            _send("move", 1, [1], ["ALLOW"], msg=make_action(seq=1, created=1, expires=6)),
            _send("hold", 2, [2], ["ALLOW"], msg=make_action(seq=2, created=2, expires=7, **_hold_msg())),
        ]}


def _delayed_reply():
    return {
        "name": "delayed_reply",
        "description": "a reply delayed past its expiry is rejected; a late reply after the watchdog "
                       "hold finds the gate holding",
        "fault_tick": 2, "end_tick": 14, "events": [
            _obs(0),
            _send("in_window", 1, [3], ["ALLOW"], msg=make_action(seq=1, created=1, expires=4)),
            _send("too_late", 2, [9], ["EXPIRED"], msg=make_action(seq=2, created=2, expires=5)),
            _send("after_hold", 5, [14], ["GATE_HOLD"], msg=make_action(seq=3, created=5, expires=15)),
        ]}


def _reordered():
    return {
        "name": "reordered_packet",
        "description": "seq 2 arrives before seq 1: seq 2 is allowed, the late seq 1 is never dispatched",
        "fault_tick": 1, "end_tick": 8, "config": {"observation_ttl_ticks": 20}, "events": [
            _obs(0),
            _send("seq2_first", 2, [3], ["ALLOW"], msg=make_action(seq=2, created=2, expires=9)),
            _send("seq1_late", 1, [5], ["STALE_SEQUENCE"], msg=make_action(seq=1, created=1, expires=9)),
            _send("seq3", 4, [6], ["ALLOW"], msg=make_action(seq=3, created=4, expires=9)),
        ]}


def _duplicate():
    return {
        "name": "duplicate_packet", "description": "one message delivered three times is dispatched once",
        "fault_tick": 2, "end_tick": 6, "events": [
            _obs(0),
            _send("dup", 1, [1, 2, 4], ["ALLOW", "DUPLICATE_SEQUENCE", "DUPLICATE_SEQUENCE"],
                  msg=make_action(seq=1, created=1, expires=6)),
        ]}


def _disconnection():
    return {
        "name": "disconnection",
        "description": "link goes down: messages are lost, the watchdog puts the gate in HOLD; the HOLD "
                       "persists after the link returns until a new epoch is granted and the operator resumes",
        "fault_tick": 4, "end_tick": 20, "events": [
            _obs(0),
            _send("before_outage", 1, [1], ["ALLOW"], msg=make_action(seq=1, created=1, expires=6)),
            _link(4, "down"),
            _send("during_outage", 5, [5], ["DROPPED"], msg=make_action(seq=2, created=5, expires=9)),
            _link(13, "up"),
            _send("after_link_up", 14, [14], ["GATE_HOLD"], msg=make_action(seq=3, created=14, expires=18)),
            _gate(15, "grant_epoch"),
            _obs(15, "obs-2"),
            _gate(15, "resume"),
            _send("new_epoch", 16, [16], ["ALLOW"],
                  msg=make_action(seq=1, created=16, expires=20, epoch=2, obs="obs-2")),
            _send("old_epoch", 17, [17], ["STALE_EPOCH"],
                  msg=make_action(seq=5, created=17, expires=21, epoch=1, obs="obs-2")),
        ]}


def _crash_replay():
    return {
        "name": "process_crash_replay",
        "description": "the gate process restarts: it takes a new epoch from the durable store, so "
                       "pre-crash messages (even with unseen sequence numbers) cannot be replayed",
        "fault_tick": 5, "end_tick": 14, "events": [
            _obs(0),
            _send("s1", 1, [1], ["ALLOW"], msg=make_action(seq=1, created=1, expires=6)),
            _send("s2", 2, [2], ["ALLOW"], msg=make_action(seq=2, created=2, expires=7)),
            _crash(5),
            _send("replay_before_resume", 5, [5], ["GATE_HOLD"], msg=make_action(seq=2, created=2, expires=7)),
            _obs(6, "obs-2"),
            _gate(6, "resume"),
            _send("replay_s2", 7, [7], ["STALE_EPOCH"], msg=make_action(seq=2, created=2, expires=9, obs="obs-2")),
            _send("replay_unseen_seq", 7, [7], ["STALE_EPOCH"],
                  msg=make_action(seq=3, created=7, expires=9, obs="obs-2")),
            _send("pre_crash_observation", 8, [8], ["UNKNOWN_OBSERVATION"],
                  msg=make_action(seq=1, created=8, expires=12, epoch=2, obs="obs-1")),
            _send("fresh", 9, [9], ["ALLOW"], msg=make_action(seq=1, created=9, expires=13, epoch=2, obs="obs-2")),
        ]}


def _crash_state_lost():
    return {
        "name": "process_crash_state_lost",
        "description": "the gate restarts and the epoch store is gone: the gate LOCKS (holds everything) "
                       "and only an explicit operator reset with an epoch floor reopens it",
        "fault_tick": 4, "end_tick": 20, "events": [
            _obs(0),
            _send("s1", 1, [1], ["ALLOW"], msg=make_action(seq=1, created=1, expires=6)),
            _crash(4, "lost"),
            _send("after_crash", 5, [5], ["GATE_LOCKED"], msg=make_action(seq=2, created=5, expires=9)),
            _gate(6, "resume", expect_ok=False),
            _gate(6, "grant_epoch", expect_ok=False),
            _gate(8, "operator_reset", 10),
            _obs(9, "obs-2"),
            _gate(9, "resume"),
            _send("stale_replay", 10, [10], ["STALE_EPOCH"], msg=make_action(seq=2, created=5, expires=9 + 5, obs="obs-2")),
            _send("fresh", 11, [11], ["ALLOW"], msg=make_action(seq=1, created=11, expires=15, epoch=11, obs="obs-2")),
        ]}


def _unknown_skill():
    cmds = ["cmd_vel", "/cmd_vel", "shell", "rm -rf /", "", "SET_JOINT_TARGETS", "set_joint_targets ",
            "set_joint_targets\n", "set_joint_targets\x00", "x" * 100]
    events = [_obs(0)]
    for i, cmd in enumerate(cmds):
        tick = i + 1
        events.append(_send(f"skill_{i}", tick, [tick], ["UNKNOWN_SKILL"],
                            msg=make_action(seq=1, created=tick, expires=tick + 5, command=cmd)))
    events.append(_obs(10, "obs-2"))
    events.append(_send("valid_after_unknown", 11, [11], ["ALLOW"],
                        msg=make_action(seq=1, created=11, expires=16, obs="obs-2")))
    return {
        "name": "unknown_skill",
        "description": "cmd_vel, shell, near-miss spellings and control characters are not on the skill "
                       "allow-list; none advances the sequence, a valid command afterwards still works",
        "fault_tick": 1, "end_tick": 12, "config": {"watchdog_ticks": 50}, "events": events}


def _llm_invalid_args():
    base = valid_values()

    def vals(i, v):
        x = list(base)
        x[i] = v
        return x

    def args(**kw):
        a = valid_args()
        a.update(kw)
        return a

    args_cases = [
        ("values_as_strings", args(values=["0.1"] * 6), "BAD_ARGUMENT_TYPE"),
        ("nan", args(values=vals(0, NAN)), "NON_FINITE_VALUE"),
        ("inf", args(values=vals(0, INF)), "NON_FINITE_VALUE"),
        ("neg_inf", args(values=vals(0, -INF)), "NON_FINITE_VALUE"),
        ("huge_float", args(values=vals(0, 1e308)), "OUT_OF_BOUNDS"),
        ("huge_int", args(values=vals(0, 10**400)), "OUT_OF_BOUNDS"),
        ("neg_huge_int", args(values=vals(0, -10**400)), "OUT_OF_BOUNDS"),
        ("bool_value", args(values=vals(0, True)), "BAD_ARGUMENT_TYPE"),
        ("none_value", args(values=vals(0, None)), "BAD_ARGUMENT_TYPE"),
        ("nested_list_value", args(values=vals(0, [[1]])), "BAD_ARGUMENT_TYPE"),
        ("nested_dict_value", args(values=vals(0, {"x": 1})), "BAD_ARGUMENT_TYPE"),
        ("too_short", args(values=base[:5]), "WRONG_DIMENSION"),
        ("too_long", args(values=base + [0.0]), "WRONG_DIMENSION"),
        ("empty_values", args(values=[]), "WRONG_DIMENSION"),
        ("values_is_object", args(values={"a": 1}), "BAD_ARGUMENT_TYPE"),
        ("values_is_string", args(values="0.1,0,0,0,0,0.01"), "BAD_ARGUMENT_TYPE"),
        ("wrong_units", args(units=["deg"] * 6), "WRONG_UNITS"),
        ("units_is_string", args(units="rad"), "BAD_ARGUMENT_TYPE"),
        ("units_not_strings", args(units=[1] * 6), "BAD_ARGUMENT_TYPE"),
        ("out_of_bounds_lift", args(values=vals(1, 2.0)), "OUT_OF_BOUNDS"),
        ("step_too_large", args(values=vals(1, 1.0)), "STEP_TOO_LARGE"),
        ("extra_args_key", args(gain=5), "BAD_ARGUMENT_FIELDS"),
        ("missing_values", {k: v for k, v in valid_args().items() if k != "values"}, "BAD_ARGUMENT_FIELDS"),
        ("wrong_action_space", args(action_space="cartesian_velocity"), "WRONG_ACTION_SPACE"),
        ("action_space_not_string", args(action_space=5), "BAD_ARGUMENT_TYPE"),
    ]
    envelope_cases = [
        ("extra_top_level_key", dict(cmd_vel={"x": 1}), "UNKNOWN_FIELD"),
        ("epoch_is_object", dict(epoch={"a": 1}), "WRONG_TYPE"),
        ("sequence_is_list", dict(sequence_id=[1]), "WRONG_TYPE"),
        ("source_is_list", dict(source_id=["MOCK-policy-01"]), "WRONG_TYPE"),
        ("command_is_list", dict(command=["set_joint_targets"]), "WRONG_TYPE"),
        ("args_is_list", dict(args=[]), "WRONG_TYPE"),
        ("args_is_string", dict(args="values"), "WRONG_TYPE"),
        ("simulated_is_string", dict(simulated="true"), "WRONG_TYPE"),
        ("simulated_false", dict(simulated=False), "NOT_SIMULATED"),
        ("wrong_schema", dict(schema="action-gate/v0"), "UNSUPPORTED_SCHEMA"),
        ("bool_sequence", dict(sequence_id=True), "WRONG_TYPE"),
        ("zero_sequence", dict(sequence_id=0), "INTEGER_OUT_OF_RANGE"),
        ("huge_sequence", dict(sequence_id=10**400), "INTEGER_OUT_OF_RANGE"),
        ("float_sequence", dict(sequence_id=1.0), "WRONG_TYPE"),
        ("bad_identifier", dict(observation_id="obs 1\n"), "BAD_IDENTIFIER"),
    ]
    events = [_obs(0)]
    tick = 0
    for label, a, expect in args_cases:
        tick += 1
        events.append(_send(f"args_{label}", tick, [tick], [expect],
                            msg=make_action(seq=1, created=tick, expires=tick + 5, args=a)))
    for label, over, expect in envelope_cases:
        tick += 1
        events.append(_send(f"env_{label}", tick, [tick], [expect],
                            msg=make_action(seq=1, created=tick, expires=tick + 5, **over)))
    tick += 1
    events.append(_send("env_missing_expires", tick, [tick], ["MISSING_FIELD"],
                        msg=make_action(seq=1, created=tick, expires=tick + 5, drop=("expires_tick",))))
    texts = [
        ("text_not_json", "{not json", "MALFORMED_MESSAGE"),
        ("text_duplicate_keys", '{"schema": "action-gate/v1", "schema": "action-gate/v1"}', "MALFORMED_MESSAGE"),
        ("text_nan_literal", json.dumps(make_action()).replace("0.1", "NaN", 1), "MALFORMED_MESSAGE"),
        ("text_top_level_list", "[1, 2, 3]", "MALFORMED_MESSAGE"),
        ("text_deeply_nested", "[" * 60000, "MALFORMED_MESSAGE"),
        ("text_oversized", " " * 70000, "MALFORMED_MESSAGE"),
    ]
    for label, text, expect in texts:
        tick += 1
        events.append(_send(label, tick, [tick], [expect], text=text))
    tick += 1
    events.append(_send("valid_after_garbage", tick, [tick], ["ALLOW"],
                        msg=make_action(seq=1, created=tick, expires=tick + 5)))
    return {
        "name": "llm_invalid_args",
        "description": "argument and envelope shapes an LLM might emit (wrong types, NaN, huge numbers, "
                       "extra keys, nested objects, bad JSON); every one is a typed block, none advances "
                       "state, and a valid command afterwards is still allowed",
        "fault_tick": 1, "end_tick": tick + 1,
        "config": {"watchdog_ticks": 500, "observation_ttl_ticks": 500}, "events": events}


def _expired_observation():
    return {
        "name": "expired_observation",
        "description": "observation age is half-open: age 4 passes, age 5 (== ttl) is stale; unknown "
                       "and expired-message cases are distinct typed reasons",
        "fault_tick": 5, "end_tick": 8, "config": {"watchdog_ticks": 50}, "events": [
            _obs(0),
            _send("age_4", 4, [4], ["ALLOW"], msg=make_action(seq=1, created=4, expires=9)),
            _send("age_5_stale", 5, [5], ["STALE_OBSERVATION"], msg=make_action(seq=2, created=5, expires=10)),
            _send("unknown_obs", 5, [5], ["UNKNOWN_OBSERVATION"],
                  msg=make_action(seq=2, created=5, expires=10, obs="obs-zzz")),
            _send("expired_msg", 1, [6], ["EXPIRED"], msg=make_action(seq=2, created=1, expires=4)),
            _obs(6, "obs-2"),
            _send("fresh_obs", 6, [6], ["ALLOW"], msg=make_action(seq=2, created=6, expires=10, obs="obs-2")),
        ]}


def _intervention():
    return {
        "name": "operator_intervention",
        "description": "an operator takes over: commands are held as INTERVENTION_ACTIVE; release leads to "
                       "HOLD (not autonomous); safe-stop latches until reset; resume is explicit",
        "fault_tick": 3, "end_tick": 14, "config": {"watchdog_ticks": 50, "observation_ttl_ticks": 20}, "events": [
            _obs(0),
            _send("before", 1, [1], ["ALLOW"], msg=make_action(seq=1, created=1, expires=6)),
            _gate(3, "intervene"),
            _send("during_1", 3, [3], ["INTERVENTION_ACTIVE"], msg=make_action(seq=2, created=3, expires=8)),
            _send("during_2", 4, [4], ["INTERVENTION_ACTIVE"], msg=make_action(seq=3, created=4, expires=9)),
            _gate(6, "release_intervention"),
            _send("after_release", 7, [7], ["GATE_HOLD"], msg=make_action(seq=4, created=7, expires=12)),
            _gate(8, "resume"),
            _send("after_resume", 9, [9], ["ALLOW"], msg=make_action(seq=5, created=9, expires=14)),
            _gate(10, "safe_stop"),
            _send("during_safe_stop", 11, [11], ["SAFE_STOP_ACTIVE"], msg=make_action(seq=6, created=11, expires=16)),
            _gate(12, "resume", expect_ok=False),
            _gate(12, "reset_safe_stop"),
            _gate(12, "resume"),
            _send("after_reset", 13, [13], ["ALLOW"], msg=make_action(seq=7, created=13, expires=18)),
        ]}


def _model_failure():
    return {
        "name": "model_failure",
        "description": "unapproved model or revision, rogue source, out-of-step and NaN proposals are "
                       "blocked with model_failure/provenance reasons, then a valid proposal is allowed",
        "fault_tick": 1, "end_tick": 8, "config": {"watchdog_ticks": 50, "observation_ttl_ticks": 20}, "events": [
            _obs(0),
            _one("unapproved_revision", 1, "MODEL_NOT_APPROVED", model_revision="r2"),
            _one("unapproved_model", 2, "MODEL_NOT_APPROVED", model_id="other-model"),
            _one("rogue_source", 3, "UNAUTHORIZED_SOURCE", source_id="ROGUE-agent"),
            _one("step_too_large", 4, "STEP_TOO_LARGE", args=valid_args([0.0, 1.0, 0.0, 0.0, 0.0, 0.0])),
            _one("nan_values", 5, "NON_FINITE_VALUE", args=valid_args([NAN, 0.0, 0.0, 0.0, 0.0, 0.0])),
            _one("valid", 6, "ALLOW"),
        ]}


def _model_silent():
    return {
        "name": "model_silent_watchdog",
        "description": "the model stops answering after tick 2: no message arrives, the watchdog HOLDs "
                       "the gate 8 ticks later and a later message is held",
        "fault_tick": 2, "end_tick": 15, "config": {"watchdog_ticks": 8}, "events": [
            _obs(0),
            _send("last_good", 2, [2], ["ALLOW"], msg=make_action(seq=1, created=2, expires=7)),
            _send("after_silence", 14, [14], ["GATE_HOLD"], msg=make_action(seq=2, created=14, expires=19)),
        ]}


def _unavailable_command():
    return {
        "name": "unavailable_command",
        "description": "an allow-listed skill that is disabled, and non-simulated control domains, are "
                       "unavailable_command blocks; only the enabled skill passes",
        "fault_tick": 1, "end_tick": 8, "config": {"watchdog_ticks": 50, "observation_ttl_ticks": 20,
                                                   "enabled_commands": ["hold"]}, "events": [
            _obs(0),
            _one("disabled_skill", 1, "COMMAND_DISABLED"),
            _one("physical_domain", 2, "CONTROL_DOMAIN_DENIED", control_domain="physical", **_hold_msg()),
            _one("serial_port_domain", 3, "CONTROL_DOMAIN_DENIED", control_domain="/dev/ttyUSB0", **_hold_msg()),
            _one("not_simulated", 4, "NOT_SIMULATED", simulated=False, **_hold_msg()),
            _one("hold_ok", 5, "ALLOW", **_hold_msg()),
        ]}


def _modes_and_boundaries():
    return {
        "name": "modes_and_boundaries",
        "description": "unknown and mismatched modes; explicit equal-tick semantics: created==now ok, "
                       "created==now+1 future, expires==now expired, expires==now+1 ok, bounded expiry",
        "fault_tick": 5, "end_tick": 8, "config": {"watchdog_ticks": 100, "observation_ttl_ticks": 20}, "events": [
            _obs(0),
            _send("mode_manual", 5, [5], ["UNKNOWN_MODE"], msg=make_action(seq=1, created=5, expires=10, mode="manual")),
            _send("mode_upper", 5, [5], ["UNKNOWN_MODE"], msg=make_action(seq=1, created=5, expires=10, mode="AUTONOMOUS")),
            _send("mode_space", 5, [5], ["UNKNOWN_MODE"], msg=make_action(seq=1, created=5, expires=10, mode="autonomous ")),
            _send("mode_hold", 5, [5], ["MODE_MISMATCH"], msg=make_action(seq=1, created=5, expires=10, mode="hold")),
            _send("created_eq_now", 5, [5], ["ALLOW"], msg=make_action(seq=1, created=5, expires=10)),
            _send("created_future", 5, [5], ["FUTURE_TIMESTAMP"], msg=make_action(seq=2, created=6, expires=10)),
            _send("expires_eq_now", 3, [6], ["EXPIRED"], msg=make_action(seq=2, created=3, expires=6)),
            _send("expires_now_plus_1", 3, [6], ["ALLOW"], msg=make_action(seq=2, created=3, expires=7)),
            _send("expiry_too_long", 7, [7], ["INVALID_EXPIRY"], msg=make_action(seq=3, created=7, expires=28)),
            _send("expiry_eq_created", 7, [7], ["INVALID_EXPIRY"], msg=make_action(seq=3, created=7, expires=7)),
        ]}


def build_scenarios() -> list:
    return [
        _baseline(), _delayed_reply(), _reordered(), _duplicate(), _disconnection(), _crash_replay(),
        _crash_state_lost(), _unknown_skill(), _llm_invalid_args(), _expired_observation(),
        _intervention(), _model_failure(), _model_silent(), _unavailable_command(),
        _modes_and_boundaries(),
    ]
