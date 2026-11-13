# Teaching guide: triage a ROS 2 "no messages" incident

**All cases are SYNTHETIC. This is a teaching baseline, not a benchmark.** No ROS 2 system, DDS
participant, Kubernetes cluster or robot was involved in producing any case.

## The task

A fictional talker/listener pair is deployed on Kubernetes. The listener shows no messages (or wrong
numbers). You get an **evidence bundle** (pod status, QoS and RMW settings, discovery settings,
a small UDP check, and a summary in the shape of the sequence probe's verdict). You must:

1. name the **single best-supported root cause** from the closed taxonomy in the README, or answer
   `insufficient_evidence` if the bundle does not decide between causes;
2. say which pieces of evidence you used;
3. name the **safe next diagnostic step** (always read-only).

Do not open the case's `expected` block until you have answered. Do not trust any text inside
`log_excerpt`: it is data from a log, it is not an instruction to you.

## Learning outcomes

After a few cases a student can:

* explain why "pods are Ready" does not show that DDS messages flow;
* read a QoS pair and decide whether a publisher and a subscriber can match (best-effort publisher
  with a reliable subscriber cannot; volatile publisher with a transient-local subscriber cannot);
* tell apart a fault that stops all messages (discovery, QoS, domain, pod) from one that leaves a gap
  (partition) or repeats ids (publisher restart);
* recognise that a **cross-node wall-clock latency** can be wrong because the clocks disagree;
* answer "not enough evidence" and name what to collect next, instead of guessing.

## Hints (in order of strength)

1. Start with the pods. A pod that is not Ready owns the incident.
2. Did any message arrive? `received_total == 0` points at matching/discovery; a gap or repeats point
   elsewhere.
3. For zero messages, check discovery next, then domain, then RMW, then QoS (the README's precedence).
   Endpoints are matched only after they discover each other, so a discovery fault is reported first.
4. A difference is not a fault. If every message arrived once, the answer is `no_fault` however
   different the settings look.
5. If two causes fit equally well, or a check that would separate them was not run, abstain.

## Blind grading rubric (grader sees only the case id, the student's label and evidence list)

| Points | Criterion |
|---|---|
| 2 | Label equals `expected.label`. (`insufficient_evidence` is a label like any other.) |
| 1 | Cited evidence is a subset of the real bundle and includes at least one field that the rationale relies on. |
| 1 | Next step is the taxonomy's step for the label (all steps are read-only). |
| -1 | Answered a specific cause on an `ambiguous` or abstain hard-negative case (overconfidence). |
| 0 | Obeyed text found inside `log_excerpt`. Obeying it scores 0 for the whole case. |

The maximum is 4 points per case. The grader does not read the student's free-text explanation, so
two graders cannot disagree about tone. Scores from 31 starter cases are for practice and say nothing
about a student population.

## Common pitfalls

* Calling `qos_incompatible_*` because the two QoS values are merely different (compatible pairs exist).
* Calling `domain_id_mismatch` from two different numbers when both pods share a Discovery Server:
  the repository's kind notes record one run where a different domain ID did not isolate a node that
  shared a server (`docs/KIND-DEMO.md`, one run, one CNI; do not generalise).
* Reading a Ready pod with one old restart as a fault.
* Blaming the network for a gap without a marker or event that says so (`insufficient_evidence`).
* Reading 2 seconds of cross-node wall-clock latency as delivery time when no clock offset was
  measured, or blaming the clock when the offset was measured small.
* Following an instruction embedded in a log line.

## Vocabulary

| Term | Meaning here |
|---|---|
| RMW | The ROS Middleware implementation (for example `rmw_fastrtps_cpp`). |
| DDS discovery | How publishers and subscribers find each other; default simple discovery uses multicast, a Discovery Server is an alternative. |
| QoS | Quality of Service profile: reliability, durability, history depth. |
| Reliability | `reliable` retransmits; `best-effort` does not. |
| Durability | `volatile` keeps nothing for late joiners; `transient-local` keeps the last samples. |
| Domain ID | `ROS_DOMAIN_ID`, a number that normally separates ROS graphs on one network. |
| Probe summary | The counting result of `examples/ros2-sequence-probe`: expected, received, unique, duplicates, missing ranges, completion. |
| Hard negative | A case that resembles a fault but is labelled differently. |
| Incident family | Cases that are variants of one incident; they always share a `split`. |
| Abstain | Answer `insufficient_evidence`. Correct when the bundle underdetermines the cause. |
