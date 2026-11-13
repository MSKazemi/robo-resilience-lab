# Evaluation plan (NOT RUN): rule baseline vs a bounded Choice question

Everything below describes how a **future** comparison could be made. **Nothing in this directory
calls a language model, and no model result exists.** The only evaluated system is the offline rule
baseline, and only on the 31 SYNTHETIC starter cases, which were authored together with its rules.

## Research question (from issue #84)

Can a bounded `Choice` question rank diagnostic categories from synthetic DDS/QoS/Kubernetes event
evidence more usefully than the offline deterministic rule baseline, without mistaking confidence for
proof? Null result: not better, or not useful enough to justify the cost.

## Starter-fixture rule

No accuracy statistic from these starter fixtures alone is a finding. Splits are defined **by
`incident_family_id`** (`split` is `dev` or `holdout`; the validator rejects a family whose cases
disagree), so paraphrases of one incident cannot sit on both sides. A real study needs many more
families, written by people who have not seen the rules.

## Choice question (specification only)

* Input: the `evidence` object of one case, with `log_excerpt` kept as quoted data. The `symptom`
  text is optional and should be ablated.
* Options: the 12 non-abstain labels plus `insufficient_evidence`, each with a one-line criterion
  taken from `triage/taxonomy.py` (`LABEL_DESCRIPTIONS`), written so that two options are not
  satisfied by the same evidence unless the precedence rule in the README says which wins.
* Output: exactly one option id. Anything else counts as a failure, not as a wrong label.
* Ablation to record: **reordered options**. Run each case with the option list in the taxonomy order
  and in at least two fixed other orders; report how often the answer changes.
* Injection check: cases 26 and 27 carry instruction-like log lines. An answer that follows the line
  is a failure.

## Table to fill in later (all cells are NOT MEASURED)

| Quantity | Rule baseline | Choice question |
|---|---|---|
| macro-F1 over fault labels | NOT MEASURED on a held-out set | NOT RUN |
| recall on critical classes (`pod_not_running`, `discovery_server_unreachable`, `network_path_blocked`) | NOT MEASURED on a held-out set | NOT RUN |
| abstention coverage | printed by `python3 -m triage evaluate` for the starter set only | NOT RUN |
| answer changes under option reordering | not applicable (deterministic) | NOT RUN |
| p95 hosted round trip | not applicable | NOT RUN |
| failure count (invalid or refused output) | `R00` count only | NOT RUN |
| approximate bill | none (CPU only) | NOT RUN |

The deterministic correctness tests (`tests/`) are separate from this plan: they pin what the rules do
and never involve a model.
