# ROS 2 DDS/QoS fault triage casebook (SYNTHETIC)

**SYNTHETIC cases: a teaching baseline, not a benchmark.** This directory answers issue #84: an openly
inspectable casebook plus a small deterministic rule baseline, for ROS 2 beginners, students and
teaching assistants. Standard library only, CPU only, offline. No ROS 2, DDS, Kubernetes cluster, robot,
network, GPU or language model is involved, and nothing here is a performance claim.

| Part | What it is | Status |
|---|---|---|
| `fixtures/case_NNN.json` | 31 hand-authored SYNTHETIC incident cases in 15 incident families | validated and pinned by tests |
| `triage/validator.py` | schema + closed taxonomy + typed diagnostics | unit-tested (positive and negative) |
| `triage/rules.py` | rule-based baseline triager that abstains when evidence is insufficient | unit-tested; 15 mutations of it and the validator were each caught (see "Mutation evidence") |
| `triage/evaluate.py` | per-class results, confusion table, abstention/coverage, misclassified list | unit-tested |
| `docs/TEACHING.md` | task, learning outcomes, hints, blind rubric, pitfalls, vocabulary | text |
| `docs/EVALUATION-PLAN.md` | Choice-question spec, reordered-options ablation, table to fill later | **NOT RUN**: no model is called anywhere |

## What this does NOT claim

* **No real ROS 2 or DDS behaviour was observed.** Every case is invented. A label is "the root cause the
  author constructed and wrote the evidence to support", not something measured on a system. For
  `insufficient_evidence` the evidence is written to under-determine the cause.
* **Not a benchmark and not an accuracy result.** The cases and the rules were written together by the
  same author, so the baseline scoring 31/31 (below) is circular. It shows the rules reproduce the
  authored expectations (a regression pin), not that they would work on real incidents. The per-class
  counts are 1 or 2 cases each. No macro-F1 or p95 is reported.
* **No Jev, language-model or Choice-question result exists.** `docs/EVALUATION-PLAN.md` is a plan; every
  measurement cell in it says NOT RUN or NOT MEASURED.
* **No statement about the repository's clusters.** The legacy Vagrant/kubeadm lab is ROS 2 Foxy
  (end of life), Kubernetes 1.29 and Weave Net; the Jazzy image is `container tested` inside one
  container only; the one `cross-worker verified` case is the separate kind profile (see
  [`docs/ROS2-KUBERNETES-DDS-GUIDE.md`](../../docs/ROS2-KUBERNETES-DDS-GUIDE.md#support-states-and-evidence)).
  This casebook verifies no cross-worker DDS delivery. Where a case echoes a finding from
  [`docs/KIND-DEMO.md`](../../docs/KIND-DEMO.md#dds-discovery-findings-what-was-tested) (default multicast
  failed on that CNI; a domain ID did not isolate nodes sharing a Discovery Server), it echoes one recorded
  run on one CNI and is not a general rule.
* **Deadline, history-depth, liveliness and subscriber-restart faults are not covered.** The taxonomy is
  small on purpose.
* **Confidence tiers are not probabilities.** `high` means the rule needed several independent pieces of
  evidence; `medium` means fewer or an assumption; neither is proof.
* The mixed-RMW case (`rmw_mismatch`) is a teaching construction. Whether two RMWs interoperate on a real
  system is not established here either way (case 13 is a hard negative where they differ and messages arrive).

## Run it (from the repository root)

```sh
cd examples/ros2-fault-triage-casebook
python3 -m triage validate              # every case file: schema, taxonomy, ids, types; exit 0/1
python3 -m triage fixtures --check      # files byte-identical to canonical form, MANIFEST.json current; exit 0/1
python3 -m triage evaluate              # the baseline over all cases (add --json, --split dev|holdout, --strict)
python3 -m triage coverage              # cases per label and role
python3 -m triage triage fixtures/case_001.json    # one case (or a bare evidence object): label, tier, rule, evidence used
python3 -m triage taxonomy              # the closed label list with next steps
```

Tests (this exact line is for the repository's CI runner list):

```sh
python3 -m unittest discover -s examples/ros2-fault-triage-casebook/tests -t examples/ros2-fault-triage-casebook
```

Exit codes: `0` success, `1` problems in the cases / drift / (with `--strict`) a misclassified case, `2` bad
command line or an unreadable single input file.

## Taxonomy (closed; 13 labels)

| Label | Meaning (synthetic) | Safe next step id (read-only) |
|---|---|---|
| `no_fault` | Messages arrived in full; nothing wrong visible | `no_action` |
| `pod_not_running` | A pod is Pending, crash-looping or not Ready | `inspect_pod_status_and_events` |
| `discovery_server_unreachable` | A Discovery Server is configured but does not answer | `check_discovery_server_reachability` |
| `network_path_blocked` | Plain UDP unicast between the pods times out | `run_udp_unicast_check` |
| `discovery_multicast_blocked` | Default discovery; unicast works, multicast times out | `run_udp_multicast_vs_unicast_check` |
| `domain_id_mismatch` | Different `ROS_DOMAIN_ID` under default discovery | `compare_domain_and_discovery_settings` |
| `rmw_mismatch` | Different RMW and nothing else explains zero messages | `compare_rmw_settings` |
| `qos_incompatible_reliability` | Publisher best-effort, subscriber reliable | `compare_qos_profiles` |
| `qos_incompatible_durability` | Publisher volatile, subscriber transient-local | `compare_qos_profiles` |
| `clock_skew_misleading_latency` | Delivery complete; latency wrong because two clocks disagree | `check_clock_offset_before_trusting_latency` |
| `network_partition` | A mid-stream gap with partition markers; delivery resumed | `check_fault_markers_and_network_events` |
| `publisher_restart` | Two publisher starts under one run id; ids repeated | `check_publisher_start_events` |
| `insufficient_evidence` | The abstain label: the bundle does not decide | `collect_more_evidence` |

Every next step is a read-only diagnostic; none changes a cluster, a robot or a firewall.
`python3 -m triage taxonomy` prints the same list with descriptions from `triage/taxonomy.py`.

**Precedence for zero-message incidents** (the baseline's rule, a teaching convention): pod, then Discovery
Server, then UDP path, then multicast, then domain, then RMW, then QoS reliability, then QoS durability.
Endpoints are matched only after discovery, so a discovery fault is reported before a QoS one; the QoS
fault is then recorded in the case as a `secondary_labels` entry (case 3).

## Case format (`rrl.triage-case/1`)

One JSON object per file, `fixtures/case_NNN.json`, keys sorted, 2-space indent, ASCII, final newline.

| Field | Meaning |
|---|---|
| `schema` | always `rrl.triage-case/1` |
| `case_id`, `incident_family_id` | `case-NNN` (matches the file name) and `fam-<words>`; siblings of one incident share the family |
| `split` | `dev` or `holdout`; all cases of a family must agree (stops paraphrase leakage across a split) |
| `synthetic`, `source` | must be `true` and `SYNTHETIC: <text>` |
| `case_role` | `positive`, `hard_negative` (resembles `looks_like` but is not it) or `ambiguous` (must be `insufficient_evidence`) |
| `symptom` | what an operator would say, printable ASCII, at most 300 characters |
| `evidence` | optional sections below; absent means "not captured" |
| `expected` | `label`, `secondary_labels`, `rationale`, `safe_next_step` |
| `cites` | ids from `triage/taxonomy.py` `CITATIONS`; a test checks that repository paths and anchors exist |

Evidence sections and where each field comes from:

| Section | Fields | Source |
|---|---|---|
| `pods.publisher/subscriber` | `phase`, `ready`, `restarts`, `reason`, `last_termination` | Kubernetes pod status words; troubleshooting checklist items 1 to 2 in [the DDS guide](../../docs/ROS2-KUBERNETES-DDS-GUIDE.md#troubleshooting-checklist) |
| `ros.publisher/subscriber` | `domain_id`, `rmw`, `qos.reliability` (`reliable`/`best-effort`), `qos.durability` (`volatile`/`transient-local`), `qos.depth` | checklist item 4; spellings and depth bound from the probe's `seqprobe/qos.py` |
| `discovery` | `mode` (`multicast_default`/`discovery_server`), `server_check` | [guide, discovery section](../../docs/ROS2-KUBERNETES-DDS-GUIDE.md#why-ros-2-discovery-matters-on-kubernetes) |
| `udp_check` | `unicast`, `multicast`: `received`/`timed_out`/`not_run` | the plain-UDP probe in [KIND-DEMO findings](../../docs/KIND-DEMO.md#dds-discovery-findings-what-was-tested) |
| `probe_summary` | `expected_count`, `received_total`, `unique`, `duplicates`, `reordered`, `missing`, `missing_ranges`, `completion`, `timeout_reason`, `publisher_start_events`, `ok` | the sequence probe [Verdict](../ros2-sequence-probe/README.md#verdict) and [log schema](../ros2-sequence-probe/README.md#log-schema-version-1); the validator checks the arithmetic the probe's tracker guarantees |
| `latency_summary`, `clock_offset_check` | `clock_domain` (`wall_cross_node`/`same_node`), `samples`, `negative_samples`, `min_ns`, `max_ns`; `not_run`/`offset_small`/`offset_large` | [clock semantics](../ros2-sequence-probe/README.md#clock-semantics) and e02's [negative-latency behaviour](../../experiments/e02-dds-resilience/README.md#validation-behaviour) |
| `fault_markers` | `partition_begin`, `partition_end`, `restart_begin`, `restart_end` | e02's `<name>_begin`/`_end` markers |
| `log_excerpt` | up to 5 short lines, **never read by the baseline** | the prompt-injection cases (26, 27) |

Privacy: the validator rejects IPv4 addresses, e-mail addresses, URLs and long token-like strings in the
free-text fields. No hostnames, addresses or credentials are used.

## The baseline (`triage/rules.py`)

Each of the 16 rules has an id (`R01` to `R12` plus `b` variants), a label, a tier, a citation id and one
readable condition. All rules are evaluated:

* exactly one label among the hits: answer with it, using the highest tier among that label's rules;
* hits for **two or more labels**: abstain with `R90` (`conflicting_rules`), e.g. case 21 where partition
  markers and a second publisher start both fit;
* no hit: abstain with `R91` (`no_rule_matched`, or `no_observation` when the bundle has no probe summary
  or broken pod);
* invalid evidence: abstain with `R00` and the diagnostic codes, never an exception.

The result carries `label`, `confidence` (`high`/`medium`/`abstain`), `rule_id`, `rules_fired`,
`evidence_used` (paths and values), `reason` and `safe_next_step`. The baseline never reads `symptom`
or `log_excerpt`, so an instruction hidden in a log cannot change the answer (a test pins this).
`LATENCY_SUSPICIOUS_NS = 1_000_000_000` is a teaching convention, not a measured threshold.

## Result of the baseline on the starter cases

Command: `python3 -m triage evaluate` (run from this directory; a test requires this block to equal the
current output byte for byte). Read "SYNTHETIC ... not a benchmark" first. Classes with fewer than 3 expected cases print `n/a (n<3)` instead of precision/recall (no statistical claim from n<3); `--json` marks them `"small_n": true` with `null` precision/recall.

<!-- BEGIN evaluate-output -->
```text
SYNTHETIC cases: a teaching baseline, not a benchmark
cases: 31

exact match (label == expected, abstain counts as a label): 31/31
coverage (answered, i.e. did not abstain): 23/31 = 0.742; abstained: 8
accuracy on answered: 23/23
abstention: correct=8  abstained_on_answerable=0  answered_when_should_abstain=0
circular: cases and rules were authored together; this is a regression check, not accuracy

per class (precision/recall are n/a (n<3) when a class has fewer than 3 expected cases; counts are still shown):
  label                            support predicted correct precision    recall
  no_fault                               8         8       8     1.000     1.000
  pod_not_running                        2         2       2 n/a (n<3) n/a (n<3)  n<3: not meaningful
  discovery_server_unreachable           2         2       2 n/a (n<3) n/a (n<3)  n<3: not meaningful
  network_path_blocked                   1         1       1 n/a (n<3) n/a (n<3)  n<3: not meaningful
  discovery_multicast_blocked            1         1       1 n/a (n<3) n/a (n<3)  n<3: not meaningful
  domain_id_mismatch                     1         1       1 n/a (n<3) n/a (n<3)  n<3: not meaningful
  rmw_mismatch                           1         1       1 n/a (n<3) n/a (n<3)  n<3: not meaningful
  qos_incompatible_reliability           2         2       2 n/a (n<3) n/a (n<3)  n<3: not meaningful
  qos_incompatible_durability            1         1       1 n/a (n<3) n/a (n<3)  n<3: not meaningful
  clock_skew_misleading_latency          2         2       2 n/a (n<3) n/a (n<3)  n<3: not meaningful
  network_partition                      1         1       1 n/a (n<3) n/a (n<3)  n<3: not meaningful
  publisher_restart                      1         1       1 n/a (n<3) n/a (n<3)  n<3: not meaningful
  insufficient_evidence                  8         8       8     1.000     1.000

confusion (rows = expected label, columns = predicted label; only non-zero cells):
  clock_skew_misleading_latency -> clock_skew_misleading_latency: 2
  discovery_multicast_blocked -> discovery_multicast_blocked: 1
  discovery_server_unreachable -> discovery_server_unreachable: 2
  domain_id_mismatch -> domain_id_mismatch: 1
  insufficient_evidence -> insufficient_evidence: 8
  network_partition -> network_partition: 1
  network_path_blocked -> network_path_blocked: 1
  no_fault -> no_fault: 8
  pod_not_running -> pod_not_running: 2
  publisher_restart -> publisher_restart: 1
  qos_incompatible_durability -> qos_incompatible_durability: 1
  qos_incompatible_reliability -> qos_incompatible_reliability: 2
  rmw_mismatch -> rmw_mismatch: 1

by confidence tier (a tier is not a probability):
  abstain  n=8 correct=8
  high     n=22 correct=22
  medium   n=1 correct=1

misclassified cases: 0
```
<!-- END evaluate-output -->

How to read it: `coverage` is the share of cases where the baseline answered rather than abstained.
`abstained_on_answerable` counts cases where a label was determinable but the baseline abstained;
`answered_when_should_abstain` counts overconfident answers. The tier table is there to show that a
confident tier can be wrong: here it is not, because the cases were written with the rules.

Case counts per expected label and role (`python3 -m triage coverage`; also pinned by a test):

<!-- BEGIN coverage-output -->
```text
SYNTHETIC cases: a teaching baseline, not a benchmark
cases: 31  families: 15

  expected label                        positive hard_negative     ambiguous   total  resembled_by_hard_negative
  no_fault                                     1             7             0       8                             0
  pod_not_running                              2             0             0       2                             1
  discovery_server_unreachable                 2             0             0       2                             1
  network_path_blocked                         1             0             0       1                             1
  discovery_multicast_blocked                  1             0             0       1                             1
  domain_id_mismatch                           1             0             0       1                             1
  rmw_mismatch                                 1             0             0       1                             2
  qos_incompatible_reliability                 2             0             0       2                             1
  qos_incompatible_durability                  1             0             0       1                             1
  clock_skew_misleading_latency                2             0             0       2                             1
  network_partition                            1             0             0       1                             1
  publisher_restart                            1             0             0       1                             1
  insufficient_evidence                        0             5             3       8                             0
```
<!-- END coverage-output -->

Honest coverage against issue #84's "every class has a positive, a negative and an ambiguous
example": every fault class has at least one positive and at least one hard negative that resembles it.
`no_fault` has positives and hard negatives; the abstain label has ambiguous cases and hard negatives.
**Not every class has its own ambiguous example**: the ambiguous cases are about multicast, no
evidence at all, and a partition/restart combination. Add more (see below) before drawing conclusions.

## Add a case (beginner guide)

1. Pick an incident family (`fam-...`, new or existing) and the next free number, say `case-032`.
2. Copy the closest existing case to `fixtures/case_032.json`. Change `case_id` to `case-032`.
3. Decide the `case_role`:
   * `positive`: the fault is present and the bundle supports it;
   * `hard_negative`: it looks like another fault (put that fault in `looks_like`) but is not;
   * `ambiguous`: the label must be `insufficient_evidence`.
4. Write `symptom` as an operator would say it, then build the `evidence` from the table above. Leave out
   a section if it "was not captured". Keep the probe numbers consistent (`missing = expected_count -
   unique`; `ok` only for a clean complete run); the validator will tell you if they are not.
5. Set `expected.label`, a short `rationale`, the label's `safe_next_step` (see the taxonomy table) and
   `secondary_labels` (usually `[]`). Choose `cites` from the registry. Use the same `split` as the other
   cases of the family.
6. Run:

   ```sh
   python3 -m triage validate          # fix every diagnostic; codes are listed below
   python3 -m triage fixtures --write  # canonical layout + MANIFEST.json; review the diff
   python3 -m triage fixtures --check  # must say OK
   python3 -m triage triage fixtures/case_032.json   # what does the baseline say, and why?
   python3 -m triage evaluate          # is your case misclassified? (that is useful information)
   ```

7. If the baseline disagrees with your label and you believe the label, do not change the rules to fit it
   silently: open the case in your pull request and say so. Update the README blocks above if the numbers
   changed (a test tells you).

No real addresses, hostnames, tokens, screenshots or private logs, ever. Keep text ASCII.

### Validator diagnostics

| Code | Meaning |
|---|---|
| `UNKNOWN_KEY`, `MISSING_KEY` | key not in the schema / required key absent |
| `WRONG_TYPE` | for example a list or object where a scalar is expected, `true` where an integer is expected, `1` where a boolean is expected, a float where an integer is expected |
| `BAD_VALUE`, `OUT_OF_RANGE`, `BAD_ID`, `BAD_TEXT` | outside the closed taxonomy, a number outside its range, an id or text that does not match in full (a trailing newline fails) |
| `NON_FINITE` | `NaN` or infinity (in the JSON text or in memory) |
| `INCONSISTENT` | fields contradict each other (role vs label, probe arithmetic, discovery mode vs server check, next step vs label) |
| `DUPLICATE_KEY`, `DUPLICATE_ID`, `SPLIT_CONFLICT` | repeated JSON key, repeated `case_id`, siblings in different splits |
| `UNKNOWN_CITATION`, `NOT_SYNTHETIC`, `FORBIDDEN_CONTENT` | citation not in the registry, case not marked SYNTHETIC, address/e-mail/URL/token in free text |
| `UNREADABLE`, `FILE_TOO_LARGE`, `BAD_FILENAME`, `UNEXPECTED_FILE`, `TOO_MANY_FILES` | file-level problems; nothing in the directory is skipped silently |

## Tests and mutation evidence

All tests are standard-library `unittest` (command above).

Mutation evidence (source copied to a scratch directory, one line changed, full suite run; each row
failed the named test, while the unmutated source passed). Re-run with your own scratch copy if you
change the rules.

| Mutation | Result |
|---|---|
| R04 ignores the unicast result | 5 tests fail (`ShippedCaseBehaviour...`) |
| conflicting rules no longer abstain | 5 tests fail |
| baseline reads `log_excerpt` | 5 tests fail (injection) |
| R12 ignores suspicious latency | 9 tests fail |
| domain rule also fires under a Discovery Server | `test_domain_mismatch_does_not_fire_with_a_discovery_server` |
| clock rule ignores a measured-small offset | `test_negative_latency_with_small_measured_offset_is_not_blamed_on_skew` |
| pod rule counts a Ready pod with restarts as broken | `test_past_restarts_on_a_ready_pod_do_not_hide_the_real_cause` |
| `bool` accepted as an integer | `test_bool_is_not_an_integer` |
| ids matched with `match()` instead of `fullmatch()` | 3 identifier tests |
| `NaN` accepted when parsing | 2 tests |
| duplicate `case_id` not detected | `test_duplicate_case_id_across_files` |
| enum check no longer type-checks before membership | `test_unhashable_where_enum_expected` |
| coverage counts abstentions as answered | 2 evaluator tests |
| `fixtures --write` writes despite an invalid case | CLI refusal test |
| split conflict not detected | `test_family_siblings_must_share_a_split` |

## Boundaries and review notes

* Python 3.12 only was used to run it. No ROS, Docker, kind, kubectl or network access is used by the code.
* The JSON files are the source of truth. `fixtures --write` only rewrites them into canonical layout and
  regenerates `MANIFEST.json` (sha256 per file); it refuses to write anything if any case is invalid.
* Issue #84 items not done here: the website scenario S1 link and the Jev provider are out of scope
  (this change contains no model integration); the starter set is 31 cases, not 20 to 40 of equal depth per class.
