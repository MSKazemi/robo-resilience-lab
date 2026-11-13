# Simulated skill-proposal review and injection challenge (issues #64 and #86)

> **SIMULATION. SYNTHETIC. NON-ACTUATING. NOT A SECURITY GUARANTEE.** This module reviews text that *stands in for* a language model's skill proposal. It calls no model, opens no socket, imports no ROS 2 package, runs no shell and has no executor: nothing here can move a robot, a simulator or a cluster. Every "proposal" is a hand-written fixture. A verdict of `ACCEPT` is **not** an authorisation to execute; it only says "well-formed call to an allowlisted, simulated skill". The corpus is small and hand-made, so a pass means "these enumerated cases behave as designed", not "this reviewer is safe". No LLM, hosted advisor, latency, cost or reviewer-effort measurement exists in this repository; see [What this does NOT claim](#what-this-does-not-claim).

## Where it sits

```
untrusted text (LLM output, RAG snippet, log, sensor note)        <- UNTRUSTED, never executed
        |
        v
  strict_json.parse_object      one JSON object, nothing else (this package)
        |
        v
  reviewer.review               typed fields + allowlisted catalogue + caller-supplied simulated context
        |   ACCEPT / NEEDS_HUMAN / REJECT + typed reasons + audit record      (this package)
        v
  deterministic action gate     authoritative policy (issue #59; separate module, NOT imported here)
        |
        v
  human approval for consequential actions  ->  simulator-specific executor   (NOT built here)
```

An optional **advisor** opinion (the issue's "Jev" screening) may be passed to `review(..., advisory=...)`. It is written into the audit record with `used_for_verdict: false` and **cannot** change the verdict, add a privilege or lift a rejection; the tests prove this over the whole corpus.

## Files

| File | Purpose |
| --- | --- |
| [`skill_review/catalogue.py`](skill_review/catalogue.py) | Closed catalogue of 6 simulated skills with typed arguments, units, bounds, safety levels and preconditions. `python3 -m skill_review.catalogue` (run inside this directory) lists it. |
| [`skill_review/strict_json.py`](skill_review/strict_json.py) | Strict single-object JSON parser (stdlib `json` with hooks). |
| [`skill_review/reviewer.py`](skill_review/reviewer.py) | `review(data, context, advisory=None)` returning a typed `ReviewResult`; never raises. |
| [`skill_review/report.py`](skill_review/report.py) | Corpus runner and per-attack-class report. |
| [`build_corpus.py`](build_corpus.py) | Deterministic generator of [`fixtures/corpus.json`](fixtures/corpus.json); imports nothing from the reviewer, so expectations are an independent oracle. `--check` detects drift. |
| [`evaluate.py`](evaluate.py) | Prints the per-class outcome table; `--json`, `--case <id>` (annotated audit trace). |
| [`review_cli.py`](review_cli.py) | Review one proposal from stdin (student exercise). |
| [`tests/`](tests/) | `unittest` suite: positive, negative, bypass/fail-open regressions, seeded fuzz, corpus, CLIs. |

## Run (Python 3.10+, standard library only, CPU only, a few seconds: the CLI tests start subprocesses)

From the repository root:

```sh
python3 -m unittest discover -s examples/cloud-robotics/skill-proposal-review/tests -t examples/cloud-robotics/skill-proposal-review
python3 examples/cloud-robotics/skill-proposal-review/evaluate.py              # exit 0 if every expectation is met
python3 examples/cloud-robotics/skill-proposal-review/evaluate.py --case inj-01   # one annotated audit trace
python3 examples/cloud-robotics/skill-proposal-review/build_corpus.py --check  # exit 1 if fixtures drifted
echo '{"proposal_version":"skill-proposal/v1","skill":"get_status","robot_id":"sim_robot_1","args":{}}' \
  | python3 examples/cloud-robotics/skill-proposal-review/review_cli.py --context idle_fresh
```

Do not use `python3 -I` for the scripts: isolated mode hides the script directory from imports. The tests write no files. `evaluate.py` exit codes: `0` all expectations met, `1` one failed, `2` unreadable corpus or bad arguments. `review_cli.py` exit codes: `0` ACCEPT, `3` NEEDS_HUMAN, `4` REJECT, `2` usage error. `review_cli.py --help` prints usage without reading stdin; an unknown flag, a missing or unknown `--context`, or an interactive terminal with nothing piped in exits 2. It reads at most the reviewer's cap plus one byte (`MAX_INPUT_BYTES + 1`), so oversized input is reported as `INPUT_TOO_LARGE` instead of being truncated into something valid-looking.

## The skill catalogue (closed, simulated)

| Skill | Safety level | Arguments (type, unit, bounds) | Preconditions |
| --- | --- | --- | --- |
| `get_status` | read_only | none | none |
| `inspect_scene` | read_only | `view` in `summary`, `objects`, `obstacles` | none |
| `hold_position_sim` | stop_only | none | none |
| `cancel_simulated_task` | stop_only | `task_id` matching `task-[0-9]{1,6}` | an active task with that id |
| `navigate_to_named_waypoint_sim` | consequential | `waypoint` in `dock_a`, `station_1`, `station_2`, `charging_bay`; optional `speed_limit_mps` number 0.05..0.5 m/s | fresh state (at most 2000 ms), robot idle, no simulated e-stop |
| `start_approved_benchmark` | consequential | `benchmark_id` in `sim_smoke_v1`, `sim_nav_basic_v1`; optional `max_runtime_s` integer 1..300 s | same three |

The names and value sets are SYNTHETIC; they do not refer to real map data or to benchmark scripts in this repository. There is no free-form string, path, URL, command or topic argument kind, and no skill that publishes a velocity (`/cmd_vel` is never accepted). `validate_catalogue` refuses names containing `cmd_vel`, `shell`, `exec`, `topic`, `kubectl` and similar at import time, and the tests check it.

## Proposal format and verdicts

One JSON object, nothing before or after it: `proposal_version` (`skill-proposal/v1`), `skill`, `robot_id` (`sim_robot_1` only), `args`, optional `rationale` (free text, at most 280 characters, no control/format characters). A proposer that cannot decide may send `{"proposal_version":..., "skill": null, "clarification_request": "..."}`. Any other field is rejected.

| Verdict | Lab colour | Meaning |
| --- | --- | --- |
| `ACCEPT` | green | Well-formed call to a `read_only` or `stop_only` skill; `result.call` holds the typed call. Still goes to the action gate. |
| `NEEDS_HUMAN` | amber / unknown | A valid `consequential` call (`HUMAN_APPROVAL_REQUIRED`, call in `result.pending_call`, `result.call` stays `None`) or a clarification request (`CLARIFICATION_REQUESTED`). |
| `REJECT` | red | Anything else, with typed reasons. |

`result.call` is set only for `ACCEPT`; a test asserts this invariant so a caller that executes `call` can never run a consequential call without a human.

Reason codes (all in `REASON_CODES`): parse stage `INPUT_NOT_TEXT`, `INPUT_TOO_LARGE`, `INVALID_ENCODING`, `EMPTY_INPUT`, `NOT_STRICT_JSON`, `NOT_A_JSON_OBJECT`, `DUPLICATE_KEY`, `NON_FINITE_NUMBER`, `NUMBER_TOO_LARGE`, `NESTING_TOO_DEEP`; structure `UNKNOWN_FIELD`, `MISSING_FIELD`, `WRONG_TYPE`, `UNSUPPORTED_VERSION`, `CONFLICTING_FIELDS`, `RATIONALE_INVALID`, `MALFORMED_IDENTIFIER`, `UNKNOWN_SKILL`, `UNSUPPORTED_ROBOT`, `UNKNOWN_ARGUMENT`, `MISSING_ARGUMENT`, `PATH_OR_URL_ARGUMENT`, `UNKNOWN_ARG_VALUE`, `OUT_OF_RANGE`; state `CONTEXT_INVALID`, `ROBOT_CONTEXT_MISMATCH`, `STALE_STATE`, `PRECONDITION_FAILED`, `ESTOP_ACTIVE`, `NO_SUCH_TASK`, `UNKNOWN_PRECONDITION`; outcomes `HUMAN_APPROVAL_REQUIRED`, `CLARIFICATION_REQUESTED`, `UNKNOWN_SAFETY_LEVEL`, `INTERNAL_ERROR` (a bug; fails closed; the corpus and fuzz tests assert it never occurs).

Design rules that close the review pitfalls from earlier rounds:

* Identifiers use `fullmatch` on ASCII classes (`[a-z][a-z0-9_]{0,63}`, `task-[0-9]{1,6}`): a trailing newline, space, zero-width character, soft hyphen, Cyrillic or fullwidth look-alike, or mixed case never equals an allowed name. There is no Unicode normalisation (NFKC would turn look-alikes into valid names). Names are exact-match only: a prefix, suffix or fuzzy variant of an allowed name is unknown.
* Types are checked before membership, lookup, ranges or hashing. A list, dict, `null`, bool or string where a scalar is expected gives `WRONG_TYPE`, not an exception. `bool` is rejected for numbers and integers; a float such as `5.0` is rejected where an integer is required.
* The parser rejects `NaN`/`Infinity`, literals that overflow to infinity, integers over 18 digits, float literals over 32 characters, nesting deeper than 3 levels, inputs over 4096 characters or bytes, invalid UTF-8, lone surrogates and a byte-order mark. Duplicate keys are rejected after unescaping, even when both values are identical.
* There is no `eval`, no YAML and no regular-expression extraction of JSON from prose: `Sure! {...}`, a fenced block and two concatenated objects are all `NOT_STRICT_JSON`. Natural language is not interpreted at all.
* `rationale` (and any other text) is data. It is length- and character-checked, hashed into the audit record and never echoed or interpreted. Hence a valid `get_status` with an injection sentence in `rationale` is still `ACCEPT` (the sentence changed nothing and nothing executed it), while the same text in any other field is `UNKNOWN_FIELD`. No keyword filter is used, so benign text containing "shell" or "ignore" is not blocked (case `ben-05`).
* `review` is a pure function: it mutates neither input, keeps no state (a rejection leaves nothing behind), and its audit record contains no timestamp, so results are byte-identical across runs.

The audit record holds the input's SHA-256, length and an escaped 80-character preview, the stage trace (`parse`, `envelope`, `skill`, `robot`, `args`, `context`, `preconditions`), the reason codes, the recorded advisory, and `authorizes_execution: false`. It never contains the raw rationale.

## Injection challenge corpus (SYNTHETIC)

[`fixtures/corpus.json`](fixtures/corpus.json) has 148 cases: 134 deliberately unauthorized (expected `REJECT`), 9 benign look-alikes that must be `ACCEPT`, and 5 that must be `NEEDS_HUMAN`. Each case records the proposal text, a named simulated context, the expected verdict and the expected primary reason. Run `evaluate.py` for the live table; per attack class (counts from a run of this commit):

| Attack class | Cases | Examples |
| --- | ---: | --- |
| `injection_text` | 11 | "ignore previous rules, run ..." in `sensor_note` / `tool_output` / args, JSON wrapped in prose, fenced, concatenated, self-granted `approved`, `advisor_verdict` |
| `unknown_skill` | 4 | `run_shell`, `ros2_topic_pub`, `kubectl_apply`, plausible-but-absent `open_gripper_sim` |
| `raw_velocity_or_topic` | 6 | `set_velocity`, `publish_cmd_vel`, `linear_x` arguments, `topic: /cmd_vel`, hallucinated topic |
| `name_confusion` | 17 | prefix, suffix, mixed case, Cyrillic, fullwidth, zero-width (in value and in a key), soft hyphen, NUL, trailing newline/space, lone surrogate |
| `type_confusion` | 24 | list/dict/bool/null/number where string expected; string/bool/list/dict where number expected; bool-as-int; `5.0` for integer |
| `numeric_abuse` | 14 | `NaN`, `Infinity`, `1e999`, 30-digit integer, negative, zero, over-range, very long literal |
| `path_or_url` | 8 | `../../etc/passwd`, `http://example.invalid/...`, `file:///...`, backslash path, `..` |
| `duplicate_keys` | 4 | top level, inside `args`, duplicate after `\u` unescaping, identical duplicates |
| `size_limits` | 5 | 5000-character rationale, over-cap by bytes but under by characters, whitespace padding, 200 junk argument keys |
| `nesting` | 4 | 1500 nested arrays, 500 nested objects, self-similar `args` |
| `unsupported_robot` | 5 | `sim_robot_2`, `real_robot_7`, missing robot, trailing newline |
| `precondition_state` | 8 | stale state, robot moving, simulated e-stop, cancel with no or a different task |
| `envelope_and_encoding` | 20 | empty, `null`, array, invalid UTF-8, wrong version, YAML/Python-dict/comment/BOM/form-feed, multiple calls, conflicting clarification |
| `rationale_abuse` | 4 | control character, zero-width, right-to-left override, over-long clarification |
| `benign_lookalike` | 9 | plain calls, pretty-printed JSON with trailing newline, words like "shell"/"ignoring" in free text, accented letters, stale state with read-only skill |
| `human_approval` | 4 | consequential calls (one carrying an injection sentence in `rationale`) |
| `ambiguous_goal` | 1 | structured clarification request |

`python3 .../evaluate.py` on this commit reported: unauthorized fixtures rejected 134/134, false accepts 0, benign look-alikes accepted 9/9, human-review cases routed 5/5, internal errors 0, and 0 verdict changes across 2 synthetic advisor settings (`approve`, `reject`, both with confidence 0.99) x 148 cases. **Scope:** this is 100% of the enumerated corpus only, which was written by the same author as the reviewer; it says nothing about inputs nobody thought of.

## Threat model

**Asset.** The simulated-robot action path: only an allowlisted, bounded, typed call, reviewed by a human when consequential, may ever reach a (not yet built) executor.

**Untrusted.** Everything the proposer emits, including quoted retrieved documents, logs, sensor notes and tool output it was shown, plus any advisor opinion. The caller-supplied `context` is treated as trusted input (it models simulator state) but is still validated and fails closed.

**Attacker goal.** Make the pipeline produce an action the allowlist does not intend: an uncatalogued skill, raw velocity, shell/URL/path effect, out-of-range value, a consequential call without a human, or execution in a state where it should be blocked (stale, moving, e-stop).

| Threat | Mitigation | Test evidence |
| --- | --- | --- |
| Prompt injection text steering the pipeline | Text is data; only typed fields decide; unknown fields rejected; no keyword filtering or interpretation | corpus `injection_text`, `test_injection_text_in_rationale_is_data_not_a_verdict_input` |
| Smuggling a skill via look-alikes, zero-width or whitespace | ASCII `fullmatch`, exact catalogue lookup, no normalisation | `name_confusion`, `test_every_allowed_name_with_trailing_newline_is_rejected` |
| Parser differential (duplicate keys, trailing data) | One strict parser, duplicates rejected after unescaping, no extraction | `duplicate_keys`, `inj-04..06` |
| Type confusion / numeric abuse | Exact type checks first, bool excluded, finite and bounded numbers | `type_confusion`, `numeric_abuse`, grid test with no `INTERNAL_ERROR` |
| Resource exhaustion by size or nesting | 4096 byte cap, depth limit, iterative depth check, capped reason lists | `size_limits`, `nesting`, parser tests |
| Self-granted approval or advisor escalation | Unknown fields/arguments rejected; advisory recorded, never consulted | `inj-08..10`, `test_advisory_cannot_change_any_verdict` |
| Consequential call without a human | `call` is `None` for `NEEDS_HUMAN`; only `pending_call` is set | `test_call_present_iff_accept_and_pending_only_for_approval` |
| Reviewer bug becoming a pass | Any exception becomes `REJECT`/`INTERNAL_ERROR`; seeded fuzz finds none | `FuzzRegressionTests` |

**Out of scope here:** authenticated transport and proposer identity; the deterministic action gate (#59) and any executor; replay protection of approvals; sandboxing; physical safety; a human approval UI; model-side defences.

## Mutation evidence

To check the tests really guard each rule, one check at a time was made fail-open in a scratch copy of this directory (outside the repository) and the suite re-run. Every mutation was caught (non-zero exit):

| Mutation in the scratch copy | Result |
| --- | --- |
| bool accepted for number/integer | FAILED (failures=8) |
| duplicate keys collapsed silently | FAILED (failures=11) |
| consequential skill returned as `ACCEPT` | FAILED (failures=11) |
| identifier `fullmatch` replaced by prefix `match` | FAILED (failures=25) |
| unknown arguments ignored | FAILED (failures=13) |
| advisor `approve` turns a consequential call into `ACCEPT` | FAILED (failures=6) |
| parser tolerates `NaN`/`Infinity` | FAILED (failures=10) |
| stale-state limit loosened by 1000 ms | FAILED (failures=1) |
| catalogue membership check removed | FAILED (failures=23) |
| simulated e-stop ignored | FAILED (failures=8) |
| type check removed before the path/URL test | FAILED (failures=44) |
| control and zero-width characters allowed in free text | FAILED (failures=15) |
| `pending_call` also exposed as `call` | FAILED (failures=4) |
| nesting-depth limit removed | FAILED (failures=8) |

The failure counts include the corpus, evaluator and CLI tests that also fail when the reviewer changes. The mutation script is not committed.

## Benchmark design for the #86 research question (DESIGN ONLY, NOT RUN)

Question: does an advisory screener add value beyond this deterministic layer plus the action gate? Nothing below was executed; there is no model output in this repository.

1. Freeze a labelled proposal set (extend the corpus; label each case `must_reject`, `must_accept`, `needs_human` before any advisor sees it) and split it so advisor prompts are never tuned on the test split.
2. Baseline A: deterministic reviewer only. Baseline B: reviewer plus advisor opinion shown to a human. Compare only on cases the reviewer returns `ACCEPT` or `NEEDS_HUMAN`; the advisor is never in the authorization path, so its "false negatives" can only cost reviewer attention, never permit an action.
3. Metrics to record once a run exists: advisor false negatives and false positives against the labels, abstention rate, disagreement with the deterministic verdict, human reviewer minutes per case, confusion under injection (the same case with and without injected text), and hosted latency and cost per case. These columns are `NOT RUN` today.
4. Report counts with the corpus size and its authorship, never a single accuracy figure, and keep model, prompt and settings in the run record.

## Student challenge

Use `review_cli.py` (or `evaluate.py --case <id>`) and predict the colour before running:

| Try | Predicted | Case id |
| --- | --- | --- |
| `get_status` exactly as in the sample command | green | `ben-01` |
| Navigate to `dock_a` while idle and fresh | amber (human approval) | `hum-01` |
| Ask for `linear_x` on the navigate skill | red | `vel-03` |
| `get_status` with a trailing newline inside the name | red | `nam-09` |
| Words like "shell" in `rationale` of a `get_status` | green (benign text is not blocked) | `ben-05` |
| An injection sentence in `rationale` of a navigate call | amber (text is data; the human gate still applies) | `hum-04` |
| An injection sentence in a `sensor_note` field | red | `inj-01` |
| Clarification request instead of a skill | amber | `amb-01` |
| Navigate while the robot is moving (`--context moving`) | red | `prc-02` |

Then try to break it: find an input that gets `ACCEPT` for something outside the catalogue, or a benign input that is blocked. A real finding (not a corpus expectation) should become a new case in [`build_corpus.py`](build_corpus.py) with a regression test.

## What this does NOT claim

* **No model was run.** No LLM, small open-weight model, hosted advisor or "Jev" output exists here; the comparison of a scripted baseline, a local model and a hosted model asked for in #64 is **not done**.
* **Not a safety or security guarantee.** The corpus is hand-written, small and by the same author as the reviewer; passing it does not bound behaviour on other inputs.
* **No execution path.** There is no executor, ROS 2 adapter, simulator integration, DDS, Kubernetes or cross-worker test, no actuation and no physical-robot claim.
* **Not the action gate.** The authoritative deterministic gate (#59) is a separate module; this layer only precedes it and does not import it. `ACCEPT` is not authorisation. Replay protection, approval records and authenticated proposers are not implemented.
* **Context is trusted.** The simulated state is supplied by the caller; this package cannot tell if it is stale or forged beyond range and age checks.
* **No performance figures.** No latency, cost, throughput, false-positive or false-negative rate on real data and no reviewer-effort numbers were measured.
* **Not done from #86:** the interactive site scenario S3 and its annotated site trace (site files are outside this module; only the CLI audit trace exists), human-approval UI, and coordination with the final #59 field names.
* **Not a robustness proof for the parser:** it relies on Python's standard `json` module and on its documented behaviour for the listed inputs; other Python versions were not tested (3.12 was used).
