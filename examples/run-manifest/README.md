# Run manifest, metrics and analysis contract (`run-manifest/v1`)

> **SYNTHETIC and offline.** This directory defines a **contract** (what a reproducible cloud-robotics experiment run must record, how its metrics are defined, and how results may be summarised) plus a validator and a summary generator. Every manifest, run file, table and plot shipped here is invented by `fixture_gen.py`. **No ROS 2, DDS, Kubernetes, GPU, simulator or robot was run, and nothing here is a measurement.** Real benchmarking stays gated on the cross-worker baseline ([#14](https://github.com/MSKazemi/robo-resilience-lab/issues/14), [#15](https://github.com/MSKazemi/robo-resilience-lab/issues/15), [#16](https://github.com/MSKazemi/robo-resilience-lab/issues/16)) with at least 3 independent repetitions as a starting minimum (see [Statistics guidance](#statistics-guidance)).

Addresses issue [#60](https://github.com/MSKazemi/robo-resilience-lab/issues/60). Python standard library only, CPU only, no network, no cluster, no GPU. It follows the conventions of [`examples/cloud-robotics/mock-policy`](../cloud-robotics/mock-policy/README.md) and [`examples/lerobot-data-quality`](../lerobot-data-quality/README.md) (a versioned contract, a hand-written validator with typed diagnostics, a deterministic fixture generator with `--check`) but imports neither.

## Commands (run from the repository root)

```sh
# 1. Validate manifests (exit 0 all valid, 1 at least one invalid, 2 usage/I-O error)
python3 examples/run-manifest/rm_validate.py examples/run-manifest/fixtures/manifests/valid/*.json
python3 examples/run-manifest/rm_validate.py examples/run-manifest/fixtures/manifests/invalid/unit_missing.json   # exit 1, typed diagnostic
python3 examples/run-manifest/rm_validate.py --list-codes
# opt-in: re-hash the files a manifest lists in data_hashes[]
python3 examples/run-manifest/rm_validate.py --verify-hashes examples/run-manifest examples/run-manifest/fixtures/manifests/valid/syn-local-baseline-r01.json

# 2. Validate run files and cross-check them against their manifests
python3 examples/run-manifest/rm_runfile.py --base-dir examples/run-manifest/fixtures examples/run-manifest/fixtures/run-files/*.json

# 3. THE offline regeneration command: recreates the SYNTHETIC tables and plots byte-identically
python3 examples/run-manifest/rm_summary.py
python3 examples/run-manifest/rm_summary.py --check        # exit 1 if reports/ differs from a fresh regeneration

# 4. Regenerate / check the schemas, metric registry, METRICS.md and all fixtures
python3 examples/run-manifest/fixture_gen.py
python3 examples/run-manifest/fixture_gen.py --check       # exit 1 on drift or stray files

# 5. Tests
python3 -m unittest discover -s examples/run-manifest/tests -t examples/run-manifest
```

Example diagnostic (from the command above, exit code 1):

```text
FAIL 'examples/run-manifest/fixtures/manifests/invalid/unit_missing.json'
  [error:UNIT_MISSING] /timing/action_age_deadline/unit: required field 'unit' is absent
      hint: Write {"value": <number>, "unit": "<unit>"}.
0 valid, 1 invalid (run-manifest/v1)
```

## What is in here

| Path | Role |
|---|---|
| `schema/run-manifest.v1.schema.json` | JSON Schema (2020-12) of the manifest: structure, enums, patterns, used/applied conditionals. **Generated** by `fixture_gen.py`. The Python validator is the executable authority; the schema cannot express the cross-field rules |
| `schema/run-metrics.v1.schema.json` | JSON Schema of a per-run metric file (generated) |
| `schema/metrics-registry.v1.json` | Machine-readable metric registry (generated from `rm_metrics.py`) |
| [`METRICS.md`](METRICS.md) | Human-readable metric definitions (generated from the same registry, so prose and code cannot disagree) |
| `rm_validate.py`, `rm_diag.py`, `rm_contract.py` | Manifest validator, typed-field helpers, constants and the code table |
| `rm_runfile.py` | Run-file validator, manifest cross-check, directory loader |
| `rm_stats.py`, `rm_summary.py` | Seeded bootstrap, Markdown tables, hand-written SVG plots |
| `fixture_gen.py` | Deterministic generator + `--check` for every committed fixture |
| `fixtures/manifests/valid/` | 10 valid SYNTHETIC manifests (9 are referenced by run files; `syn-variant-r01` exercises GPU, dataset, simulator, `keep_all`) |
| `fixtures/manifests/invalid/` + `expected_diagnostics.json` | 29 invalid manifests, each with the exact `(code, path)` it must produce |
| `fixtures/run-files/` | 9 SYNTHETIC run files in 3 arms (4 + 4 + 1 runs) |
| `reports/` | `SUMMARY.md` and 3 SVG plots generated from the run files |
| `tests/` | `unittest` suite (positive, negative and hostile-type tests) |

## What a manifest records

One manifest describes **one independent run** of one experiment arm. Quantities are always `{"value": x, "unit": "..."}`; a bare number is the typed error `UNIT_MISSING`.

| Group | Fields (all required unless marked optional) |
|---|---|
| Identity | `schema_version`, `data_kind` (`synthetic` / `simulated` / `measured`), `experiment_id`, `arm`, optional `description` |
| Run | `independent_run_id`, `group_id`, `run_index`, `fresh_deployment`, `seed`, `started_utc`, `ended_utc` (UTC `...Z` only), `warmup`, `timeout` |
| Provenance | `source.git_sha` (40 hex) + `git_dirty`; `images[]` with `oci_digest` (`sha256:` + 64 hex); `model` / `dataset` with `revision` (40 hex commit), SPDX `license`, optional content hash; `code_license` |
| Stack | `software` (ROS distro, RMW and version, CNI name and version, Kubernetes version, optional container runtime); `simulator`; `hardware` (CPU, memory, GPU present or not, OS) |
| Placement and network | `placement[]` (component, symbolic node name, tier), `network` (profile; if `shaped`: tool, delay, jitter, loss, bandwidth), `qos[]` per topic |
| Clocks | `clocks.clock_basis` (`wall` / `monotonic`), `clock_domain` (`single_clock` / `synchronized` / `unsynchronized`), `simulation_time`; `synchronized` additionally needs `sync_method` and `estimated_max_offset` |
| Timing, action space | `timing.control_rate`, `timing.action_age_deadline`; `action_space` (dimension, names, units, `actuation` must be `"none"`) |
| Faults, measurement, outcome | `fault_injection`; `measurement_source` (kind, tool, version); `outcome` (status, `censored` + reason, `observed_duration`, `failure_count`, `task_success.applicable` + value) |
| Integrity | `data_hashes[]` (relative path, role, sha256) |

Vendor extensions are allowed only under keys starting with `x_`. Unknown keys elsewhere are `UNKNOWN_FIELD` (this catches typos such as `sed` for `seed`).

### How the validator fails

Every problem is a `Diagnostic` with a stable `code`, a JSON-pointer `path` (the document root is the empty string), a message and a hint. `validate_document`, `validate_text` and `validate_file` never raise, keep checking after the first problem (up to 200 diagnostics, then `truncated`), and map an unexpected internal error to `INTERNAL_ERROR` (invalid). Rules each covered by a test:

- missing **units** (`UNIT_MISSING`, wrong dimension `UNIT_NOT_ALLOWED`), **revisions** (`REVISION_MISSING`), **digests** (`DIGEST_MISSING`), **clocks** (`CLOCK_MISSING`, `CLOCK_SYNC_INCOMPLETE`), **licences** (`LICENSE_MISSING`, `LICENSE_UNDECLARED`);
- malformed or placeholder ids and hashes (`ID_MALFORMED`, `HASH_MALFORMED`, `PLACEHOLDER_VALUE` matched against the whole value, never a substring);
- malformed or non-UTC timestamps (`TIMESTAMP_MALFORMED`, `TIMESTAMP_NOT_UTC`; a trailing newline is rejected; `ended_utc == started_utc` is allowed, earlier is `TIME_ORDER`);
- unknown `schema_version` (`SCHEMA_VERSION_UNKNOWN`; nothing else is checked for an unknown version);
- `bool` where a number is required (`BOOL_AS_NUMBER`), a list/dict/null where a scalar belongs (`WRONG_TYPE`), duplicate keys at any depth (`DUPLICATE_KEY`), `NaN`/`Infinity`/`1e999` (`NON_FINITE_VALUE`, reported once per occurrence), negative or absurdly large numbers (`NEGATIVE_VALUE`, `VALUE_OUT_OF_RANGE`), nesting beyond 40 levels or files over 1 MiB;
- run semantics: `warmup < timeout`, `completed` runs not longer than `timeout`, `status` vs `censored` vs `failure_count`, a censor reason for every censored run, `task_success` only when applicable, fault fields consistent with `applied`;
- safety heuristics: `actuation` must be `none`; IPv4 addresses and credential-shaped values or keys are rejected (`ADDRESS_IN_VALUE`, `SECRET_LIKE_VALUE`, `SECRET_LIKE_KEY`). These are heuristics, **not** a secret scanner.

The validator checks the **shape and internal consistency** of metadata. It cannot know whether a recorded digest, version or clock statement is true.

## Metrics

[`METRICS.md`](METRICS.md) defines ten metrics: sensor-to-policy request, request-serve-response, validator delay, action age, buffer starvation, deadline misses, message gaps (max and count), recovery, per-run failure count and task success. For each it states the unit, start and end event, the **clock requirement**, **what it does NOT show**, and **how censored and failed runs are counted**. A run file (`run-metrics/v1`) has exactly one entry per metric, each with a `status` of `ok`, `censored`, `not_computable` or `not_applicable`; omitting a metric is `METRIC_MISSING`.

Cross-checks against the manifest (`rm_runfile.py`): ids, arm, `data_kind` and the manifest's sha256 must match; with `clock_domain: unsynchronized` every cross-host metric must be `not_computable` (`CLOCK_REQUIREMENT_VIOLATED` otherwise); recovery is applicable only if a fault was injected; task success only if the manifest says so; `failure_count` must equal the manifest's and is never omitted. Directory loading (`load_runfile_set`) rejects duplicate `independent_run_id`s, non-JSON stray files, non-synthetic inputs and invalid manifests loudly; nothing is skipped silently.

## Statistics guidance

What the code enforces (`rm_stats.py`, `rm_summary.py`) and tests pin:

1. **The independent run is the unit.** One value per metric per run; statistics use run-level values, never pooled samples. Only runs with `fresh_deployment: true` count as independent; others are listed as excluded.
2. **n < 3: no statistical claim.** With fewer than 3 independent runs holding a value, no confidence interval and no comparison interval is printed; the cell says `n<3: no statistical claim`. `n = 1` is stated as one run.
3. **Seeded bootstrap.** 95% percentile bootstrap of the mean (B = 2000, fixed base seed, per-cell seed derived from arm and metric), so output is byte-reproducible. The interval describes the observed runs only.
4. **Censored runs are never averaged in.** A cell with a censored run reports no mean, median or interval, and shows the censoring lower bound.
5. **No p-values, no "significant".** The tool produces neither. A difference interval is not a significance test.

What the code cannot enforce (for real experiments, once [#14](https://github.com/MSKazemi/robo-resilience-lab/issues/14), [#15](https://github.com/MSKazemi/robo-resilience-lab/issues/15) and [#16](https://github.com/MSKazemi/robo-resilience-lab/issues/16) provide a baseline):

- **3 independent repetitions is a starting minimum to print an interval, not a sufficient sample.** Intervals from a handful of runs are unstable. Choose the number of runs from a method-driven power or precision analysis for the declared metric, and write the analysis plan down before looking at results.
- Report every attempted run (failed, timed out, aborted) and the denominator. Never drop failed runs from a failure rate.
- Fix the primary metric, arm definitions and exclusion rules in advance; report secondary metrics as exploratory.
- Per-run medians hide tails. v1 deliberately has no p95/p99 per run; add them only after per-run sample counts justify them.
- Do not pool runs that share a deployment, a seed stream or a warm cache as if they were independent.

## Relation to existing documents

- [`docs/EXPERIMENT-STATUS.md`](../../docs/EXPERIMENT-STATUS.md) run card (#88): `repo_commit` is `source.git_sha` + `git_dirty`; `inputs` and `outputs` map to `images[]`, `data_hashes[]` and `seed`; `numbers_are` is `data_kind`; `repeats` is the number of independent run ids in a group. A run card summarises; a manifest is the machine-checkable record behind it.
- [`docs/scenario-lab/RESULTS-PUBLICATION.md`](../../docs/scenario-lab/RESULTS-PUBLICATION.md) calls its manifest an "illustrative manifest, not the final schema defined in #60". This directory is that schema's first version; it does not change that page.

## What this does NOT claim

- Any measured latency, throughput, accuracy, recovery time or success rate. Every number in `fixtures/` and `reports/` is invented by a seeded generator from arbitrary toy bases and resembles no real system.
- That any ROS 2, DDS/RMW, CNI, Kubernetes, GPU or simulator behaviour was observed. The SYNTHETIC manifests carry placeholder versions such as `0.0.0`; their hashes are sha1/sha256 of the text `SYNTHETIC:<label>` and identify no real commit, image or file.
- That a manifest that validates is *true* or that a run is *good science*. Validation checks shape and internal consistency only.
- Cross-worker delivery, physical actuation, safety certification or hard-real-time behaviour. The contract is non-actuating (`actuation` must be `none`).
- That the secret/address heuristics find every credential or address.
- That JSON Schema validation alone is sufficient: the schema omits the cross-field rules listed above.

## Dependency gate

Real benchmarking is **gated** on the cross-worker baseline ([#14](https://github.com/MSKazemi/robo-resilience-lab/issues/14), [#15](https://github.com/MSKazemi/robo-resilience-lab/issues/15), [#16](https://github.com/MSKazemi/robo-resilience-lab/issues/16)). Until a baseline run exists and is reviewed, this contract produces **no** result. `rm_summary.py` refuses any input whose `data_kind` is not `synthetic` (`NOT_SYNTHETIC`); a reviewed analysis path for real data is future work, to be added with the first real baseline and at least 3 independent repetitions. Outcomes and evidence belong on [#53](https://github.com/MSKazemi/robo-resilience-lab/issues/53) and [#54](https://github.com/MSKazemi/robo-resilience-lab/issues/54), not here.

## Changing the contract

Edit `rm_contract.py`, `rm_metrics.py` or `rm_schema.py`, then run `python3 examples/run-manifest/fixture_gen.py` and `python3 examples/run-manifest/rm_summary.py`, and commit the regenerated files together with the change. A breaking change needs a new `run-manifest/v2`; the validator rejects unknown versions rather than guessing.
