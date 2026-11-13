"""Metric registry for ``run-metrics/v1`` (stdlib only).

This module is the single source of truth for metric definitions. The
machine-readable ``schema/metrics-registry.v1.json`` and the human-readable
``METRICS.md`` are both GENERATED from it by ``fixture_gen.py`` (and checked
for drift by ``fixture_gen.py --check``), so prose and code cannot disagree.

A definition here is a *measurement contract*, not a result: nothing in this
repository has measured any of these quantities on a real ROS 2 / Kubernetes
deployment.
"""

from __future__ import annotations

import rm_contract as c

# Where each timestamp comes from decides which clock assumption a metric needs.
#   node_local          : both events are observed by ONE process/host with ONE clock
#                         (or the metric is a pure count); no cross-host sync needed.
#   same_clock_domain   : the two events are observed on possibly different hosts, so
#                         manifest clocks.clock_domain must be single_clock or
#                         synchronized. With 'unsynchronized' the value MUST be
#                         not_computable (never a number).
SCOPES = ("node_local", "same_clock_domain")

STATUSES = ("ok", "censored", "not_computable", "not_applicable")
NOT_COMPUTABLE_REASONS = ("clock_unsynchronized", "no_samples", "run_aborted_before_window")

# Order is the order of tables and plots.
METRICS = (
    {
        "id": "sensor_to_policy_request_ms",
        "title": "Sensor-to-policy request delay",
        "kind": "latency",
        "unit": "ms",
        "value_domain": "non_negative_real",
        "per_run_reduction": "median over post-warmup samples",
        "start_event": "source timestamp of an observation message (stamped where the sensor/simulator produces it)",
        "end_event": "the policy client sends the inference request that uses that observation",
        "clock_scope": "same_clock_domain",
        "clock_requirement": "Sensor stamp and request send may be taken on different hosts: needs clock_domain single_clock or synchronized (with the manifest's estimated_max_offset). unsynchronized => not_computable.",
        "censorable": False,
        "censoring_rule": "Not censorable. Observations that never produce a request are not samples; if there are no samples the status is not_computable (no_samples).",
        "failed_run_rule": "A failed/aborted run keeps whatever post-warmup samples exist; with none it is not_computable, never 0.",
        "applicable_when": "Always, when a policy client consumes sensor observations.",
        "does_not_show": [
            "Network or serving time (only the client-side wait before the request is sent).",
            "Anything about tails: one median per run hides p95/p99 and bursts.",
            "That the observation was fresh when acted on (see action_age_ms).",
        ],
    },
    {
        "id": "request_serve_response_ms",
        "title": "Request-serve-response delay",
        "kind": "latency",
        "unit": "ms",
        "value_domain": "non_negative_real",
        "per_run_reduction": "median over post-warmup samples",
        "start_event": "the policy client sends an inference request",
        "end_event": "the same client receives the matching response",
        "clock_scope": "node_local",
        "clock_requirement": "Both timestamps are taken by the client process, so a single clock on that host is enough. Do not substitute server-side timestamps.",
        "censorable": False,
        "censoring_rule": "Requests with no response before the run ends are counted in failure_count, not averaged in; they are not samples.",
        "failed_run_rule": "Same as above: partial runs keep their samples; none => not_computable.",
        "applicable_when": "Always, when a remote or local inference request/response exists.",
        "does_not_show": [
            "How the time splits between network, queueing and model compute (needs server-side tracing).",
            "Quality of the returned action.",
            "Behaviour of the failed requests that never got a response (they are excluded from the median and counted elsewhere).",
        ],
    },
    {
        "id": "validator_delay_ms",
        "title": "Validator delay",
        "kind": "latency",
        "unit": "ms",
        "value_domain": "non_negative_real",
        "per_run_reduction": "median over post-warmup samples",
        "start_event": "the validator receives a candidate action",
        "end_event": "the validator emits its verdict (accept or typed reject)",
        "clock_scope": "node_local",
        "clock_requirement": "Both events are observed in the validator process: single clock on its host.",
        "censorable": False,
        "censoring_rule": "Candidates still unprocessed at run end are not samples; they are not silently dropped from failure accounting.",
        "failed_run_rule": "Partial runs keep their samples; none => not_computable.",
        "applicable_when": "Always, when an action validator sits between policy and sink.",
        "does_not_show": [
            "That the validator is correct or safe (it measures time, not verdict quality).",
            "Anything about actuation: this contract is non-actuating.",
        ],
    },
    {
        "id": "action_age_ms",
        "title": "Action age at release",
        "kind": "latency",
        "unit": "ms",
        "value_domain": "non_negative_real",
        "per_run_reduction": "median over post-warmup released actions",
        "start_event": "source timestamp of the observation the action was computed from",
        "end_event": "the validator releases the action to the (non-actuating, simulated or logged) sink",
        "clock_scope": "same_clock_domain",
        "clock_requirement": "Start and end are usually on different hosts: needs clock_domain single_clock or synchronized. unsynchronized => not_computable; a skewed wall clock can make ages negative, which is a data error, not a result.",
        "censorable": False,
        "censoring_rule": "Actions never released (rejected or still buffered at run end) are not samples; rejections are visible via failure_count and deadline_miss_fraction denominators.",
        "failed_run_rule": "Partial runs keep their samples; none => not_computable.",
        "applicable_when": "Always, when actions are released to a sink.",
        "does_not_show": [
            "Control quality, task success or safety.",
            "Tail behaviour (median per run only).",
            "Real actuator or motor latency: the sink is non-actuating.",
        ],
    },
    {
        "id": "buffer_starvation_fraction",
        "title": "Action-buffer starvation fraction",
        "kind": "fraction",
        "unit": "fraction",
        "value_domain": "fraction_0_1",
        "per_run_reduction": "starved_ticks / control_ticks over the post-warmup window (both integers are stored)",
        "start_event": "a control tick fires",
        "end_event": "the action buffer was empty at that tick (counts as starved)",
        "clock_scope": "node_local",
        "clock_requirement": "A count of ticks on one node; no cross-host comparison. The tick source must be the control-rate timer declared in the manifest.",
        "censorable": False,
        "censoring_rule": "Ticks that did not happen because the run stopped are not counted; control_ticks = 0 gives not_computable (no_samples), never 0/0.",
        "failed_run_rule": "Counted over the ticks that occurred before the failure; the run is flagged by status/failure_count, not hidden.",
        "applicable_when": "Always, when the policy loop uses an action buffer or chunking.",
        "does_not_show": [
            "Why the buffer was empty (slow model, network loss, validator rejects).",
            "Whether starved ticks held the previous action or did nothing (a policy choice outside this metric).",
        ],
    },
    {
        "id": "deadline_miss_fraction",
        "title": "Deadline-miss fraction",
        "kind": "fraction",
        "unit": "fraction",
        "value_domain": "fraction_0_1",
        "per_run_reduction": "missed_actions / released_actions over the post-warmup window (both integers are stored)",
        "start_event": "an action is released to the sink",
        "end_event": "its age exceeds timing.action_age_deadline from the manifest (strictly greater = miss; age equal to the deadline is a hit)",
        "clock_scope": "same_clock_domain",
        "clock_requirement": "Uses action age, so the same clock requirement as action_age_ms. unsynchronized => not_computable.",
        "censorable": False,
        "censoring_rule": "Only released actions are in the denominator; actions never released are not counted as misses here (see failure_count). released_actions = 0 gives not_computable.",
        "failed_run_rule": "Counted over the actions released before the failure.",
        "applicable_when": "Always, when action_age_deadline is declared (it is required in the manifest).",
        "does_not_show": [
            "A hard-real-time guarantee: this is a soft statistic over a finite run.",
            "Whether a late action was harmful.",
        ],
    },
    {
        "id": "message_gap_max_ms",
        "title": "Largest message gap",
        "kind": "latency",
        "unit": "ms",
        "value_domain": "non_negative_real",
        "per_run_reduction": "maximum gap in the post-warmup window (a maximum, not a median)",
        "start_event": "reception of one observation message at the policy client",
        "end_event": "reception of the next message on the same topic at the same client",
        "clock_scope": "node_local",
        "clock_requirement": "Both receptions are timestamped by the same client process: single clock on that host.",
        "censorable": False,
        "censoring_rule": "A gap still open when the run ends is not counted as a gap (it is a lower bound on a longer outage that this metric does not report).",
        "failed_run_rule": "Counted over messages received before the failure; fewer than 2 messages => not_computable (no_samples).",
        "applicable_when": "Always, when the policy client subscribes to an observation topic.",
        "does_not_show": [
            "Where the loss happened (publisher, DDS, CNI, subscriber).",
            "How many gaps occurred (see message_gap_count).",
        ],
    },
    {
        "id": "message_gap_count",
        "title": "Message-gap count",
        "kind": "count",
        "unit": "count",
        "value_domain": "non_negative_int",
        "per_run_reduction": "number of gaps larger than 2 x the nominal period (1 / timing.control_rate) in the post-warmup window",
        "start_event": "reception of one observation message at the policy client",
        "end_event": "the next reception is later than 2 x nominal period (strictly greater)",
        "clock_scope": "node_local",
        "clock_requirement": "Receiver-local timestamps only: single clock on the client host.",
        "censorable": False,
        "censoring_rule": "As for message_gap_max_ms: an open gap at run end is not counted.",
        "failed_run_rule": "Counted over messages received before the failure.",
        "applicable_when": "Always, when the policy client subscribes to an observation topic.",
        "does_not_show": [
            "Gap length (see message_gap_max_ms) or cause.",
            "That the threshold (2 x nominal) is the right one for a given robot task.",
        ],
    },
    {
        "id": "recovery_time_ms",
        "title": "Recovery time after an injected fault",
        "kind": "duration_to_event",
        "unit": "ms",
        "value_domain": "non_negative_real",
        "per_run_reduction": "one value per run: the first recovery after the injected fault",
        "start_event": "the injected fault ends (manifest fault_injection.duration elapsed)",
        "end_event": "the validator releases its first valid action again",
        "clock_scope": "same_clock_domain",
        "clock_requirement": "The fault controller and the validator may run on different hosts: needs single_clock or synchronized. unsynchronized => not_computable.",
        "censorable": True,
        "censoring_rule": "RIGHT-CENSORED when the run ends or times out before recovery: status censored, value null, and censored_lower_bound = observed time without recovery. A censored run is never averaged in as if it recovered at the timeout, and a summary that contains censored runs withholds any interval.",
        "failed_run_rule": "A failed run that never recovered is censored (not dropped and not set to the timeout).",
        "applicable_when": "Only when manifest fault_injection.applied is true; otherwise not_applicable.",
        "does_not_show": [
            "That the system is resilient in general (one fault type, one run).",
            "Recovery of the task itself, only of valid-action release.",
        ],
    },
    {
        "id": "failure_count",
        "title": "Per-run failure count",
        "kind": "count",
        "unit": "count",
        "value_domain": "non_negative_int",
        "per_run_reduction": "number of failure events in the run; must equal manifest outcome.failure_count",
        "start_event": "the run starts (failures during warmup are counted too)",
        "end_event": "the run ends",
        "clock_scope": "node_local",
        "clock_requirement": "A count; no clock comparison.",
        "censorable": False,
        "censoring_rule": "Failed, timed-out and aborted runs are INCLUDED with their counts (never dropped); a timed-out or aborted run has failure_count >= 1.",
        "failed_run_rule": "Always counted. The number of attempted runs is the denominator of every failure rate; do not report rates over successful runs only.",
        "applicable_when": "Always.",
        "does_not_show": [
            "The kind or severity of each failure (record kinds in the raw log).",
            "A failure rate by itself: it needs the number of independent runs.",
        ],
    },
    {
        "id": "task_success",
        "title": "Task success (only when applicable)",
        "kind": "boolean",
        "unit": "boolean",
        "value_domain": "boolean",
        "per_run_reduction": "true/false for the run's task outcome under a pre-declared success criterion",
        "start_event": "the task starts",
        "end_event": "the pre-declared success criterion is evaluated",
        "clock_scope": "node_local",
        "clock_requirement": "No clock comparison.",
        "censorable": False,
        "censoring_rule": "A run that timed out before the criterion could be evaluated is a failure (false) only if the criterion declares it so; otherwise not_computable. Never silently dropped.",
        "failed_run_rule": "Counted in the denominator whenever the metric is applicable.",
        "applicable_when": "Only when manifest outcome.task_success.applicable is true. A transport-level experiment has no task, and reports not_applicable instead of an invented success.",
        "does_not_show": [
            "Physical task success: this repository records simulation or synthetic outcomes only.",
            "Generalisation beyond the declared task, seeds and simulator.",
        ],
    },
)

METRIC_IDS = tuple(m["id"] for m in METRICS)
BY_ID = {m["id"]: m for m in METRICS}

# Extra integer fields required by specific metrics in a run-file entry.
EXTRA_FIELDS = {
    "buffer_starvation_fraction": ("starved_ticks", "control_ticks"),
    "deadline_miss_fraction": ("missed_actions", "released_actions"),
}
# Metrics whose run-file entry carries samples counts.
SAMPLE_COUNT_METRICS = (
    "sensor_to_policy_request_ms",
    "request_serve_response_ms",
    "validator_delay_ms",
    "action_age_ms",
    "message_gap_max_ms",
)


def registry() -> dict:
    """The machine-readable registry (JSON-serialisable)."""
    return {
        "registry_version": c.REGISTRY_VERSION,
        "run_file_version": c.RUNFILE_VERSION,
        "manifest_version": c.SCHEMA_VERSION,
        "statuses": list(STATUSES),
        "not_computable_reasons": list(NOT_COMPUTABLE_REASONS),
        "clock_scopes": {
            "node_local": "Both events observed by one process on one host clock, or a pure count.",
            "same_clock_domain": "Events may be on different hosts; manifest clocks.clock_domain must be single_clock or synchronized.",
        },
        "metrics": [dict(m, extra_integer_fields=list(EXTRA_FIELDS.get(m["id"], ()))) for m in METRICS],
    }


def render_markdown() -> str:
    lines = [
        "# Metric definitions (`run-metrics/v1`)",
        "",
        "<!-- GENERATED by fixture_gen.py from rm_metrics.py. Do not edit by hand; run `python3 examples/run-manifest/fixture_gen.py`. -->",
        "",
        "> These are **measurement contracts**. Nothing here reports a measured value: no run of a real ROS 2 + Kubernetes deployment has produced any of these numbers in this repository. All example numbers elsewhere in this directory are SYNTHETIC.",
        "",
        "Every run file carries one entry per metric below (nothing may be omitted). An entry has a `status`:",
        "",
        "| status | value | meaning |",
        "|---|---|---|",
        "| `ok` | a number (boolean for `task_success`) | computed from post-warmup samples |",
        "| `censored` | `null` plus `censored_lower_bound` | observation stopped before the event (only metrics marked censorable) |",
        "| `not_computable` | `null` plus a `reason` | the metric applies but cannot be computed (for example `clock_unsynchronized`, `no_samples`) |",
        "| `not_applicable` | `null` | the metric does not apply to this run (for example no injected fault, no task) |",
        "",
        "**Clock scopes.** `node_local`: both events are observed by one process with one clock (or the metric is a count). `same_clock_domain`: the events may be on different hosts, so the manifest must declare `clocks.clock_domain` as `single_clock` or `synchronized` (with `estimated_max_offset`); with `unsynchronized` the validator requires `not_computable`, never a number. A wall clock can step or be skewed; `clock_basis: monotonic` is preferred for durations inside one host.",
        "",
        "**Per-run values.** The run (one independent deployment, one seed) is the unit of independence. Each metric is reduced to ONE value per run (see `per_run_reduction`); statistics across runs use those values, never the pooled samples.",
        "",
        "| id | unit | clock scope | censorable | per-run value |",
        "|---|---|---|---|---|",
    ]
    for m in METRICS:
        lines.append(f"| `{m['id']}` | {m['unit']} | {m['clock_scope']} | {'yes' if m['censorable'] else 'no'} | {m['per_run_reduction']} |")
    for m in METRICS:
        lines += [
            "",
            f"## `{m['id']}` - {m['title']}",
            "",
            f"- **Unit / kind:** {m['unit']} / {m['kind']} (`{m['value_domain']}`)",
            f"- **Starts at:** {m['start_event']}",
            f"- **Ends at:** {m['end_event']}",
            f"- **Clock requirement ({m['clock_scope']}):** {m['clock_requirement']}",
            f"- **Applicable when:** {m['applicable_when']}",
            f"- **Censored runs:** {m['censoring_rule']}",
            f"- **Failed runs:** {m['failed_run_rule']}",
            "- **Does NOT show:**",
        ]
        lines += [f"  - {x}" for x in m["does_not_show"]]
        extra = EXTRA_FIELDS.get(m["id"])
        if extra:
            lines.append(f"- **Extra integer fields in the run file:** `{extra[0]}`, `{extra[1]}`")
    lines.append("")
    return "\n".join(lines)
