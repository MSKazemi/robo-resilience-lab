# Fail-closed simulated action gate and fault suite (issue #59)

> **Simulation only. Non-actuating.** Nothing here talks to a robot, publishes to ROS 2 (no `/cmd_vel`), opens a
> socket, calls a model, uses a GPU or reads a clock. The gate is an educational design, **not** a safety-certified
> function, **not** real-time and **not** authentication. See [What this does NOT claim](#what-this-does-not-claim).

The gate sits between an untrusted proposal source (for example an LLM or a cloud policy) and a *simulated* actuator
sink. It decides **ALLOW / HOLD / REJECT** for each proposed simulated action and is **fail-closed**: anything it cannot
prove valid (missing field, wrong type, unknown skill, unknown mode, unparseable text, a check that raises or returns
something odd, an audit failure, a bad clock) is blocked, never allowed. Every blocked command returns a typed reason, the
failing field path and an audit record, and the sink records `dispatched: false`.

It builds on the [mock-policy contract](../mock-policy/README.md) (issue #55): the joint names, units, limits and the
action space are read from `../mock-policy/contract.py` (one source of truth); the gate does **not** duplicate or edit the
mock-policy validator. The gate's message envelope is its own (logical ticks, epoch, expiry), described below.

## Files

| File | Purpose |
| --- | --- |
| [`gate_core.py`](gate_core.py) | `ActionGate`, typed `Reason` / `Category` / `Verdict` / `Mode`, `GateConfig`, the ordered checks, `Decision`, `EpochStore`. |
| [`sim_sink.py`](sim_sink.py) | `SimActuatorSink`: the simulated, non-actuating sink; logs `dispatched: true/false` for every decision. |
| [`harness.py`](harness.py) | Gate + sink + store wiring, crash/restart, message builders, the scenario runner and report renderer. |
| [`scenarios.py`](scenarios.py) | The scripted fault cases (plain data). |
| [`run_fault_suite.py`](run_fault_suite.py) | Runs the suite; `--write` / `--check` the committed report. |
| [`reports/fault-suite.simulation.json`](reports/fault-suite.simulation.json) | Committed output of the suite. **SIMULATION output, logical ticks, deterministic**; re-generate with `--write`. |
| [`mutation_check.py`](mutation_check.py) | Flips one check at a time to fail-open in a scratch copy and expects the tests to fail. |
| [`THREAT-MODEL.md`](THREAT-MODEL.md) | Attack classes, controls, guarding tests, safe-state semantics, residual risks. |
| [`tests/`](tests/) | `unittest` suite (positive and negative). |

## Verify (stdlib only, CPU only, a few seconds)

```sh
# unit tests (the CI runner line; 147 tests at the time of writing, all passing)
python3 -m unittest discover -s examples/cloud-robotics/action-gate/tests -t examples/cloud-robotics/action-gate

# fault suite: runs every scenario, exits 0 only if every expectation held and nothing failed open
python3 examples/cloud-robotics/action-gate/run_fault_suite.py --check      # byte-compare with the committed report
python3 examples/cloud-robotics/action-gate/run_fault_suite.py              # print the full JSON report
python3 examples/cloud-robotics/action-gate/run_fault_suite.py --write      # refresh the committed report

# mutation check: 46 single-check mutants in a scratch copy, expects the tests to FAIL for each (about a minute)
python3 examples/cloud-robotics/action-gate/mutation_check.py
python3 examples/cloud-robotics/action-gate/mutation_check.py --list
```

Needs Python 3.10 or newer (developed on 3.12). The tests write no files; `mutation_check.py` works only in a temporary
directory. Run the commands with `PYTHONDONTWRITEBYTECODE=1` to avoid `__pycache__`.

## The proposed-action message (`action-gate/v1`)

All 14 fields are required; unknown fields are rejected (not ignored).

| Field | Type | Rule |
| --- | --- | --- |
| `schema` | string | must equal `action-gate/v1` |
| `simulated` | boolean | must be exactly `true` |
| `source_id` | identifier | on the source allow-list (provenance label, **not** authentication) |
| `model_id`, `model_revision` | identifier | the pair must be on the approved-model list |
| `control_domain` | string | must be in the allowed simulated domains (default `sim-arm`) |
| `command` | string | exactly `set_joint_targets` or `hold`; anything else is `UNKNOWN_SKILL` |
| `mode` | string | one of `autonomous`, `hold`, `intervention`, `safe_stop`, `locked`; must equal the gate mode |
| `epoch`, `sequence_id` | integer 1..2^53 | epoch must be the gate's current epoch; sequence strictly greater than the last allowed one |
| `observation_id` | identifier | a registered observation, fresh and not newer than the action |
| `created_tick`, `expires_tick` | integer | valid while `created_tick <= now < expires_tick`; `expires_tick - created_tick <= max_message_ttl_ticks` |
| `args` | object | for `set_joint_targets`: exactly `action_space` (`joint_position_target`), `units` (6 strings, `rad` x5 then `m`) and `values` (6 finite numbers inside the joint limits and within `max_step` of the observed state); for `hold`: `{}` |

Identifiers match `[A-Za-z0-9][A-Za-z0-9._:-]{0,63}` (whole value, ASCII only). Type checks are exact: `True` is not an
integer, subclasses of `str`/`int`/`float`/`dict`/`list` are refused.

The default `GateConfig` allows **no** source and approves **no** model, so a gate you have not configured blocks everything.

## Semantics that are explicit and tested

| Question | Answer |
| --- | --- |
| Message window | half-open: valid for `created_tick <= now < expires_tick`. `created == now` ok, `created == now + 1` is `FUTURE_TIMESTAMP`, `expires == now` is `EXPIRED`, `expires == now + 1` ok |
| Observation age | half-open: valid while `now < observation.tick + observation_ttl_ticks` (default 5): age 4 passes, age 5 is `STALE_OBSERVATION` |
| Sequence | strictly increasing per epoch; equal is `DUPLICATE_SEQUENCE`, lower is `STALE_SEQUENCE`; **gaps are allowed** so one lost message cannot wedge the gate |
| Reordering | the newest sequence wins; an older packet arriving later is never dispatched |
| Bounded memory | audit ring, observation registry and pending-decision table are bounded; every drop is counted (`audit_dropped`, `observations_evicted`, `pending_evicted`); an evicted observation or decision fails closed. The sink's in-memory log is unbounded and meant only for the harness |
| A blocked message | changes **no** gate state (asserted by snapshot equality); only an `ALLOW` advances sequence and the watchdog |
| Logical clock | may stay equal, never decreases (`CLOCK_REGRESSION`); non-integer or negative is `INVALID_CLOCK` |
| Watchdog | no allowed command for `watchdog_ticks` (default 10) puts the gate in `HOLD` (at exactly `last + 10`); checked lazily inside `evaluate` too |
| Restart | a new gate object boots in `HOLD` with epoch = stored epoch + 1 (persisted before use); pre-crash epochs are `STALE_EPOCH`; observations and pending decisions are gone |
| Unreadable epoch store | the gate `LOCKS`; only `operator_reset(epoch_floor)` leaves, and only into `HOLD` |
| Order of checks | see [THREAT-MODEL.md](THREAT-MODEL.md#check-order-first-failure-wins-reproducible); the first failing check decides the reason |

## Verdicts, reason codes, categories

Verdict is derived from the reason: `HOLD` for the categories `hold` and `intervention` (the gate is in a non-actuating
state), `REJECT` for everything else. `ALLOW` only when every check returned the `PASS` sentinel.

| Category | Verdict | Reason codes |
| --- | --- | --- |
| `unavailable_command` | REJECT | `UNKNOWN_SKILL`, `COMMAND_DISABLED`, `CONTROL_DOMAIN_DENIED`, `NOT_SIMULATED` |
| `stale_response` | REJECT | `STALE_OBSERVATION`, `UNKNOWN_OBSERVATION`, `EXPIRED`, `FUTURE_TIMESTAMP`, `DUPLICATE_SEQUENCE`, `STALE_SEQUENCE`, `STALE_EPOCH`, `EPOCH_NOT_GRANTED` |
| `intervention` | HOLD | `INTERVENTION_ACTIVE` |
| `model_failure` | REJECT | `MODEL_NOT_APPROVED`, `BAD_ARGUMENT_FIELDS`, `BAD_ARGUMENT_TYPE`, `WRONG_ACTION_SPACE`, `WRONG_DIMENSION`, `WRONG_UNITS`, `NON_FINITE_VALUE`, `OUT_OF_BOUNDS`, `STEP_TOO_LARGE` |
| `provenance` | REJECT | `UNAUTHORIZED_SOURCE` |
| `malformed` | REJECT | `MALFORMED_MESSAGE`, `MISSING_FIELD`, `UNKNOWN_FIELD`, `WRONG_TYPE`, `BAD_IDENTIFIER`, `INTEGER_OUT_OF_RANGE`, `UNSUPPORTED_SCHEMA`, `UNKNOWN_MODE`, `INVALID_EXPIRY`, `INVALID_TIMELINE`, `DUPLICATE_OBSERVATION` |
| `hold` | HOLD | `GATE_HOLD`, `SAFE_STOP_ACTIVE`, `GATE_LOCKED`, `MODE_MISMATCH` |
| `gate_fault` | REJECT | `INTERNAL_ERROR`, `CHECK_RETURNED_INVALID`, `AUDIT_FAILURE`, `CLOCK_REGRESSION`, `INVALID_CLOCK`, `INVALID_TRANSITION`, `EPOCH_PERSIST_FAILED`, `EPOCH_EXHAUSTED`, `INVALID_ARGUMENT` |

The four distinctions the issue asks for are separate categories with disjoint reason sets: **unavailable command**,
**stale response**, **intervention**, **model failure** (a test asserts each scenario produces its own category).

Sink refusal codes (the second layer, recorded when `dispatched` is `false`): `GATE_BLOCKED`, `NOT_A_DECISION`,
`SINK_NOT_ACTIVE`, `DECISION_NOT_PENDING`, `GATE_NOT_AUTONOMOUS`, `EPOCH_CHANGED`, `DECISION_EXPIRED`, `SINK_BOUNDS`,
`SINK_ERROR`.

## Simulated safe states

The sink mirrors the gate mode one to one. `hold`: nothing is dispatched and the simulated target register keeps its last
value. `intervention`: an operator has control, nothing is dispatched. `safe_stop`: like `hold` but latched until
`reset_safe_stop`. `locked`: gate state lost, nothing is dispatched until `operator_reset`. Release from `intervention`
or `safe_stop` always goes through `hold`, and `resume` is an explicit operator call. **These are logical states of a
simulation; they say nothing about motor braking or a physical stop.**

## Fault-case suite

`scenarios.py` plays scripted events on a logical-tick timeline (events, then deliveries in send order, then the watchdog,
in every tick). Each delivery has an expected outcome; the runner checks verdict, reason and category and counts
mismatches. **Time-to-block** = first blocking event (a non-ALLOW decision or a watchdog HOLD) at or after `fault_tick`,
minus `fault_tick`, in logical ticks. The gate decides synchronously on arrival, so arrival-to-decision is 0 ticks by
construction; the number that varies is fault onset to first block (it includes the scripted delay or silence).

Result of `python3 examples/cloud-robotics/action-gate/run_fault_suite.py` (SIMULATION, from the committed report):

| Scenario | Deliveries | Allowed | Held | Rejected | Dropped by link | Dispatched | Time-to-block (ticks) | First block by | failure_count |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| `baseline_valid` | 2 | 2 | 0 | 0 | 0 | 2 | n/a | n/a | 0 |
| `delayed_reply` | 3 | 1 | 1 | 1 | 0 | 1 | 7 | decision | 0 |
| `reordered_packet` | 3 | 2 | 0 | 1 | 0 | 2 | 4 | decision | 0 |
| `duplicate_packet` | 3 | 1 | 0 | 2 | 0 | 1 | 0 | decision | 0 |
| `disconnection` | 5 | 2 | 1 | 1 | 1 | 2 | 7 | watchdog | 0 |
| `process_crash_replay` | 7 | 3 | 1 | 3 | 0 | 3 | 0 | decision | 0 |
| `process_crash_state_lost` | 4 | 2 | 1 | 1 | 0 | 2 | 1 | decision | 0 |
| `unknown_skill` | 11 | 1 | 0 | 10 | 0 | 1 | 0 | decision | 0 |
| `llm_invalid_args` | 48 | 1 | 0 | 47 | 0 | 1 | 0 | decision | 0 |
| `expired_observation` | 5 | 2 | 0 | 3 | 0 | 2 | 0 | decision | 0 |
| `operator_intervention` | 7 | 3 | 4 | 0 | 0 | 3 | 0 | decision | 0 |
| `model_failure` | 6 | 1 | 0 | 5 | 0 | 1 | 0 | decision | 0 |
| `model_silent_watchdog` | 2 | 1 | 1 | 0 | 0 | 1 | 8 | watchdog | 0 |
| `unavailable_command` | 5 | 1 | 0 | 4 | 0 | 1 | 0 | decision | 0 |
| `modes_and_boundaries` | 10 | 2 | 1 | 7 | 0 | 2 | 0 | decision | 0 |
| **total (15 scenarios)** | **121** | | | | | **25** | | | **0** |

Totals: `fail_open` = 0, `false_blocks` = 0, `failure_count` = 0. Blocked decisions by category: `hold` 8, `intervention` 2, `malformed` 26, `model_failure` 29, `provenance` 1, `stale_response` 14, `unavailable_command` 15. Every one of the dispatched commands was an expected `ALLOW`; every blocked or dropped delivery has `dispatched: false`.

`fail_open` counts dispatches where a block was expected (must be 0). `failure_count` counts unmet expectations of any
kind, including false blocks and deliveries that were scheduled but never run. A test deliberately breaks expectations to
show the runner reports them (`HarnessSelfCheckTests`).

## Mutation check

`mutation_check.py` copies this directory into a scratch directory, applies one mutant at a time (for example "expiry not
enforced", "check that returns None counts as PASS", "exception inside a check is swallowed", "a rejected action advances
the sequence", "restart re-uses the old epoch", "sink ignores the gate's confirmation"), runs the unit tests and expects
failures. Run it with the command above; the last line reports `killed` and `survived`. A survivor is a hole in the tests.

Last result (run on 2026-11-13, `python3 examples/cloud-robotics/action-gate/mutation_check.py`, exit 0): unmutated control passes, **46 mutants, 46 killed, 0 survived**. This only shows the tests notice those 46 specific changes; it is not a proof that no other mutation would survive.

## What this does NOT claim

* **No certification.** Not IEC 61508, ISO 13849, ISO 10218, UL 4600 or any other safety standard; no safety case, no hazard analysis of a real system. NIST AI RMF is only a reference for vocabulary.
* **No real-time.** Time is a logical integer tick. No wall-clock latency, jitter, deadline, throughput or worst-case time is measured or implied. Any "time-to-block" number is in simulation ticks.
* **No hardware, no motors.** No physical actuator, `/cmd_vel`, serial port, motor driver or robot address is touched. A `HOLD`, `SAFE_STOP` or `locked` state is a **simulated logical state**, not motor braking and not a physical safe stop.
* **No ROS 2, DDS, Kubernetes or cross-worker behaviour was run.** The "network faults" are scripted events (delay, reorder, duplicate, loss, link down, process restart), not a real network. Full-cluster fault tests (issues #14, #15, #16) are not done here.
* **No authentication.** `source_id` and model identifiers are labels compared to allow-lists; a forger who can write messages can claim them. Real deployments need authenticated transport (for example SROS 2 or signed messages).
* **No real model output.** The proposals are synthetic fixtures written for the tests; no LLM was called and nothing here reports model quality.
* **Not a complete defence.** Limits and residual risks (epoch-store rollback, sequence exhaustion, unauthenticated observations, malicious code in the same process) are listed in [THREAT-MODEL.md](THREAT-MODEL.md). Passing these tests shows the listed cases behave as described, not that no other bypass exists.
* The committed JSON report is deterministic simulation output; it is not a benchmark and must not be quoted as one.

Related: issue #59 (this work), the typed contract in [`../mock-policy/`](../mock-policy/README.md) (#55), the
fencing/epoch idea in [`../agent-mobility/`](../agent-mobility/README.md), and the non-actuating sink and
stale/duplicate response rules in [`../../../experiments/edge-cloud-placement/`](../../../experiments/edge-cloud-placement/README.md).
