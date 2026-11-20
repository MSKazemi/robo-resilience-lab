# Threat model: simulated action gate (issue #59)

> **Simulation only.** This is a model of a software gate between an untrusted
> proposal source (for example an LLM or a cloud policy) and a *simulated*
> actuator sink. It describes what the code in this directory does and does not
> defend against. It is not a safety case, not a certification artefact and says
> nothing about a physical robot, motors, brakes or real-time behaviour.

## 1. What is protected

The single property: **a proposal that the gate cannot prove valid never reaches the simulated actuator sink.** "Reaches"
means the sink records `dispatched: true` (and changes its simulated target register). Every
other outcome is a block (`HOLD` or `REJECT`) with a typed reason code, a failing field path and an audit record, and
the sink records `dispatched: false`.

## 2. Trust boundaries and assumptions

| Boundary | Assumption |
| --- | --- |
| Proposal (message or JSON text) | **Untrusted.** Any value of any type may arrive. |
| Operator calls (`resume`, `intervene`, `safe_stop`, `operator_reset`, `grant_epoch`) | Trusted: they are method calls from the code that owns the gate. There is no authentication of the operator in this model. |
| `source_id`, `model_id`, `model_revision` | Self-declared labels compared with an allow-list. **Not authentication.** A forger who can write any message can claim any label. Real deployments need authenticated transport (for example SROS 2 or signed messages), which is not provided here. |
| Logical clock `now` | Supplied by the caller. The gate only checks it is a non-negative integer that never decreases. It cannot detect a clock that is consistently wrong. |
| `EpochStore` | Simulated durable counter. Assumed to return what was last saved or to fail visibly. A store that silently returns an older valid number is **not** detected (see residual risks). |
| Python process | The gate and the sink share one interpreter. A caller that can run arbitrary Python can bypass any check (for example by editing a private attribute). The bypass tests cover mistakes and malicious *data*, not malicious *code*. |

## 3. Attack and failure classes, controls, and the tests that guard them

Every row has at least one regression test. File names are under `tests/`.

| # | Class | Control in the code | Typed reason(s) | Guarding tests |
| --- | --- | --- | --- | --- |
| 1 | Missing, extra or wrongly typed field; non-object message | Exact key set, exact types (`type(v) is int`, so `bool` and subclasses are refused), bounded key count | `MALFORMED_MESSAGE`, `MISSING_FIELD`, `UNKNOWN_FIELD`, `WRONG_TYPE`, `BAD_IDENTIFIER`, `INTEGER_OUT_OF_RANGE` | `test_checks.EnvelopeTests`, `test_failopen.TypeConfusionTests`, `test_failopen.HostileObjectTests` |
| 2 | Bool where an integer is required (`True` as sequence, epoch, tick, joint value) | Exact-type integer check | `WRONG_TYPE`, `BAD_ARGUMENT_TYPE` | `test_failopen.TypeConfusionTests.test_bool_is_not_an_int_anywhere` |
| 3 | Unhashable or hostile value where a string is looked up (`[]`, `{}`, objects with raising `__eq__`/`__hash__`) | Type is checked **before** any `in`, dict lookup, hashing or comparison; subclasses are refused by exact type | `WRONG_TYPE` | `test_failopen.TypeConfusionTests.test_unhashable_values_where_a_string_is_looked_up`, `test_failopen.HostileObjectTests.test_str_subclass_with_hostile_methods_is_rejected_by_exact_type` |
| 4 | An exception inside a check | Every check runs in `try/except Exception`; an exception blocks | `INTERNAL_ERROR` | `test_failopen.CheckFailureTests.test_exception_in_a_check_blocks` |
| 5 | A check that returns `None`, `True`, a string or anything but the `PASS` sentinel | Only the `PASS` sentinel continues; anything else blocks | `CHECK_RETURNED_INVALID` | `test_failopen.CheckFailureTests.test_a_check_that_returns_none_or_anything_but_pass_blocks`, `..._forgets_to_return_blocks` |
| 6 | Default checks removed or reordered by configuration | The constructor can only add checks before or after the defaults | (constructor error) | `test_failopen.CheckFailureTests.test_default_checks_cannot_be_removed_through_the_api` |
| 7 | Gate bookkeeping fails (for example while writing the audit record) | Outer `try/except` returns a constant `REJECT`; if the audit record cannot be stored an `ALLOW` is downgraded ("no audit, no actuation") | `INTERNAL_ERROR`, `AUDIT_FAILURE` | `test_failopen.CheckFailureTests.test_internal_bookkeeping_error_gives_constant_reject`, `..._audit_failure_blocks_an_allow_and_changes_no_state` |
| 8 | NaN, infinity, huge numbers (`1e308`, `10**400`), out-of-range values | Type, then finiteness, then bounds, in that order; huge integers are compared, never converted first | `NON_FINITE_VALUE`, `OUT_OF_BOUNDS` | `test_checks.ArgumentTests` |
| 9 | Wrong dimension, wrong units, extra or missing argument keys, wrong action space | Exact list lengths (checked before iterating), per-dimension unit strings, exact argument key set | `WRONG_DIMENSION`, `WRONG_UNITS`, `BAD_ARGUMENT_FIELDS`, `WRONG_ACTION_SPACE` | `test_checks.ArgumentTests` |
| 10 | Large steps (trajectory jump) | Per-joint `abs(target - observed state)` bound, inclusive | `STEP_TOO_LARGE` | `test_checks.ArgumentTests.test_trajectory_step_bound_is_relative_to_the_observed_state` |
| 11 | Unknown skill (`cmd_vel`, `shell`, near-miss spellings, control characters, very long names) | Exact-match allow-list of two skills (`set_joint_targets`, `hold`); a third state "allow-listed but disabled" is its own reason | `UNKNOWN_SKILL`, `COMMAND_DISABLED` | `test_checks.ProvenanceAndCommandTests`, scenario `unknown_skill` |
| 12 | Non-simulated action, physical or serial-port control domain | `simulated` must be the boolean `True`; `control_domain` must be in the configured simulated domains | `NOT_SIMULATED`, `CONTROL_DOMAIN_DENIED` | `test_checks.ProvenanceAndCommandTests`, scenario `unavailable_command` |
| 13 | Unauthorised source, unapproved model or revision | Whole-value allow-lists; the default configuration allows nothing | `UNAUTHORIZED_SOURCE`, `MODEL_NOT_APPROVED` | `test_checks.ProvenanceAndCommandTests` |
| 14 | Replay: duplicate sequence | Sequence must be strictly greater than the last **allowed** one in the epoch | `DUPLICATE_SEQUENCE` | `test_state.SequenceTests`, scenario `duplicate_packet` |
| 15 | Reordering: an older packet arrives after a newer one | Older sequence is refused; the newest command wins, the older is never dispatched | `STALE_SEQUENCE` | `test_state.SequenceTests.test_reordered_packets_older_one_is_never_dispatched`, scenario `reordered_packet` |
| 16 | A rejected action poisons the sequence state (for example a forged huge sequence number) | State is committed last and only for an `ALLOW`; snapshot equality is asserted after every blocked message | (state unchanged) | `test_state.SequenceTests.test_a_rejected_action_does_not_advance_the_accepted_sequence`, `support.GateCase.assertBlocked` |
| 17 | Delayed reply, expired message, expired observation | Half-open windows: message valid for `created_tick <= now < expires_tick`; observation valid while `now < tick + observation_ttl_ticks`; expiry length bounded | `EXPIRED`, `STALE_OBSERVATION`, `UNKNOWN_OBSERVATION`, `FUTURE_TIMESTAMP`, `INVALID_EXPIRY`, `INVALID_TIMELINE` | `test_checks.TimelineTests`, scenarios `delayed_reply`, `expired_observation`, `modes_and_boundaries` |
| 18 | Disconnection or a silent model | Watchdog: no **allowed** command for `watchdog_ticks` puts the gate in `HOLD`. It also runs lazily inside `evaluate`, so a late message cannot slip through when nobody called `tick()`. Rejected messages do not refresh the watchdog | `GATE_HOLD` | `test_state.WatchdogTests`, scenarios `disconnection`, `model_silent_watchdog` |
| 19 | Process crash, then replay of pre-crash messages (including never-seen sequence numbers) | A restart takes a **new epoch** from a durable store (written before use). Messages must carry the current epoch exactly. Decisions issued before the crash cannot be dispatched. Observations are volatile and must be re-registered | `STALE_EPOCH`, `EPOCH_NOT_GRANTED`, `GATE_HOLD`, `UNKNOWN_OBSERVATION` | `test_state.EpochAndRestartTests`, scenario `process_crash_replay` |
| 20 | Crash with lost, corrupt or unwritable epoch store | The gate **locks** (holds everything). Only `operator_reset(epoch_floor)` leaves the locked state, and only into `HOLD` | `GATE_LOCKED`, `EPOCH_PERSIST_FAILED`, `INVALID_ARGUMENT` | `test_state.EpochAndRestartTests`, scenario `process_crash_state_lost` |
| 21 | Operator intervention or safe-stop ignored | Mode gate is the first check; only `AUTONOMOUS` passes; `release_intervention` leads to `HOLD`, never straight to autonomous; safe-stop latches until `reset_safe_stop` | `INTERVENTION_ACTIVE`, `SAFE_STOP_ACTIVE`, `GATE_HOLD`, `MODE_MISMATCH`, `UNKNOWN_MODE`, `INVALID_TRANSITION` | `test_state.ModeTransitionTests`, scenario `operator_intervention`, `modes_and_boundaries` |
| 22 | Dispatch bypass: calling the sink with a hand-made `ALLOW`, a copied decision, a replayed decision, a decision from another gate or from before a hold/epoch change | The sink dispatches only a `Decision` object the gate issued, once (identity checked, then consumed); pending decisions are cleared on any non-autonomous mode; the sink also re-checks expiry, its own mode and the target bounds | `DECISION_NOT_PENDING`, `NOT_A_DECISION`, `DECISION_EXPIRED`, `SINK_NOT_ACTIVE`, `SINK_BOUNDS` (sink refusal codes, see README) | `test_sink.BypassTests` |
| 23 | Time-of-check/time-of-use: the caller mutates the message after the gate approved it | The sink acts only on the immutable `Decision` (validated values copied into a tuple), never on the message | (n/a) | `test_sink.BypassTests.test_message_mutation_after_the_check_changes_nothing` |
| 24 | Unparseable text, duplicate JSON keys, `NaN`/`Infinity` literals, deep nesting, oversized or int-digit-bomb input | Strict parser: size cap, duplicate keys and constants rejected, every parser exception (including `RecursionError`) becomes a typed block | `MALFORMED_MESSAGE` | `test_failopen.ClockAndTextTests` |
| 25 | Hostile clock values (`None`, bool, float, negative, regress) | Integer, range and non-decreasing check on every call | `INVALID_CLOCK`, `CLOCK_REGRESSION` | `test_failopen.ClockAndTextTests` |
| 26 | Log injection or unbounded log growth through attacker-controlled strings | Audit text is reduced to printable ASCII and length-bounded; the audit ring is bounded and counts what it drops (`audit_dropped`) | (n/a) | `test_failopen.HostileObjectTests.test_audit_text_is_sanitised_and_bounded`, `test_state.AuditTests` |
| 27 | Resource exhaustion by size (huge lists, long strings, many keys, deep or cyclic structures) | Lengths are checked before iteration; values are never recursed into or copied deeply; hard caps on keys and text size | `WRONG_DIMENSION`, `MALFORMED_MESSAGE`, `BAD_ARGUMENT_TYPE` | `test_failopen.HostileObjectTests.test_huge_deep_and_cyclic_values_do_not_hang_or_raise`, `test_checks.ArgumentTests.test_huge_list_is_rejected_by_length_without_iterating` |

### Check order (first failure wins; reproducible)

1. gate mode and watchdog (`gate_mode`): looks at gate state only, so any message gets a `HOLD` while the gate holds;
2. `envelope` (exact keys and types);
3. `schema` (version, `simulated`);
4. `provenance` (source, model);
5. `domain_and_command`;
6. `mode` (message mode);
7. `args` (shape, dimension, units, finiteness, bounds);
8. `epoch_and_sequence`;
9. `timeline` (created, expiry, observation known and fresh);
10. `trajectory` (step bound against the observed state).

The sequence check sits before the timeline checks on purpose: an old replayed message is reported as a replay
(`DUPLICATE_SEQUENCE` or `STALE_SEQUENCE`), not just as expired. Only an `ALLOW` changes state.

## 4. Safe-state semantics (simulation only)

| Gate mode | Meaning in the simulation | Commands |
| --- | --- | --- |
| `autonomous` | Gate-approved commands are dispatched to the simulated sink | allowed if every check passes |
| `hold` | Nothing is dispatched; the simulated target register keeps its last value ("hold last simulated target"). Entered at boot, after a restart, by the watchdog, by a new epoch, or by the operator | `HOLD` with `GATE_HOLD` |
| `intervention` | An operator has taken control; nothing is dispatched | `HOLD` with `INTERVENTION_ACTIVE` |
| `safe_stop` | Like `hold`, but latched until `reset_safe_stop` | `HOLD` with `SAFE_STOP_ACTIVE` |
| `locked` | Gate state lost or unreadable; nothing is dispatched until `operator_reset` | `HOLD` with `GATE_LOCKED` |

These are logical states of a simulation. Nothing here brakes a motor, cuts power, or produces a physical stop, and the
suite never claims it does.

## 5. Residual risks and explicit non-goals

* **No authentication.** Labels are not identities; replay protection relies on the epoch and sequence discipline, not on a secret.
* **Epoch store rollback.** If the store silently returns an older valid number, the next epoch may repeat an old one and pre-crash messages with that epoch become valid again. `operator_reset` trusts the operator to give a floor at least as high as every epoch ever used.
* **Sequence exhaustion.** `sequence_id` is capped at 2^53; reaching the cap wedges the epoch until a new epoch is granted (tested, documented, fail-closed).
* **Observations are only registered, not authenticated.** A wrong-but-fresh observation widens or shifts the step window; the joint limits still bound every target.
* **Latency.** Decisions are synchronous in the logical-tick simulation. No wall-clock or real-time deadline is modelled or measured.
* **Malicious code in the same process** is out of scope (class 22 covers mistakes, not an attacker with code execution).
* **Gate availability.** The gate fails closed, so a fault stops motion; this model does not address how a real system would reach a safe physical state.
* **Not tested here:** ROS 2, DDS, Kubernetes, cross-node delivery, hardware, real LLM output, concurrency or threads.
