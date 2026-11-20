# Data Analysis

This folder has two very different parts. Do not mix them up:

| Part | Needs a cluster? | What it is |
|---|---|---|
| [Offline analysis](#offline-analysis-no-cluster-no-network) | **No** | Reproducible summary CSV, plot and tests that read only the committed samples in [`dataset/`](./dataset/README.md). |
| [Pod recovery measurement](#pod-recovery-measurement-rhpy-rehealingpy-recovery_metricspy) | **Analysis: No. Live collection: yes** | Versioned-schema recovery metrics, an offline analyzer and synthetic fixtures; the live collector is a dry run unless `--yes` is given. |
| [Live Prometheus queries](#live-prometheus-queries-needs-a-running-cluster) and the notebooks | **Yes** (Prometheus at `192.168.56.10:30090`) | How the historical data was exported; cannot be re-run without the original cluster. |

All committed data are **historical, Foxy-era samples** (a single recording of 2024-04-20 from the Vagrant lab cluster). They are not new benchmarks, say nothing about the Jazzy image in [`../ROS2/`](../ROS2/), and must not be quoted as performance results.

## Offline analysis (no cluster, no network)

Requires Python 3.12 or newer (tested with 3.12.3 on Linux). The pinned dependencies are in [`requirements-offline.txt`](./requirements-offline.txt).

```sh
# from the repository root; the network is only needed for this one install step
python3 -m venv .venv                      # .venv is not tracked
.venv/bin/pip install -r DataAnalysis/requirements-offline.txt

# one command: summary CSV for all 10 samples + the CPU plot + the offline tests
bash scripts/run-analysis-offline.sh       # uses ./.venv automatically; or: make analysis-offline
```

Outputs go to `DataAnalysis/output/` (ignored by version control, never committed): `<sample>_summary.csv` per sample, `namespace_cpu.png` and `PROVENANCE.txt` (the limitations that apply to every number there).

Smaller entry points:

```sh
python3 DataAnalysis/analyze_sample.py --list                        # standard library only, no install needed
python3 DataAnalysis/analyze_sample.py                               # default sample -> DataAnalysis/output/   (make analyze-sample)
python3 DataAnalysis/analyze_sample.py --sample container_memory_usage_bytes_pod
.venv/bin/python -m pytest DataAnalysis                              # offline tests                            (make analysis-test)
```

What the tool does and does not do:

- [`analyze_sample.py`](./analyze_sample.py) reads one file from `dataset/` by name (URLs and paths are rejected), validates the `{namespace: [series]}` layout and writes one CSV row per series: sample count, first/last time (UTC), min/max/mean, number of recording gaps (consecutive samples more than 120 s apart) and the longest gap. Values that are `NaN`/`Inf`/unparsable are dropped and counted, never kept.
- **Units are taken from the metric, not guessed.** CPU values in the committed files are a 1-minute *rate* in CPU cores (not raw counters and not a percentage); network values are bytes per second; memory is a byte gauge. No percent utilization is computed, because the number of CPUs a series could use is not recorded in the data. `counter_to_rate()` (reset-aware) exists for anyone who pulls *raw* cumulative counters; a warning is printed if a "rate" file looks like a raw counter.
- Nothing is smoothed, interpolated, resampled or summed across series. Gaps are reported, not filled.
- The CSV is deterministic: the test suite pins its SHA-256 for the default sample, and the tests run with network access blocked. [`plot_namespace_cpu.py`](./plot_namespace_cpu.py) (existing script) is exercised the same way; the PNG itself is only an illustration and is not byte-pinned.
- Schema, labels, units, the 2024-04-20 time axis, the ~1.5 h recording gap and everything that is **unknown/unverified** about the experiment (workload, node sizes, scrape interval, versions) are documented in the [sample dataset guide](./dataset/README.md); this README does not repeat them.

### Notebooks do not run offline

[`DataAnaysis.ipynb`](./DataAnaysis.ipynb) and [`HPA.ipynb`](./HPA.ipynb) are the original, executed lab notebooks. They are **not** part of the offline environment and are not executed by `run-analysis-offline.sh`:

- Both query a live Prometheus at `http://192.168.56.10:30090` (the Vagrant NodePort) and `HPA.ipynb` also calls `kubectl`; the data behind them died with that cluster.
- `DataAnaysis.ipynb` reads `../data_json/20240420/json/...`, a folder that is not in the repository (it is git-ignored), through [`helper.py`](./helper.py); it also uses `%reset -f`.
- Their stored outputs are historical lab output and are left untouched; do not re-save them from a different environment.

## Pod recovery measurement (`RH.py`, `rehealing.py`, `recovery_metrics.py`)

> **What this is.** Tooling to measure how long Kubernetes takes to *create* and *ready* a replacement pod after one pod is deleted, with defined, separate measurements and a versioned output schema. **What this is not.** It does not measure whether a ROS 2 application works again, and it does not re-interpret any historical number. Nothing here was run against a real cluster for this change: the live collector was exercised only with injected, recorded/synthetic `kubectl` output (see [Limitations](#limitations)).

### What changed (issue #45)

| Before | Now |
|---|---|
| Pod chosen by a generic `ros-` name prefix in namespace `ros`; deleted immediately. | `--namespace` and `--selector` are required (no defaults, no prefix guessing). **Dry run by default**; deleting needs `--yes` plus `--context NAME` (or `--yes-context-is-correct`). If the selector matches several pods you must name one with `--target-pod`. |
| `RH.py` waited for the replacement in an unbounded `while` loop. | Bounded: `--timeout-replacement`, `--timeout-ready` (1 s to 3600 s), a hard poll cap, and a nonzero exit on timeout. |
| `kubectl` return codes were never checked; output parsed without validation. | One wrapper runs every `kubectl` call (no shell, timeout, stdin closed), checks the return code, and rejects malformed/duplicate-key/NaN JSON with a typed error. |
| "Replacement" was any other pod with the prefix; `rehealing.py` looked for `Killing`/`Started` events on the *same pod name*. | The replacement is identified by **UID** (not in the baseline snapshot, same controller owner UID). Zero or more than one candidate is a typed failure, never a guess. The `Killing`/`Started`-on-one-name method is **removed**. |
| `Started` event `lastTimestamp` was reported as `running_time` ("rehealing time"). | `Started` is not used anywhere. Readiness is the pod `Ready` condition, and it is labelled Kubernetes readiness, not application recovery. |
| Timezone handling replaced the parsed offset with UTC. | Offsets (`Z`, `+hh:mm`) are parsed and preserved; naive timestamps are rejected. |
| Wall-clock subtraction between this machine and the API server. | Primary intervals use the local **monotonic** clock; timestamps stored by Kubernetes or the local wall clock appear only as rows labelled `auxiliary`. |

`RH.py` and `rehealing.py` keep their names. `rehealing.py` is now only an alias of `RH.py` (same arguments); it no longer has its own method.

### The measurements

Every row has a `role` (`primary`, `auxiliary` or `application`) and a `clock`. Durations are seconds. A value is `null`/empty unless `status` is `measured`.

| `measurement` | role | clock | definition |
|---|---|---|---|
| `deletion_request_to_replacement_pod_observed` | primary | `monotonic_local` | Seconds from issuing the delete request to the first poll that lists a pod with a new UID matching namespace + selector. Object existence only. |
| `replacement_pod_observed_to_ready_observed` | primary | `monotonic_local` | Seconds from the first poll that lists the replacement to the first poll showing its `Ready` condition `True`. Kubernetes readiness only. `0` with warning `ready_at_first_observation` means it was already Ready when first seen. |
| `aux_wallclock_request_to_replacement_creation_timestamp` | auxiliary | `local_wall_clock_vs_kubernetes_server` | Replacement `creationTimestamp` minus the local wall-clock time of the request. Subject to clock skew; 1 s timestamp resolution. |
| `aux_replacement_creation_to_ready_transition` | auxiliary | `kubernetes_server_timestamps` | `Ready` `lastTransitionTime` minus `creationTimestamp` (both server-side; 1 s resolution). |
| `aux_replacement_creation_to_scheduled_event` | auxiliary | `kubernetes_server_timestamps` | The replacement's `Scheduled` event time minus its `creationTimestamp`. Needs `--with-events`; events are best-effort and may be missing. |
| `disruption_to_first_restored_ros_message` | application | `probe_supplied` | **Not measured** (`no_message_probe`) unless a message-probe record is supplied in the observation. This repository does not yet contain a producer of such a probe (issue #14); the contract (`recovery-message-probe/1`) is exercised only with a SYNTHETIC record. |

An auxiliary interval that would be negative (skewed clocks, 1 s truncation) is reported as `not_measured` with a `*_negative_interval` warning, never as a negative duration. Failed rows carry an `error_code` such as `no_replacement_pod`, `ambiguous_match`, `ready_not_observed`, `no_ready_condition`, `replacement_pod_disappeared`, `scheduled_event_missing`, `missing_timestamp`, `bad_timestamp`.

### Output schema (`recovery-metrics/1`)

CSV and JSON carry the same columns: `schema_version, sample_id, synthetic, measurement, role, definition, clock, unit, status, value, error_code, error_message, warnings, exit_status, run_error_code, namespace, selector, target_pod, target_uid, replacement_pod, replacement_uid, owner_uid, evidence_ref`. `exit_status` is the exit status of the collecting run; `evidence_ref` is `<observation file name>#sha256=<hex>` of the raw-evidence file (the *observation*, `recovery-observation/1`: sanitized pod lists, monotonic poll times, optional events/probe). Output is deterministic (byte-identical on re-run).

### Reproduce the analysis offline from the included fixtures

Standard library only; no cluster, no network, no `kubectl`:

```sh
# from the repository root
python3 DataAnalysis/recovery_metrics.py analyze --observation DataAnalysis/fixtures_recovery/success_basic.json
# byte-for-byte check against the committed expected output (prints nothing, exit 0 when identical):
python3 DataAnalysis/recovery_metrics.py analyze --observation DataAnalysis/fixtures_recovery/success_basic.json | diff - DataAnalysis/fixtures_recovery/expected/success_basic.csv
python3 DataAnalysis/recovery_metrics.py analyze --observation DataAnalysis/fixtures_recovery/fail_no_replacement.json --format json
```

`analyze` exits `0` when both primary measurements were measured, `1` when a primary measurement failed (rows are still printed; failures are listed on stderr), `2` on corrupt or unreadable input. The fixtures in [`fixtures_recovery/`](./fixtures_recovery/) are **SYNTHETIC**, hand-written in the shape of sanitized `kubectl` output (all names, UIDs and times invented): `success_basic` (replacement timestamps in `+02:00`, a `Started` event that must be ignored), `success_multi_pod` (three same-label pods, a same-label pod in another namespace, `+05:30` wall time, a synthetic message probe), `fail_no_replacement`, `fail_never_ready`, `fail_ambiguous_replacement`, `missing_events`. Their numbers (e.g. 2.0 s and 3.0 s for `success_basic`) are arithmetic on invented times, not measurements.

Tests (`python3 -m unittest` and pytest both work):

```sh
cd DataAnalysis && python3 -m unittest tests.test_recovery_metrics       # standard library only
python3 -m pytest DataAnalysis                                           # what `make analysis-test` runs
```

### Live collection (needs a cluster; safe by default)

```sh
# 1. dry run (default): prints the current kubectl context and the pod it WOULD delete; deletes nothing
python3 DataAnalysis/RH.py --namespace ros-demo --selector app=talker --context my-lab-context
# 2. only after reading that output: really delete ONE pod and measure its replacement
python3 DataAnalysis/RH.py --namespace ros-demo --selector app=talker --context my-lab-context --yes --with-events
```

The namespace, selector and context above are placeholders. The dry run still makes read-only calls: `kubectl get pods` (and `kubectl config current-context` when `--context` is not given). With `--yes`, the tool deletes the chosen pod with `--wait=false` (the replacement is timed while the old pod is still terminating) and writes `<sample-id>_observation.json`, `<sample-id>_recovery.csv` and `<sample-id>_recovery.json` to `--out-dir` (default `DataAnalysis/output/recovery/`, ignored by version control). Existing output files are never overwritten, and the evidence is written even when the run fails or is interrupted after the deletion. Exit status: `0` ok, `1` a primary measurement failed, `2` usage/safety refusal, `3` a `kubectl` call failed, `4` a poll timed out, `5` output could not be written, `130` interrupted. The observation keeps only pod name, namespace, UID, labels, controller owner UID, phase, the `Ready` condition and timestamps (spec, env, annotations and event messages are dropped), but names, labels, UIDs and the context name are retained: review a file before sharing it.

### Limitations

- **Not ROS recovery.** A new pod or `Ready=True` says nothing about DDS discovery, topic delivery or a working node. Do not call these numbers application recovery.
- **Resolution and bias.** Primary values are quantized to the poll interval and include `kubectl` start-up and API latency; they are upper-bound observations, not exact event times. Kubernetes timestamps have 1 s resolution.
- **Monotonic readings are only meaningful as differences inside one observation** (one machine, one run); they are not comparable across runs or machines.
- **Conservative matching.** Any second new pod with the same selector and owner during the run (for example an autoscaler scale-up) makes the sample `ambiguous_match`; a replacement that vanishes before Ready is a failure. A pod whose controller owner differs from the target's (for example after a rollout creates a new ReplicaSet) is not accepted as the replacement, so such a run ends in `no_replacement_pod`; measure a plain pod deletion on an otherwise idle workload.
- **Single deletion.** The tool deletes by pod name after listing; if the name is reused (for example by a StatefulSet) between listing and deletion, the UID guard identifies the new pod but cannot stop the delete itself.
- **Not run on a cluster.** The live path (`kubectl` calls, polling, `--yes` flow) was tested only against injected fake outputs, including a fake clock; no run on a real Kubernetes cluster was performed for this change. The message-probe contract has no real producer yet.
- **Historical data is untouched.** The notebooks, [`dataset/`](./dataset/README.md) and `pod_recreation_times.csv` (whose provenance is not documented here) were not modified and must not be reinterpreted with these definitions; values produced by the old `RH.py`/`rehealing.py` (event `Started` on a pod name, offset-replaced timestamps) are not comparable with the new rows.

## Live Prometheus queries (needs a running cluster)

> These snippets and the notebooks need a reachable Prometheus plus packages (`prometheus-api-client`, `pandas`, `requests`, ...) that are deliberately **not** in the pinned offline environment, and they were not re-run for this documentation.

In this section, we discuss how to extract data from Prometheus TSDB databases using Python, and provide some examples of data analysis.

Prometheus, an open-source systems monitoring and alerting toolkit, uses a time series database (TSDB) to store all its data. This data can be queried using PromQL, Prometheus's built-in query language, but sometimes you may find it useful to extract this data and use it with other tools such as Python for further analysis.

To interact with Prometheus using Python, we can use the 'prometheus-api-client' library, which provides a set of tools to handle the metrics data stored in a Prometheus TSDB.

Here's a basic example of how to use this library:

```python
from prometheus_api_client import PrometheusConnect

# Create a connection to the Prometheus server
prom = PrometheusConnect(url="http://localhost:9090", disable_ssl=True)

# Get a list of all the metrics that the Prometheus server has
all_metrics = prom.all_metrics()

# Choose a specific metric
metric = all_metrics[0]

# Get the metric data
data = prom.get_metric_range_data(
    metric_name=metric,
    start_time="2021-01-01T00:00:00Z",
    end_time="2021-01-02T00:00:00Z",
)

```

In the above example, we first establish a connection to the Prometheus server. We then fetch a list of all available metrics, and choose one for further querying. Lastly, we fetch the data for this metric in a specific time range.

Once you have the data, you can proceed to analyze it as per your needs. For instance, you could use the Pandas library to explore the data and Matplotlib to visualize it. Here's a simple example of how to do this:

```python
import pandas as pd
import matplotlib.pyplot as plt

# Convert the data to a Pandas DataFrame
df = pd.DataFrame(data)

# Plot the data
plt.plot(df['value'])
plt.show()

```

In this example, we convert the input data into a Pandas DataFrame, which then allows us to use the powerful data manipulation capabilities of Pandas to analyze the data. We then create a simple plot of the data using Matplotlib.

Note: The above examples are simplistic and meant only to illustrate the basic process. In a real use-case, you would likely need to deal with more complex queries and data transformations.
